"""Tests for the rebalance-planner research overlay in rebalance_overlay.py: the
pure band-vs-thesis conflict classifier and the in-place row enrichment that reads
the per-ticker dossier. Offline; the module's RESEARCH_DIR is pointed at a temp
dir and symbol resolution is stubbed to identity so no alias file is needed."""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _support  # noqa: F401
import quote_cache
import rebalance_overlay


class ResearchConflict(unittest.TestCase):
    def test_trim_against_add_like_thesis_is_conflict(self):
        for thesis in ("add", "accumulate", "buy", "build", "increase", "overweight"):
            self.assertTrue(rebalance_overlay.research_conflict("trim", thesis), thesis)

    def test_buy_against_trim_like_thesis_is_conflict(self):
        for thesis in ("trim", "sell", "reduce", "exit", "avoid", "underweight", "do_not_add"):
            self.assertTrue(rebalance_overlay.research_conflict("buy", thesis), thesis)

    def test_agreement_and_neutral_are_not_conflicts(self):
        self.assertFalse(rebalance_overlay.research_conflict("trim", "sell"))   # both bearish
        self.assertFalse(rebalance_overlay.research_conflict("buy", "accumulate"))  # both bullish
        self.assertFalse(rebalance_overlay.research_conflict("trim", "hold"))   # neutral thesis
        self.assertFalse(rebalance_overlay.research_conflict("review", "sell"))  # non-trade action
        self.assertFalse(rebalance_overlay.research_conflict("trim", None))     # no thesis
        self.assertFalse(rebalance_overlay.research_conflict("trim", ""))
        self.assertFalse(rebalance_overlay.research_conflict(None, "add"))

    def test_thesis_action_is_case_and_space_insensitive(self):
        self.assertTrue(rebalance_overlay.research_conflict("trim", "  Accumulate "))


class ThesisLean(unittest.TestCase):
    """The single add/trim vocabulary, emitted to the UI as research.thesis_lean."""

    def test_add_like(self):
        for ta in ("add", "accumulate", "buy", "build", "increase", "overweight"):
            self.assertEqual(rebalance_overlay.thesis_lean(ta), "add", ta)

    def test_trim_like(self):
        for ta in ("trim", "sell", "reduce", "exit", "avoid", "underweight", "do_not_add"):
            self.assertEqual(rebalance_overlay.thesis_lean(ta), "trim", ta)

    def test_neutral_and_empty(self):
        for ta in ("hold", "wait", "watch", "", None, "  HOLD  "):
            self.assertEqual(rebalance_overlay.thesis_lean(ta), "neutral", repr(ta))

    def test_case_and_space_insensitive(self):
        self.assertEqual(rebalance_overlay.thesis_lean("  Accumulate "), "add")


class AttachResearchOverlay(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        # Point the dossier directory at the temp dir and make symbol resolution a
        # no-op so the overlay reads <tmp>/<NAME>.json directly.
        self._patches = [
            mock.patch.object(rebalance_overlay, "RESEARCH_DIR", tmp),
            mock.patch.object(rebalance_overlay, "resolve_symbol", lambda s: s),
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
        rebalance_overlay.attach_research_overlay(plan)
        row = plan["rows"][0]
        self.assertTrue(row["research_conflict"])  # trim vs accumulate
        res = row["research"]
        self.assertEqual(res["data_quality"], "WARN")
        self.assertEqual(res["decision"], "HOLD")
        self.assertEqual(res["momentum_3m_pct"], -12.3)
        self.assertEqual(res["thesis_action"], "accumulate")
        self.assertEqual(res["thesis_lean"], "add")  # accumulate -> add-like
        self.assertEqual(res["thesis_summary"], "cheap, growing")
        # Only the compact, decision-support fields ship; raw metrics stay out.
        self.assertEqual(set(res), {
            "as_of", "data_quality", "decision", "momentum_3m_pct",
            "thesis_action", "thesis_lean", "thesis_summary", "thesis_as_of",
        })

    def test_no_thesis_means_no_conflict_but_still_overlaid(self):
        self._write_dossier("BBB", as_of="2026-06-01T00:00:00+00:00",
                             cross_checks=[], decision="ACCUMULATE", momentum={})
        plan = {"rows": [{"kind": "target", "held": True, "name": "BBB", "action": "buy"}]}
        rebalance_overlay.attach_research_overlay(plan)
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
        rebalance_overlay.attach_research_overlay(plan)
        for row in plan["rows"]:
            self.assertNotIn("research", row)
            self.assertNotIn("research_conflict", row)

    def test_missing_dossier_is_silently_skipped(self):
        plan = {"rows": [{"kind": "target", "held": True, "name": "ZZZ", "action": "trim"}]}
        rebalance_overlay.attach_research_overlay(plan)  # ZZZ.json does not exist
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
            mock.patch.object(rebalance_overlay, "RESEARCH_DIR", tmp),
            mock.patch.object(rebalance_overlay, "resolve_symbol", lambda s: s),
            mock.patch.object(price_levels, "LEVELS_JSON", tmp / "price-levels.json"),
            # Point the fresh-quote cache at an empty temp path so the overlay's
            # quote-overlay step is inert unless a test writes a quote itself.
            mock.patch.object(quote_cache, "QUOTES_JSON", tmp / "quotes.json"),
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
        rebalance_overlay.attach_research_overlay(plan, self._holdings("AMD", 100.0))
        row = plan["rows"][0]
        self.assertEqual(row["action"], "wait")  # 100 > 92 -> too dear, wait
        self.assertEqual(row["price_gate"]["blocked_action"], "buy")
        self.assertTrue(row["price_gate"]["blocks_buy"])
        self.assertEqual(row["price_gate"]["current"], 100.0)

    def test_buy_allowed_when_price_at_or_below(self):
        self.price_levels.lock("AMD", buy_below=92, currency="USD")
        plan = {"rows": [{"kind": "target", "held": True, "name": "AMD",
                          "action": "buy", "suggest_delta_pct": 1.0}]}
        rebalance_overlay.attach_research_overlay(plan, self._holdings("AMD", 90.0))
        row = plan["rows"][0]
        self.assertEqual(row["action"], "buy")  # 90 <= 92 -> favorable, untouched
        self.assertFalse(row["price_gate"]["blocks_buy"])

    def test_trim_blocked_when_price_below_trim_above(self):
        self.price_levels.lock("NVDA", trim_above=145, currency="USD")
        plan = {"rows": [{"kind": "target", "held": True, "name": "NVDA",
                          "action": "trim", "suggest_delta_pct": -1.0}]}
        rebalance_overlay.attach_research_overlay(plan, self._holdings("NVDA", 120.0))
        row = plan["rows"][0]
        self.assertEqual(row["action"], "wait")
        self.assertEqual(row["price_gate"]["blocked_action"], "trim")

    def test_no_level_means_no_gate(self):
        plan = {"rows": [{"kind": "target", "held": True, "name": "AMD", "action": "buy"}]}
        rebalance_overlay.attach_research_overlay(plan, self._holdings("AMD", 100.0))
        self.assertNotIn("price_gate", plan["rows"][0])
        self.assertEqual(plan["rows"][0]["action"], "buy")

    def test_dossier_price_fallback_when_unheld_in_map(self):
        self.price_levels.lock("AMD", buy_below=92, currency="USD")
        (self.tmp / "AMD.json").write_text(json.dumps({
            "price": {"value": 100.0},
            "currency": "USD",
            "as_of": "2026-07-01T12:00:00+00:00",
        }), encoding="utf-8")
        plan = {"rows": [{"kind": "target", "held": True, "name": "AMD", "action": "buy"}]}
        rebalance_overlay.attach_research_overlay(plan, {"positions": []})  # not in mark map
        self.assertEqual(plan["rows"][0]["action"], "wait")
        self.assertEqual(plan["rows"][0]["price_gate"]["current"], 100.0)
        self.assertEqual(plan["rows"][0]["last_quote"], {
            "price": 100.0,
            "currency": "USD",
            "source": "cached dossier",
            "at": "2026-07-01T12:00:00+00:00",
        })

    def test_unheld_target_and_sleeve_member_receive_cached_dossier_quotes(self):
        for symbol, price in (("AMD", 100.0), ("ADI", 200.0)):
            (self.tmp / f"{symbol}.json").write_text(json.dumps({
                "price": {"value": price},
                "currency": "USD",
            }), encoding="utf-8")
        plan = {"rows": [
            {"kind": "target", "held": False, "name": "AMD", "action": "buy"},
            {"kind": "sleeve", "name": "analog", "members": [{"symbol": "ADI"}]},
        ]}

        rebalance_overlay.attach_research_overlay(plan, {"positions": []})

        self.assertEqual(plan["rows"][0]["last_quote"]["price"], 100.0)
        self.assertEqual(plan["rows"][1]["members"][0]["last_quote"]["price"], 200.0)

    def test_unknown_price_does_not_block(self):
        self.price_levels.lock("AMD", buy_below=92, currency="USD")
        plan = {"rows": [{"kind": "target", "held": True, "name": "AMD", "action": "buy"}]}
        rebalance_overlay.attach_research_overlay(plan, {"positions": []})  # no mark, no dossier
        row = plan["rows"][0]
        self.assertEqual(row["action"], "buy")  # can't confirm -> leave to the human
        self.assertFalse(row["price_gate"]["price_known"])

    def test_partial_ladder_scales_the_suggested_delta(self):
        # Two buy tranches at 360 (50%) and 320 (50%). At 340 only the first is
        # live, so the buy stays a buy but its delta is halved.
        self.price_levels.lock(
            "AMD", fair_value=400,
            buy_ladder=[{"price": 360, "size_pct": 0.5}, {"price": 320, "size_pct": 0.5}],
            currency="USD")
        plan = {"rows": [{"kind": "target", "held": True, "name": "AMD",
                          "action": "buy", "suggest_delta_pct": 2.0, "suggest_delta_czk": 1000}]}
        rebalance_overlay.attach_research_overlay(plan, self._holdings("AMD", 340.0))
        row = plan["rows"][0]
        gate = row["price_gate"]
        self.assertEqual(row["action"], "buy")  # partially live, still actionable
        self.assertTrue(gate["partial"])
        self.assertAlmostEqual(gate["applied_fraction"], 0.5)
        self.assertEqual(gate["full_suggest_delta_pct"], 2.0)
        self.assertEqual(row["suggest_delta_pct"], 1.0)  # scaled by 0.5
        self.assertEqual(row["suggest_delta_czk"], 500)
        self.assertEqual(gate["buy_live"], 1)
        self.assertEqual(gate["buy_total"], 2)
        self.assertEqual(gate["next_buy"]["price"], 320.0)

    def test_fully_unlocked_ladder_keeps_full_delta(self):
        self.price_levels.lock(
            "AMD", fair_value=400,
            buy_ladder=[{"price": 360, "size_pct": 0.5}, {"price": 320, "size_pct": 0.5}],
            currency="USD")
        plan = {"rows": [{"kind": "target", "held": True, "name": "AMD",
                          "action": "buy", "suggest_delta_pct": 2.0}]}
        rebalance_overlay.attach_research_overlay(plan, self._holdings("AMD", 300.0))
        row = plan["rows"][0]
        self.assertEqual(row["action"], "buy")
        self.assertFalse(row["price_gate"].get("partial", False))
        self.assertEqual(row["suggest_delta_pct"], 2.0)  # unscaled
        self.assertAlmostEqual(row["price_gate"]["applied_fraction"], 1.0)

    def test_ladder_above_all_tranches_waits(self):
        self.price_levels.lock(
            "AMD", fair_value=400,
            buy_ladder=[{"price": 360, "size_pct": 0.5}, {"price": 320, "size_pct": 0.5}],
            currency="USD")
        plan = {"rows": [{"kind": "target", "held": True, "name": "AMD",
                          "action": "buy", "suggest_delta_pct": 2.0}]}
        rebalance_overlay.attach_research_overlay(plan, self._holdings("AMD", 380.0))
        row = plan["rows"][0]
        self.assertEqual(row["action"], "wait")
        self.assertEqual(row["price_gate"]["blocked_action"], "buy")
        self.assertEqual(row["price_gate"]["applied_fraction"], 0.0)

    def _write_quote(self, sym, price, *, hours_ago):
        at = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours_ago)).isoformat()
        quote_cache.save({sym: {"price": price, "currency": "USD", "at": at}})

    def test_fresh_quote_beats_a_stale_holdings_mark(self):
        # Mark says 100 (would block a buy_below=92), but a fresh quote at 90
        # crossed the level today -> the gate opens within the hour.
        self.price_levels.lock("AMD", buy_below=92, currency="USD")
        self._write_quote("AMD", 90.0, hours_ago=1)
        plan = {"rows": [{"kind": "target", "held": True, "name": "AMD",
                          "action": "buy", "suggest_delta_pct": 1.0}]}
        rebalance_overlay.attach_research_overlay(plan, self._holdings("AMD", 100.0))
        row = plan["rows"][0]
        self.assertEqual(row["action"], "buy")             # fresh quote unblocks
        self.assertEqual(row["price_gate"]["current"], 90.0)
        self.assertEqual(row["last_quote"]["price"], 90.0)
        self.assertEqual(row["last_quote"]["source"], "quote cache")

    def test_stale_quote_loses_to_the_holdings_mark(self):
        # A 10h-old quote is past the 4h freshness window, so the mark wins.
        self.price_levels.lock("AMD", buy_below=92, currency="USD")
        self._write_quote("AMD", 90.0, hours_ago=10)
        plan = {"rows": [{"kind": "target", "held": True, "name": "AMD",
                          "action": "buy", "suggest_delta_pct": 1.0}]}
        rebalance_overlay.attach_research_overlay(plan, self._holdings("AMD", 100.0))
        row = plan["rows"][0]
        self.assertEqual(row["action"], "wait")            # mark 100 still blocks
        self.assertEqual(row["price_gate"]["current"], 100.0)
        self.assertEqual(row["last_quote"]["price"], 100.0)
        self.assertEqual(row["last_quote"]["source"], "holdings snapshot")


class OptionsOverlay(unittest.TestCase):
    """Folding pending option exposure onto plan rows/members: a buy the short
    puts already cover is downgraded and its pre-staged default dropped."""

    # Short 2x KLAC puts: on this fixture ~3.5% of invested pending long exposure.
    def _holdings(self):
        return {"positions": [
            {"symbol": "STK", "base_market_value": 28_697_329.0},
            {"symbol": "KLAC  260717P00238000", "asset_class": "OPT", "quantity": -2.0,
             "mark_price": 27.4615, "market_value": -5492.3, "base_market_value": -116650.96},
        ]}

    def test_full_cover_downgrades_target_buy_and_drops_default(self):
        plan = {"rows": [{"kind": "target", "held": False, "name": "KLAC",
                          "action": "buy", "suggest_delta_pct": 2.5, "suggest_delta_czk": 700_000}]}
        rebalance_overlay.attach_research_overlay(plan, self._holdings())
        row = plan["rows"][0]
        self.assertEqual(row["action"], "wait")          # 3.5% covers a 2.5% buy
        self.assertEqual(row["options"]["covers"], "full")
        self.assertEqual(row["options"]["full_suggest_delta_pct"], 2.5)
        self.assertEqual(row["suggest_delta_pct"], 0.0)  # not pre-staged
        self.assertEqual(row["suggest_delta_czk"], 0)

    def test_partial_cover_annotates_but_keeps_the_buy(self):
        plan = {"rows": [{"kind": "target", "held": False, "name": "KLAC",
                          "action": "buy", "suggest_delta_pct": 6.0, "suggest_delta_czk": 1_000_000}]}
        rebalance_overlay.attach_research_overlay(plan, self._holdings())
        row = plan["rows"][0]
        self.assertEqual(row["action"], "buy")           # 3.5% only partly covers 6%
        self.assertEqual(row["options"]["covers"], "partial")
        self.assertEqual(row["suggest_delta_pct"], 6.0)  # left to act
        self.assertGreater(row["options"]["covered_pct"], 0)

    def test_sleeve_aggregates_members_and_tempers_each(self):
        plan = {"rows": [{
            "kind": "sleeve", "held": False, "name": "semis-equipment",
            "action": "buy", "suggest_delta_pct": 2.5,
            "members": [
                {"symbol": "KLAC", "member_action": "buy",
                 "suggest_delta_pct": 2.5, "suggest_delta_czk": 700_000},
                {"symbol": "OTHER", "member_action": "buy", "suggest_delta_pct": 1.0},
            ],
        }]}
        rebalance_overlay.attach_research_overlay(plan, self._holdings())
        row = plan["rows"][0]
        klac = row["members"][0]
        other = row["members"][1]
        self.assertEqual(klac["options"]["covers"], "full")   # covered member
        self.assertIsNone(klac["member_action"])              # no move staged
        self.assertEqual(klac["suggest_delta_pct"], 0.0)
        self.assertNotIn("options", other)                    # no exposure -> untouched
        self.assertEqual(other["member_action"], "buy")
        self.assertIn("options", row)                         # sleeve aggregate present
        self.assertEqual(row["action"], "wait")               # sleeve buy covered too

    def test_no_options_leaves_rows_untouched(self):
        plan = {"rows": [{"kind": "target", "held": True, "name": "KLAC",
                          "action": "buy", "suggest_delta_pct": 2.5}]}
        rebalance_overlay.attach_research_overlay(plan, {"positions": [
            {"symbol": "KLAC", "base_market_value": 100.0}]})
        row = plan["rows"][0]
        self.assertNotIn("options", row)
        self.assertEqual(row["action"], "buy")


if __name__ == "__main__":
    unittest.main()
