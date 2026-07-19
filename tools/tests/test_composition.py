"""Whole-book allocation-segment composition propose / normalize / stage."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import _support  # noqa: F401
import composition as comp
import target_model
import target_staging as ts
from store import load as _load, write_json as _write_json


class Normalize(unittest.TestCase):
    def test_scales_down_when_over_budget(self):
        out = comp.normalize_targets(
            {"a": 60, "b": 60}, cash_target=10.0)
        self.assertAlmostEqual(sum(out.values()) + 10.0, 100.0, places=1)
        self.assertTrue(all(v <= 60 for v in out.values()))

    def test_drops_negatives(self):
        out = comp.normalize_targets({"a": 10, "b": -5}, cash_target=0)
        self.assertNotIn("b", out)
        self.assertEqual(out["a"], 10.0)


class SnapshotAndPropose(unittest.TestCase):
    def test_snapshot_lists_sleeves(self):
        model = {
            "cash_target_pct": 5,
            "sleeves": {
                "analog": {
                    "low": 3, "high": 5, "rule": "accumulate",
                    "members": ["TXN", "ADI"],
                },
            },
            "targets": {"NVDA": {"low": 8, "high": 10, "rule": "accumulate"}},
        }
        holdings = {"positions": [
            {"symbol": "TXN", "base_market_value": 4.0},
            {"symbol": "ADI", "base_market_value": 2.0},
            {"symbol": "NVDA", "base_market_value": 10.0},
            {"symbol": "REST", "base_market_value": 84.0},
        ], "net_asset_value": 105.0}
        snap = comp.snapshot(model, holdings)
        self.assertEqual(len(snap["segments"]), 1)
        self.assertEqual(snap["segments"][0]["name"], "analog")
        self.assertAlmostEqual(snap["segments"][0]["current_pct"], 6.0, places=1)
        self.assertEqual(snap["unassigned"][0]["symbol"], "NVDA")

    def test_heuristic_propose_without_llm(self):
        model = {
            "cash_target_pct": 5,
            "sleeves": {
                "analog": {
                    "low": 3, "high": 5, "rule": "accumulate",
                    "members": ["TXN"],
                },
            },
            "targets": {},
        }
        holdings = {"positions": [
            {"symbol": "TXN", "base_market_value": 4.0},
            {"symbol": "REST", "base_market_value": 96.0},
        ]}
        prop = comp.propose("", use_llm=False, model=model, holdings=holdings)
        self.assertEqual(prop["source"], "heuristic")
        self.assertIn("analog", prop["targets"])


class StageRatios(unittest.TestCase):
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
            (comp, "TARGET_MODEL_JSON"): comp.TARGET_MODEL_JSON,
            (comp, "HOLDINGS_JSON"): comp.HOLDINGS_JSON,
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
        comp.TARGET_MODEL_JSON = self.live
        comp.HOLDINGS_JSON = self.holdings
        _write_json(self.live, {
            "as_of": "2026-01-01",
            "cash_target_pct": 5.0,
            "targets": {},
            "sleeves": {
                "analog": {
                    "low": 3, "high": 5, "rule": "accumulate",
                    "members": ["TXN", "ADI"],
                },
            },
        })
        _write_json(self.holdings, {"positions": []})

    def tearDown(self):
        for (mod, name), val in self._orig.items():
            setattr(mod, name, val)
        self.tmp.cleanup()

    def test_stages_sleeve_bands(self):
        result = comp.stage_ratios({"analog": 8.0}, cash_target_pct=6.0)
        self.assertIn("[analog]", result["applied"])
        staged = _load(self.staged)
        sl = staged["sleeves"]["analog"]
        self.assertEqual(sl["low"], 6.5)
        self.assertEqual(sl["high"], 9.5)
        self.assertEqual(staged["cash_target_pct"], 6.0)
        self.assertEqual(sl["members"], ["TXN", "ADI"])


if __name__ == "__main__":
    unittest.main()
