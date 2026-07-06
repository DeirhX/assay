"""Tests for analysis_prompts: the pure prompt builders and the grounding-rule
switch that flips wording between offline (DATA-only) and web-enabled runs."""

from __future__ import annotations

import unittest

import _support  # noqa: F401
import analysis_prompts as ap


class GroundRules(unittest.TestCase):
    def test_offline_rule_restricts_to_data(self):
        self.assertIn("ONLY", ap._data_rule(False))
        self.assertNotIn("web search", ap._data_rule(False))

    def test_web_rule_permits_search_and_demands_citations(self):
        rule = ap._data_rule(True)
        self.assertIn("web search", rule)
        self.assertIn("source URL", rule)

    def test_qa_rules_track_web_flag(self):
        self.assertIn("ONLY", ap._qa_data_rule(False))
        self.assertIn("web search", ap._qa_data_rule(True))

    def test_sources_section_only_when_web(self):
        self.assertEqual(ap._sources_section(False), "")
        self.assertIn("Sources", ap._sources_section(True))

    def test_prompt_includes_sources_section_when_web(self):
        rec = {"symbol": "AMD", "metrics": {}, "portfolio": {}}
        self.assertIn("## Sources", ap.build_prompt(rec, allow_web=True))
        self.assertNotIn("## Sources", ap.build_prompt(rec, allow_web=False))


class SegmentDraftPrompt(unittest.TestCase):
    def test_web_flag_switches_the_ticker_ground_rule(self):
        self.assertIn("Use only tickers", ap.build_segment_draft_prompt("x", allow_web=False))
        self.assertIn("web search", ap.build_segment_draft_prompt("x", allow_web=True))


class DocQaPrompt(unittest.TestCase):
    """The Deep Research follow-up prompt grounds the model in the report +
    citations and the prior thread, and bounds each so it can't grow unbounded."""

    def test_includes_report_question_and_sources(self):
        p = ap.build_doc_qa_prompt(
            "Space Exploration", "Rocket Lab is the clearest launch pure-play.",
            [{"href": "https://example.com/x", "label": "ex\nmore"}],
            [{"role": "user", "text": "earlier q"}, {"role": "assistant", "text": "earlier a"}],
            "Is RKLB overvalued?")
        self.assertIn("Space Exploration", p)
        self.assertIn("Rocket Lab is the clearest", p)
        self.assertIn("https://example.com/x", p)
        self.assertIn("Is RKLB overvalued?", p)
        self.assertIn("earlier q", p)

    def test_truncates_a_huge_report(self):
        p = ap.build_doc_qa_prompt("T", "x" * 40000, [], [], "q?")
        self.assertIn("[report truncated]", p)
        self.assertLess(len(p), 20000)


if __name__ == "__main__":
    unittest.main()
