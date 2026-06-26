"""Tests for the staging layer: the working draft that sits between exploratory
runs and the single live target model. Covers provenance backfill, composing
multiple runs, the whole-book diff + reconciliation, drops, sleeve persistence,
conviction pins (set/clear, drop-guard, challenge), and commit/discard. Offline:
the site regenerator is stubbed and every disk path is redirected to a temp dir.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import _support  # noqa: F401
import target_model
import target_staging as ts
from store import load as _load, write_json as _write_json


class _StagingCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.live = root / "target-model.json"
        self.staged = root / "target-model.staged.json"
        self.holdings = root / "current-holdings.json"
        self._orig = {
            (ts, "TARGET_MODEL_JSON"): ts.TARGET_MODEL_JSON,
            (ts, "STAGED_JSON"): ts.STAGED_JSON,
            (ts, "HOLDINGS_JSON"): ts.HOLDINGS_JSON,
            (ts, "_regenerate_site"): ts._regenerate_site,
            (target_model, "TARGET_MODEL_JSON"): target_model.TARGET_MODEL_JSON,
            (target_model, "TARGET_MODEL_BACKUP_DIR"): target_model.TARGET_MODEL_BACKUP_DIR,
            (target_model, "REPO_ROOT"): target_model.REPO_ROOT,
        }
        ts.TARGET_MODEL_JSON = self.live
        ts.STAGED_JSON = self.staged
        ts.HOLDINGS_JSON = self.holdings
        ts._regenerate_site = lambda: {"ok": True, "written": []}
        target_model.TARGET_MODEL_JSON = self.live
        target_model.TARGET_MODEL_BACKUP_DIR = root / "backups"
        target_model.REPO_ROOT = root

    def tearDown(self):
        for (mod, name), val in self._orig.items():
            setattr(mod, name, val)
        self.tmp.cleanup()

    def _seed_live(self, model=None):
        model = model or {
            "as_of": "2026-01-01",
            "cash_target_pct": 5.0,
            "targets": {"TSM": {"low": 6, "high": 8, "rule": "accumulate"}},
            "sleeves": {},
            "funding_order": ["TSM"],
        }
        _write_json(self.live, model)
        return model

    def _add(self, sym, low, high, rule="accumulate", sleeve=None):
        pt = {"low": low, "high": high, "rule": rule}
        if sleeve:
            pt["sleeve"] = sleeve
        return {"action": "add_target", "symbol": sym, "proposed_target": pt}


class ProvenanceBackfill(_StagingCase):
    def test_legacy_bands_are_flagged(self):
        self._seed_live()
        ts.load_staged(create=True)
        prov = _load(self.staged)["provenance"]
        self.assertEqual(prov["TSM"]["source"], "legacy-plan")
        self.assertEqual(prov["TSM"]["set_at"], "2026-01-01")


class StageCompose(_StagingCase):
    def test_two_runs_compose_with_provenance(self):
        self._seed_live()
        ts.stage_changes([self._add("NVDA", 8, 10)], run_id="run-a",
                         segment="add-ai", source="strategy")
        ts.stage_changes([self._add("ASML", 4, 6)], run_id="run-b",
                         segment="add-equip", source="strategy")
        staged = _load(self.staged)
        self.assertIn("NVDA", staged["targets"])
        self.assertIn("ASML", staged["targets"])
        # TSM (legacy) is untouched and still present.
        self.assertIn("TSM", staged["targets"])
        self.assertEqual(staged["provenance"]["NVDA"]["run_id"], "run-a")
        self.assertEqual(staged["provenance"]["ASML"]["segment"], "add-equip")
        self.assertEqual(len(staged["_runs"]), 2)

    def test_sleeve_is_persisted_on_the_band(self):
        self._seed_live()
        ts.stage_changes([self._add("NVDA", 8, 10, sleeve="semis-compute")],
                         run_id="r", segment="s")
        staged = _load(self.staged)
        self.assertEqual(staged["targets"]["NVDA"]["sleeve"], "semis-compute")


class DiffAndReconcile(_StagingCase):
    def test_diff_classifies_changes(self):
        self._seed_live()
        ts.stage_changes([
            self._add("NVDA", 8, 10),                                  # added
            {"action": "modify_target", "symbol": "TSM",
             "proposed_target": {"low": 3, "high": 5}},                # modified
        ], run_id="r", segment="s")
        diff = ts.diff_staged_vs_live()
        self.assertTrue(diff["has_draft"])
        by_key = {r["key"]: r for r in diff["targets"]}
        self.assertEqual(by_key["NVDA"]["change"], "added")
        self.assertEqual(by_key["TSM"]["change"], "modified")
        self.assertEqual(diff["counts"]["total"], 2)

    def test_reconciliation_totals_and_untargeted(self):
        self._seed_live({
            "as_of": "2026-01-01", "cash_target_pct": 0.0,
            "targets": {"AAA": {"low": 10, "high": 20, "rule": "hold"}},
            "sleeves": {},
        })
        _write_json(self.holdings, {"generated_at": "2026-06-01", "positions": [
            {"symbol": "AAA", "base_market_value": 1500.0},
            {"symbol": "ZZZ", "base_market_value": 500.0},  # untargeted
        ]})
        diff = ts.diff_staged_vs_live()  # no draft -> live reconciliation
        rec = diff["reconciliation"]
        self.assertAlmostEqual(rec["targeted_mid_pct"], 15.0, places=2)
        self.assertAlmostEqual(rec["available_pct"], 85.0, places=2)
        self.assertEqual([u["symbol"] for u in rec["untargeted"]], ["ZZZ"])


class Drops(_StagingCase):
    def test_remove_target_drops_band_and_provenance(self):
        self._seed_live()
        ts.stage_changes([{"action": "remove_target", "symbol": "TSM"}],
                         run_id="r", segment="drop-tsm")
        staged = _load(self.staged)
        self.assertNotIn("TSM", staged["targets"])
        self.assertNotIn("TSM", staged["provenance"])


class Pins(_StagingCase):
    def test_set_and_load_pin(self):
        self._seed_live()
        ts.set_pin("TSM", stance="accumulate", floor_pct=3.0, rationale="I like it")
        pins = ts.load_pins()
        self.assertIn("TSM", pins)
        self.assertEqual(pins["TSM"]["stance"], "accumulate")
        self.assertEqual(pins["TSM"]["floor_pct"], 3.0)
        self.assertTrue(ts.is_locked("TSM"))

    def test_clear_pin(self):
        self._seed_live()
        ts.set_pin("TSM", stance="hold")
        self.assertEqual(ts.clear_pin("TSM")["cleared"], True)
        self.assertNotIn("TSM", ts.load_pins())

    def test_pinned_drop_is_blocked_without_override(self):
        self._seed_live()
        ts.set_pin("TSM", stance="accumulate")
        res = ts.stage_changes([{"action": "remove_target", "symbol": "TSM"}],
                               run_id="r", segment="drop")
        self.assertNotIn("-TSM", res["applied"])
        self.assertTrue(any("pinned" in s["reason"] for s in res["skipped"]))
        self.assertIn("TSM", _load(self.staged)["targets"])

    def test_pinned_drop_allowed_with_override(self):
        self._seed_live()
        ts.set_pin("TSM", stance="accumulate")
        res = ts.stage_changes([{"action": "remove_target", "symbol": "TSM"}],
                               run_id="r", segment="drop", allow_drop_pinned=True)
        self.assertIn("-TSM", res["applied"])

    def test_invalid_stance_rejected(self):
        self._seed_live()
        with self.assertRaises(ValueError):
            ts.set_pin("TSM", stance="moon")


class Revert(_StagingCase):
    def test_revert_restores_live_band(self):
        self._seed_live()
        ts.stage_changes([{"action": "modify_target", "symbol": "TSM",
                           "proposed_target": {"low": 1, "high": 2}}],
                         run_id="r", segment="s")
        self.assertEqual(_load(self.staged)["targets"]["TSM"]["high"], 2)
        ts.revert_key("TSM")
        self.assertEqual(_load(self.staged)["targets"]["TSM"]["high"], 8)

    def test_revert_removes_added_key(self):
        self._seed_live()
        ts.stage_changes([self._add("NVDA", 8, 10)], run_id="r", segment="s")
        ts.revert_key("NVDA")
        self.assertNotIn("NVDA", _load(self.staged)["targets"])


class ActiveModel(_StagingCase):
    def test_returns_live_when_no_draft(self):
        live = self._seed_live()
        active = ts.active_model()
        self.assertEqual(active.get("targets"), live["targets"])

    def test_returns_draft_when_one_exists(self):
        self._seed_live()
        ts.stage_changes([self._add("NVDA", 8, 10)], run_id="r", segment="s")
        active = ts.active_model()
        # The planner/what-if now see the staged NVDA, not just the live TSM.
        self.assertIn("NVDA", active.get("targets", {}))
        self.assertIn("TSM", active.get("targets", {}))


class CommitDiscard(_StagingCase):
    def test_commit_blocks_on_check_model_error_and_keeps_draft(self):
        self._seed_live()
        _write_json(self.holdings, {"generated_at": "2026-06-15", "positions": []})
        # 'reduce' on a name we don't hold is a hard ERROR in check_model: there
        # is nothing to reduce. Such a draft must never be promoted to live.
        ts.stage_changes([{"action": "add_target", "symbol": "ZZZ",
                           "proposed_target": {"low": 1, "high": 2, "rule": "reduce"}}],
                         run_id="r", segment="s")
        with self.assertRaises(ValueError) as ctx:
            ts.commit_staged(True)
        self.assertIn("blocking", str(ctx.exception).lower())
        # Live model untouched, draft preserved, and no backup was written.
        self.assertNotIn("ZZZ", _load(self.live).get("targets", {}))
        self.assertTrue(self.staged.exists())
        self.assertFalse((Path(self.tmp.name) / "backups").exists())

    def test_commit_writes_live_bumps_as_of_clears_draft(self):
        self._seed_live()
        _write_json(self.holdings, {"generated_at": "2026-06-15", "positions": []})
        ts.stage_changes([self._add("NVDA", 8, 10)], run_id="r", segment="s")
        out = ts.commit_staged(True)
        self.assertTrue(out["committed"])
        live = _load(self.live)
        self.assertIn("NVDA", live["targets"])
        self.assertNotEqual(live["as_of"], "2026-01-01")  # bumped
        self.assertEqual(live["basis_snapshot"], "2026-06-15")
        self.assertNotIn("_runs", live)
        self.assertFalse(self.staged.exists())  # draft cleared

    def test_commit_requires_confirm(self):
        self._seed_live()
        ts.stage_changes([self._add("NVDA", 8, 10)], run_id="r", segment="s")
        with self.assertRaises(ValueError):
            ts.commit_staged(False)

    def test_commit_without_draft_raises(self):
        self._seed_live()
        with self.assertRaises(ValueError):
            ts.commit_staged(True)

    def test_discard_removes_draft(self):
        self._seed_live()
        ts.stage_changes([self._add("NVDA", 8, 10)], run_id="r", segment="s")
        self.assertTrue(ts.discard_staged()["discarded"])
        self.assertFalse(self.staged.exists())
        self.assertEqual(ts.discard_staged()["discarded"], False)  # idempotent


if __name__ == "__main__":
    unittest.main()
