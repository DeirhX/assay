"""Tests for the rebalance-planner research overlay in serve.py: the pure
band-vs-thesis conflict classifier and the in-place row enrichment that reads the
per-ticker dossier. Offline; serve's RESEARCH_DIR is pointed at a temp dir and
symbol resolution is stubbed to identity so no alias file is needed."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _support  # noqa: F401
import serve


class ResearchConflict(unittest.TestCase):
    def test_trim_against_add_like_thesis_is_conflict(self):
        for thesis in ("add", "accumulate", "buy", "build", "increase", "overweight"):
            self.assertTrue(serve._research_conflict("trim", thesis), thesis)

    def test_buy_against_trim_like_thesis_is_conflict(self):
        for thesis in ("trim", "sell", "reduce", "exit", "avoid", "underweight", "do_not_add"):
            self.assertTrue(serve._research_conflict("buy", thesis), thesis)

    def test_agreement_and_neutral_are_not_conflicts(self):
        self.assertFalse(serve._research_conflict("trim", "sell"))   # both bearish
        self.assertFalse(serve._research_conflict("buy", "accumulate"))  # both bullish
        self.assertFalse(serve._research_conflict("trim", "hold"))   # neutral thesis
        self.assertFalse(serve._research_conflict("review", "sell"))  # non-trade action
        self.assertFalse(serve._research_conflict("trim", None))     # no thesis
        self.assertFalse(serve._research_conflict("trim", ""))
        self.assertFalse(serve._research_conflict(None, "add"))

    def test_thesis_action_is_case_and_space_insensitive(self):
        self.assertTrue(serve._research_conflict("trim", "  Accumulate "))


class AttachResearchOverlay(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        # Point the dossier directory at the temp dir and make symbol resolution a
        # no-op so the overlay reads <tmp>/<NAME>.json directly.
        self._patches = [
            mock.patch.object(serve, "RESEARCH_DIR", tmp),
            mock.patch.object(serve, "_resolve_symbol", lambda s: s),
        ]
        for p in self._patches:
            p.start()
        self.tmp = tmp

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _write_dossier(self, sym: str, **rec) -> None:
        (self.tmp / f"{sym}.json").write_text(json.dumps(rec), encoding="utf-8")

    def test_held_target_gets_compact_overlay_and_conflict_flag(self):
        self._write_dossier(
            "AAA",
            as_of="2026-06-01T00:00:00+00:00",
            cross_checks=[{"severity": "WARN"}, {"severity": "INFO"}],
            decision="HOLD",
            momentum={"chg_3m_pct": -12.3, "chg_12m_pct": 4.0},
            thesis={"action": "accumulate", "summary": "cheap, growing",
                    "as_of": "2026-05-01T00:00:00+00:00"},
            metrics={"pe_fwd": 20},  # noise that must NOT leak into the overlay
        )
        plan = {"rows": [{"kind": "target", "held": True, "name": "AAA", "action": "trim"}]}
        serve._attach_research_overlay(plan)
        row = plan["rows"][0]
        self.assertTrue(row["research_conflict"])  # trim vs accumulate
        res = row["research"]
        self.assertEqual(res["data_quality"], "WARN")
        self.assertEqual(res["decision"], "HOLD")
        self.assertEqual(res["momentum_3m_pct"], -12.3)
        self.assertEqual(res["thesis_action"], "accumulate")
        self.assertEqual(res["thesis_summary"], "cheap, growing")
        # Only the compact, decision-support fields ship; raw metrics stay out.
        self.assertEqual(set(res), {
            "as_of", "data_quality", "decision", "momentum_3m_pct",
            "thesis_action", "thesis_summary", "thesis_as_of",
        })

    def test_no_thesis_means_no_conflict_but_still_overlaid(self):
        self._write_dossier("BBB", as_of="2026-06-01T00:00:00+00:00",
                             cross_checks=[], decision="ACCUMULATE", momentum={})
        plan = {"rows": [{"kind": "target", "held": True, "name": "BBB", "action": "buy"}]}
        serve._attach_research_overlay(plan)
        row = plan["rows"][0]
        self.assertIn("research", row)
        self.assertFalse(row["research_conflict"])
        self.assertIsNone(row["research"]["thesis_action"])
        self.assertEqual(row["research"]["data_quality"], "INFO")  # empty checks -> INFO

    def test_sleeve_and_unheld_rows_are_left_alone(self):
        self._write_dossier("CCC", as_of="x", cross_checks=[], momentum={})
        plan = {"rows": [
            {"kind": "sleeve", "held": True, "name": "CCC", "action": "trim"},
            {"kind": "target", "held": False, "name": "CCC", "action": "buy"},
        ]}
        serve._attach_research_overlay(plan)
        for row in plan["rows"]:
            self.assertNotIn("research", row)
            self.assertNotIn("research_conflict", row)

    def test_missing_dossier_is_silently_skipped(self):
        plan = {"rows": [{"kind": "target", "held": True, "name": "ZZZ", "action": "trim"}]}
        serve._attach_research_overlay(plan)  # ZZZ.json does not exist
        self.assertNotIn("research", plan["rows"][0])


class PriceGate(unittest.TestCase):
    """The locked-level gate on rebalance rows: a blocking level downgrades the
    suggested action to 'wait'; a favorable price leaves it alone."""

    def setUp(self):
        import price_levels
        self.price_levels = price_levels
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self._patches = [
            mock.patch.object(serve, "RESEARCH_DIR", tmp),
            mock.patch.object(serve, "_resolve_symbol", lambda s: s),
            mock.patch.object(price_levels, "LEVELS_JSON", tmp / "price-levels.json"),
        ]
        for p in self._patches:
            p.start()
        self.tmp = tmp

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _holdings(self, sym, mark):
        return {"positions": [{"symbol": sym, "mark_price": mark, "currency": "USD"}]}

    def test_buy_blocked_when_price_above_buy_below(self):
        self.price_levels.lock("AMD", buy_below=92, currency="USD")
        plan = {"rows": [{"kind": "target", "held": True, "name": "AMD",
                          "action": "buy", "suggest_delta_pct": 1.0}]}
        serve._attach_research_overlay(plan, self._holdings("AMD", 100.0))
        row = plan["rows"][0]
        self.assertEqual(row["action"], "wait")  # 100 > 92 -> too dear, wait
        self.assertEqual(row["price_gate"]["blocked_action"], "buy")
        self.assertTrue(row["price_gate"]["blocks_buy"])
        self.assertEqual(row["price_gate"]["current"], 100.0)

    def test_buy_allowed_when_price_at_or_below(self):
        self.price_levels.lock("AMD", buy_below=92, currency="USD")
        plan = {"rows": [{"kind": "target", "held": True, "name": "AMD",
                          "action": "buy", "suggest_delta_pct": 1.0}]}
        serve._attach_research_overlay(plan, self._holdings("AMD", 90.0))
        row = plan["rows"][0]
        self.assertEqual(row["action"], "buy")  # 90 <= 92 -> favorable, untouched
        self.assertFalse(row["price_gate"]["blocks_buy"])

    def test_trim_blocked_when_price_below_trim_above(self):
        self.price_levels.lock("NVDA", trim_above=145, currency="USD")
        plan = {"rows": [{"kind": "target", "held": True, "name": "NVDA",
                          "action": "trim", "suggest_delta_pct": -1.0}]}
        serve._attach_research_overlay(plan, self._holdings("NVDA", 120.0))
        row = plan["rows"][0]
        self.assertEqual(row["action"], "wait")
        self.assertEqual(row["price_gate"]["blocked_action"], "trim")

    def test_no_level_means_no_gate(self):
        plan = {"rows": [{"kind": "target", "held": True, "name": "AMD", "action": "buy"}]}
        serve._attach_research_overlay(plan, self._holdings("AMD", 100.0))
        self.assertNotIn("price_gate", plan["rows"][0])
        self.assertEqual(plan["rows"][0]["action"], "buy")

    def test_dossier_price_fallback_when_unheld_in_map(self):
        self.price_levels.lock("AMD", buy_below=92, currency="USD")
        (self.tmp / "AMD.json").write_text(json.dumps({"price": {"value": 100.0}}), encoding="utf-8")
        plan = {"rows": [{"kind": "target", "held": True, "name": "AMD", "action": "buy"}]}
        serve._attach_research_overlay(plan, {"positions": []})  # not in mark map
        self.assertEqual(plan["rows"][0]["action"], "wait")
        self.assertEqual(plan["rows"][0]["price_gate"]["current"], 100.0)

    def test_unknown_price_does_not_block(self):
        self.price_levels.lock("AMD", buy_below=92, currency="USD")
        plan = {"rows": [{"kind": "target", "held": True, "name": "AMD", "action": "buy"}]}
        serve._attach_research_overlay(plan, {"positions": []})  # no mark, no dossier
        row = plan["rows"][0]
        self.assertEqual(row["action"], "buy")  # can't confirm -> leave to the human
        self.assertFalse(row["price_gate"]["price_known"])


if __name__ == "__main__":
    unittest.main()
