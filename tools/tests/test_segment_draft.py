"""Tests for LLM-backed segment drafting -- the path that lets the research
console propose a real ticker universe for a brand-new theme you don't hold
(e.g. "space exploration") instead of only keyword-matching held names.

Covers: the tolerant JSON extractor, ticker_analysis.draft_segment_members
(with a stubbed backend), segments_service.merge_draft_members dedupe/validation,
and the segments_service.run_draft_job state machine across the LLM-ok,
LLM-failed, and no-CLI paths.
"""

from __future__ import annotations

import unittest
from unittest import mock

import _support  # noqa: F401
import jobs
import segments_service
import ticker_analysis as ta

_GOOD_JSON = (
    '{"title": "Space Exploration",'
    ' "comment": "Launch, satellites, and space-defense names.",'
    ' "sleeves": ["launch", "satellites"],'
    ' "members": ['
    '{"symbol": "RKLB", "sleeve": "launch", "rationale": "Small-lift launch + space systems.", "confidence": "high"},'
    '{"symbol": "ASTS", "sleeve": "satellites", "rationale": "Direct-to-cell satellites.", "confidence": "medium"}'
    ']}'
)


class ExtractJsonObject(unittest.TestCase):
    def test_bare_object(self):
        obj = ta._extract_json_object(_GOOD_JSON)
        self.assertIsInstance(obj, dict)
        self.assertEqual(obj["title"], "Space Exploration")

    def test_fenced_block(self):
        obj = ta._extract_json_object("Here you go:\n```json\n" + _GOOD_JSON + "\n```\nDone.")
        self.assertEqual(len(obj["members"]), 2)

    def test_prose_wrapped(self):
        obj = ta._extract_json_object("Sure! " + _GOOD_JSON + " Hope that helps.")
        self.assertEqual(obj["members"][0]["symbol"], "RKLB")

    def test_nested_braces_balanced(self):
        text = '{"a": {"b": 1}, "members": []}'
        self.assertEqual(ta._extract_json_object(text), {"a": {"b": 1}, "members": []})

    def test_garbage_returns_none(self):
        self.assertIsNone(ta._extract_json_object("no json at all here"))
        self.assertIsNone(ta._extract_json_object(""))

    def test_non_object_json_returns_none(self):
        # A bare array is valid JSON but not the object shape we need.
        self.assertIsNone(ta._extract_json_object("[1, 2, 3]"))


class DraftSegmentMembers(unittest.TestCase):
    def _cfg(self):
        return {"providers": [], "allow_web": False, "timeout_sec": 300}

    def test_parses_backend_json(self):
        fake = {"ok": True, "report": _GOOD_JSON, "backend": "claude",
                "backend_label": "Claude (claude)", "model": "sonnet"}
        with mock.patch.object(ta, "_run_with_fallback", return_value=fake):
            out = ta.draft_segment_members("space exploration", cfg=self._cfg())
        self.assertTrue(out["ok"])
        self.assertEqual([m["symbol"] for m in out["members"]], ["RKLB", "ASTS"])
        self.assertEqual(out["title"], "Space Exploration")
        self.assertEqual(out["backend_label"], "Claude (claude)")

    def test_unparseable_report_is_error(self):
        fake = {"ok": True, "report": "I could not find any tickers, sorry."}
        with mock.patch.object(ta, "_run_with_fallback", return_value=fake):
            out = ta.draft_segment_members("space exploration", cfg=self._cfg())
        self.assertFalse(out["ok"])
        self.assertIn("parseable", out["error"])

    def test_backend_failure_propagates(self):
        fake = {"ok": False, "error": "all backends out of quota"}
        with mock.patch.object(ta, "_run_with_fallback", return_value=fake):
            out = ta.draft_segment_members("space exploration", cfg=self._cfg())
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "all backends out of quota")

    def test_web_rule_flips_with_allow_web(self):
        self.assertIn("Use only tickers", ta.build_segment_draft_prompt("x", allow_web=False))
        self.assertIn("web search", ta.build_segment_draft_prompt("x", allow_web=True))


class MergeDraftMembers(unittest.TestCase):
    def test_dedupes_baseline_wins(self):
        baseline = [{"symbol": "NVDA", "sleeve": "compute", "rationale": "held", "confidence": "high"}]
        extra = [{"symbol": "nvda", "sleeve": "duplicate"},
                 {"symbol": "RKLB", "sleeve": "launch", "rationale": "rockets", "confidence": "medium"}]
        out = segments_service.merge_draft_members(baseline, extra)
        self.assertEqual([m["symbol"] for m in out], ["NVDA", "RKLB"])
        # Baseline entry kept its own sleeve, not the duplicate's.
        self.assertEqual(out[0]["sleeve"], "compute")

    def test_skips_invalid_and_non_dict(self):
        extra = [{"symbol": ""}, {"symbol": "(private)"}, "not-a-dict",
                 {"symbol": "LMT", "sleeve": "Defense-Prime"}]
        out = segments_service.merge_draft_members([], extra)
        self.assertEqual([m["symbol"] for m in out], ["LMT"])
        # Sleeve is normalized to a lowercase slug.
        self.assertEqual(out[0]["sleeve"], "defense-prime")


class SegmentDraftJob(unittest.TestCase):
    def _run(self, query="space exploration"):
        job = jobs.new_job("segment_draft", query=query)
        segments_service.run_draft_job(job["id"], query)
        return jobs.get_public(job["id"])

    def test_llm_success_merges_members(self):
        llm = {"ok": True, "members": [{"symbol": "RKLB", "sleeve": "launch"}],
               "title": "Space Exploration", "comment": "Launch + satellites.",
               "backend_label": "Claude (claude)"}
        with mock.patch.object(segments_service.ticker_analysis, "available_backends",
                               return_value={"claude": True, "cursor": False}), \
             mock.patch.object(segments_service.ticker_analysis, "draft_segment_members",
                               return_value=llm):
            pub = self._run()
        self.assertEqual(pub["state"], "done")
        res = pub["result"]
        self.assertEqual(res["member_count"], 1)
        self.assertEqual(res["definition"]["members"][0]["symbol"], "RKLB")
        self.assertEqual(res["definition"]["title"], "Space Exploration")
        self.assertFalse(res["warnings"])  # clean run, nothing to warn about

    def test_no_cli_warns_and_falls_back(self):
        with mock.patch.object(segments_service.ticker_analysis, "available_backends",
                               return_value={"claude": False, "cursor": False}):
            pub = self._run()
        self.assertEqual(pub["state"], "done")
        res = pub["result"]
        self.assertEqual(res["member_count"], 0)  # nothing held matches the theme
        joined = " ".join(res["warnings"])
        self.assertIn("No analysis CLI", joined)
        self.assertIn("No candidate tickers", joined)
        # The manual prompt is still handed back as the fallback path.
        self.assertIn("space exploration", res["llm_prompt"].lower())

    def test_llm_failure_warns_but_completes(self):
        with mock.patch.object(segments_service.ticker_analysis, "available_backends",
                               return_value={"claude": True, "cursor": False}), \
             mock.patch.object(segments_service.ticker_analysis, "draft_segment_members",
                               return_value={"ok": False, "error": "boom"}):
            pub = self._run()
        self.assertEqual(pub["state"], "done")
        self.assertIn("LLM draft failed", " ".join(pub["result"]["warnings"]))


if __name__ == "__main__":
    unittest.main()
