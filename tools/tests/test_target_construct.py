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
