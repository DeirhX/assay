"""Tests for the first-time setup surface.

Covers serve._setup_status's composed envelope (the llm / perplexity /
environment / data blocks the /api/setup/status endpoint returns). The
backend-detection and smoke-check plumbing it wraps is tested directly against
analysis_backends in test_analysis_backends.py."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _support  # noqa: F401


class ServeSetupStatus(unittest.TestCase):
    """serve._setup_status composes the llm/perplexity/environment envelope the
    /api/setup/status endpoint returns. Importing serve is safe -- the server
    only starts under __main__."""

    def setUp(self):
        import serve
        self.serve = serve

    def test_environment_bools_track_env_vars(self):
        with mock.patch.dict(self.serve.os.environ,
                             {"SEC_USER_AGENT": "assay (me@example.com)", "FMP_API_KEY": "k"}, clear=False):
            st = self.serve._setup_status()
        self.assertTrue(st["environment"]["sec_user_agent"])
        self.assertTrue(st["environment"]["fmp_api_key"])
        self.assertIn("llm", st)
        self.assertIn("perplexity", st)

    def test_environment_missing_when_unset(self):
        with mock.patch.dict(self.serve.os.environ, {}, clear=False):
            self.serve.os.environ.pop("SEC_USER_AGENT", None)
            self.serve.os.environ.pop("FMP_API_KEY", None)
            st = self.serve._setup_status()
        self.assertFalse(st["environment"]["sec_user_agent"])
        self.assertFalse(st["environment"]["fmp_api_key"])
        self.assertTrue(st["environment"]["pplx_profile_dir"])  # always a path string
        self.assertIn("data", st)

    def test_data_status_reports_empty_when_private_files_missing(self):
        root = Path(tempfile.mkdtemp())
        with mock.patch.object(self.serve, "HOLDINGS_JSON", root / "missing-holdings.json"), \
             mock.patch.object(self.serve, "TARGET_MODEL_JSON", root / "missing-target.json"):
            st = self.serve._data_status()
        self.assertFalse(st["ready"])
        self.assertTrue(st["empty"])
        self.assertFalse(st["holdings"]["exists"])
        self.assertFalse(st["target_model"]["exists"])

    def test_data_status_ready_with_holdings_and_target_model(self):
        root = Path(tempfile.mkdtemp())
        holdings = root / "current-holdings.json"
        target = root / "target-model.json"
        holdings.write_text(json.dumps({"positions": [{"symbol": "AMD"}]}), encoding="utf-8")
        target.write_text(json.dumps({"targets": {"AMD": {"target": 1.0}}}), encoding="utf-8")
        with mock.patch.object(self.serve, "HOLDINGS_JSON", holdings), \
             mock.patch.object(self.serve, "TARGET_MODEL_JSON", target):
            st = self.serve._data_status()
        self.assertTrue(st["ready"])
        self.assertFalse(st["empty"])
        self.assertEqual(st["holdings"]["positions"], 1)


if __name__ == "__main__":
    unittest.main()
