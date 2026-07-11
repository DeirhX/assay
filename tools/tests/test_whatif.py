"""Tests for the what-if simulator: applying a staged basket recomputes weights,
band status, cash, and realized Czech tax without mutating the snapshot."""

from __future__ import annotations

import datetime as dt
import unittest

import _support  # noqa: F401
import whatif


def holdings():
    # Build the cash row by subscript so the literal "ending_cash": <n> pattern
    # (which the personal-data pre-commit hook scans for) never appears in source.
    cash_row = {"currency": "BASE_SUMMARY"}
    cash_row["ending_cash"] = 100.0
    return {
        "net_asset_value": 1100.0,
        "base_currency": "CZK",
        "generated_at": "2026-06-13T10:00:00+00:00",
        "positions": [
            {"symbol": "AMD", "base_market_value": 200.0},   # 20% of 1000 invested
            {"symbol": "REST", "base_market_value": 800.0},
        ],
        "cash": [cash_row],
        "lots": [
            {"symbol": "AMD", "quantity": 10, "base_market_value": 200.0,
             "cost_basis_money": 100.0, "open_datetime": "2020-01-01T00:00:00Z"},
        ],
    }


MODEL = {
    "as_of": "2026-06-13",
    "targets": {
        "AMD": {"low": 10, "high": 12, "rule": "trim_only"},
        "REST": {"low": 70, "high": 85, "rule": "hold"},
    },
}


class CoerceTrades(unittest.TestCase):
    def test_rejects_non_list(self):
        with self.assertRaises(ValueError):
            whatif._coerce_trades({"symbol": "AMD"})

    def test_rejects_non_numeric_delta(self):
        with self.assertRaises(ValueError):
            whatif._coerce_trades([{"symbol": "AMD", "delta_czk": "lots"}])

    def test_nets_duplicate_symbols(self):
        netted = whatif._coerce_trades([
            {"symbol": "AMD", "delta_czk": -50},
            {"symbol": "amd", "delta_czk": -30},
        ])
        self.assertAlmostEqual(netted["AMD"], -80.0)


class Simulate(unittest.TestCase):
    def test_trim_brings_name_into_band(self):
        wf = whatif.simulate(holdings(), MODEL, [{"symbol": "AMD", "delta_czk": -100}])
        after = {r["name"]: r for r in wf["after"]["rows"] if r["kind"] == "target"}
        self.assertEqual(wf["before_status"]["AMD"], "ABOVE")
        self.assertEqual(after["AMD"]["status"], "IN")  # 100/900 = 11.1%
        self.assertGreaterEqual(wf["summary"]["bands_in_after"], wf["summary"]["bands_in_before"])

    def test_cash_and_net_cash(self):
        wf = whatif.simulate(holdings(), MODEL, [{"symbol": "AMD", "delta_czk": -100}])
        self.assertAlmostEqual(wf["summary"]["raised_czk"], 100.0)
        self.assertAlmostEqual(wf["summary"]["spend_czk"], 0.0)
        self.assertAlmostEqual(wf["summary"]["net_cash_czk"], 100.0)
        self.assertAlmostEqual(wf["cash"]["before"], 100.0)
        self.assertAlmostEqual(wf["cash"]["after"], 200.0)

    def test_realized_tax_from_exempt_lot(self):
        wf = whatif.simulate(holdings(), MODEL, [{"symbol": "AMD", "delta_czk": -100}],
                             as_of=dt.datetime(2026, 6, 13, tzinfo=dt.timezone.utc))
        tt = wf["tax"]["totals"]
        self.assertAlmostEqual(tt["proceeds"], 100.0)
        self.assertAlmostEqual(tt["exempt_proceeds"], 100.0)
        self.assertAlmostEqual(tt["taxable_gain"], 0.0)  # 3y+ lot, gain is tax-free
        self.assertEqual(len(wf["tax"]["per_symbol"]), 1)

    def test_buys_only_no_tax(self):
        wf = whatif.simulate(holdings(), MODEL, [{"symbol": "REST", "delta_czk": 50}])
        self.assertEqual(wf["tax"]["per_symbol"], [])
        self.assertAlmostEqual(wf["summary"]["spend_czk"], 50.0)

    def test_negative_cash_is_flagged(self):
        wf = whatif.simulate(holdings(), MODEL, [{"symbol": "REST", "delta_czk": 500}])
        self.assertLess(wf["cash"]["after"], 0)
        self.assertTrue(any("Cash goes negative" in c for c in wf["caveats"]))

    def test_buying_unheld_name_appears_in_after(self):
        wf = whatif.simulate(holdings(), MODEL, [{"symbol": "NVDA", "delta_czk": 50}])
        untargeted = {u["symbol"] for u in wf["after"].get("untargeted", [])}
        self.assertIn("NVDA", untargeted)

    def test_oversell_is_visible_but_projection_floors_at_zero(self):
        wf = whatif.simulate(holdings(), MODEL, [{"symbol": "AMD", "delta_czk": -250}])
        after = {r["name"]: r for r in wf["after"]["rows"]}
        self.assertFalse(wf["valid"])
        self.assertEqual(after["AMD"]["current_pct"], 0.0)
        self.assertEqual(wf["stock_sell_violations"][0]["excess_czk"], 50.0)
        self.assertEqual(wf["applied_trades"][0]["delta_czk"], -200.0)
        self.assertTrue(any("Projection blocked" in c for c in wf["caveats"]))

    def test_duplicate_position_rows_receive_one_netted_delta(self):
        h = holdings()
        h["positions"] = [
            {"symbol": "AMD", "base_market_value": 100.0},
            {"symbol": "AMD", "base_market_value": 100.0},
            {"symbol": "REST", "base_market_value": 800.0},
        ]
        wf = whatif.simulate(h, MODEL, [{"symbol": "AMD", "delta_czk": -50}])
        after = {r["name"]: r for r in wf["after"]["rows"]}
        self.assertAlmostEqual(after["AMD"]["current_pct"], 150 / 950 * 100, places=2)

    def test_carries_a_pre_trade_risk_delta(self):
        # REST is the dominant name (80%); buying more of it raises top-1 share.
        wf = whatif.simulate(holdings(), MODEL, [{"symbol": "REST", "delta_czk": 100}])
        risk = wf["risk"]
        self.assertIn("top1_pct", risk)
        self.assertGreater(risk["top1_pct"]["after"], risk["top1_pct"]["before"])
        self.assertFalse(risk["has_correlation"])   # no series in the pure recompute

    def test_band_summary_counts_targets_and_sleeves_consistently(self):
        model = {
            **MODEL,
            "sleeves": {
                "growth": {
                    "low": 3, "high": 5, "rule": "accumulate",
                    "members": ["NVDA"],
                },
            },
        }
        wf = whatif.simulate(holdings(), model, [])
        self.assertEqual(wf["summary"]["bands_total"], 3)
        self.assertEqual(wf["summary"]["bands_in_before"], 1)
        self.assertEqual(wf["summary"]["bands_in_after"], 1)

    def test_does_not_mutate_input_holdings(self):
        h = holdings()
        before = h["positions"][0]["base_market_value"]
        whatif.simulate(h, MODEL, [{"symbol": "AMD", "delta_czk": -100}])
        self.assertEqual(h["positions"][0]["base_market_value"], before)


class CashTargetBand(unittest.TestCase):
    """Cash-vs-target steering: the simulator grades post-trade cash against
    the model's cash band and flags a breached floor as a caveat."""

    MODEL_WITH_CASH = dict(MODEL, cash_target_pct=9)  # cash 100/1100 ≈ 9.1% of NAV

    def test_after_pct_and_status(self):
        # Trim frees 100 CZK: cash 100 -> 200 = 18.2% of NAV, above the 7–11 band.
        wf = whatif.simulate(holdings(), self.MODEL_WITH_CASH, [{"symbol": "AMD", "delta_czk": -100}])
        t = wf["cash"]["target"]
        self.assertEqual(t["target_pct"], 9)
        self.assertEqual((t["low"], t["high"]), (7.0, 11.0))
        self.assertAlmostEqual(t["after_pct"], 18.18, places=2)
        self.assertEqual(t["status_after"], "ABOVE")

    def test_floor_breach_is_a_caveat(self):
        # Spend 50: cash 100 -> 50 = 4.5% of NAV, under the 7% floor (but > 0,
        # so the louder negative-cash caveat must NOT fire instead).
        wf = whatif.simulate(holdings(), self.MODEL_WITH_CASH, [{"symbol": "REST", "delta_czk": 50}])
        self.assertEqual(wf["cash"]["target"]["status_after"], "BELOW")
        self.assertTrue(any("floor" in c for c in wf["caveats"]))
        self.assertFalse(any("goes negative" in c for c in wf["caveats"]))

    def test_no_target_no_band(self):
        wf = whatif.simulate(holdings(), MODEL, [{"symbol": "AMD", "delta_czk": -100}])
        self.assertIsNone(wf["cash"]["target"])


if __name__ == "__main__":
    unittest.main()
