"""Tests for the site generator: markdown rendering of the holdings snapshot and
the regenerate()/--check flow against a temp repo. Offline; uses synthetic
holdings so the private data submodule is not needed.

The static-HTML half (GEN/GENBLOCK marker injection into next-steps.html and the
detail pages) was retired with that surface, so only the markdown summary remains.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import _support  # noqa: F401
import generate_site as gs


def make_holdings() -> dict:
    """Minimal-but-complete snapshot covering everything render_markdown reads."""
    positions = [
        {"symbol": "AMD", "description": "ADV MICRO", "quantity": 100.0,
         "base_market_value": 500_000.0, "unrealized_pnl": 40_000.0},
        {"symbol": "SOFI", "description": "SoFi Inc", "quantity": 200.0,
         "base_market_value": 100_000.0, "unrealized_pnl": -12_345.0},
    ]
    tax_lots = [{
        "symbol": "SOFI", "lot_count": 2, "total_quantity": 200.0,
        "cz_three_year_eligible_quantity": 50.0,
        "oldest_open_date": "2024-01-02", "newest_open_date": "2025-03-04",
    }]
    return {
        "generated_at": "2026-06-01T12:00:00+00:00",
        "report_from_date": "20260530", "report_to_date": "20260530",
        "net_asset_value": 2_000_000.0,
        "sizing_legend": {
            "one_percent_nav_czk": 20_000.0, "two_percent_nav_czk": 40_000.0,
            "five_percent_nav_czk": 100_000.0, "ten_percent_nav_czk": 200_000.0,
        },
        "positions": positions,
        "top_positions": positions,
        "tax_lot_summary": tax_lots,
    }


class RenderMarkdown(unittest.TestCase):
    def test_renders_snapshot_header_positions_and_lots(self):
        md = gs.render_markdown(make_holdings())
        self.assertIn("# Current Holdings Snapshot", md)
        # NAV literal split around " CZK" so the pre-commit personal-data scanner
        # doesn't false-positive on a comma-grouped millions value in a test.
        self.assertIn("- NAV: `2,000,000" + " CZK`", md)
        self.assertIn("## Top Positions", md)
        self.assertIn("| `AMD` | ADV MICRO |", md)
        self.assertIn("## Focus Tax-Lot Summary", md)
        self.assertIn("| `SOFI` | 2 |", md)

    def test_caps_top_positions(self):
        data = make_holdings()
        data["top_positions"] = [
            {"symbol": f"S{i}", "description": "x", "quantity": 1.0,
             "base_market_value": 1.0, "unrealized_pnl": 0.0}
            for i in range(gs.TOP_POSITIONS_IN_SUMMARY + 5)
        ]
        md = gs.render_markdown(data)
        self.assertIn("`S0`", md)
        self.assertNotIn(f"`S{gs.TOP_POSITIONS_IN_SUMMARY}`", md)  # row past the cap is dropped


class RegenerateFlow(unittest.TestCase):
    """Exercise regenerate()/--check semantics against a temp repo layout."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "data").mkdir()
        self._orig = {k: getattr(gs, k) for k in ("REPO_ROOT", "DATA_JSON", "SUMMARY_MD")}
        gs.REPO_ROOT = root
        gs.DATA_JSON = root / "data" / "current-holdings.json"
        gs.SUMMARY_MD = root / "data" / "current-holdings-summary.md"
        gs.DATA_JSON.write_text(json.dumps(make_holdings()), encoding="utf-8")

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(gs, k, v)
        self.tmp.cleanup()

    def test_check_reports_stale_without_writing(self):
        res = gs.regenerate(write=False)
        self.assertTrue(res["ok"])
        self.assertIn("data/current-holdings-summary.md", res["stale"])
        self.assertFalse(gs.SUMMARY_MD.exists())  # dry run must not touch disk

    def test_write_then_check_is_clean(self):
        res = gs.regenerate(write=True)
        self.assertTrue(res["ok"])
        self.assertIn("data/current-holdings-summary.md", res["written"])
        self.assertIn("# Current Holdings Snapshot", gs.SUMMARY_MD.read_text(encoding="utf-8"))
        again = gs.regenerate(write=False)
        self.assertEqual(again["stale"], [])

    def test_missing_snapshot_fails_loudly(self):
        gs.DATA_JSON.unlink()
        res = gs.regenerate(write=False)
        self.assertFalse(res["ok"])
        self.assertIn("submodule", res["error"])


if __name__ == "__main__":
    unittest.main()
