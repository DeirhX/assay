"""Advisory within-segment opportunity-cost ranking."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import _support  # noqa: F401
import opportunity_cost as oc


class Prospect(unittest.TestCase):
    def test_explicit_prospect(self):
        self.assertEqual(oc.prospect_from_row({"prospect": 72}), 72.0)

    def test_conviction_map(self):
        self.assertEqual(oc.prospect_from_row({"conviction": "high"}), 85.0)
        self.assertEqual(oc.prospect_from_row({"conviction": "avoid"}), 10.0)

    def test_research_score_fallback(self):
        self.assertEqual(oc.prospect_from_row({"research_score": 55}), 55.0)


class Rank(unittest.TestCase):
    def test_orders_by_oc_and_sets_dense_rank(self):
        rows = [
            {"symbol": "RICH", "research_score": 90, "ps": 40.0, "pe_fwd": 50.0},
            {"symbol": "CHEAP", "research_score": 70, "ps": 5.0, "pe_fwd": 12.0},
            {"symbol": "MID", "research_score": 80, "ps": 12.0, "pe_fwd": 20.0},
        ]
        ranked = oc.rank_members(rows)
        by_sym = {r["symbol"]: r for r in ranked}
        # Cheap should beat rich despite lower raw score (valuation drag).
        self.assertLess(by_sym["CHEAP"]["oc_rank"], by_sym["RICH"]["oc_rank"])
        self.assertEqual({r["oc_rank"] for r in ranked if r["oc_rank"]}, {1, 2, 3})

    def test_data_quality_error_penalized(self):
        rows = [
            {"symbol": "CLEAN", "research_score": 60, "data_quality": "INFO"},
            {"symbol": "DIRTY", "research_score": 90, "data_quality": "ERROR"},
        ]
        ranked = oc.rank_members(rows)
        by_sym = {r["symbol"]: r for r in ranked}
        self.assertLess(by_sym["CLEAN"]["oc_rank"], by_sym["DIRTY"]["oc_rank"])

    def test_unscored_trails(self):
        rows = [
            {"symbol": "A", "research_score": 50},
            {"symbol": "B"},  # no signal
        ]
        ranked = oc.rank_members(rows)
        self.assertEqual(ranked[0]["symbol"], "A")
        self.assertIsNone(ranked[1]["oc_rank"])

    def test_annotate_stamps_home(self):
        rows = [{"symbol": "TXN", "research_score": 60}]
        out = oc.annotate_segment_members(rows, home_by_symbol={"TXN": "analog"})
        self.assertEqual(out[0]["home_segment"], "analog")
        self.assertEqual(out[0]["oc_rank"], 1)


class PersistRanks(unittest.TestCase):
    def test_store_and_load_sleeve_ranks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "oc-ranks.json"
            oc.store_sleeve_ranks(
                "analog",
                [{"symbol": "TXN", "oc_rank": 1, "oc_score": 12.0, "prospect": 85}],
                path=path,
            )
            loaded = oc.ranks_for_sleeve("analog", path=path)
            self.assertEqual(loaded["TXN"]["oc_rank"], 1)
            self.assertEqual(loaded["TXN"]["prospect"], 85)


if __name__ == "__main__":
    unittest.main()
