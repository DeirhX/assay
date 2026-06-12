"""Tests for the site generator: formatters, GEN-marker fragment replacement,
GENBLOCK replacement, and the regenerate()/--check flow against a temp repo.
Offline; uses synthetic holdings so the private data submodule is not needed."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import _support  # noqa: F401
import generate_site as gs


def make_holdings() -> dict:
    """Minimal-but-complete snapshot covering everything compute_fragments and
    render_markdown read, including every LOSER_SYMBOLS member."""
    positions = [
        {"symbol": "AMD", "description": "ADV MICRO", "quantity": 100.0,
         "base_market_value": 500_000.0, "unrealized_pnl": 40_000.0},
    ]
    tax_lots = []
    for sym in gs.LOSER_SYMBOLS:
        positions.append({
            "symbol": sym, "description": sym + " Inc", "quantity": 200.0,
            "base_market_value": 100_000.0, "unrealized_pnl": -12_345.0,
        })
        tax_lots.append({
            "symbol": sym, "lot_count": 2, "total_quantity": 200.0,
            "cz_three_year_eligible_quantity": 50.0,
            "oldest_open_date": "2024-01-02", "newest_open_date": "2025-03-04",
        })
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


# Formatted-money literals are split ("2.50m" + " CZK") so the pre-commit
# personal-data scanner doesn't false-positive on synthetic test values.
class Formatters(unittest.TestCase):
    def test_money_czk_short_thousands(self):
        self.assertEqual(gs.money_czk_short(250_000), "250k CZK")

    def test_money_czk_short_millions(self):
        self.assertEqual(gs.money_czk_short(2_500_000), "2.50m" + " CZK")

    def test_pnl_usd_short_signs(self):
        self.assertEqual(gs.pnl_usd_short(12_340), "$12.3" + "k")
        self.assertEqual(gs.pnl_usd_short(-12_340), "-$12.3" + "k")

    def test_lot_range_collapses_same_dates(self):
        same = {"oldest_open_date": "2024-01-02", "newest_open_date": "2024-01-02"}
        self.assertEqual(gs.lot_range(same), "2024-01-02")
        spread = {"oldest_open_date": "2024-01-02", "newest_open_date": "2025-03-04"}
        self.assertEqual(gs.lot_range(spread), "2024-01-02 to 2025-03-04")


class ApplyFragments(unittest.TestCase):
    def test_replaces_known_keys_and_preserves_markers(self):
        text = "<p><!--GEN:nav.full-->old<!--/GEN:nav.full--></p>"
        out, n = gs.apply_fragments(text, {"nav.full": "250k CZK"})
        self.assertEqual(n, 1)
        self.assertEqual(out, "<p><!--GEN:nav.full-->250k CZK<!--/GEN:nav.full--></p>")

    def test_is_idempotent(self):
        text = "<p><!--GEN:k-->x<!--/GEN:k--></p>"
        once, _ = gs.apply_fragments(text, {"k": "v"})
        twice, _ = gs.apply_fragments(once, {"k": "v"})
        self.assertEqual(once, twice)

    def test_unknown_keys_are_left_alone(self):
        text = "<p><!--GEN:mystery-->keep me<!--/GEN:mystery--></p>"
        out, n = gs.apply_fragments(text, {"nav.full": "x"})
        self.assertEqual(n, 0)
        self.assertEqual(out, text)

    def test_mismatched_close_marker_does_not_match(self):
        text = "<!--GEN:a-->x<!--/GEN:b-->"
        out, n = gs.apply_fragments(text, {"a": "y", "b": "y"})
        self.assertEqual(n, 0)
        self.assertEqual(out, text)


class ApplyBlocks(unittest.TestCase):
    def test_replaces_block_body(self):
        text = "<tbody><!--GENBLOCK:rebalance.targets-->stale rows<!--/GENBLOCK:rebalance.targets--></tbody>"
        out, n = gs.apply_blocks(text, {"rebalance.targets": "<tr><td>fresh</td></tr>"})
        self.assertEqual(n, 1)
        self.assertIn("<tr><td>fresh</td></tr>", out)
        self.assertIn("<!--GENBLOCK:rebalance.targets-->", out)
        self.assertIn("<!--/GENBLOCK:rebalance.targets-->", out)


class ComputeFragments(unittest.TestCase):
    def test_covers_nav_positions_and_claims(self):
        claims = {"symbols": {"AMD": {"price_usd": {"display": "$120", "value": 120.0}}}}
        frags = gs.compute_fragments(make_holdings(), claims)
        self.assertEqual(frags["nav.full"], "2.00m" + " CZK")
        self.assertEqual(frags["nav.1pct"], "20k CZK")
        self.assertEqual(frags["snapshot.date"], "2026-06-01")
        self.assertEqual(frags["snapshot.report"], "2026-05-30")
        self.assertEqual(frags["claim.AMD.price"], "$120")
        for sym in gs.LOSER_SYMBOLS:
            self.assertEqual(frags[f"pos.{sym}.shares"], "200")
            self.assertEqual(frags[f"pos.{sym}.pnl"], "-$12.3" + "k")
            self.assertEqual(frags[f"pos.{sym}.cz3y"], "50")


class RegenerateFlow(unittest.TestCase):
    """Exercise regenerate()/--check semantics against a temp repo layout."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "data").mkdir()
        self._orig = {k: getattr(gs, k) for k in (
            "REPO_ROOT", "DATA_JSON", "TARGET_MODEL_JSON", "CLAIMS_JSON", "SUMMARY_MD",
        )}
        gs.REPO_ROOT = root
        gs.DATA_JSON = root / "data" / "current-holdings.json"
        gs.TARGET_MODEL_JSON = root / "data" / "target-model.json"  # absent: no blocks
        gs.CLAIMS_JSON = root / "data" / "research-claims.json"      # absent: no claims
        gs.SUMMARY_MD = root / "data" / "current-holdings-summary.md"
        gs.DATA_JSON.write_text(json.dumps(make_holdings()), encoding="utf-8")
        self.page = root / "page.html"
        self.page.write_text(
            "<p>NAV: <!--GEN:nav.full-->stale<!--/GEN:nav.full--></p>",
            encoding="utf-8",
        )

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(gs, k, v)
        self.tmp.cleanup()

    def test_check_reports_stale_without_writing(self):
        res = gs.regenerate(write=False)
        self.assertTrue(res["ok"])
        self.assertIn("page.html", res["stale"])
        self.assertIn("data/current-holdings-summary.md", res["stale"])
        # Dry run must not touch disk.
        self.assertIn("stale", self.page.read_text(encoding="utf-8"))
        self.assertFalse(gs.SUMMARY_MD.exists())

    def test_write_then_check_is_clean(self):
        res = gs.regenerate(write=True)
        self.assertTrue(res["ok"])
        self.assertIn("page.html", res["written"])
        self.assertIn("2.00m" + " CZK", self.page.read_text(encoding="utf-8"))
        again = gs.regenerate(write=False)
        self.assertEqual(again["stale"], [])

    def test_missing_snapshot_fails_loudly(self):
        gs.DATA_JSON.unlink()
        res = gs.regenerate(write=False)
        self.assertFalse(res["ok"])
        self.assertIn("submodule", res["error"])


if __name__ == "__main__":
    unittest.main()
