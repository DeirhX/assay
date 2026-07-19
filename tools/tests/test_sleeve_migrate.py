"""Fold standalone targets into allocation sleeves."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import _support  # noqa: F401
import sleeve_migrate as sm
import target_model
import target_staging as ts
from store import load as _load, write_json as _write_json


class PlanMigration(unittest.TestCase):
    def test_groups_by_tag_home_etf_and_other(self):
        model = {
            "targets": {
                "ENTG": {"low": 2, "high": 3, "rule": "accumulate", "sleeve": "semis-materials"},
                "NVDA": {"low": 4, "high": 5, "rule": "hold", "sleeve": "other"},
                "SMH": {"low": 0, "high": 0, "rule": "avoid"},
                "MU": {"low": 1, "high": 2, "rule": "wait"},
            },
            "sleeves": {
                "semis-etf": {
                    "low": 5, "high": 10, "rule": "accumulate", "members": ["XSD"],
                },
            },
            "provenance": {},
        }
        plan = sm.plan_migration(model)
        by = {a["symbol"]: a for a in plan["assignments"]}
        self.assertEqual(by["ENTG"]["sleeve"], "semis-materials")
        self.assertEqual(by["NVDA"]["sleeve"], "other")
        self.assertEqual(by["SMH"]["sleeve"], "semis-etf")
        self.assertEqual(by["MU"]["sleeve"], "other")
        etf = next(s for s in plan["sleeves"] if s["name"] == "semis-etf")
        self.assertIn("XSD", etf["members_after"])
        self.assertIn("SMH", etf["members_after"])
        # Existing band preserved as baseline + newcomer.
        self.assertGreaterEqual(etf["high"], 10.0)

    def test_skips_existing_sleeve_members(self):
        model = {
            "targets": {"TXN": {"low": 1, "high": 2, "rule": "hold"}},
            "sleeves": {
                "analog": {"low": 3, "high": 5, "rule": "accumulate", "members": ["TXN"]},
            },
        }
        plan = sm.plan_migration(model)
        self.assertEqual(plan["counts"]["standalone"], 0)


class StageMigration(unittest.TestCase):
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
            (sm, "TARGET_MODEL_JSON"): sm.TARGET_MODEL_JSON,
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
        sm.TARGET_MODEL_JSON = self.live
        _write_json(self.holdings, {"positions": []})
        _write_json(self.live, {
            "as_of": "2026-01-01",
            "cash_target_pct": 5.0,
            "targets": {
                "NVDA": {"low": 4, "high": 5.4, "rule": "hold", "sleeve": "other"},
                "ENTG": {"low": 2, "high": 3, "rule": "accumulate", "sleeve": "materials"},
            },
            "sleeves": {
                "analog": {
                    "low": 5, "high": 6, "rule": "accumulate",
                    "members": ["TXN", "ADI"],
                },
            },
            "provenance": {},
        })

    def tearDown(self):
        for (mod, name), val in self._orig.items():
            setattr(mod, name, val)
        self.tmp.cleanup()

    def test_stages_sleeves_and_keeps_home_provenance(self):
        res = sm.stage_migration()
        self.assertTrue(res["staged"])
        staged = _load(self.staged)
        self.assertNotIn("NVDA", staged["targets"])
        self.assertNotIn("ENTG", staged["targets"])
        self.assertIn("other", staged["sleeves"])
        self.assertIn("NVDA", staged["sleeves"]["other"]["members"])
        self.assertIn("semis-materials", staged["sleeves"])
        self.assertIn("ENTG", staged["sleeves"]["semis-materials"]["members"])
        self.assertEqual(staged["provenance"]["NVDA"]["home_segment"], "other")
        self.assertEqual(staged["provenance"]["ENTG"]["home_segment"], "semis-materials")
        # Existing sleeve untouched.
        self.assertEqual(staged["sleeves"]["analog"]["members"], ["TXN", "ADI"])


if __name__ == "__main__":
    unittest.main()
