"""Tests for analysis_backends: the LLM-CLI detection / smoke-check / credential
probe plumbing and the setup_status envelope. Real CLIs are never invoked:
backend presence is faked via the REBAL_*_CLI env overrides (and shutil.which),
and the smoke subprocess is stubbed. setup_status sandboxes analysis_config's
CONFIG_PATH to a temp dir so a real analysis-config.json is never touched."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _support  # noqa: F401
import analysis_backends as ab
import analysis_config as ac


def _no_backends_env():
    """Context where neither CLI resolves: overrides removed, PATH lookup empty."""
    ctx_env = mock.patch.dict(ab.os.environ, {}, clear=False)
    ctx_which = mock.patch.object(ab.shutil, "which", return_value=None)
    return ctx_env, ctx_which


class AvailableBackends(unittest.TestCase):
    def test_env_overrides_mark_installed(self):
        with mock.patch.dict(ab.os.environ, {"REBAL_CLAUDE_CLI": "/x/claude",
                                             "REBAL_CURSOR_CLI": "/x/cursor-agent"}, clear=False):
            self.assertEqual(ab.available_backends(), {"claude": True, "cursor": True})

    def test_missing_when_unresolved(self):
        ctx_env, ctx_which = _no_backends_env()
        with ctx_env:
            ab.os.environ.pop("REBAL_CLAUDE_CLI", None)
            ab.os.environ.pop("REBAL_CURSOR_CLI", None)
            with ctx_which:
                self.assertEqual(ab.available_backends(), {"claude": False, "cursor": False})


class ConfiguredProvider(unittest.TestCase):
    def test_returns_match_from_config(self):
        cfg = {"providers": [{"id": "cursor", "enabled": False, "model": "m", "extra_args": ["--a"]}]}
        p = ab._configured_provider("cursor", cfg)
        self.assertFalse(p["enabled"])
        self.assertEqual(p["model"], "m")

    def test_falls_back_to_default(self):
        p = ab._configured_provider("claude", {"providers": []})
        self.assertEqual(p["id"], "claude")
        self.assertTrue(p["enabled"])  # default claude is enabled

    def test_unknown_pid_yields_disabled_stub(self):
        self.assertEqual(ab._configured_provider("ghost", {"providers": []}),
                         {"id": "ghost", "enabled": False, "model": "", "extra_args": []})


class LastLine(unittest.TestCase):
    def test_returns_last_nonblank(self):
        self.assertEqual(ab._last_line("a\n  b  \n\n"), "b")

    def test_blank_or_none_is_empty(self):
        self.assertEqual(ab._last_line("\n  \n"), "")
        self.assertEqual(ab._last_line(None), "")


class SmokeArgv(unittest.TestCase):
    def test_claude_argv_feeds_prompt_via_stdin(self):
        with mock.patch.dict(ab.os.environ, {"REBAL_CLAUDE_CLI": "/x/claude"}, clear=False):
            argv, stdin = ab._smoke_argv("claude", {"model": "opus", "extra_args": ["--foo"]})
        self.assertEqual(argv[0], "/x/claude")
        self.assertIn("--tools", argv)
        self.assertIn("--model", argv)
        self.assertIn("opus", argv)
        self.assertIn("--foo", argv)
        self.assertEqual(stdin, ab._SMOKE_PROMPT)

    def test_cursor_argv_passes_prompt_as_argument(self):
        with mock.patch.dict(ab.os.environ, {"REBAL_CURSOR_CLI": "/x/cursor-agent"}, clear=False):
            argv, stdin = ab._smoke_argv("cursor", {"model": "", "extra_args": []})
        self.assertEqual(argv[0], "/x/cursor-agent")
        self.assertIn(ab._SMOKE_PROMPT, argv)  # cursor reads prompt from argv, not stdin
        self.assertIn("ask", argv)
        self.assertIsNone(stdin)

    def test_missing_backends_return_error_envelopes(self):
        ctx_env, ctx_which = _no_backends_env()
        with ctx_env:
            ab.os.environ.pop("REBAL_CLAUDE_CLI", None)
            ab.os.environ.pop("REBAL_CURSOR_CLI", None)
            with ctx_which:
                claude = ab._smoke_argv("claude", {})
                cursor = ab._smoke_argv("cursor", {})
        self.assertEqual(claude["status"], "missing")
        self.assertEqual(cursor["status"], "missing")

    def test_unknown_backend_is_unsupported(self):
        self.assertEqual(ab._smoke_argv("ghost", {})["status"], "unsupported")


class SmokeCheckBackend(unittest.TestCase):
    @staticmethod
    def _proc(rc, out="", err=""):
        return subprocess.CompletedProcess(args=[], returncode=rc, stdout=out, stderr=err)

    def test_missing_short_circuits_without_running(self):
        ctx_env, ctx_which = _no_backends_env()
        with ctx_env:
            ab.os.environ.pop("REBAL_CLAUDE_CLI", None)
            with ctx_which, mock.patch.object(ab.subprocess, "run",
                                              side_effect=AssertionError("must not shell out")):
                out = ab._smoke_check_backend("claude", {})
        self.assertEqual(out["status"], "missing")

    def test_success_reports_ok_with_last_line(self):
        with mock.patch.dict(ab.os.environ, {"REBAL_CLAUDE_CLI": "/x/claude"}, clear=False):
            with mock.patch.object(ab.subprocess, "run", return_value=self._proc(0, out="noise\nOK")):
                out = ab._smoke_check_backend("claude", {})
        self.assertTrue(out["ok"])
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["message"], "OK")

    def test_quota_phrase_flagged_as_quota(self):
        with mock.patch.dict(ab.os.environ, {"REBAL_CLAUDE_CLI": "/x/claude"}, clear=False):
            with mock.patch.object(ab.subprocess, "run",
                                   return_value=self._proc(1, err="Error: usage limit reached")):
                out = ab._smoke_check_backend("claude", {})
        self.assertFalse(out["ok"])
        self.assertEqual(out["status"], "quota")

    def test_auth_phrase_flagged_as_auth(self):
        with mock.patch.dict(ab.os.environ, {"REBAL_CLAUDE_CLI": "/x/claude"}, clear=False):
            with mock.patch.object(ab.subprocess, "run",
                                   return_value=self._proc(1, err="Error: not logged in")):
                out = ab._smoke_check_backend("claude", {})
        self.assertFalse(out["ok"])
        self.assertEqual(out["status"], "auth")

    def test_generic_failure_is_error(self):
        with mock.patch.dict(ab.os.environ, {"REBAL_CLAUDE_CLI": "/x/claude"}, clear=False):
            with mock.patch.object(ab.subprocess, "run", return_value=self._proc(1, err="boom")):
                out = ab._smoke_check_backend("claude", {})
        self.assertEqual(out["status"], "error")

    def test_timeout_reported(self):
        with mock.patch.dict(ab.os.environ, {"REBAL_CLAUDE_CLI": "/x/claude"}, clear=False):
            with mock.patch.object(ab.subprocess, "run",
                                   side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=45)):
                out = ab._smoke_check_backend("claude", {}, timeout=45)
        self.assertEqual(out["status"], "timeout")


class CursorVersionResolution(unittest.TestCase):
    """The app must run the SAME version the official launcher runs. The launcher
    only accepts versions/<YYYY.MM.DD-commithex>; folders with extra segments
    (e.g. an orphaned 2026.06.12-19-59-36-f6aba9a) are ignored, and dist-package
    is the fallback when no version-named dir exists."""

    def _resolve(self, names: list[str], *, with_dist=True):
        # Build a REAL install tree in a temp dir so the resolution exercises
        # actual filesystem semantics (string-matching path separators is
        # platform-coupled and breaks between Windows and CI's Linux).
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "cursor-agent"
        (root / "versions").mkdir(parents=True)
        launcher = root / "cursor-agent.cmd"
        launcher.write_text("@echo off\n", encoding="utf-8")

        def _mk(name):
            d = root / "versions" / name
            d.mkdir()
            (d / "index.js").write_text("//\n", encoding="utf-8")
            (d / "node.exe").write_text("\n", encoding="utf-8")

        for name in names:
            _mk(name)
        if with_dist:
            _mk("dist-package")

        with mock.patch.object(ab.sys, "platform", "win32"), \
             mock.patch.object(ab.shutil, "which", return_value=str(launcher)):
            return ab._cursor_argv_base()

    def test_picks_newest_official_version_ignoring_orphan(self):
        argv = self._resolve(["2026.04.17-787b533", "2026.06.12-19-59-36-f6aba9a"])
        self.assertIsNotNone(argv)
        self.assertIn("2026.04.17-787b533", argv[1])
        self.assertNotIn("19-59-36", argv[1])

    def test_falls_back_to_dist_package_when_no_official_dir(self):
        argv = self._resolve(["2026.06.12-19-59-36-f6aba9a"])
        self.assertIsNotNone(argv)
        self.assertIn("dist-package", argv[1])

    def test_orphan_only_and_no_dist_is_unresolved(self):
        argv = self._resolve(["2026.06.12-19-59-36-f6aba9a"], with_dist=False)
        self.assertIsNone(argv)


class SetupStatus(unittest.TestCase):
    def setUp(self):
        self._orig = ac.CONFIG_PATH
        ac.CONFIG_PATH = Path(tempfile.mkdtemp()) / "analysis-config.json"
        # The auth-probe cache is process-global; clear it so a probe result
        # mocked in one test can't leak into the next via the TTL cache.
        ab._clear_auth_probe_cache()

    def tearDown(self):
        ac.CONFIG_PATH = self._orig

    def test_shape_without_checks(self):
        # Probe inconclusive -> installed but credential state unknown.
        with mock.patch.object(ab, "_auth_probe", return_value=None):
            with mock.patch.dict(ab.os.environ, {"REBAL_CLAUDE_CLI": "/x/claude",
                                                 "REBAL_CURSOR_CLI": "/x/cursor-agent"}, clear=False):
                st = ab.setup_status()
        self.assertFalse(st["config_exists"])  # tmp file not written yet
        self.assertEqual({b["id"] for b in st["backends"]}, {"claude", "cursor"})
        for b in st["backends"]:
            self.assertTrue(b["installed"])
            self.assertEqual(b["status"], "installed")
            self.assertIsNone(b["authenticated"])
            self.assertNotIn("check", b)

    def test_auth_probe_distinguishes_logged_out_from_ready(self):
        with mock.patch.object(ab, "_auth_probe", side_effect=lambda pid, **k: pid == "claude"):
            with mock.patch.dict(ab.os.environ, {"REBAL_CLAUDE_CLI": "/x/claude",
                                                 "REBAL_CURSOR_CLI": "/x/cursor-agent"}, clear=False):
                st = ab.setup_status()
        by = {b["id"]: b for b in st["backends"]}
        self.assertTrue(by["claude"]["authenticated"])
        self.assertEqual(by["claude"]["status"], "ready")
        self.assertFalse(by["cursor"]["authenticated"])
        self.assertEqual(by["cursor"]["status"], "logged_out")

    def test_missing_backends_report_missing(self):
        ctx_env, ctx_which = _no_backends_env()
        with ctx_env:
            ab.os.environ.pop("REBAL_CLAUDE_CLI", None)
            ab.os.environ.pop("REBAL_CURSOR_CLI", None)
            with ctx_which:
                st = ab.setup_status()
        for b in st["backends"]:
            self.assertFalse(b["installed"])
            self.assertEqual(b["status"], "missing")

    def test_run_checks_attaches_check_and_propagates_status(self):
        fake = {"ok": True, "status": "ok", "message": "OK"}
        with mock.patch.object(ab, "_smoke_check_backend", return_value=fake):
            with mock.patch.dict(ab.os.environ, {"REBAL_CLAUDE_CLI": "/x/claude",
                                                 "REBAL_CURSOR_CLI": "/x/cursor-agent"}, clear=False):
                st = ab.setup_status(run_checks=True)
        for b in st["backends"]:
            self.assertEqual(b["check"], fake)
            self.assertEqual(b["status"], "ok")

    def test_config_exists_flips_after_save(self):
        ac.save_config({"providers": [], "timeout_sec": 100, "allow_web": False})
        self.assertTrue(ab.setup_status()["config_exists"])


if __name__ == "__main__":
    unittest.main()
