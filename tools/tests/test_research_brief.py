"""Tests for the per-name research brief builder. The summarizer is best-effort
and the deterministic pieces are the floor, so coverage centers on: facts built
purely from a review row, narrative lookup (newest, deep-over-analysis, case and
sidecar handling), summary caching keyed by content, the excerpt fallback when no
backend is available, and budget-bounded block assembly. The LLM is always
mocked -- no backend is ever invoked."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _support  # noqa: F401
import research_brief as rb


def _cfg(*, enabled=True, model="haiku", budget=6000):
    return {
        "providers": [{"id": "claude", "enabled": True, "model": "", "extra_args": []}],
        "allow_web": False,
        "summarizer": {"enabled": enabled, "model": model, "max_research_chars": budget},
    }


class DeterministicFacts(unittest.TestCase):
    def test_compact_digest_from_row(self):
        row = {
            "symbol": "NVDA", "held_pct_nav": 6.2, "target_rule": "accumulate",
            "target_band": [4.0, 7.0], "market_cap": "$4,230.0B", "pe_ttm": "52.1x",
            "ps": "38.0x", "data_quality": "WARN", "report_action": "add",
            "cross_checks": [{"severity": "WARN", "metric": "market_cap",
                              "message": "Yahoo vs SEC differ 8%"}],
            # A bulk field that must never leak into the brief.
            "price_history": [1, 2, 3, 4, 5],
        }
        facts = rb.deterministic_facts(row)
        self.assertIn("held 6.20% NAV", facts)
        self.assertIn("target accumulate [4-7]", facts)
        self.assertIn("P/E 52.1x", facts)
        self.assertIn("data WARN", facts)
        self.assertIn("segment report leans add", facts)
        self.assertIn("flag: Yahoo vs SEC differ 8%", facts)
        self.assertNotIn("price_history", facts)

    def test_unheld_and_clean_data_are_terse(self):
        row = {"symbol": "AMD", "held_pct_nav": None, "data_quality": "OK",
               "market_cap": "n/a", "pe_ttm": "n/a", "ps": "n/a",
               "report_action": "not mentioned", "cross_checks": []}
        facts = rb.deterministic_facts(row)
        self.assertEqual(facts, "not held")  # nothing else worth stating


class LatestNarrative(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self._orig = {k: getattr(rb, k) for k in ("DEEP_DIR", "ANALYSIS_DIR")}
        rb.DEEP_DIR = root / "deep"
        rb.ANALYSIS_DIR = root / "analysis"
        rb.DEEP_DIR.mkdir(parents=True)
        rb.ANALYSIS_DIR.mkdir(parents=True)

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(rb, k, v)
        self.tmp.cleanup()

    def test_prefers_newest_deep_over_analysis_and_ignores_sidecars(self):
        # Deep stems are slugified lowercase on disk.
        (rb.DEEP_DIR / "ticker-nvda-2026-06-01.md").write_text("deep old", encoding="utf-8")
        (rb.DEEP_DIR / "ticker-nvda-2026-06-10.md").write_text("deep new", encoding="utf-8")
        (rb.DEEP_DIR / "ticker-nvda-2026-06-10.review.md").write_text("review", encoding="utf-8")
        (rb.ANALYSIS_DIR / "NVDA-2026-06-09.md").write_text("analysis", encoding="utf-8")
        got = rb.latest_narrative("NVDA")  # upper query, lower file
        self.assertEqual(got["kind"], "deep")
        self.assertEqual(got["text"], "deep new")

    def test_falls_back_to_analysis_when_no_deep(self):
        (rb.ANALYSIS_DIR / "AMD-2026-06-09.md").write_text("analysis note", encoding="utf-8")
        got = rb.latest_narrative("AMD")
        self.assertEqual(got["kind"], "analysis")
        self.assertEqual(got["text"], "analysis note")

    def test_none_when_absent(self):
        self.assertIsNone(rb.latest_narrative("ZZZZ"))


class Summarize(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = rb.BRIEFS_DIR
        rb.BRIEFS_DIR = Path(self.tmp.name) / "briefs"

    def tearDown(self):
        rb.BRIEFS_DIR = self._orig
        self.tmp.cleanup()

    def test_llm_summary_is_cached_and_reused(self):
        with mock.patch.object(rb.ticker_analysis, "available_backends",
                               return_value={"claude": True, "cursor": False}), \
             mock.patch.object(rb.ticker_analysis, "_run_with_fallback",
                               return_value={"ok": True, "report": "Bullish: AI demand. Risk: capex cut."}) as run:
            first = rb.summarize_narrative("NVDA", "long report body", _cfg())
            self.assertEqual(first["method"], "llm")
            self.assertIn("Bullish", first["summary"])
            self.assertTrue(rb._cache_path("NVDA").exists())
            self.assertEqual(run.call_count, 1)

            # Same content -> served from cache, no second backend call.
            second = rb.summarize_narrative("NVDA", "long report body", _cfg())
            self.assertEqual(second["method"], "cache")
            self.assertEqual(run.call_count, 1)

            # Changed content -> key differs -> re-summarized.
            third = rb.summarize_narrative("NVDA", "a different report body", _cfg())
            self.assertEqual(third["method"], "llm")
            self.assertEqual(run.call_count, 2)

    def test_excerpt_fallback_when_no_backend(self):
        with mock.patch.object(rb.ticker_analysis, "available_backends",
                               return_value={"claude": False, "cursor": False}), \
             mock.patch.object(rb.ticker_analysis, "_run_with_fallback") as run:
            out = rb.summarize_narrative("NVDA", "some body text", _cfg())
            self.assertEqual(out["method"], "excerpt")
            run.assert_not_called()
            self.assertFalse(rb._cache_path("NVDA").exists())

    def test_disabled_summarizer_uses_excerpt_even_with_backend(self):
        with mock.patch.object(rb.ticker_analysis, "available_backends",
                               return_value={"claude": True, "cursor": False}), \
             mock.patch.object(rb.ticker_analysis, "_run_with_fallback") as run:
            out = rb.summarize_narrative("NVDA", "body", _cfg(enabled=False))
            self.assertEqual(out["method"], "excerpt")
            run.assert_not_called()

    def test_failed_call_falls_back_and_does_not_cache(self):
        with mock.patch.object(rb.ticker_analysis, "available_backends",
                               return_value={"claude": True, "cursor": False}), \
             mock.patch.object(rb.ticker_analysis, "_run_with_fallback",
                               return_value={"ok": False, "error": "quota"}):
            out = rb.summarize_narrative("NVDA", "body", _cfg())
            self.assertEqual(out["method"], "excerpt")
            self.assertFalse(rb._cache_path("NVDA").exists())


class Excerpt(unittest.TestCase):
    def test_short_text_returned_whole(self):
        self.assertEqual(rb._excerpt("  hi there  "), "hi there")

    def test_long_text_keeps_both_ends(self):
        body = ("HEAD " + "x " * 1000 + " TAILVERDICT")
        out = rb._excerpt(body)
        self.assertIn("HEAD", out)
        self.assertIn("TAILVERDICT", out)
        self.assertIn("[...]", out)
        self.assertLess(len(out), len(body))


class BuildBlock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self._orig = {k: getattr(rb, k) for k in ("DEEP_DIR", "ANALYSIS_DIR", "BRIEFS_DIR")}
        rb.DEEP_DIR = root / "deep"
        rb.ANALYSIS_DIR = root / "analysis"
        rb.BRIEFS_DIR = root / "briefs"
        rb.DEEP_DIR.mkdir(parents=True)
        rb.ANALYSIS_DIR.mkdir(parents=True)
        (rb.ANALYSIS_DIR / "AAA-2026-06-01.md").write_text("AAA analyst note body", encoding="utf-8")
        (rb.ANALYSIS_DIR / "CCC-2026-06-01.md").write_text("CCC analyst note body", encoding="utf-8")

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(rb, k, v)
        self.tmp.cleanup()

    def _rows(self):
        return [
            {"symbol": "AAA", "held_pct_nav": 5.0},   # narrative + mid weight
            {"symbol": "BBB", "held_pct_nav": 10.0},  # no narrative, top weight
            {"symbol": "CCC", "held_pct_nav": 1.0},   # narrative + low weight
        ]

    def test_empty_rows_yield_empty_block(self):
        self.assertEqual(rb.build_research_block([], _cfg(), use_llm=False), "")

    def test_orders_material_first_and_includes_all_under_budget(self):
        block = rb.build_research_block(self._rows(), _cfg(), use_llm=False)
        self.assertTrue(block.startswith("PER-NAME RESEARCH"))
        i_aaa, i_ccc, i_bbb = block.index("AAA"), block.index("CCC"), block.index("BBB")
        # Narratives lead (AAA before CCC by held weight), no-narrative BBB last.
        self.assertLess(i_aaa, i_ccc)
        self.assertLess(i_ccc, i_bbb)
        self.assertIn("research (analysis):", block)

    def test_tight_budget_drops_least_material_names(self):
        block = rb.build_research_block(self._rows(), _cfg(budget=40), use_llm=False)
        self.assertIn("AAA", block)        # most material survives
        self.assertNotIn("BBB", block)     # budget exhausted before it


if __name__ == "__main__":
    unittest.main()
