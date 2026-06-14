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


if __name__ == "__main__":
    unittest.main()
