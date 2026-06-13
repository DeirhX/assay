"""Tests for the Czech tax-lot sell planner: the 3-year exemption boundary, the
minimize-tax lot ordering, partial fills, bucket totals, and plan enrichment."""

from __future__ import annotations

import datetime as dt
import unittest

import _support  # noqa: F401
import tax_lots as tl

AS_OF = dt.datetime(2026, 6, 13, tzinfo=dt.timezone.utc)


def lot(symbol, qty, mv, cost, opened):
    return {"symbol": symbol, "quantity": qty, "base_market_value": mv,
            "cost_basis_money": cost, "open_datetime": opened}


class HoldingPeriod(unittest.TestCase):
    def test_add_years_handles_leap_day(self):
        self.assertEqual(tl.add_years(dt.date(2020, 2, 29), 3), dt.date(2023, 2, 28))

    def test_exempt_boundary_is_inclusive_at_three_years(self):
        exactly = tl._parse_open("2023-06-13T00:00:00Z")
        oneday = tl._parse_open("2023-06-14T00:00:00Z")
        self.assertTrue(tl.is_exempt(exactly, AS_OF))    # 3y to the day -> exempt
        self.assertFalse(tl.is_exempt(oneday, AS_OF))    # one day short -> taxable

    def test_naive_open_datetime_is_treated_as_utc(self):
        self.assertTrue(tl.is_exempt(tl._parse_open("2020-01-01"), AS_OF))


class SelectLots(unittest.TestCase):
    def test_exempt_gain_is_sold_first(self):
        lots = [
            lot("X", 10, 50.0, 80.0, "2025-06-01T00:00:00Z"),   # taxable loss
            lot("X", 10, 100.0, 60.0, "2020-01-01T00:00:00Z"),  # exempt gain
        ]
        out = tl.select_lots(lots, 50.0, as_of=AS_OF)
        self.assertEqual(out["n_lots_used"], 1)
        self.assertEqual(out["lots"][0]["bucket"], "exempt_gain")
        self.assertEqual(out["totals"]["taxable_gain"], 0.0)
        self.assertEqual(out["totals"]["exempt_proceeds"], 50.0)

    def test_partial_fill_prorates_cost_and_gain(self):
        lots = [lot("X", 10, 100.0, 60.0, "2020-01-01T00:00:00Z")]  # exempt, gain 40
        out = tl.select_lots(lots, 50.0, as_of=AS_OF)
        sel = out["lots"][0]
        self.assertAlmostEqual(sel["shares"], 5.0)
        self.assertAlmostEqual(sel["proceeds"], 50.0)
        self.assertAlmostEqual(sel["cost"], 30.0)
        self.assertAlmostEqual(sel["gain"], 20.0)

    def test_loss_harvesting_and_net_taxable_gain(self):
        lots = [
            lot("X", 10, 100.0, 70.0, "2025-06-01T00:00:00Z"),  # taxable gain +30
            lot("X", 10, 50.0, 80.0, "2025-06-01T00:00:00Z"),   # taxable loss -30
        ]
        # Need 150 -> both lots fully. Loss sells before the gain (priority).
        out = tl.select_lots(lots, 150.0, as_of=AS_OF)
        self.assertEqual(out["lots"][0]["bucket"], "taxable_loss")
        self.assertAlmostEqual(out["totals"]["taxable_gain"], 0.0)      # +30 - 30
        self.assertAlmostEqual(out["totals"]["harvestable_loss"], 30.0)

    def test_shortfall_when_lots_cannot_cover_trim(self):
        lots = [lot("X", 10, 100.0, 60.0, "2020-01-01T00:00:00Z")]
        out = tl.select_lots(lots, 250.0, as_of=AS_OF)
        self.assertAlmostEqual(out["raised"], 100.0)
        self.assertAlmostEqual(out["shortfall"], 150.0)

    def test_days_to_exempt_reported_for_taxable_lot(self):
        lots = [lot("X", 10, 100.0, 60.0, "2025-06-13T00:00:00Z")]  # exempt 2028-06-13
        out = tl.select_lots(lots, 10.0, as_of=AS_OF)
        # 2025-06-13 -> 2028-06-13 spans a leap day (Feb 29 2028), so 731, not 730.
        self.assertEqual(out["lots"][0]["days_to_exempt"], 731)

    def test_cost_falls_back_to_market_minus_pnl(self):
        lots = [{"symbol": "X", "quantity": 10, "base_market_value": 100.0,
                 "unrealized_pnl": 40.0, "open_datetime": "2020-01-01T00:00:00Z"}]
        out = tl.select_lots(lots, 100.0, as_of=AS_OF)
        self.assertAlmostEqual(out["lots"][0]["gain"], 40.0)  # cost inferred as 60


class BreakdownAndEnrich(unittest.TestCase):
    def _holdings(self):
        return {
            "base_currency": "CZK",
            "lots": [
                lot("AMD", 10, 100.0, 60.0, "2020-01-01T00:00:00Z"),
                lot("AMD", 10, 50.0, 80.0, "2025-06-01T00:00:00Z"),
            ],
        }

    def test_breakdown_for_symbol_tags_currency_and_lots(self):
        out = tl.breakdown_for_symbol(self._holdings(), "AMD", 120.0, as_of=AS_OF)
        self.assertEqual(out["symbol"], "AMD")
        self.assertEqual(out["currency"], "CZK")
        self.assertTrue(out["has_lots"])

    def test_breakdown_no_lots_does_not_raise(self):
        out = tl.breakdown_for_symbol({"lots": []}, "NONE", 50.0, as_of=AS_OF)
        self.assertFalse(out["has_lots"])
        self.assertEqual(out["raised"], 0.0)

    def test_enrich_plan_only_tags_single_name_trims(self):
        plan = {"rows": [
            {"kind": "target", "name": "AMD", "action": "trim", "suggest_delta_czk": -90},
            {"kind": "target", "name": "NVDA", "action": "buy", "suggest_delta_czk": 50},
            {"kind": "sleeve", "name": "analog", "action": "trim", "suggest_delta_czk": -40},
        ]}
        out = tl.enrich_plan(plan, self._holdings(), as_of=AS_OF)
        rows = {r["name"]: r for r in out["rows"]}
        self.assertIn("tax", rows["AMD"])
        self.assertNotIn("tax", rows["NVDA"])   # a buy, not a trim
        self.assertNotIn("tax", rows["analog"])  # sleeve, spread by hand


if __name__ == "__main__":
    unittest.main()
