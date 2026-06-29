"""Tests for the target-model synthesis engine. The deterministic normalizer is
the safety-critical part (an unbounded LLM would emit an infeasible >100% book),
so it gets the bulk of the coverage: budget sanity, per-name + sleeve caps,
blocked exclusion, trim-only for avoid, and add-vs-modify selection. Offline;
the LLM pass is never invoked (use_llm=False / direct normalizer calls)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import _support  # noqa: F401
import target_construct as tc


def _row(symbol, *, action="hold", held=None, sleeve="core", dq="OK"):
    return {"symbol": symbol, "report_action": action, "held_pct_nav": held,
            "sleeve": sleeve, "data_quality": dq}


class HeuristicConvictions(unittest.TestCase):
    def test_action_to_conviction_mapping(self):
        rows = [_row("AAA", action="add"), _row("BBB", action="hold"),
                _row("CCC", action="trim"), _row("DDD", action="not mentioned")]
        conv = tc.heuristic_convictions(rows)
        self.assertEqual(conv["AAA"], "high")
        self.assertEqual(conv["BBB"], "medium")
        self.assertEqual(conv["CCC"], "avoid")
        self.assertNotIn("DDD", conv)  # no opinion -> no target


class NormalizeTargets(unittest.TestCase):
    def _convictions(self, mapping):
        return {sym: {"conviction": c, "rationale": "", "source": "heuristic"}
                for sym, c in mapping.items()}

    def test_feasibility_caps_and_exclusions(self):
        rows = [
            _row("AAA", held=10.0),   # held + high  -> modify_target
            _row("BBB"),              # new  + high   -> add_target, sleeve-capped
            _row("CCC"),              # new  + medium -> add_target
            _row("DDD", held=4.0),    # held + avoid  -> trim_only
            _row("EEE"),              # new  + avoid  -> skipped entirely
            _row("FFF"),              # blocked       -> never proposed
        ]
        convictions = self._convictions({
            "AAA": "high", "BBB": "high", "CCC": "medium",
            "DDD": "avoid", "EEE": "avoid", "FFF": "high",
        })
        model = {
            "targets": {"AAA": {"low": 8, "high": 12, "rule": "hold", "note": "keep"}},
            "sleeves": {"core": {"member_caps": {"BBB": 3.0}}},
        }
        changes, meta = tc.normalize_targets(
            convictions, rows, model, blocked={"FFF"}, segment_budget_pct=20.0)
        by_sym = {c["symbol"]: c for c in changes}

        # Blocked + unheld-avoid are never sized.
        self.assertNotIn("FFF", by_sym)
        self.assertNotIn("EEE", by_sym)

        # add vs modify is driven by the existing model.
        self.assertEqual(by_sym["AAA"]["action"], "modify_target")
        self.assertEqual(by_sym["BBB"]["action"], "add_target")
        self.assertEqual(by_sym["CCC"]["action"], "add_target")

        # Avoid+held trims toward zero (it has no prior band, so it's an add with
        # a trim rule rather than a modify).
        self.assertEqual(by_sym["DDD"]["proposed_target"]["rule"], "trim_only")
        self.assertEqual(by_sym["DDD"]["proposed_target"]["low"], 0.0)

        # Sleeve member_cap binds BBB's midpoint.
        bbb = by_sym["BBB"]["proposed_target"]
        bbb_mid = (bbb["low"] + bbb["high"]) / 2.0
        self.assertLessEqual(bbb_mid, 3.0 + 1e-6)

        # Total sized buy midpoints never exceed the segment budget.
        buy_mid = sum(((c["proposed_target"]["low"] + c["proposed_target"]["high"]) / 2.0)
                      for c in changes if c["proposed_target"]["rule"] != "trim_only")
        self.assertLessEqual(buy_mid, 20.0 + 1e-6)
        self.assertLessEqual(meta["sized_midpoint_total_pct"], 20.0 + 1e-6)

    def test_avoid_held_trims_toward_existing_band_not_half_weight(self):
        # An avoid-rated name that already has a sized band should trim toward
        # that band (matching the analyst's stated target), not to a blunt
        # half-current-weight cut that contradicts the rationale.
        rows = [_row("AMD", held=15.3)]
        convictions = self._convictions({"AMD": "avoid"})
        model = {"targets": {"AMD": {"low": 10, "high": 12, "rule": "trim_only"}}}
        changes, _ = tc.normalize_targets(convictions, rows, model, segment_budget_pct=20.0)
        pt = {c["symbol"]: c for c in changes}["AMD"]["proposed_target"]
        self.assertEqual(pt["rule"], "trim_only")
        self.assertEqual(pt["low"], 10.0)
        self.assertEqual(pt["high"], 12.0)  # NOT 15.3 * 0.5 = 7.7

    def test_avoid_held_without_prior_target_falls_back_to_half_weight(self):
        rows = [_row("ZZZ", held=15.3)]
        convictions = self._convictions({"ZZZ": "avoid"})
        changes, _ = tc.normalize_targets(convictions, rows, {}, segment_budget_pct=20.0)
        pt = {c["symbol"]: c for c in changes}["ZZZ"]["proposed_target"]
        self.assertEqual(pt["rule"], "trim_only")
        self.assertEqual(pt["low"], 0.0)
        self.assertEqual(pt["high"], 7.7)  # 15.3 * 0.5, rounded

    def test_per_name_cap_binds(self):
        rows = [_row("AAA")]
        convictions = self._convictions({"AAA": "high"})
        changes, _ = tc.normalize_targets(convictions, rows, {}, segment_budget_pct=40.0,
                                          per_name_cap=5.0)
        pt = changes[0]["proposed_target"]
        self.assertLessEqual((pt["low"] + pt["high"]) / 2.0, 5.0 + 1e-6)

    def test_band_has_minimum_width(self):
        rows = [_row("AAA")]
        convictions = self._convictions({"AAA": "low"})
        changes, _ = tc.normalize_targets(convictions, rows, {}, segment_budget_pct=10.0)
        pt = changes[0]["proposed_target"]
        self.assertGreaterEqual(pt["high"] - pt["low"], tc.MIN_BAND_WIDTH - 1e-9)

    def test_blocked_set_is_respected(self):
        rows = [_row("AAA"), _row("BBB")]
        convictions = self._convictions({"AAA": "high", "BBB": "high"})
        changes, _ = tc.normalize_targets(convictions, rows, {}, blocked={"AAA"},
                                          segment_budget_pct=10.0)
        self.assertEqual({c["symbol"] for c in changes}, {"BBB"})

    def test_pin_floor_anchors_band(self):
        # A pinned floor raises the band's low even if the budget would size it
        # smaller -- the pin is the sizing anchor, not just the transient band.
        rows = [_row("TSM", held=2.0)]
        convictions = self._convictions({"TSM": "low"})  # would size tiny
        model = {"targets": {}, "provenance": {
            "TSM": {"source": "user-pin", "locked": True, "stance": "accumulate", "floor_pct": 3.0}}}
        changes, meta = tc.normalize_targets(convictions, rows, model, segment_budget_pct=5.0)
        pt = {c["symbol"]: c for c in changes}["TSM"]["proposed_target"]
        self.assertGreaterEqual(pt["low"], 3.0 - 1e-9)
        self.assertEqual(meta["pinned_count"], 1)

    def test_avoid_on_pinned_name_challenges_but_does_not_drop(self):
        rows = [_row("TSM", held=6.0)]
        convictions = self._convictions({"TSM": "avoid"})
        model = {"targets": {"TSM": {"low": 6, "high": 8, "rule": "accumulate"}},
                 "provenance": {"TSM": {"source": "user-pin", "locked": True,
                                        "stance": "accumulate", "floor_pct": 3.0}}}
        changes, meta = tc.normalize_targets(convictions, rows, model,
                                             segment_budget_pct=20.0, drop_mode=True)
        ch = {c["symbol"]: c for c in changes}["TSM"]
        self.assertNotEqual(ch["action"], "remove_target")  # never auto-dropped
        self.assertTrue(ch.get("challenges_pin"))
        self.assertIn("TSM", meta["challenges_pins"])

    def test_drop_mode_removes_unpinned_avoid_held(self):
        rows = [_row("XYZ", held=4.0)]
        convictions = self._convictions({"XYZ": "avoid"})
        changes, _ = tc.normalize_targets(convictions, rows, {}, segment_budget_pct=20.0,
                                          drop_mode=True)
        ch = {c["symbol"]: c for c in changes}["XYZ"]
        self.assertEqual(ch["action"], "remove_target")

    def test_sleeve_label_is_normalized(self):
        # "compute" maps to "semis-compute" in data/sleeve-aliases.json; the
        # normalized tag rides on the proposed band so the apply persists it.
        rows = [_row("NVDA", sleeve="compute")]
        convictions = self._convictions({"NVDA": "high"})
        changes, _ = tc.normalize_targets(convictions, rows, {}, segment_budget_pct=20.0)
        ch = changes[0]
        self.assertEqual(ch["sleeve"], "semis-compute")
        self.assertEqual(ch["proposed_target"]["sleeve"], "semis-compute")
        self.assertFalse(ch["sleeve_unknown"])

    def test_unknown_sleeve_is_flagged(self):
        rows = [_row("NVDA", sleeve="totally-made-up")]
        convictions = self._convictions({"NVDA": "high"})
        changes, _ = tc.normalize_targets(convictions, rows, {}, segment_budget_pct=20.0)
        self.assertTrue(changes[0]["sleeve_unknown"])

    def test_book_reconciliation_flags_over_allocation(self):
        rows = [_row("AAA")]
        convictions = self._convictions({"AAA": "high"})
        model = {"targets": {"BIG": {"low": 90, "high": 100, "rule": "hold"}},
                 "cash_target_pct": 5.0}
        holdings = {"positions": [{"symbol": "AAA", "base_market_value": 100.0}]}
        _changes, meta = tc.normalize_targets(convictions, rows, model,
                                              segment_budget_pct=20.0, holdings=holdings)
        self.assertTrue(meta["book_reconciliation"]["over_allocated"])

    # --- position-aware rule derivation (the rule must match the move needed to
    # reach the band, not just the conviction; otherwise 'hold' lands on names
    # not held or far from their band and the rebalancer flags a contradiction).
    def _rule_for(self, conviction, held):
        rows = [_row("AAA", held=held)]
        convictions = self._convictions({"AAA": conviction})
        changes, _ = tc.normalize_targets(convictions, rows, {}, segment_budget_pct=10.0)
        return {c["symbol"]: c for c in changes}["AAA"]["proposed_target"]

    def test_owned_name_not_held_accumulates(self):
        # medium conviction, no current position -> build toward the band.
        pt = self._rule_for("medium", None)
        self.assertEqual(pt["rule"], "accumulate")

    def test_owned_name_below_band_accumulates(self):
        # held well under the sized band low (band ~ [8.5, 11.5] for budget 10).
        pt = self._rule_for("medium", 2.0)
        self.assertEqual(pt["rule"], "accumulate")

    def test_owned_name_in_band_holds(self):
        pt = self._rule_for("medium", 10.0)
        self.assertEqual(pt["rule"], "hold")
        self.assertLessEqual(pt["low"], 10.0)
        self.assertGreaterEqual(pt["high"], 10.0)

    def test_owned_name_above_band_trims(self):
        pt = self._rule_for("medium", 18.0)
        self.assertEqual(pt["rule"], "trim_only")

    def test_high_conviction_in_band_holds_not_accumulate(self):
        # Even a high-conviction name already inside its band is 'hold' -- the
        # band is a no-trade zone, so we don't keep buying within it.
        pt = self._rule_for("high", 10.0)
        self.assertEqual(pt["rule"], "hold")

    def test_low_conviction_stays_wait_regardless_of_holding(self):
        # 'wait' is a deliberate "don't chase a lukewarm name" -- position must
        # NOT promote it to accumulate.
        self.assertEqual(self._rule_for("low", None)["rule"], "wait")
        self.assertEqual(self._rule_for("low", 0.5)["rule"], "wait")

    def test_sleeve_member_is_not_sized_standalone(self):
        # A name already governed by a named sleeve must not also get a top-level
        # target (the rebalancer flags 'both a sleeve member and a target').
        rows = [_row("AAA"), _row("SOXX")]
        convictions = self._convictions({"AAA": "high", "SOXX": "medium"})
        model = {"sleeves": {"semis-etf": {"low": 5, "high": 10, "rule": "accumulate",
                                           "members": ["XSD", "SOXX"]}}}
        changes, meta = tc.normalize_targets(convictions, rows, model, segment_budget_pct=20.0)
        syms = {c["symbol"] for c in changes}
        self.assertIn("AAA", syms)
        self.assertNotIn("SOXX", syms)
        self.assertEqual(meta["sleeve_managed_skipped"], ["SOXX"])


class BuildLlmPrompt(unittest.TestCase):
    def test_research_block_is_injected(self):
        block = "PER-NAME RESEARCH:\n- AAA: held 5% NAV; research (deep): bullish."
        prompt = tc._build_llm_prompt("Segment essay here.", ["AAA", "BBB"], block)
        self.assertIn(block, prompt)
        self.assertIn("AAA, BBB", prompt)
        self.assertIn("Segment essay here.", prompt)

    def test_empty_block_keeps_prompt_clean(self):
        prompt = tc._build_llm_prompt("Segment essay.", ["AAA"], "")
        self.assertNotIn("PER-NAME RESEARCH", prompt)
        self.assertIn("AAA", prompt)


class ConstructWritesProposal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self._orig = {k: getattr(tc, k) for k in ("DEEP_DIR", "TARGET_MODEL_JSON")}
        tc.DEEP_DIR = root / "deep"
        tc.TARGET_MODEL_JSON = root / "target-model.json"
        tc.DEEP_DIR.mkdir(parents=True)

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(tc, k, v)
        self.tmp.cleanup()

    def test_construct_offline_writes_enriched_proposal(self):
        review = {
            "rows": [_row("AAA", action="add", held=10.0), _row("BBB", action="hold")],
            "blocked_symbols": [],
            "report": "We would add AAA and hold BBB.",
            "warnings": [],
            "findings": [],
            "source_summary": {"count": 3},
        }
        proposal = tc.construct("test-seg", "2026-06-01", review, use_llm=False,
                                model={"targets": {}}, segment_budget_pct=20.0)
        self.assertEqual(proposal["schema_version"], 2)
        self.assertTrue(proposal["changes"])
        path = tc.proposal_path("test-seg", "2026-06-01")
        self.assertTrue(path.exists())
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["segment"], "test-seg")


if __name__ == "__main__":
    unittest.main()
