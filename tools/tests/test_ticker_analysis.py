"""Tests for the on-demand analysis layer: web-tool wiring, timeouts, the
grounding-rule switch, usage normalization, config validation, and the
cancellable subprocess runner."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import _support  # noqa: F401
import ticker_analysis as ta


class ClaudeToolArgs(unittest.TestCase):
    def test_web_off_disables_all_tools(self):
        self.assertEqual(ta._claude_tool_args({"allow_web": False}), ["--tools", ""])

    def test_web_on_scopes_and_preapproves_web_tools(self):
        args = ta._claude_tool_args({"allow_web": True})
        self.assertEqual(args,
                         ["--tools", "WebSearch", "WebFetch",
                          "--allowedTools", "WebSearch", "WebFetch"])
        self.assertNotIn("Bash", args)
        self.assertNotIn("Edit", args)


class RunTimeout(unittest.TestCase):
    def test_no_web_uses_configured_value(self):
        self.assertEqual(ta._run_timeout({"timeout_sec": 300}), 300)

    def test_web_floors_at_600(self):
        self.assertEqual(ta._run_timeout({"timeout_sec": 300, "allow_web": True}), 600)

    def test_web_keeps_larger_configured_value(self):
        self.assertEqual(ta._run_timeout({"timeout_sec": 900, "allow_web": True}), 900)


class ProviderOrder(unittest.TestCase):
    def test_claude_is_preferred_even_if_config_is_reversed(self):
        cfg = {"providers": [
            {"id": "cursor", "enabled": True},
            {"id": "claude", "enabled": True},
        ]}
        self.assertEqual([p["id"] for p in ta._ordered_providers(cfg)], ["claude", "cursor"])

    def test_unknown_providers_are_ignored_for_runtime_order(self):
        cfg = {"providers": [{"id": "bogus"}, {"id": "cursor"}]}
        self.assertEqual([p["id"] for p in ta._ordered_providers(cfg)], ["cursor"])


class ClaudeQaArgs(unittest.TestCase):
    def test_qa_does_not_use_removed_dynamic_prompt_flag(self):
        seen = {}

        def fake_run(argv, *, input_text, timeout, cancel):
            seen["argv"] = argv
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout='{"result":"OK","session_id":"sid","usage":{}}',
                stderr="",
            )

        rec = {"symbol": "AMD", "name": "AMD", "metrics": {}, "portfolio": {}}
        with mock.patch.dict(ta.os.environ, {"REBAL_CLAUDE_CLI": "/x/claude"}, clear=False), \
             mock.patch.object(ta, "_run_proc", side_effect=fake_run):
            out = ta._run_claude_qa(
                rec, [], "question?", None,
                {"id": "claude", "enabled": True, "model": "", "extra_args": []},
                {"allow_web": False, "timeout_sec": 300},
                None,
                None,
            )
        self.assertTrue(out["ok"])
        self.assertNotIn("--exclude-dynamic-system-prompt-sections", seen["argv"])
        self.assertIn("--session-id", seen["argv"])


class CursorWebArgs(unittest.TestCase):
    """Cursor's read-only ask mode includes a web tool, but headless -p rejects
    tool calls that need approval -- so we add --force ONLY when web is requested.
    Verified separately that --mode ask keeps write/shell denied even under
    --force, so this can't escalate past read-only."""

    def _argv_for(self, allow_web: bool) -> list:
        seen = {}

        def fake_run(argv, *, input_text, timeout, cancel):
            seen["argv"] = argv
            return subprocess.CompletedProcess(argv, 0, stdout="## Verdict\nHold.", stderr="")

        with mock.patch.object(ta, "_cursor_argv_base", return_value=["cursor-agent"]), \
             mock.patch.object(ta, "_run_proc", side_effect=fake_run):
            ta._run_cursor("PROMPT", {"id": "cursor", "enabled": True, "model": "", "extra_args": []},
                           {"allow_web": allow_web, "timeout_sec": 300}, None)
        return seen["argv"]

    def test_force_added_only_when_web_enabled(self):
        web_on = self._argv_for(True)
        web_off = self._argv_for(False)
        self.assertIn("--force", web_on)
        self.assertNotIn("--force", web_off)
        # Read-only mode is pinned in BOTH cases -- --force never replaces it.
        for argv in (web_on, web_off):
            self.assertEqual(argv[argv.index("--mode") + 1], "ask")


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

        with mock.patch.object(ta.sys, "platform", "win32"), \
             mock.patch.object(ta.shutil, "which", return_value=str(launcher)):
            return ta._cursor_argv_base()

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


class Config(unittest.TestCase):
    def setUp(self):
        self._orig = ta.CONFIG_PATH
        self._tmp = Path(tempfile.mkdtemp()) / "analysis-config.json"
        ta.CONFIG_PATH = self._tmp

    def tearDown(self):
        ta.CONFIG_PATH = self._orig

    def test_save_normalizes_and_drops_unknown_providers(self):
        saved = ta.save_config({
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
        ta.save_config({"timeout_sec": 200, "allow_web": False,
                        "providers": [{"id": "cursor", "enabled": False, "model": "", "extra_args": []}]})
        loaded = ta.load_config()
        self.assertEqual(loaded["timeout_sec"], 200)
        self.assertFalse(loaded["allow_web"])
        self.assertEqual(loaded["providers"][0]["id"], "cursor")

    def test_missing_file_falls_back_to_defaults(self):
        loaded = ta.load_config()  # tmp path does not exist yet
        self.assertEqual(loaded["timeout_sec"], ta.DEFAULT_CONFIG["timeout_sec"])


class RunProc(unittest.TestCase):
    def test_runs_to_completion_without_cancel(self):
        proc = ta._run_proc([sys.executable, "-c", "print('hi')"],
                            input_text=None, timeout=30, cancel=None)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("hi", proc.stdout)

    def test_feeds_stdin(self):
        proc = ta._run_proc(
            [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read().upper())"],
            input_text="abc", timeout=30, cancel=lambda: False)
        self.assertIn("ABC", proc.stdout)

    def test_cancel_kills_quickly(self):
        start = time.monotonic()
        with self.assertRaises(ta._Cancelled):
            ta._run_proc([sys.executable, "-c", "import time; time.sleep(30)"],
                         input_text=None, timeout=30, cancel=lambda: True)
        self.assertLess(time.monotonic() - start, 8.0)  # killed, not waited out

    def test_timeout_propagates(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            ta._run_proc([sys.executable, "-c", "import time; time.sleep(5)"],
                         input_text=None, timeout=1, cancel=lambda: False)


class AskAboutDoc(unittest.TestCase):
    def test_ask_about_doc_runs_through_fallback(self):
        # ask_about_doc must not be tied to a ticker record; it just builds the
        # doc prompt and delegates to the generic backend fallback.
        with mock.patch.object(ta, "_run_with_fallback",
                               return_value={"ok": True, "report": "ans"}) as m:
            res = ta.ask_about_doc("T", "doc body", [], [], "q?",
                                   cfg={"allow_web": False, "providers": []})
        self.assertTrue(res["ok"])
        self.assertTrue(m.called)
        prompt_arg = m.call_args[0][0]
        self.assertIn("doc body", prompt_arg)
        self.assertIn("q?", prompt_arg)


class FallbackErrorLogging(unittest.TestCase):
    """A backend failing -- even when the next one succeeds -- must leave a
    durable record, so the lead backend silently hitting its quota (and us
    quietly deferring to the fallback) stops being invisible."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self._orig_path = ta.errorlog.LOG_PATH
        ta.errorlog.LOG_PATH = Path(self._dir.name) / "error_log.jsonl"
        self._cfg = {"providers": [
            {"id": "cursor", "enabled": True},
            {"id": "claude", "enabled": True},
        ]}

    def tearDown(self):
        ta.errorlog.LOG_PATH = self._orig_path
        self._dir.cleanup()

    def test_silent_lead_fallback_is_logged_as_warning(self):
        # Claude leads now; when it's out of quota we defer to Cursor, but the
        # lead failure must still leave a durable warning.
        runners = {
            "claude": lambda *a, **k: {"ok": False, "fatal": False,
                                       "error": "Claude: usage limit reached"},
            "cursor": lambda *a, **k: {"ok": True, "report": "ok", "backend": "cursor",
                                       "backend_label": "Cursor", "model": "(default)"},
        }
        with mock.patch.dict(ta._RUNNERS, runners, clear=False):
            res = ta._run_with_fallback("p", self._cfg, label="analysis")
        self.assertTrue(res["ok"])  # cursor served it
        entries = ta.errorlog.recent()
        self.assertEqual(len(entries), 1)  # the claude failure, not a total-fail error
        self.assertEqual(entries[0]["level"], "warning")
        self.assertEqual(entries[0]["context"]["backend"], "claude")
        self.assertEqual(entries[0]["context"]["reason"], "quota")
        self.assertEqual(entries[0]["context"]["op"], "analysis")

    def test_all_backends_failing_logs_an_error(self):
        runners = {
            "cursor": lambda *a, **k: {"ok": False, "fatal": False, "error": "rate limit"},
            "claude": lambda *a, **k: {"ok": False, "fatal": False, "error": "rate limit"},
        }
        with mock.patch.dict(ta._RUNNERS, runners, clear=False):
            res = ta._run_with_fallback("p", self._cfg, label="analysis")
        self.assertFalse(res["ok"])
        levels = [e["level"] for e in ta.errorlog.recent()]
        self.assertIn("error", levels)  # the aggregate failure
        self.assertEqual(levels.count("warning"), 2)  # each attempt


if __name__ == "__main__":
    unittest.main()
