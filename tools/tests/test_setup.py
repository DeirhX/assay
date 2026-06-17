"""Tests for the first-time setup surface.

Covers the backend-detection / smoke-check plumbing in ticker_analysis that the
/api/setup endpoints expose, plus serve._setup_status's composed envelope. Real
CLIs are never invoked: backend presence is faked via the REBAL_*_CLI env
overrides (and shutil.which), and the smoke subprocess is stubbed."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
import json
from pathlib import Path
from unittest import mock

import _support  # noqa: F401
import ticker_analysis as ta


def _no_backends_env():
    """Context where neither CLI resolves: overrides removed, PATH lookup empty."""
    ctx_env = mock.patch.dict(ta.os.environ, {}, clear=False)
    ctx_which = mock.patch.object(ta.shutil, "which", return_value=None)
    return ctx_env, ctx_which


class NormalizeProviders(unittest.TestCase):
    def test_drops_unknown_dedups_and_cleans_extra_args(self):
        out = ta._normalize_providers([
            {"id": "claude", "enabled": True, "model": "opus", "extra_args": ["--x", "", "  "]},
            {"id": "claude", "enabled": False},  # duplicate id -> ignored
            {"id": "bogus"},                      # unknown id -> dropped
            "not-a-dict",                          # skipped
        ])
        self.assertEqual([p["id"] for p in out], ["claude"])
        self.assertEqual(out[0]["extra_args"], ["--x"])  # blank args dropped
        self.assertTrue(out[0]["enabled"])

    def test_non_list_returns_none(self):
        self.assertIsNone(ta._normalize_providers({"id": "claude"}))
        self.assertIsNone(ta._normalize_providers(None))

    def test_empty_after_filtering_returns_none(self):
        self.assertIsNone(ta._normalize_providers([{"id": "bogus"}]))

    def test_strip_model_only_when_requested(self):
        self.assertEqual(ta._normalize_providers([{"id": "cursor", "model": "  gpt  "}], strip_model=True)[0]["model"], "gpt")
        self.assertEqual(ta._normalize_providers([{"id": "cursor", "model": "  gpt  "}])[0]["model"], "  gpt  ")


class AvailableBackends(unittest.TestCase):
    def test_env_overrides_mark_installed(self):
        with mock.patch.dict(ta.os.environ, {"REBAL_CLAUDE_CLI": "/x/claude",
                                             "REBAL_CURSOR_CLI": "/x/cursor-agent"}, clear=False):
            self.assertEqual(ta.available_backends(), {"claude": True, "cursor": True})

    def test_missing_when_unresolved(self):
        ctx_env, ctx_which = _no_backends_env()
        with ctx_env:
            ta.os.environ.pop("REBAL_CLAUDE_CLI", None)
            ta.os.environ.pop("REBAL_CURSOR_CLI", None)
            with ctx_which:
                self.assertEqual(ta.available_backends(), {"claude": False, "cursor": False})


class ConfiguredProvider(unittest.TestCase):
    def test_returns_match_from_config(self):
        cfg = {"providers": [{"id": "cursor", "enabled": False, "model": "m", "extra_args": ["--a"]}]}
        p = ta._configured_provider("cursor", cfg)
        self.assertFalse(p["enabled"])
        self.assertEqual(p["model"], "m")

    def test_falls_back_to_default(self):
        p = ta._configured_provider("claude", {"providers": []})
        self.assertEqual(p["id"], "claude")
        self.assertTrue(p["enabled"])  # default claude is enabled

    def test_unknown_pid_yields_disabled_stub(self):
        self.assertEqual(ta._configured_provider("ghost", {"providers": []}),
                         {"id": "ghost", "enabled": False, "model": "", "extra_args": []})


class LastLine(unittest.TestCase):
    def test_returns_last_nonblank(self):
        self.assertEqual(ta._last_line("a\n  b  \n\n"), "b")

    def test_blank_or_none_is_empty(self):
        self.assertEqual(ta._last_line("\n  \n"), "")
        self.assertEqual(ta._last_line(None), "")


class SmokeArgv(unittest.TestCase):
    def test_claude_argv_feeds_prompt_via_stdin(self):
        with mock.patch.dict(ta.os.environ, {"REBAL_CLAUDE_CLI": "/x/claude"}, clear=False):
            argv, stdin = ta._smoke_argv("claude", {"model": "opus", "extra_args": ["--foo"]})
        self.assertEqual(argv[0], "/x/claude")
        self.assertIn("--tools", argv)
        self.assertIn("--model", argv)
        self.assertIn("opus", argv)
        self.assertIn("--foo", argv)
        self.assertEqual(stdin, ta._SMOKE_PROMPT)

    def test_cursor_argv_passes_prompt_as_argument(self):
        with mock.patch.dict(ta.os.environ, {"REBAL_CURSOR_CLI": "/x/cursor-agent"}, clear=False):
            argv, stdin = ta._smoke_argv("cursor", {"model": "", "extra_args": []})
        self.assertEqual(argv[0], "/x/cursor-agent")
        self.assertIn(ta._SMOKE_PROMPT, argv)  # cursor reads prompt from argv, not stdin
        self.assertIn("ask", argv)
        self.assertIsNone(stdin)

    def test_missing_backends_return_error_envelopes(self):
        ctx_env, ctx_which = _no_backends_env()
        with ctx_env:
            ta.os.environ.pop("REBAL_CLAUDE_CLI", None)
            ta.os.environ.pop("REBAL_CURSOR_CLI", None)
            with ctx_which:
                claude = ta._smoke_argv("claude", {})
                cursor = ta._smoke_argv("cursor", {})
        self.assertEqual(claude["status"], "missing")
        self.assertEqual(cursor["status"], "missing")

    def test_unknown_backend_is_unsupported(self):
        self.assertEqual(ta._smoke_argv("ghost", {})["status"], "unsupported")


class SmokeCheckBackend(unittest.TestCase):
    @staticmethod
    def _proc(rc, out="", err=""):
        return subprocess.CompletedProcess(args=[], returncode=rc, stdout=out, stderr=err)

    def test_missing_short_circuits_without_running(self):
        ctx_env, ctx_which = _no_backends_env()
        with ctx_env:
            ta.os.environ.pop("REBAL_CLAUDE_CLI", None)
            with ctx_which, mock.patch.object(ta.subprocess, "run",
                                              side_effect=AssertionError("must not shell out")):
                out = ta._smoke_check_backend("claude", {})
        self.assertEqual(out["status"], "missing")

    def test_success_reports_ok_with_last_line(self):
        with mock.patch.dict(ta.os.environ, {"REBAL_CLAUDE_CLI": "/x/claude"}, clear=False):
            with mock.patch.object(ta.subprocess, "run", return_value=self._proc(0, out="noise\nOK")):
                out = ta._smoke_check_backend("claude", {})
        self.assertTrue(out["ok"])
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["message"], "OK")

    def test_quota_phrase_flagged_as_quota(self):
        with mock.patch.dict(ta.os.environ, {"REBAL_CLAUDE_CLI": "/x/claude"}, clear=False):
            with mock.patch.object(ta.subprocess, "run",
                                   return_value=self._proc(1, err="Error: usage limit reached")):
                out = ta._smoke_check_backend("claude", {})
        self.assertFalse(out["ok"])
        self.assertEqual(out["status"], "quota")

    def test_auth_phrase_flagged_as_auth(self):
        with mock.patch.dict(ta.os.environ, {"REBAL_CLAUDE_CLI": "/x/claude"}, clear=False):
            with mock.patch.object(ta.subprocess, "run",
                                   return_value=self._proc(1, err="Error: not logged in")):
                out = ta._smoke_check_backend("claude", {})
        self.assertFalse(out["ok"])
        self.assertEqual(out["status"], "auth")

    def test_generic_failure_is_error(self):
        with mock.patch.dict(ta.os.environ, {"REBAL_CLAUDE_CLI": "/x/claude"}, clear=False):
            with mock.patch.object(ta.subprocess, "run", return_value=self._proc(1, err="boom")):
                out = ta._smoke_check_backend("claude", {})
        self.assertEqual(out["status"], "error")

    def test_timeout_reported(self):
        with mock.patch.dict(ta.os.environ, {"REBAL_CLAUDE_CLI": "/x/claude"}, clear=False):
            with mock.patch.object(ta.subprocess, "run",
                                   side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=45)):
                out = ta._smoke_check_backend("claude", {}, timeout=45)
        self.assertEqual(out["status"], "timeout")


class SetupStatus(unittest.TestCase):
    def setUp(self):
        self._orig = ta.CONFIG_PATH
        ta.CONFIG_PATH = Path(tempfile.mkdtemp()) / "analysis-config.json"
        # The auth-probe cache is process-global; clear it so a probe result
        # mocked in one test can't leak into the next via the TTL cache.
        ta._clear_auth_probe_cache()

    def tearDown(self):
        ta.CONFIG_PATH = self._orig

    def test_shape_without_checks(self):
        # Probe inconclusive -> installed but credential state unknown.
        with mock.patch.object(ta, "_auth_probe", return_value=None):
            with mock.patch.dict(ta.os.environ, {"REBAL_CLAUDE_CLI": "/x/claude",
                                                 "REBAL_CURSOR_CLI": "/x/cursor-agent"}, clear=False):
                st = ta.setup_status()
        self.assertFalse(st["config_exists"])  # tmp file not written yet
        self.assertEqual({b["id"] for b in st["backends"]}, {"claude", "cursor"})
        for b in st["backends"]:
            self.assertTrue(b["installed"])
            self.assertEqual(b["status"], "installed")
            self.assertIsNone(b["authenticated"])
            self.assertNotIn("check", b)

    def test_auth_probe_distinguishes_logged_out_from_ready(self):
        with mock.patch.object(ta, "_auth_probe", side_effect=lambda pid, **k: pid == "claude"):
            with mock.patch.dict(ta.os.environ, {"REBAL_CLAUDE_CLI": "/x/claude",
                                                 "REBAL_CURSOR_CLI": "/x/cursor-agent"}, clear=False):
                st = ta.setup_status()
        by = {b["id"]: b for b in st["backends"]}
        self.assertTrue(by["claude"]["authenticated"])
        self.assertEqual(by["claude"]["status"], "ready")
        self.assertFalse(by["cursor"]["authenticated"])
        self.assertEqual(by["cursor"]["status"], "logged_out")

    def test_missing_backends_report_missing(self):
        ctx_env, ctx_which = _no_backends_env()
        with ctx_env:
            ta.os.environ.pop("REBAL_CLAUDE_CLI", None)
            ta.os.environ.pop("REBAL_CURSOR_CLI", None)
            with ctx_which:
                st = ta.setup_status()
        for b in st["backends"]:
            self.assertFalse(b["installed"])
            self.assertEqual(b["status"], "missing")

    def test_run_checks_attaches_check_and_propagates_status(self):
        fake = {"ok": True, "status": "ok", "message": "OK"}
        with mock.patch.object(ta, "_smoke_check_backend", return_value=fake):
            with mock.patch.dict(ta.os.environ, {"REBAL_CLAUDE_CLI": "/x/claude",
                                                 "REBAL_CURSOR_CLI": "/x/cursor-agent"}, clear=False):
                st = ta.setup_status(run_checks=True)
        for b in st["backends"]:
            self.assertEqual(b["check"], fake)
            self.assertEqual(b["status"], "ok")

    def test_config_exists_flips_after_save(self):
        ta.save_config({"providers": [], "timeout_sec": 100, "allow_web": False})
        self.assertTrue(ta.setup_status()["config_exists"])


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
