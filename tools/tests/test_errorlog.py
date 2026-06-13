"""Tests for the operational error log: it records real incidents (backend
fallbacks, server crashes), keeps newest-first ordering, drops empty context,
stays bounded, and clears. The log path is redirected to a temp file so tests
never touch the committed data/ dir."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import _support  # noqa: F401
import errorlog


class ErrorLog(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self._orig_path = errorlog.LOG_PATH
        errorlog.LOG_PATH = Path(self._dir.name) / "error_log.jsonl"

    def tearDown(self):
        errorlog.LOG_PATH = self._orig_path
        self._dir.cleanup()

    def test_empty_log_reads_as_empty_list(self):
        self.assertEqual(errorlog.recent(), [])

    def test_round_trips_newest_first_with_levels(self):
        errorlog.warn("llm_backend", "cursor auth expired", backend="cursor", reason="auth")
        errorlog.error("server", "KeyError: x", request="POST /api/analyze/AMD")
        entries = errorlog.recent()
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["message"], "KeyError: x")  # newest first
        self.assertEqual(entries[0]["level"], "error")
        self.assertEqual(entries[1]["level"], "warning")
        self.assertEqual(entries[1]["category"], "llm_backend")
        self.assertEqual(entries[1]["context"], {"backend": "cursor", "reason": "auth"})

    def test_context_drops_none_values(self):
        errorlog.warn("llm_backend", "x", backend="cursor", op=None)
        self.assertEqual(errorlog.recent()[0]["context"], {"backend": "cursor"})

    def test_no_context_key_when_all_none(self):
        errorlog.error("server", "boom", op=None)
        self.assertNotIn("context", errorlog.recent()[0])

    def test_log_is_bounded_to_max_entries(self):
        orig_max = errorlog.MAX_ENTRIES
        errorlog.MAX_ENTRIES = 5
        try:
            for i in range(12):
                errorlog.error("server", f"e{i}")
            entries = errorlog.recent(100)
            self.assertEqual(len(entries), 5)
            self.assertEqual(entries[0]["message"], "e11")   # newest kept
            self.assertEqual(entries[-1]["message"], "e7")   # oldest kept
        finally:
            errorlog.MAX_ENTRIES = orig_max

    def test_recent_honours_limit(self):
        for i in range(10):
            errorlog.error("server", f"e{i}")
        self.assertEqual(len(errorlog.recent(3)), 3)

    def test_clear_wipes_the_log(self):
        errorlog.error("server", "boom")
        errorlog.clear()
        self.assertEqual(errorlog.recent(), [])

    def test_corrupt_line_is_skipped(self):
        errorlog.error("server", "good")
        with errorlog.LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write("this is not json\n")
        entries = errorlog.recent()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["message"], "good")


if __name__ == "__main__":
    unittest.main()
