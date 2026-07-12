"""Tests for weight computation and target-band decision logic.

The broker's percent_of_nav lies for options; weights must be recomputed from
market value. Band status and decision labels drive every trim/add suggestion,
so they get exhaustive branch coverage."""

from __future__ import annotations

import datetime as dt
import unittest

import _support  # noqa: F401
import portfolio as pf


HOLDINGS = {
    "positions": [
        {"symbol": "AAA", "base_market_value": 100.0},
        {"symbol": "BBB", "base_market_value": 300.0},
        {"symbol": "OPT", "base_market_value": None},  # option-ish, no MV
    ]
}


class Weights(unittest.TestCase):
    def test_position_weight_is_mv_over_invested(self):
        self.assertEqual(pf.position_weight_pct({"base_market_value": 100.0}, 400.0), 25.0)

    def test_position_weight_none_when_no_mv_or_zero_invested(self):
        self.assertIsNone(pf.position_weight_pct({"base_market_value": None}, 400.0))
        self.assertIsNone(pf.position_weight_pct({"base_market_value": 100.0}, 0.0))

    def test_holdings_weights_recomputed_and_skips_null_mv(self):
        w = pf.holdings_weights(HOLDINGS)
        self.assertEqual(w["AAA"], 25.0)
        self.assertEqual(w["BBB"], 75.0)
        self.assertNotIn("OPT", w)

    def test_invested_value_sums_only_numeric(self):
        self.assertEqual(pf.invested_value(HOLDINGS["positions"]), 400.0)

    def test_stock_values_aggregate_rows_and_exclude_options(self):
        values = pf.stock_base_values({"positions": [
            {"symbol": "AAA", "base_market_value": 100.0},
            {"symbol": "aaa", "base_market_value": 50.0},
            {"symbol": "AAA  260717C00100000", "asset_class": "OPT",
             "base_market_value": 10.0},
        ]})
        self.assertEqual(values, {"AAA": 150.0})

    def test_stock_sell_violations_report_only_the_excess(self):
        violations = pf.stock_sell_violations(
            HOLDINGS,
            {"AAA": -125.0, "BBB": -250.0, "NEW": -10.0},
        )
        self.assertEqual(
            violations,
            [
                {
                    "symbol": "AAA",
                    "held_czk": 100.0,
                    "requested_sell_czk": 125.0,
                    "excess_czk": 25.0,
                    "after_czk": -25.0,
                },
                {
                    "symbol": "NEW",
                    "held_czk": 0.0,
                    "requested_sell_czk": 10.0,
                    "excess_czk": 10.0,
                    "after_czk": -10.0,
                },
            ],
        )


class OptionExposure(unittest.TestCase):
    # A real 2-lot SPY put: premium ~870 CZK, but ~9% of invested if exercised.
    PUT = {
        "symbol": "SPY   260618P00655000",
        "asset_class": "OPT",
        "quantity": 2.0,
        "mark_price": 0.2091,
        "market_value": 41.82,
        "base_market_value": 870.14874,
    }

    def test_parse_occ_symbol(self):
        self.assertEqual(pf.parse_occ_symbol("SPY   260618P00655000"), ("P", 655.0))

    def test_parse_occ_rejects_non_option(self):
        self.assertIsNone(pf.parse_occ_symbol("AMD"))
        self.assertIsNone(pf.parse_occ_symbol(None))

    def test_parse_occ_expiry(self):
        self.assertEqual(
            pf.parse_occ_expiry("SPY   260618P00655000"),
            dt.date(2026, 6, 18),
        )
        self.assertIsNone(pf.parse_occ_expiry("AMD"))
        self.assertIsNone(pf.parse_occ_expiry("SPY   269918P00655000"))

    def test_long_put_is_negative_exposure(self):
        o = pf.option_exposure(self.PUT, 29_536_352.0)
        self.assertEqual(o["right"], "P")
        self.assertEqual(o["multiplier"], 100)  # inferred from premium math, not hardcoded
        self.assertLess(o["exercise_pct"], 0)  # a long put is short on exercise
        self.assertAlmostEqual(o["exercise_pct"], -9.23, places=1)
        self.assertAlmostEqual(o["notional_base"], 2_725_705.0, delta=1000)

    def test_short_put_flips_sign(self):
        short = {**self.PUT, "quantity": -2.0}
        self.assertGreater(pf.option_exposure(short, 29_536_352.0)["exercise_pct"], 0)

    def test_none_without_invested(self):
        self.assertIsNone(pf.option_exposure(self.PUT, 0.0))


class PendingOptionExposure(unittest.TestCase):
    """Per-underlying options exposure the planner folds onto rebalance rows."""

    # A short put and a long put on the same name, plus a stock line so invested
    # is nonzero. base_market_value/market_value give the FX (CZK per USD).
    def _holdings(self):
        return {"positions": [
            {"symbol": "STK", "base_market_value": 10_000_000.0},
            {"symbol": "KLAC  260717P00238000", "asset_class": "OPT", "quantity": -2.0,
             "mark_price": 27.4615, "market_value": -5492.3, "base_market_value": -116650.96},
            {"symbol": "KLAC  260717P00200000", "asset_class": "OPT", "quantity": 1.0,
             "mark_price": 5.0, "market_value": 1000.0, "base_market_value": 21240.0},
        ]}

    def test_short_put_is_bullish_long_exposure(self):
        expo = pf.pending_option_exposure(self._holdings())
        self.assertIn("KLAC", expo)
        k = expo["KLAC"]
        self.assertGreater(k["long_pct"], 0)    # short put -> long on assignment
        self.assertGreater(k["short_pct"], 0)   # long put -> short/hedge
        self.assertEqual(k["contracts"], 3)     # 2 + 1, sign-agnostic count
        self.assertEqual(len(k["legs"]), 2)
        self.assertIn("short 2\u00d7 238P", k["label"])
        # net = long - short (both legs signed and summed; each rounded to 4dp)
        self.assertAlmostEqual(k["net_pct"], k["long_pct"] - k["short_pct"], places=2)

    def test_root_parsing_handles_padded_and_unpadded_occ(self):
        self.assertEqual(pf.option_root("KLAC  260717P00238000"), "KLAC")
        self.assertEqual(pf.option_root("KLAC260717P00238000"), "KLAC")

    def test_no_options_is_empty(self):
        self.assertEqual(pf.pending_option_exposure({"positions": [
            {"symbol": "AAA", "base_market_value": 100.0}]}), {})


class PositionFx(unittest.TestCase):
    def test_prefers_explicit_rate(self):
        self.assertEqual(pf.position_fx_to_base({
            "fx_rate_to_base": 23.5,
            "market_value": 100,
            "base_market_value": 2000,
        }), 23.5)

    def test_derives_rate_from_paired_values(self):
        self.assertEqual(pf.position_fx_to_base({
            "market_value": 100,
            "base_market_value": 2350,
        }), 23.5)

    def test_incomplete_position_falls_back_to_one(self):
        self.assertEqual(pf.position_fx_to_base({"market_value": 0}), 1.0)


class TargetContext(unittest.TestCase):
    MODEL = {
        "targets": {"AMD": {"low": 10, "high": 12, "rule": "trim_only"}},
        "sleeves": {"fintech": {"low": 5, "high": 8, "rule": "accumulate",
                                "members": ["SOFI", "PYPL"]}},
    }

    def test_top_level_target(self):
        ctx = pf.target_context(self.MODEL, "AMD")
        self.assertEqual(ctx["kind"], "target")
        self.assertEqual(ctx["rule"], "trim_only")

    def test_sleeve_membership(self):
        ctx = pf.target_context(self.MODEL, "SOFI")
        self.assertEqual(ctx["kind"], "sleeve")
        self.assertEqual(ctx["sleeve"], "fintech")
        self.assertNotIn("members", ctx)  # membership list stripped from the view

    def test_unmanaged_symbol(self):
        self.assertEqual(pf.target_context(self.MODEL, "TSLA")["kind"], "none")


class BandStatus(unittest.TestCase):
    MODEL = {"targets": {"X": {"low": 10, "high": 12, "rule": "trim_only"}}}

    def _ctx(self, weight):
        holdings = {"positions": [{"symbol": "X", "base_market_value": weight},
                                  {"symbol": "_", "base_market_value": 100.0 - weight}]}
        return pf.portfolio_context("X", holdings=holdings, model=self.MODEL)

    def test_above_band(self):
        ctx = self._ctx(14.7)
        self.assertEqual(ctx["status"], "above_band")
        self.assertLess(ctx["gap_to_band_pct"], 0)  # high - current, negative when over

    def test_below_band(self):
        ctx = self._ctx(5.0)
        self.assertEqual(ctx["status"], "below_band")
        self.assertAlmostEqual(ctx["gap_to_band_pct"], 5.0)  # low - current

    def test_in_band(self):
        ctx = self._ctx(11.0)
        self.assertEqual(ctx["status"], "in_band")
        self.assertEqual(ctx["gap_to_band_pct"], 0.0)

    def test_held_no_target(self):
        holdings = {"positions": [{"symbol": "Z", "base_market_value": 50.0, "quantity": 12},
                                  {"symbol": "_", "base_market_value": 50.0}]}
        ctx = pf.portfolio_context("Z", holdings=holdings, model={"targets": {}})
        self.assertEqual(ctx["status"], "held_no_target")
        self.assertEqual(ctx["current_quantity"], 12)

    def test_not_held(self):
        ctx = pf.portfolio_context("NOPE", holdings={"positions": []}, model={"targets": {}})
        self.assertEqual(ctx["status"], "not_held")
        self.assertIsNone(ctx["current_quantity"])


class DecisionLabel(unittest.TestCase):
    def lbl(self, rule, status):
        return pf.decision_label({"target": {"rule": rule}, "status": status})

    def test_avoid(self):
        self.assertEqual(self.lbl("avoid", "in_band"), "avoid")

    def test_reduce_is_trim(self):
        self.assertEqual(self.lbl("reduce", "above_band"), "trim")

    def test_trim_only_above_band_is_trim(self):
        self.assertEqual(self.lbl("trim_only", "above_band"), "trim")

    def test_trim_only_in_band_is_hold(self):
        self.assertEqual(self.lbl("trim_only", "in_band"), "hold")

    def test_accumulate_below_band_is_add_candidate(self):
        self.assertEqual(self.lbl("accumulate", "below_band"), "add_candidate")

    def test_accumulate_in_band_is_accumulate(self):
        self.assertEqual(self.lbl("accumulate", "in_band"), "accumulate")

    def test_wait_is_watch(self):
        self.assertEqual(self.lbl("wait", "in_band"), "watch")

    def test_unknown_rule_is_research(self):
        self.assertEqual(self.lbl(None, "not_held"), "research")


if __name__ == "__main__":
    unittest.main()
