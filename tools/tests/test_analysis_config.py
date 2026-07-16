"""Tests for analysis_config: provider normalization and the on-disk config
round-trip. The config test sandboxes CONFIG_PATH to a temp dir so a real
analysis-config.json is never touched."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import _support  # noqa: F401
import analysis_config as ac


class NormalizeProviders(unittest.TestCase):
    def test_drops_unknown_dedups_and_cleans_extra_args(self):
        out = ac._normalize_providers([
            {"id": "claude", "enabled": True, "model": "opus", "extra_args": ["--x", "", "  "]},
            {"id": "claude", "enabled": False},  # duplicate id -> ignored
            {"id": "bogus"},                      # unknown id -> dropped
            "not-a-dict",                          # skipped
        ])
        self.assertEqual([p["id"] for p in out], ["claude"])
        self.assertEqual(out[0]["extra_args"], ["--x"])  # blank args dropped
        self.assertTrue(out[0]["enabled"])

    def test_non_list_returns_none(self):
        self.assertIsNone(ac._normalize_providers({"id": "claude"}))
        self.assertIsNone(ac._normalize_providers(None))

    def test_empty_after_filtering_returns_none(self):
        self.assertIsNone(ac._normalize_providers([{"id": "bogus"}]))

    def test_strip_model_only_when_requested(self):
        self.assertEqual(ac._normalize_providers([{"id": "cursor", "model": "  gpt  "}], strip_model=True)[0]["model"], "gpt")
        self.assertEqual(ac._normalize_providers([{"id": "cursor", "model": "  gpt  "}])[0]["model"], "  gpt  ")


class Config(unittest.TestCase):
    def setUp(self):
        self._orig = ac.CONFIG_PATH
        self._tmp = Path(tempfile.mkdtemp()) / "analysis-config.json"
        ac.CONFIG_PATH = self._tmp

    def tearDown(self):
        ac.CONFIG_PATH = self._orig

    def test_save_normalizes_and_drops_unknown_providers(self):
        saved = ac.save_config({
            "providers": [
                {"id": "claude", "enabled": True, "model": "opus", "extra_args": []},
                {"id": "bogus", "enabled": True},  # unknown id -> dropped
            ],
            "timeout_sec": 120,
            "allow_web": True,
        })
        self.assertEqual([p["id"] for p in saved["providers"]], ["claude"])
        self.assertEqual(saved["timeout_sec"], 120)
        self.assertTrue(saved["allow_web"])

    def test_round_trip_through_disk(self):
        ac.save_config({"timeout_sec": 200, "allow_web": False,
                        "providers": [{"id": "cursor", "enabled": False, "model": "", "extra_args": []}]})
        loaded = ac.load_config()
        self.assertEqual(loaded["timeout_sec"], 200)
        self.assertFalse(loaded["allow_web"])
        self.assertEqual(loaded["providers"][0]["id"], "cursor")

    def test_missing_file_falls_back_to_defaults(self):
        loaded = ac.load_config()  # tmp path does not exist yet
        self.assertEqual(loaded["timeout_sec"], ac.DEFAULT_CONFIG["timeout_sec"])


if __name__ == "__main__":
    unittest.main()
