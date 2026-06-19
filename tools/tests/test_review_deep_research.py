"""Tests for the Deep Research review gate: source bucketing, report-action
inference, and the full review() flow (conflicts, proposals, blocks) against a
synthetic report in a temp repo. Offline; no Perplexity, no submodule."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import _support  # noqa: F401
import review_deep_research as rd


class NormalizeSources(unittest.TestCase):
    def test_dedupes_and_falls_back_to_netloc(self):
        raw = {"citations": [
            {"href": "https://ir.example.com/q1", "label": "Q1 report"},
            {"href": "https://ir.example.com/q1", "label": "duplicate"},
            {"url": "https://reuters.com/x"},
            {"label": "no link at all"},
        ]}
        out = rd.normalize_sources(raw)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["label"], "Q1 report")
        self.assertEqual(out[1]["label"], "reuters.com")

    def test_garbage_input_is_empty(self):
        self.assertEqual(rd.normalize_sources("nope"), [])
        self.assertEqual(rd.normalize_sources(None), [])


class SourceBuckets(unittest.TestCase):
    def test_bucket_classification(self):
        self.assertEqual(rd.source_bucket("https://ir.amd.com/news"), "primary_ir")
        self.assertEqual(rd.source_bucket("https://www.sec.gov/filing"), "primary_ir")
        self.assertEqual(rd.source_bucket("https://www.reuters.com/markets"), "major_media")
        self.assertEqual(rd.source_bucket("https://seekingalpha.com/article"), "secondary")
        self.assertEqual(rd.source_bucket("https://reddit.com/r/stocks"), "weak")
        self.assertEqual(rd.source_bucket("https://random.blog/post"), "other")

    def test_summary_counts(self):
        sources = [
            {"label": "a", "href": "https://ir.amd.com/x"},
            {"label": "b", "href": "https://reddit.com/y"},
        ]
        info = rd.source_summary(sources)
        self.assertEqual(info["count"], 2)
        self.assertEqual(info["buckets"]["primary_ir"], 1)
        self.assertEqual(info["buckets"]["weak"], 1)
        self.assertEqual(info["weak_sources"][0]["href"], "https://reddit.com/y")


class InferReportAction(unittest.TestCase):
    def test_not_mentioned(self):
        self.assertEqual(rd.infer_report_action("nothing about it", "AAA"), "not mentioned")

    def test_add_verb_wins(self):
        text = "We would accumulate AAA on weakness and overweight it."
        self.assertEqual(rd.infer_report_action(text, "AAA"), "add")

    def test_trim_verb(self):
        text = "AAA looks stretched; trim the position and reduce exposure."
        self.assertEqual(rd.infer_report_action(text, "AAA"), "trim")

    def test_mention_without_verbs(self):
        self.assertEqual(rd.infer_report_action("AAA exists.", "AAA"), "mentioned")

    def test_symbol_match_is_word_bounded(self):
        # "BAAAM" must not count as a mention of AAA.
        self.assertEqual(rd.infer_report_action("BAAAM is great", "AAA"), "not mentioned")


class WorstSeverity(unittest.TestCase):
    def test_empty_is_info(self):
        self.assertEqual(rd.worst_severity([]), "INFO")

    def test_error_dominates(self):
        checks = [{"severity": "INFO"}, {"severity": "ERROR"}, {"severity": "WARN"}]
        self.assertEqual(rd.worst_severity(checks), "ERROR")


class ReviewFlow(unittest.TestCase):
    """Full review() against a synthetic segment + report in a temp repo."""

    SEGMENT = "test-seg"
    DATE = "2026-06-01"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        data = root / "data"
        self._orig = {k: getattr(rd, k) for k in (
            "REPO_ROOT", "SEGMENT_DEF_DIR", "RESEARCH_DIR",
            "DEEP_DIR", "HOLDINGS_JSON", "TARGET_MODEL_JSON",
        )}
        rd.REPO_ROOT = root
        rd.SEGMENT_DEF_DIR = data / "segments"
        rd.RESEARCH_DIR = data / "research"
        rd.DEEP_DIR = rd.RESEARCH_DIR / "deep"
        rd.HOLDINGS_JSON = data / "current-holdings.json"
        rd.TARGET_MODEL_JSON = data / "target-model.json"
        for d in (rd.SEGMENT_DEF_DIR, rd.RESEARCH_DIR, rd.DEEP_DIR):
            d.mkdir(parents=True)

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(rd, k, v)
        self.tmp.cleanup()

    def _write(self, path: Path, payload):
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(payload, str):
            path.write_text(payload, encoding="utf-8")
        else:
            path.write_text(json.dumps(payload), encoding="utf-8")

    def _setup_run(self, *, report: str, members, targets=None, holdings=None,
                   sources=None, research=None):
        self._write(rd.SEGMENT_DEF_DIR / f"{self.SEGMENT}.json",
                    {"title": "Test Segment", "members": members})
        self._write(rd.DEEP_DIR / f"{self.SEGMENT}-{self.DATE}.md", report)
        if sources is not None:
            self._write(rd.DEEP_DIR / f"{self.SEGMENT}-{self.DATE}.sources.json",
                        {"citations": sources})
        self._write(rd.HOLDINGS_JSON, holdings or {"positions": []})
        self._write(rd.TARGET_MODEL_JSON, {"targets": targets or {}, "funding_order": []})
        for sym, rec in (research or {}).items():
            self._write(rd.RESEARCH_DIR / f"{sym}.json", rec)

    def test_add_vs_no_buy_rule_is_conflict_warn(self):
        self._setup_run(
            report="We would add AAA aggressively here.",
            members=[{"symbol": "AAA", "sleeve": "core"}],
            targets={"AAA": {"low": 1, "high": 3, "rule": "do_not_add"}},
        )
        result = rd.review(self.SEGMENT, self.DATE, write=False)
        row = result["rows"][0]
        self.assertEqual(row["report_action"], "add")
        self.assertIn("do_not_add", row["conflict"])
        self.assertTrue(any(f["level"] == "WARN" and f["symbol"] == "AAA"
                            for f in result["findings"]))

    def test_held_symbol_without_target_gets_draft_proposal(self):
        self._setup_run(
            report="AAA is fine.",
            members=[{"symbol": "AAA", "sleeve": "core"}],
            holdings={"positions": [
                {"symbol": "AAA", "base_market_value": 10.0},
                {"symbol": "REST", "base_market_value": 90.0},
            ]},
        )
        result = rd.review(self.SEGMENT, self.DATE, write=False)
        changes = result["proposal"]["changes"]
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["symbol"], "AAA")
        self.assertEqual(changes[0]["action"], "add_target")
        self.assertEqual(changes[0]["status"], "draft")
        # Band drafted around the 10% held weight.
        self.assertAlmostEqual(changes[0]["proposed_target"]["low"], 9.0)
        self.assertAlmostEqual(changes[0]["proposed_target"]["high"], 11.0)

    def test_error_level_cross_checks_block_the_symbol(self):
        self._setup_run(
            report="AAA report.",
            members=[{"symbol": "AAA", "sleeve": "core"}],
            targets={"AAA": {"low": 1, "high": 3, "rule": "hold"}},
            research={"AAA": {"cross_checks": [{"severity": "ERROR", "message": "impossible mcap"}]}},
        )
        result = rd.review(self.SEGMENT, self.DATE, write=False)
        self.assertEqual(result["blocked_symbols"], ["AAA"])
        self.assertEqual(result["rows"][0]["data_quality"], "ERROR")

    def test_empty_segment_is_blocking(self):
        self._setup_run(report="empty", members=[])
        result = rd.review(self.SEGMENT, self.DATE, write=False)
        self.assertTrue(any(f["level"] == "BLOCK" for f in result["findings"]))

    def test_source_quality_findings(self):
        self._setup_run(
            report="AAA",
            members=[{"symbol": "AAA", "sleeve": "core"}],
            sources=[{"href": "https://reddit.com/r/wsb", "label": "trust me bro"}],
        )
        result = rd.review(self.SEGMENT, self.DATE, write=False)
        messages = " | ".join(f["message"] for f in result["findings"])
        self.assertIn("Source count is low", messages)
        self.assertIn("No primary/IR-like sources", messages)
        self.assertIn("Weak/social sources", messages)

    def test_write_emits_review_and_proposal_files(self):
        self._setup_run(report="AAA", members=[{"symbol": "AAA", "sleeve": "core"}])
        rd.review(self.SEGMENT, self.DATE, write=True)
        paths = rd.run_paths(self.SEGMENT, self.DATE)
        self.assertTrue(paths["review"].exists())
        self.assertTrue(paths["proposal"].exists())
        proposal = json.loads(paths["proposal"].read_text(encoding="utf-8"))
        self.assertEqual(proposal["status"], "draft")

    def test_missing_segment_definition_exits(self):
        with self.assertRaises(SystemExit):
            rd.review("never-defined", self.DATE, write=False)


if __name__ == "__main__":
    unittest.main()
