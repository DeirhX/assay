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
        self.prov_log = root / "provenance-log.jsonl"
        self.backups = root / "backups"
        self._orig = {
            (ts, "TARGET_MODEL_JSON"): ts.TARGET_MODEL_JSON,
            (ts, "STAGED_JSON"): ts.STAGED_JSON,
            (ts, "HOLDINGS_JSON"): ts.HOLDINGS_JSON,
            (ts, "PROVENANCE_LOG"): ts.PROVENANCE_LOG,
            (ts, "TARGET_MODEL_BACKUP_DIR"): ts.TARGET_MODEL_BACKUP_DIR,
            (ts, "_regenerate_site"): ts._regenerate_site,
            (target_model, "TARGET_MODEL_JSON"): target_model.TARGET_MODEL_JSON,
            (target_model, "TARGET_MODEL_BACKUP_DIR"): target_model.TARGET_MODEL_BACKUP_DIR,
            (target_model, "REPO_ROOT"): target_model.REPO_ROOT,
        }
        ts.TARGET_MODEL_JSON = self.live
        ts.STAGED_JSON = self.staged
        ts.HOLDINGS_JSON = self.holdings
        ts.PROVENANCE_LOG = self.prov_log
        ts.TARGET_MODEL_BACKUP_DIR = self.backups
        ts._regenerate_site = lambda: {"ok": True, "written": []}
        target_model.TARGET_MODEL_JSON = self.live
        target_model.TARGET_MODEL_BACKUP_DIR = self.backups
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

    def test_avoid_pin_sets_live_and_existing_draft_to_zero(self):
        self._seed_live()
        ts.load_staged(create=True)

        pin = ts.set_pin("TSM", stance="avoid", rationale="Exit permanently")

        self.assertEqual(pin["floor_pct"], 0.0)
        self.assertEqual(pin["ceiling_pct"], 0.0)
        for path in (self.live, self.staged):
            model = _load(path)
            self.assertEqual(
                model["targets"]["TSM"],
                {
                    "low": 0.0,
                    "high": 0.0,
                    "rule": "avoid",
                    "note": "Exit permanently",
                },
            )
            self.assertEqual(model["provenance"]["TSM"]["stance"], "avoid")

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


class RestoreBackup(_StagingCase):
    """The visible undo: diff + restore of a pre-apply target-model backup."""

    def setUp(self):
        super().setUp()
        root = Path(self.tmp.name)
        # _resolve_backup also reads REPO_ROOT from target_staging's namespace;
        # the base case already redirects ts.TARGET_MODEL_BACKUP_DIR.
        self._orig[(ts, "REPO_ROOT")] = ts.REPO_ROOT
        ts.REPO_ROOT = root

    def test_diff_then_restore_roundtrip(self):
        self._seed_live()  # TSM 6–8
        backup_rel = target_model._backup_target_model()  # snapshot the 6–8 model
        self.assertIsNotNone(backup_rel)
        # Move the live band away from the backup.
        self._seed_live({
            "as_of": "2026-02-02", "cash_target_pct": 5.0,
            "targets": {"TSM": {"low": 10, "high": 12, "rule": "accumulate"}},
            "sleeves": {}, "funding_order": ["TSM"],
        })
        diff = ts.diff_backup_vs_live(backup_rel)
        self.assertEqual(diff["counts"]["total"], 1)
        row = diff["targets"][0]
        self.assertEqual(row["change"], "modified")
        self.assertEqual(row["before"]["low"], 10)  # current live
        self.assertEqual(row["after"]["low"], 6)     # what restoring lands on
        res = ts.restore_backup(backup_rel, confirm=True)
        self.assertTrue(res["restored"])
        self.assertIsNotNone(res["backup_of_current"])  # restore is itself reversible
        self.assertEqual(_load(self.live)["targets"]["TSM"]["low"], 6)

    def test_restore_requires_confirm(self):
        self._seed_live()
        rel = target_model._backup_target_model()
        with self.assertRaises(ValueError):
            ts.restore_backup(rel, confirm=False)
        self.assertEqual(_load(self.live)["targets"]["TSM"]["low"], 6)  # untouched

    def test_rejects_paths_outside_the_backups_dir(self):
        self._seed_live()
        for bad in ("../../etc/passwd", "data/target-model.json", "", "backups/nope.json"):
            with self.assertRaises(ValueError):
                ts.diff_backup_vs_live(bad)


class ProvenanceTimeline(_StagingCase):
    """The append-only decision log: pure diffing, the commit hook, corrupt-line
    tolerance, and idempotent backfill from dated backups."""

    def test_pure_entries_capture_add_modify_remove(self):
        prior = {
            "targets": {"TSM": {"low": 6, "high": 8, "rule": "accumulate"},
                        "OLD": {"low": 1, "high": 2, "rule": "hold"}},
            "sleeves": {},
        }
        later = {
            "targets": {"TSM": {"low": 7, "high": 9, "rule": "accumulate"},  # modified
                        "NVDA": {"low": 8, "high": 10, "rule": "accumulate"}},  # added; OLD removed
            "sleeves": {},
            "provenance": {
                "TSM": {"source": "optimizer", "run_id": "r2"},
                "NVDA": {"source": "strategy", "run_id": "r1", "segment": "ai", "conviction": "high"},
            },
        }
        by_key = {e["key"]: e for e in ts._provenance_entries(prior, later, at="T0")}
        self.assertEqual(by_key["NVDA"]["change"], "added")
        self.assertIsNone(by_key["NVDA"]["before"])
        self.assertEqual(by_key["NVDA"]["after"], {"low": 8, "high": 10, "rule": "accumulate"})
        self.assertEqual(by_key["NVDA"]["source"], "strategy")
        self.assertEqual(by_key["NVDA"]["conviction"], "high")
        self.assertEqual(by_key["TSM"]["change"], "modified")
        self.assertEqual(by_key["TSM"]["before"]["low"], 6)
        self.assertEqual(by_key["TSM"]["after"]["low"], 7)
        self.assertEqual(by_key["TSM"]["source"], "optimizer")
        self.assertEqual(by_key["OLD"]["change"], "removed")
        self.assertIsNone(by_key["OLD"]["after"])
        self.assertTrue(all(e["at"] == "T0" for e in by_key.values()))

    def test_commit_appends_entry_with_provenance(self):
        self._seed_live()
        _write_json(self.holdings, {"generated_at": "2026-06-15", "positions": []})
        ts.stage_changes([self._add("NVDA", 8, 10)], run_id="r", segment="s", source="strategy")
        out = ts.commit_staged(True)
        self.assertEqual(out["provenance_logged"], 1)
        log = ts.read_provenance_log(self.prov_log)
        self.assertEqual(len(log), 1)
        e = log[0]
        self.assertEqual((e["key"], e["kind"], e["change"]), ("NVDA", "target", "added"))
        self.assertEqual(e["source"], "strategy")
        self.assertEqual(e["run_id"], "r")
        self.assertEqual(e["segment"], "s")
        self.assertEqual(e["after"], {"low": 8, "high": 10, "rule": "accumulate"})
        self.assertNotIn("backfill", e)

    def test_commit_records_modified_before_and_after(self):
        self._seed_live()
        _write_json(self.holdings, {"generated_at": "2026-06-15", "positions": []})
        ts.stage_changes(
            [{"action": "modify_target", "symbol": "TSM",
              "proposed_target": {"low": 7, "high": 9, "rule": "accumulate"}}],
            run_id="r", segment="s")
        ts.commit_staged(True)
        e = ts.read_provenance_log(self.prov_log)[0]
        self.assertEqual(e["change"], "modified")
        self.assertEqual(e["before"], {"low": 6, "high": 8, "rule": "accumulate"})
        self.assertEqual(e["after"], {"low": 7, "high": 9, "rule": "accumulate"})

    def test_commit_survives_a_log_write_failure(self):
        self._seed_live()
        _write_json(self.holdings, {"generated_at": "2026-06-15", "positions": []})
        ts.stage_changes([self._add("NVDA", 8, 10)], run_id="r", segment="s")
        orig = ts._append_provenance_log
        ts._append_provenance_log = lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
        try:
            out = ts.commit_staged(True)  # must not raise
        finally:
            ts._append_provenance_log = orig
        self.assertTrue(out["committed"])
        self.assertEqual(out["provenance_logged"], 0)
        self.assertIn("NVDA", _load(self.live)["targets"])  # commit still landed

    def test_append_dedupes_on_rerun(self):
        entries = [{"at": "T0", "kind": "target", "key": "NVDA", "change": "added",
                    "before": None, "after": {"low": 8, "high": 10}, "source": "strategy"}]
        self.assertEqual(ts._append_provenance_log(entries, path=self.prov_log), 1)
        self.assertEqual(ts._append_provenance_log(entries, path=self.prov_log), 0)
        self.assertEqual(len(ts.read_provenance_log(self.prov_log)), 1)

    def test_read_skips_corrupt_lines(self):
        self.prov_log.write_text(
            '{"at":"T0","key":"NVDA"}\nnot-json\n\n{"at":"T1","key":"TSM"}\n',
            encoding="utf-8")
        log = ts.read_provenance_log(self.prov_log)
        self.assertEqual([e["key"] for e in log], ["NVDA", "TSM"])

    def _write_backup(self, name, model):
        self.backups.mkdir(parents=True, exist_ok=True)
        _write_json(self.backups / name, model)

    def test_backfill_reconstructs_and_is_idempotent(self):
        self._write_backup("target-model-20260101T000000Z.json", {
            "as_of": "2026-01-01",
            "targets": {"TSM": {"low": 6, "high": 8, "rule": "accumulate"}},
            "provenance": {"TSM": {"source": "legacy-plan"}}})
        self._write_backup("target-model-20260201T000000Z.json", {
            "as_of": "2026-02-01",
            "targets": {"TSM": {"low": 6, "high": 8, "rule": "accumulate"},
                        "NVDA": {"low": 8, "high": 10, "rule": "accumulate"}},
            "provenance": {"NVDA": {"source": "strategy", "run_id": "r1"}}})
        _write_json(self.live, {
            "as_of": "2026-03-01",
            "targets": {"TSM": {"low": 6, "high": 8, "rule": "accumulate"},
                        "NVDA": {"low": 9, "high": 11, "rule": "accumulate"}},
            "provenance": {"NVDA": {"source": "optimizer", "run_id": "r2"}}})

        res = ts.backfill_provenance_log(path=self.prov_log, backup_dir=self.backups)
        self.assertEqual(res["snapshots"], 3)
        self.assertEqual(res["written"], 2)  # NVDA added (b1->b2), NVDA modified (b2->live)
        log = ts.read_provenance_log(self.prov_log)
        self.assertTrue(all(e.get("backfill") for e in log))
        changes = sorted((e["change"], e["source"]) for e in log)
        self.assertEqual(changes, [("added", "strategy"), ("modified", "optimizer")])

        again = ts.backfill_provenance_log(path=self.prov_log, backup_dir=self.backups)
        self.assertEqual(again["written"], 0)  # idempotent
        self.assertEqual(len(ts.read_provenance_log(self.prov_log)), 2)


class HomeSegment(_StagingCase):
    def test_membership_backfilled_on_load_staged(self):
        self._seed_live({
            "as_of": "2026-01-01", "cash_target_pct": 0,
            "targets": {},
            "sleeves": {
                "analog": {
                    "low": 3, "high": 5, "rule": "accumulate",
                    "members": ["TXN"],
                },
            },
        })
        staged = ts.load_staged(create=True)
        self.assertEqual(staged["provenance"]["TXN"]["home_segment"], "analog")

    def test_conflict_skips_without_allow_rehome(self):
        self._seed_live({
            "as_of": "2026-01-01", "cash_target_pct": 0,
            "targets": {},
            "sleeves": {
                "analog": {
                    "low": 3, "high": 5, "rule": "accumulate",
                    "members": ["TXN"],
                },
                "semis-etf": {
                    "low": 2, "high": 4, "rule": "accumulate",
                    "members": ["SOXX"],
                },
            },
        })
        # Staging a standalone target that claims a different home must skip.
        res = ts.stage_changes([{
            "action": "add_target", "symbol": "TXN",
            "home_segment": "semis-etf",
            "proposed_target": {"low": 1, "high": 2, "rule": "hold"},
        }])
        self.assertEqual(res["applied"], [])
        self.assertTrue(any("home_segment conflict" in (s.get("reason") or "")
                            for s in res["skipped"]))

    def test_research_lineage_preserves_home(self):
        self._seed_live({
            "as_of": "2026-01-01", "cash_target_pct": 0,
            "targets": {"NVDA": {"low": 8, "high": 10, "rule": "accumulate"}},
            "sleeves": {
                "analog": {
                    "low": 3, "high": 5, "rule": "accumulate", "members": [],
                },
            },
            "provenance": {"NVDA": {
                "source": "legacy-plan", "home_segment": "analog",
            }},
        })
        ts.stage_changes([{
            "action": "modify_target", "symbol": "NVDA",
            "proposed_target": {"low": 7, "high": 9, "rule": "accumulate"},
        }], segment="ai-topic", source="pipeline")
        staged = _load(self.staged)
        self.assertEqual(staged["provenance"]["NVDA"]["segment"], "ai-topic")
        self.assertEqual(staged["provenance"]["NVDA"]["home_segment"], "analog")


if __name__ == "__main__":
    unittest.main()
