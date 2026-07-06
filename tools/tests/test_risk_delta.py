"""Tests for the pre-trade risk delta. Concentration + effective-names are pure
weight math; the correlation-aware path delegates to risk.analyze when a series is
supplied. Pre-flight warnings fire on the concentration thresholds."""

from __future__ import annotations

import unittest

import _support  # noqa: F401
import risk_delta


class Concentration(unittest.TestCase):
    def test_top5_and_effective_names(self):
        # Five equal 20% names -> top5 100%, effective names ~5.
        c = risk_delta.concentration({s: 20.0 for s in "ABCDE"})
        self.assertAlmostEqual(c["top5_pct"], 100.0)
        self.assertAlmostEqual(c["effective_names"], 5.0, places=1)
        self.assertEqual(c["n"], 5)

    def test_one_dominant_name_collapses_effective_names(self):
        c = risk_delta.concentration({"BIG": 90.0, "a": 5.0, "b": 5.0})
        self.assertAlmostEqual(c["top1_pct"], 90.0)
        self.assertLess(c["effective_names"], 1.3)   # behaves like ~1 position

    def test_empty_book(self):
        c = risk_delta.concentration({})
        self.assertEqual(c["top5_pct"], 0.0)
        self.assertEqual(c["effective_names"], 0.0)

    def test_ignores_zero_and_negative_weights(self):
        c = risk_delta.concentration({"A": 50.0, "B": 0.0, "C": -3.0})
        self.assertEqual(c["n"], 1)


class Delta(unittest.TestCase):
    def test_reports_before_after_delta(self):
        before = {"A": 30.0, "B": 20.0, "C": 10.0}
        after = {"A": 40.0, "B": 20.0, "C": 10.0}   # A grew
        d = risk_delta.delta(before, after)
        self.assertFalse(d["has_correlation"])       # no series supplied
        self.assertEqual(d["top1_pct"]["before"], 30.0)
        self.assertEqual(d["top1_pct"]["after"], 40.0)
        self.assertAlmostEqual(d["top1_pct"]["delta"], 10.0)

    def test_correlation_path_runs_when_series_supplied(self):
        # Two perfectly-correlated names: effective bets should be well under the
        # name count. Just assert the block appears and is populated.
        series = {
            "A": [{"date": f"2026-01-{i:02d}", "close": 100 + i} for i in range(1, 20)],
            "B": [{"date": f"2026-01-{i:02d}", "close": 200 + 2 * i} for i in range(1, 20)],
        }
        d = risk_delta.delta({"A": 50.0, "B": 50.0}, {"A": 60.0, "B": 40.0}, series=series)
        self.assertTrue(d["has_correlation"])
        self.assertIn("effective_bets", d)
        self.assertIn("portfolio_vol_pct", d)


class Warnings(unittest.TestCase):
    def test_flags_a_material_concentration_jump(self):
        before = {"A": 30.0, "B": 10.0, "C": 8.0, "D": 6.0, "E": 4.0}   # top5 58
        after = {"A": 36.0, "B": 10.0, "C": 8.0, "D": 6.0, "E": 4.0}    # top5 64, +6pp
        d = risk_delta.delta(before, after)
        self.assertTrue(any("top-5 concentration" in w for w in d["warnings"]))

    def test_flags_crossing_the_ceiling(self):
        before = {"A": 40.0, "B": 12.0, "C": 6.0, "D": 3.0, "E": 1.0}   # top5 62 already
        after = {"A": 41.0, "B": 12.0, "C": 6.0, "D": 3.0, "E": 1.0}    # 63, still up
        d = risk_delta.delta(before, after)
        self.assertTrue(any("past the" in w for w in d["warnings"]))

    def test_no_warning_when_concentration_eases(self):
        before = {"A": 40.0, "B": 10.0}
        after = {"A": 30.0, "B": 10.0, "C": 10.0}   # spread out
        d = risk_delta.delta(before, after)
        self.assertEqual(d["warnings"], [])


if __name__ == "__main__":
    unittest.main()
