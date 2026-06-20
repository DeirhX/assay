"""Tests for tools/analysis_jobs.py: the pure thread helpers (Q&A archive load /
trim), the artifact round-trip (save -> latest), and the start-gate guards.

The CLI tiers, deterministic pulls and job registry are mocked, and the research
cache dirs are pointed at a temp dir, so nothing runs a subprocess or touches the
real cache."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _support  # noqa: F401
import analysis_jobs as aj
from apierror import Conflict


class DropQaExchange(unittest.TestCase):
    def _thread(self):
        return {"session": "sess-1", "turns": [
            {"role": "user", "text": "q1"}, {"role": "assistant", "text": "a1"},
            {"role": "user", "text": "q2"}, {"role": "assistant", "text": "a2"},
        ]}

    def test_removes_user_and_following_assistant_and_drops_session(self):
        thread = self._thread()
        self.assertTrue(aj.drop_qa_exchange(thread, 0))
        self.assertEqual([t["text"] for t in thread["turns"]], ["q2", "a2"])
        self.assertNotIn("session", thread)  # session reseeds from trimmed history

    def test_removes_trailing_user_only_turn(self):
        thread = {"turns": [{"role": "user", "text": "q1"}]}
        self.assertTrue(aj.drop_qa_exchange(thread, 0))
        self.assertEqual(thread["turns"], [])

    def test_rejects_index_not_on_a_user_turn(self):
        thread = self._thread()
        self.assertFalse(aj.drop_qa_exchange(thread, 1))  # 1 is an assistant turn
        self.assertEqual(len(thread["turns"]), 4)

    def test_rejects_out_of_range_and_non_int_index(self):
        thread = self._thread()
        self.assertFalse(aj.drop_qa_exchange(thread, 99))
        self.assertFalse(aj.drop_qa_exchange(thread, "nope"))
        self.assertFalse(aj.drop_qa_exchange({"turns": "bad"}, 0))


class CacheBackedTest(unittest.TestCase):
    """Shared base that redirects the research-cache dirs at a temp location."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self._orig = {name: getattr(aj, name) for name in ("ANALYSIS_DIR", "RESEARCH_DIR", "DEEP_DIR")}
        aj.ANALYSIS_DIR = root / "analysis"
        aj.RESEARCH_DIR = root / "research"
        aj.DEEP_DIR = root / "deep"
        for d in (aj.ANALYSIS_DIR, aj.RESEARCH_DIR, aj.DEEP_DIR):
            d.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        for name, val in self._orig.items():
            setattr(aj, name, val)
        self.tmp.cleanup()


class LoadQa(CacheBackedTest):
    def test_missing_thread_returns_empty_shape(self):
        self.assertEqual(aj.load_qa("aapl"), {"symbol": "AAPL", "turns": []})

    def test_non_dict_payload_falls_back_to_default(self):
        aj.qa_path("AAPL").write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
        self.assertEqual(aj.load_qa("aapl"), {"symbol": "AAPL", "turns": []})

    def test_normalizes_bad_turns_field(self):
        aj.qa_path("AAPL").write_text(json.dumps({"symbol": "AAPL", "turns": "oops"}), encoding="utf-8")
        self.assertEqual(aj.load_qa("AAPL")["turns"], [])

    def test_deep_qa_defaults_and_keys_on_stem(self):
        self.assertEqual(aj.load_deep_qa("AI Semis"), {"stem": "ai-semis", "turns": []})


class ArtifactRoundTrip(CacheBackedTest):
    def test_save_then_latest_round_trips_report_and_meta(self):
        (aj.RESEARCH_DIR / "AAPL.json").write_text(json.dumps({"currency": "usd"}), encoding="utf-8")
        meta = {"backend": "claude", "backend_label": "Claude", "model": "sonnet"}
        with mock.patch.object(aj.ticker_analysis, "parse_price_levels",
                               return_value=[{"kind": "buy", "price": 100}]) as parse:
            saved = aj.save_analysis_artifact("aapl", "  the report  ", meta)
        parse.assert_called_once()
        self.assertEqual(saved["meta"]["currency"], "USD")           # lifted from dossier
        self.assertEqual(saved["meta"]["backend_label"], "Claude")
        self.assertEqual(saved["meta"]["price_levels_suggested"], [{"kind": "buy", "price": 100}])

        latest = aj.latest_analysis("AAPL")
        self.assertEqual(latest["symbol"], "AAPL")
        self.assertEqual(latest["report"], "the report\n")           # stripped + trailing newline
        self.assertEqual(latest["meta"]["model"], "sonnet")

    def test_latest_analysis_is_none_when_nothing_saved(self):
        self.assertIsNone(aj.latest_analysis("AAPL"))


class StartGuards(CacheBackedTest):
    def test_start_qa_rejects_empty_question(self):
        with self.assertRaises(ValueError):
            aj.start_qa("AAPL", "   ")

    def test_start_qa_conflicts_when_already_running(self):
        with mock.patch.object(aj.jobs, "running", return_value=True):
            with self.assertRaises(Conflict):
                aj.start_qa("AAPL", "why?")

    def test_start_qa_spawns_job_when_idle(self):
        with mock.patch.object(aj.jobs, "running", return_value=False), \
             mock.patch.object(aj, "new_job", return_value={"id": "j1"}), \
             mock.patch.object(aj, "public", side_effect=lambda j: j), \
             mock.patch.object(aj.threading, "Thread") as Thread:
            out = aj.start_qa("aapl", "why?")
        Thread.assert_called_once()
        self.assertEqual(out, {"id": "j1"})

    def test_start_analysis_conflicts_when_already_running(self):
        with mock.patch.object(aj.jobs, "running", return_value=True):
            with self.assertRaises(Conflict):
                aj.start_analysis("AAPL", False)

    def test_start_deep_qa_requires_existing_report(self):
        with self.assertRaises(ValueError):
            aj.start_deep_qa("ghost", "what now?")

    def test_start_deep_qa_spawns_job_when_report_exists(self):
        (aj.DEEP_DIR / "ai-semis.md").write_text("# report", encoding="utf-8")
        with mock.patch.object(aj.jobs, "running", return_value=False), \
             mock.patch.object(aj, "new_job", return_value={"id": "d1"}), \
             mock.patch.object(aj, "public", side_effect=lambda j: j), \
             mock.patch.object(aj.threading, "Thread") as Thread:
            out = aj.start_deep_qa("ai-semis", "what now?")
        Thread.assert_called_once()
        self.assertEqual(out, {"id": "d1"})


if __name__ == "__main__":
    unittest.main()
