"""Tests for the portfolio risk lens: the pure correlation/vol/effective-bets/
stress math (fixtures, no network) and the report assembly with an injected
price fetcher so it never touches Yahoo."""

from __future__ import annotations

import math
import unittest

import _support  # noqa: F401
import risk


def closes_from_logrets(logrets, *, start=100.0):
    """Build a close series whose recovered log-returns equal ``logrets`` exactly,
    so betas/correlations are deterministic (ln is not linear, so cumulating raw
    percentages would not preserve a clean 2x relationship)."""
    points = [{"date": "2026-01-01", "close": round(start, 6)}]
    c = start
    for i, lr in enumerate(logrets, start=2):
        c = c * math.exp(lr)
        points.append({"date": f"2026-01-{i:02d}", "close": round(c, 6)})
    return points


BASE = [0.01, -0.02, 0.015, -0.01, 0.02, -0.005, 0.012, -0.018, 0.009, -0.011,
        0.014, -0.006, 0.008, -0.02, 0.017, -0.009, 0.011, -0.013, 0.006, -0.004]


class PureMath(unittest.TestCase):
    def test_log_returns(self):
        r = risk._log_returns([100.0, 110.0, 99.0])
        self.assertAlmostEqual(r[0], math.log(1.1))
        self.assertAlmostEqual(r[1], math.log(0.9))

    def test_pearson_perfect_and_inverse(self):
        a = [0.01, -0.02, 0.03, -0.01]
        self.assertAlmostEqual(risk._pearson(a, a), 1.0, places=6)
        self.assertAlmostEqual(risk._pearson(a, [-x for x in a]), -1.0, places=6)

    def test_pearson_flat_is_none(self):
        self.assertIsNone(risk._pearson([0.0, 0.0, 0.0], [0.1, 0.2, 0.3]))

    def test_beta_is_slope(self):
        f = [0.01, -0.02, 0.015, -0.01]
        a = [2 * x for x in f]
        self.assertAlmostEqual(risk._beta(a, f), 2.0, places=6)


class Analyze(unittest.TestCase):
    def test_perfectly_correlated_book_is_one_bet(self):
        series = {"A": closes_from_logrets(BASE), "B": closes_from_logrets(BASE)}
        out = risk.analyze({"A": 50.0, "B": 50.0}, series, scenarios=[])
        self.assertEqual(out["correlation"]["matrix"]["A"]["B"], 1.0)
        # Two identical bets => ~1 effective bet, half the variance is co-movement.
        self.assertAlmostEqual(out["metrics"]["effective_bets"], 1.0, places=2)
        self.assertAlmostEqual(out["metrics"]["covariance_share_pct"], 50.0, places=1)

    def test_anti_correlated_pair(self):
        series = {"A": closes_from_logrets(BASE), "B": closes_from_logrets([-x for x in BASE])}
        out = risk.analyze({"A": 50.0, "B": 50.0}, series, scenarios=[])
        self.assertEqual(out["correlation"]["matrix"]["A"]["B"], -1.0)

    def test_uncorrelated_names_give_more_than_one_bet(self):
        # Orthogonal square waves: zero correlation over full periods.
        a = [0.02, -0.02, 0.02, -0.02] * 5
        b = [0.02, 0.02, -0.02, -0.02] * 5
        series = {"A": closes_from_logrets(a), "B": closes_from_logrets(b)}
        out = risk.analyze({"A": 50.0, "B": 50.0}, series, scenarios=[])
        self.assertLess(abs(out["correlation"]["matrix"]["A"]["B"]), 0.2)
        self.assertGreater(out["metrics"]["effective_bets"], 1.3)

    def test_names_without_series_are_excluded_not_dropped_silently(self):
        series = {"A": closes_from_logrets(BASE)}
        out = risk.analyze({"A": 60.0, "B": 40.0}, series, scenarios=[])
        self.assertEqual(out["excluded"], ["B"])
        self.assertAlmostEqual(out["excluded_weight_pct"], 40.0)
        self.assertTrue(any("no usable price series" in c for c in out["caveats"]))

    def test_stress_uses_beta_and_weight(self):
        factor = BASE
        series = {"A": closes_from_logrets([2 * x for x in factor])}  # beta 2 to factor
        scen = [{"key": "k", "label": "shock", "factor": "F", "shock_pct": -10.0}]
        out = risk.analyze({"A": 100.0}, series, scenarios=scen,
                           factor_series={"F": closes_from_logrets(factor)})
        s = out["stress"][0]
        self.assertTrue(s["measurable"])
        self.assertAlmostEqual(s["contributions"][0]["beta"], 2.0, places=3)
        # norm weight 1.0 * beta 2 * shock -10 = -20% NAV
        self.assertAlmostEqual(s["nav_impact_pct"], -20.0, places=2)

    def test_stress_without_factor_series_is_not_measurable(self):
        series = {"A": closes_from_logrets(BASE)}
        scen = [{"key": "k", "label": "x", "factor": "MISSING", "shock_pct": -10.0}]
        out = risk.analyze({"A": 100.0}, series, scenarios=scen)
        self.assertFalse(out["stress"][0]["measurable"])
        self.assertIsNone(out["stress"][0]["nav_impact_pct"])

    def test_thin_sample_is_flagged(self):
        short = [0.01, -0.01, 0.02]
        out = risk.analyze({"A": 100.0}, {"A": closes_from_logrets(short)}, scenarios=[])
        self.assertFalse(out["enough_data"])
        self.assertTrue(any("overlapping observations" in c for c in out["caveats"]))


class RiskReport(unittest.TestCase):
    def test_report_uses_injected_fetch_no_network(self):
        holdings = {
            "generated_at": "2026-06-13T10:00:00+00:00",
            "base_currency": "CZK",
            "net_asset_value": 1000.0,
            "positions": [
                {"symbol": "A", "base_market_value": 600.0, "asset_class": "STK"},
                {"symbol": "B", "base_market_value": 400.0, "asset_class": "STK"},
            ],
        }
        bank = {"A": closes_from_logrets(BASE), "B": closes_from_logrets([-x for x in BASE])}

        def fake_fetch(symbol, rng):
            return bank.get(symbol)

        out = risk.risk_report(holdings, rng="1y", scenarios=[], fetch=fake_fetch)
        syms = out["correlation"]["symbols"]
        self.assertEqual(set(syms), {"A", "B"})
        self.assertEqual(out["range"], "1y")
        self.assertEqual(out["snapshot"], "2026-06-13T10:00:00+00:00")
        self.assertEqual(out["correlation"]["matrix"]["A"]["B"], -1.0)
        self.assertIsNotNone(out["metrics"]["portfolio_vol_pct"])

    def test_options_and_unfetchable_names_excluded(self):
        holdings = {
            "generated_at": "2026-06-13T10:00:00+00:00",
            "positions": [
                {"symbol": "A", "base_market_value": 500.0, "asset_class": "STK"},
                {"symbol": "SPY   260618P00655000", "base_market_value": 5.0, "asset_class": "OPT"},
                {"symbol": "NODATA", "base_market_value": 200.0, "asset_class": "STK"},
            ],
        }

        def fake_fetch(symbol, rng):
            return closes_from_logrets(BASE) if symbol == "A" else None

        out = risk.risk_report(holdings, rng="1y", scenarios=[], fetch=fake_fetch)
        self.assertEqual(out["correlation"]["symbols"], ["A"])
        self.assertIn("NODATA", out["excluded"])
        # The option leg is not researchable, so it never enters the weight set.
        self.assertNotIn("SPY   260618P00655000", out["excluded"])


if __name__ == "__main__":
    unittest.main()
