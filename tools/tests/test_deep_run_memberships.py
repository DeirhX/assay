from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import _support  # noqa: F401
import deep_runs


class DeepRunMemberships(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.deep = self.root / "data" / "research" / "deep"
        self.segments = self.root / "data" / "segments"
        self.deep.mkdir(parents=True)
        self.segments.mkdir(parents=True)
        self.orig = (deep_runs.DEEP_DIR, deep_runs.SEGMENT_DEF_DIR, deep_runs.REPO_ROOT)
        deep_runs.DEEP_DIR = self.deep
        deep_runs.SEGMENT_DEF_DIR = self.segments
        deep_runs.REPO_ROOT = self.root
        self._segment("semiconductors", "Semiconductors", ["NVDA", "AMD"])
        self._segment("ai-infrastructure", "AI Infrastructure", ["MSFT"])
        self._report(
            "semiconductors-2026-07-01",
            "The segment includes $NVDA as a core accelerator supplier.",
            sources=3,
        )
        self._report(
            "ai-infrastructure-2026-07-02",
            "Cloud capex increasingly depends on $NVDA systems.",
            sources=5,
        )
        self._report(
            "ticker-nvda-2026-07-03",
            "A ticker-specific report about $NVDA.",
            sources=7,
        )

    def tearDown(self):
        deep_runs.DEEP_DIR, deep_runs.SEGMENT_DEF_DIR, deep_runs.REPO_ROOT = self.orig
        self.tmp.cleanup()

    def _segment(self, slug: str, title: str, symbols: list[str]):
        (self.segments / f"{slug}.json").write_text(
            json.dumps({
                "title": title,
                "members": [{"symbol": symbol} for symbol in symbols],
            }),
            encoding="utf-8",
        )

    def _report(self, stem: str, text: str, *, sources: int):
        (self.deep / f"{stem}.md").write_text(text, encoding="utf-8")
        (self.deep / f"{stem}.sources.json").write_text(
            json.dumps({"citations": [{"href": f"https://example.com/{i}"} for i in range(sources)]}),
            encoding="utf-8",
        )

    def test_returns_overlapping_sector_reports_and_relationship(self):
        rows = deep_runs.deep_runs_for_symbol("NVDA")

        self.assertEqual(
            [row["stem"] for row in rows],
            ["ai-infrastructure-2026-07-02", "semiconductors-2026-07-01"],
        )
        self.assertEqual(rows[0]["relationship"], "mentioned")
        self.assertEqual(rows[0]["source_count"], 5)
        self.assertEqual(rows[1]["relationship"], "member+mentioned")
        self.assertNotIn("ticker-nvda-2026-07-03", [row["stem"] for row in rows])

    def test_includes_defined_segment_member_even_without_explicit_mention(self):
        rows = deep_runs.deep_runs_for_symbol("AMD")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Semiconductors")
        self.assertEqual(rows[0]["relationship"], "member")

    def test_normalizes_exchange_punctuation_and_unknown_symbols(self):
        self._segment("foreign", "Foreign", ["TUI1.DE"])
        self._report("foreign-2026-07-04", "Travel demand remains cyclical.", sources=1)

        self.assertEqual(
            deep_runs.deep_runs_for_symbol("TUI1-DE")[0]["relationship"],
            "member",
        )
        self.assertEqual(deep_runs.deep_runs_for_symbol("NOPE"), [])


if __name__ == "__main__":
    unittest.main()
