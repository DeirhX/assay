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


class GroundRules(unittest.TestCase):
    def test_offline_rule_restricts_to_data(self):
        self.assertIn("ONLY", ta._data_rule(False))
        self.assertNotIn("web search", ta._data_rule(False))

    def test_web_rule_permits_search_and_demands_citations(self):
        rule = ta._data_rule(True)
        self.assertIn("web search", rule)
        self.assertIn("source URL", rule)

    def test_qa_rules_track_web_flag(self):
        self.assertIn("ONLY", ta._qa_data_rule(False))
        self.assertIn("web search", ta._qa_data_rule(True))

    def test_sources_section_only_when_web(self):
        self.assertEqual(ta._sources_section(False), "")
        self.assertIn("Sources", ta._sources_section(True))

    def test_prompt_includes_sources_section_when_web(self):
        rec = {"symbol": "AMD", "metrics": {}, "portfolio": {}}
        self.assertIn("## Sources", ta.build_prompt(rec, allow_web=True))
        self.assertNotIn("## Sources", ta.build_prompt(rec, allow_web=False))


class Usage(unittest.TestCase):
    def test_keeps_known_counters_as_ints(self):
        u = ta._norm_usage({"input_tokens": 10, "output_tokens": 5.0,
                            "cache_read_input_tokens": 3, "junk": "x"})
        self.assertEqual(u, {"input_tokens": 10, "output_tokens": 5,
                             "cache_read_input_tokens": 3})

    def test_non_dict_is_empty(self):
        self.assertEqual(ta._norm_usage(None), {})


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


if __name__ == "__main__":
    unittest.main()
