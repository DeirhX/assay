"""Tests for the extended target-proposal apply path in serve.py: the new
modify_target merge (which used to be skipped as "unsupported action"), the
add_target/blocked/sleeve branches of the shared model mutator, and the
end-to-end _apply_target_proposal (backup + status + skip reasons). Offline;
serve's module paths are pointed at a temp dir and site regeneration is stubbed."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _support  # noqa: F401
import serve


class ApplyChangesToModel(unittest.TestCase):
    def test_modify_target_merges_and_preserves_untouched_keys(self):
        model = {"targets": {"AAA": {"low": 1, "high": 3, "rule": "hold", "note": "keep me"}}}
        changes = [{"action": "modify_target", "symbol": "AAA",
                    "proposed_target": {"low": 5, "high": 8, "rule": "accumulate"}}]
        applied, skipped = serve._apply_changes_to_model(model, changes, blocked=set())
        self.assertEqual(applied, ["AAA"])
        self.assertEqual(skipped, [])
        # New band/rule applied; the pre-existing note survives the merge.
        self.assertEqual(model["targets"]["AAA"],
                         {"low": 5, "high": 8, "rule": "accumulate", "note": "keep me"})

    def test_modify_target_strips_non_schema_keys(self):
        model = {"targets": {"AAA": {"low": 1, "high": 3, "rule": "hold"}}}
        changes = [{"action": "modify_target", "symbol": "AAA",
                    "proposed_target": {"low": 4, "high": 6, "rule": "hold",
                                        "conviction": "high", "sleeve": "core"}}]
        serve._apply_changes_to_model(model, changes, blocked=set())
        self.assertNotIn("conviction", model["targets"]["AAA"])
        self.assertNotIn("sleeve", model["targets"]["AAA"])

    def test_add_target_new_and_existing(self):
        model = {"targets": {"AAA": {"low": 1, "high": 3, "rule": "hold"}}}
        changes = [
            {"action": "add_target", "symbol": "BBB", "proposed_target": {"low": 2, "high": 4, "rule": "hold"}},
            {"action": "add_target", "symbol": "AAA", "proposed_target": {"low": 9, "high": 9, "rule": "hold"}},
        ]
        applied, skipped = serve._apply_changes_to_model(model, changes, blocked=set())
        self.assertEqual(applied, ["BBB"])
        self.assertEqual(skipped[0]["symbol"], "AAA")
        self.assertIn("already exists", skipped[0]["reason"])

    def test_blocked_symbol_is_skipped(self):
        model = {"targets": {}}
        changes = [{"action": "add_target", "symbol": "AAA",
                    "proposed_target": {"low": 1, "high": 2, "rule": "hold"}}]
        applied, skipped = serve._apply_changes_to_model(model, changes, blocked={"AAA"})
        self.assertEqual(applied, [])
        self.assertIn("blocked", skipped[0]["reason"])

    def test_sleeve_upsert(self):
        model = {"sleeves": {}}
        changes = [{"action": "set_sleeve", "sleeve": "fintech",
                    "proposed_sleeve": {"low": 10, "high": 20, "rule": "accumulate"}}]
        applied, _ = serve._apply_changes_to_model(model, changes, blocked=set())
        self.assertEqual(applied, ["[fintech]"])
        self.assertEqual(model["sleeves"]["fintech"]["high"], 20)

    def test_unknown_action_is_recorded_not_dropped(self):
        model = {"targets": {}}
        changes = [{"action": "delete_everything", "symbol": "AAA"}]
        applied, skipped = serve._apply_changes_to_model(model, changes, blocked=set())
        self.assertEqual(applied, [])
        self.assertIn("unsupported action", skipped[0]["reason"])


class ApplyTargetProposal(unittest.TestCase):
    SEG = "test-seg"
    DATE = "2026-06-01"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self._orig = {k: getattr(serve, k) for k in (
            "REPO_ROOT", "DATA_DIR", "DEEP_DIR", "TARGET_MODEL_JSON",
            "HOLDINGS_JSON", "TARGET_MODEL_BACKUP_DIR",
        )}
        serve.REPO_ROOT = root
        serve.DATA_DIR = root / "data"
        serve.DEEP_DIR = serve.DATA_DIR / "research" / "deep"
        serve.TARGET_MODEL_JSON = serve.DATA_DIR / "target-model.json"
        serve.HOLDINGS_JSON = serve.DATA_DIR / "current-holdings.json"
        serve.TARGET_MODEL_BACKUP_DIR = serve.DATA_DIR / "backups"
        serve.DEEP_DIR.mkdir(parents=True)
        # Don't render the real static site against the temp/empty data dir.
        self._regen = mock.patch.object(serve, "_regenerate_site",
                                        return_value={"ok": True, "written": []})
        self._regen.start()

    def tearDown(self):
        self._regen.stop()
        for k, v in self._orig.items():
            setattr(serve, k, v)
        self.tmp.cleanup()

    def _write(self, path: Path, payload):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_apply_modify_target_end_to_end(self):
        self._write(serve.TARGET_MODEL_JSON,
                    {"targets": {"AAA": {"low": 1, "high": 3, "rule": "hold"}}})
        self._write(serve.DEEP_DIR / f"{self.SEG}-{self.DATE}.target-proposal.json", {
            "schema_version": 2, "segment": self.SEG, "date": self.DATE, "status": "draft",
            "blocked_symbols": [],
            "changes": [
                {"action": "modify_target", "symbol": "AAA",
                 "proposed_target": {"low": 5, "high": 8, "rule": "accumulate"}},
                {"action": "add_target", "symbol": "BBB",
                 "proposed_target": {"low": 2, "high": 4, "rule": "hold"}},
            ],
        })
        result = serve._apply_target_proposal(self.SEG, self.DATE, True)
        self.assertEqual(sorted(result["applied"]), ["AAA", "BBB"])
        self.assertTrue(result["backup"])  # a backup path was returned

        model = json.loads(serve.TARGET_MODEL_JSON.read_text(encoding="utf-8"))
        self.assertEqual(model["targets"]["AAA"]["rule"], "accumulate")
        self.assertEqual(model["targets"]["AAA"]["high"], 8)
        self.assertIn("BBB", model["targets"])
        self.assertEqual(result["proposal"]["status"], "applied")
        # The backup file actually exists on disk.
        self.assertTrue(list(serve.TARGET_MODEL_BACKUP_DIR.glob("target-model-*.json")))

    def test_requires_confirm(self):
        with self.assertRaises(ValueError):
            serve._apply_target_proposal(self.SEG, self.DATE, False)


if __name__ == "__main__":
    unittest.main()
