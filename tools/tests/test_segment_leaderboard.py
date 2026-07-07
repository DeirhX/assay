"""Tests for segments_service.leaderboard -- the ranked "which segment is showing
the most promise, and am I positioned for it?" aggregator.

Covers: median momentum / breadth / valuation-vs-growth from fixture caches,
live-exposure overlay (holdings_weights, NOT the cached owned_pct_nav), the
transparent rank-sum score and its ordering, basket-plan exclusion, skipping
uncached segments, overlap detection, staleness, and null/degenerate fields.
"""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _support  # noqa: F401
import segments_service


def _iso(days_ago: int = 0) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_ago)).isoformat()


class LeaderboardBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.def_dir = root / "def"
        self.out_dir = root / "out"
        self.def_dir.mkdir()
        self.out_dir.mkdir()
        p_def = mock.patch.object(segments_service, "SEGMENT_DEF_DIR", self.def_dir)
        p_out = mock.patch.object(segments_service, "SEGMENT_OUT_DIR", self.out_dir)
        p_def.start()
        p_out.start()
        self.addCleanup(p_def.stop)
        self.addCleanup(p_out.stop)
        self.addCleanup(self._tmp.cleanup)

    def write(self, slug: str, definition: dict, cache: dict | None) -> None:
        (self.def_dir / f"{slug}.json").write_text(json.dumps(definition), encoding="utf-8")
        if cache is not None:
            (self.out_dir / f"{slug}.json").write_text(json.dumps(cache), encoding="utf-8")

    def run_lb(self, held: dict[str, float] | None = None) -> dict:
        with mock.patch.object(segments_service, "holdings_weights", return_value=held or {}):
            return segments_service.leaderboard()


class Aggregation(LeaderboardBase):
    def test_medians_breadth_valgrowth(self):
        self.write(
            "alpha",
            {"title": "Alpha", "members": [{"symbol": "AAA"}, {"symbol": "BBB"}, {"symbol": "CCC"}]},
            {
                "as_of": _iso(1),
                "members": [
                    {"symbol": "AAA", "chg_3m_pct": 10.0, "chg_12m_pct": 20.0, "pe_fwd": 20.0, "rev_growth_yoy_pct": 10.0},
                    {"symbol": "BBB", "chg_3m_pct": 30.0, "chg_12m_pct": 40.0, "pe_fwd": 30.0, "rev_growth_yoy_pct": 30.0},
                    {"symbol": "CCC", "chg_3m_pct": -5.0, "chg_12m_pct": 5.0},
                ],
            },
        )
        row = self.run_lb()["segments"][0]
        self.assertEqual(row["segment"], "alpha")
        self.assertEqual(row["title"], "Alpha")
        self.assertEqual(row["member_count"], 3)
        self.assertEqual(row["momentum_3m_med"], 10.0)   # median of [-5, 10, 30]
        self.assertEqual(row["momentum_12m_med"], 20.0)
        # breadth: 2 of 3 members have positive 3M momentum
        self.assertAlmostEqual(row["breadth_3m"], 2 / 3, places=3)
        # val/growth median over the two members with both fields: [2.0, 1.0] -> 1.5
        self.assertEqual(row["val_growth_med"], 1.5)
        self.assertEqual(row["val_growth_coverage"], 2)
        self.assertFalse(row["stale"])

    def test_exposure_uses_live_holdings_not_cached(self):
        self.write(
            "alpha",
            {"members": [{"symbol": "AAA"}, {"symbol": "BBB"}]},
            {
                "as_of": _iso(0),
                "members": [
                    {"symbol": "AAA", "chg_3m_pct": 1.0, "owned_pct_nav": 99.0},
                    {"symbol": "BBB", "chg_3m_pct": 1.0, "owned_pct_nav": 99.0},
                ],
            },
        )
        row = self.run_lb(held={"AAA": 4.0, "BBB": 1.5, "ZZZ": 10.0})["segments"][0]
        self.assertEqual(row["exposure_pct"], 5.5)   # 4.0 + 1.5, cached 99s ignored
        self.assertEqual(row["held_count"], 2)

    def test_score_orders_by_momentum_and_breadth(self):
        # hot: high momentum + full breadth; cold: low momentum + weak breadth.
        self.write(
            "hot",
            {"members": [{"symbol": "H1"}, {"symbol": "H2"}]},
            {"as_of": _iso(0), "members": [
                {"symbol": "H1", "chg_3m_pct": 40.0}, {"symbol": "H2", "chg_3m_pct": 50.0},
            ]},
        )
        self.write(
            "cold",
            {"members": [{"symbol": "C1"}, {"symbol": "C2"}]},
            {"as_of": _iso(0), "members": [
                {"symbol": "C1", "chg_3m_pct": -10.0}, {"symbol": "C2", "chg_3m_pct": 5.0},
            ]},
        )
        segs = self.run_lb()["segments"]
        self.assertEqual(segs[0]["segment"], "hot")
        self.assertEqual(segs[1]["segment"], "cold")
        self.assertGreater(segs[0]["score"], segs[1]["score"])


class Exclusions(LeaderboardBase):
    def test_basket_plan_excluded(self):
        self.write(
            "basket-plan-2026-06-29",
            {"title": "Basket plan", "origin": {"type": "basket"}, "members": [{"symbol": "AAA"}]},
            {"as_of": _iso(0), "members": [{"symbol": "AAA", "chg_3m_pct": 99.0}]},
        )
        self.write(
            "real",
            {"members": [{"symbol": "BBB"}]},
            {"as_of": _iso(0), "members": [{"symbol": "BBB", "chg_3m_pct": 1.0}]},
        )
        segs = self.run_lb()["segments"]
        self.assertEqual([s["segment"] for s in segs], ["real"])

    def test_uncached_segment_skipped(self):
        self.write("nocache", {"members": [{"symbol": "AAA"}]}, None)
        self.write("empty", {"members": []}, {"as_of": _iso(0), "members": []})
        self.write("real", {"members": [{"symbol": "BBB"}]},
                   {"as_of": _iso(0), "members": [{"symbol": "BBB", "chg_3m_pct": 1.0}]})
        segs = self.run_lb()["segments"]
        self.assertEqual([s["segment"] for s in segs], ["real"])


class EdgeCases(LeaderboardBase):
    def test_missing_and_nonnumeric_fields(self):
        self.write(
            "sparse",
            {"members": [{"symbol": "AAA"}, {"symbol": "BBB"}]},
            {"as_of": _iso(0), "members": [
                {"symbol": "AAA", "chg_3m_pct": None, "pe_fwd": "n/a", "rev_growth_yoy_pct": 0.0},
                {"symbol": "BBB"},
            ]},
        )
        row = self.run_lb()["segments"][0]
        self.assertIsNone(row["momentum_3m_med"])
        self.assertIsNone(row["momentum_12m_med"])
        self.assertIsNone(row["breadth_3m"])
        self.assertIsNone(row["val_growth_med"])
        self.assertEqual(row["val_growth_coverage"], 0)   # growth 0 excluded (no div-by-zero)
        self.assertEqual(row["score"], 0)                 # no rankable metric

    def test_overlap_flag(self):
        self.write("a", {"members": [{"symbol": "SHARED"}]},
                   {"as_of": _iso(0), "members": [{"symbol": "SHARED", "chg_3m_pct": 1.0}]})
        self.write("b", {"members": [{"symbol": "SHARED"}]},
                   {"as_of": _iso(0), "members": [{"symbol": "SHARED", "chg_3m_pct": 2.0}]})
        self.assertTrue(self.run_lb()["overlap"])

    def test_no_overlap_flag(self):
        self.write("a", {"members": [{"symbol": "AAA"}]},
                   {"as_of": _iso(0), "members": [{"symbol": "AAA", "chg_3m_pct": 1.0}]})
        self.write("b", {"members": [{"symbol": "BBB"}]},
                   {"as_of": _iso(0), "members": [{"symbol": "BBB", "chg_3m_pct": 2.0}]})
        self.assertFalse(self.run_lb()["overlap"])

    def test_stale_flag_from_old_as_of(self):
        self.write("old", {"members": [{"symbol": "AAA"}]},
                   {"as_of": _iso(90), "members": [{"symbol": "AAA", "chg_3m_pct": 1.0}]})
        row = self.run_lb()["segments"][0]
        self.assertTrue(row["stale"])

    def test_empty_when_no_segments(self):
        out = self.run_lb()
        self.assertEqual(out["segments"], [])
        self.assertFalse(out["overlap"])
        self.assertEqual(out["stale_days"], 45)


if __name__ == "__main__":
    unittest.main()
