"""Tests for trade_service pure helpers. The gateway-touching entry points are
gated and exercised elsewhere; here we pin the per-order band context that feeds
the preview's "effect on band" tracks, since a wrong before/after weight at the
confirmation moment is worse than none."""

from __future__ import annotations

import unittest
from unittest import mock

import _support  # noqa: F401
import ibkr_trade
import rebalance
import trade_service


def _holdings():
    return {
        "net_asset_value": 1100.0,
        "base_currency": "CZK",
        "generated_at": "2026-06-13T10:00:00+00:00",
        "positions": [
            {"symbol": "AMD", "base_market_value": 200.0},   # 20% of 1000 invested
            {"symbol": "REST", "base_market_value": 800.0},   # 80%
        ],
    }


MODEL = {
    "as_of": "2026-06-13",
    "targets": {
        "AMD": {"low": 10, "high": 12, "rule": "trim_only"},
        "REST": {"low": 70, "high": 85, "rule": "hold"},
    },
}


class TradeSizingFx(unittest.TestCase):
    def test_derives_missing_fx_from_base_and_local_market_values(self):
        position = {
            "symbol": "ARM", "currency": "USD", "mark_price": 300.24,
            "market_value": 90072.0, "base_market_value": 1914030.0,
        }
        self.assertAlmostEqual(
            trade_service._position_fx_to_base(position), 21.25, places=6,
        )
        with mock.patch.object(trade_service, "_load",
                               return_value={"positions": [position]}):
            prices = trade_service._trade_price_map()
            fx = trade_service._fx_by_currency()
        self.assertAlmostEqual(prices["ARM"]["fx_to_base"], 21.25, places=6)
        self.assertAlmostEqual(fx["USD"], 21.25, places=6)

    def test_explicit_fx_wins_and_one_is_last_resort(self):
        self.assertEqual(
            trade_service._position_fx_to_base({
                "fx_rate_to_base": 22.0,
                "market_value": 100.0,
                "base_market_value": 2125.0,
            }),
            22.0,
        )
        self.assertEqual(trade_service._position_fx_to_base({}), 1.0)


class OrderBandContext(unittest.TestCase):
    def test_merges_before_and_after_weights_per_target(self):
        holdings = _holdings()
        # After trimming AMD by 100 CZK the invested book shrinks to 900; AMD is
        # 100/900 = 11.1% (in band), matching the what-if's own recompute.
        after_plan = rebalance.plan(MODEL, {
            **holdings,
            "positions": [
                {"symbol": "AMD", "base_market_value": 100.0},
                {"symbol": "REST", "base_market_value": 800.0},
            ],
        })
        ctx = trade_service._order_band_context(MODEL, holdings, after_plan)
        self.assertIn("AMD", ctx)
        amd = ctx["AMD"]
        self.assertEqual((amd["low"], amd["high"]), (10.0, 12.0))
        self.assertAlmostEqual(amd["before_pct"], 20.0, places=1)   # 200/1000
        self.assertAlmostEqual(amd["after_pct"], 11.11, places=1)   # 100/900
        self.assertEqual(amd["status_after"], "IN")

    def test_empty_without_model_or_after_plan(self):
        self.assertEqual(trade_service._order_band_context({}, _holdings(), {"rows": []}), {})
        self.assertEqual(trade_service._order_band_context(MODEL, {}, {"rows": []}), {})
        self.assertEqual(trade_service._order_band_context(MODEL, _holdings(), None), {})

    def test_skips_non_target_rows(self):
        holdings = _holdings()
        after_plan = {"rows": [
            {"kind": "sleeve", "name": "[Growth]", "current_pct": 5.0, "low": 3, "high": 8, "status": "IN"},
            {"kind": "untargeted", "symbol": "ZZZ", "current_pct": 1.0},
        ]}
        self.assertEqual(trade_service._order_band_context(MODEL, holdings, after_plan), {})

    def test_maps_sleeve_band_to_each_member_with_explicit_scope(self):
        after_plan = {"rows": [{
            "kind": "sleeve", "name": "analog", "current_pct": 4.8,
            "low": 5, "high": 6, "status": "BELOW",
            "members": [{"symbol": "TXN"}, {"symbol": "ADI"}],
        }]}
        before_plan = {"rows": [{
            "kind": "sleeve", "name": "analog", "current_pct": 1.2,
            "members": [{"symbol": "TXN"}, {"symbol": "ADI"}],
        }]}
        with mock.patch.object(rebalance, "plan", return_value=before_plan):
            ctx = trade_service._order_band_context(
                {"sleeves": {"analog": {"members": ["TXN", "ADI"]}}},
                _holdings(), after_plan)
        self.assertEqual(ctx["ADI"]["scope"], "sleeve")
        self.assertEqual(ctx["ADI"]["scope_members"], ["TXN", "ADI"])
        self.assertEqual(ctx["ADI"]["before_pct"], 1.2)
        self.assertEqual(ctx["ADI"]["after_pct"], 4.8)
        self.assertIs(ctx["ADI"], ctx["TXN"])


class OrderTerminal(unittest.TestCase):
    def test_terminal_statuses(self):
        for st in ("Filled", "Cancelled", "Canceled", "Expired", "Rejected", "ApiCancelled"):
            self.assertTrue(trade_service._order_terminal({"status": st}), st)

    def test_working_statuses(self):
        for st in ("Submitted", "PreSubmitted", "PendingSubmit", "Inactive", "", None):
            self.assertFalse(trade_service._order_terminal({"status": st}), st)


class HeldAvgCost(unittest.TestCase):
    # Two lots of AMD (weighted avg cost basis) and one MSFT lot. NVDA is held as
    # a position with no lots -> falls back to mark - upnl/qty. The position-level
    # cost_price is null (as IBKR leaves it) and must be ignored.
    HOLDINGS = {
        "positions": [
            {"symbol": "AMD", "cost_price": None, "mark_price": 100.0, "quantity": 30, "unrealized_pnl": 300.0},
            {"symbol": "NVDA", "cost_price": None, "mark_price": 120.0, "quantity": 10, "unrealized_pnl": 200.0},
            {"symbol": "FLAT", "mark_price": 50.0, "quantity": 0, "unrealized_pnl": 0.0},  # no qty -> skipped
        ],
        "lots": [
            {"symbol": "AMD", "cost_basis_money": 1000.0, "quantity": 10},   # 100/sh
            {"symbol": "AMD", "cost_basis_money": 2000.0, "quantity": 20},   # 100/sh -> weighted 100
            {"symbol": "MSFT", "cost_basis_money": 3000.0, "quantity": 10},  # 300/sh
        ],
    }

    def test_lot_weighted_cost_with_position_fallback(self):
        with mock.patch.object(trade_service, "_load", return_value=self.HOLDINGS):
            costs = trade_service._held_avg_cost()
        self.assertAlmostEqual(costs["AMD"], 100.0)                 # (1000+2000)/(10+20)
        self.assertAlmostEqual(costs["MSFT"], 300.0)
        self.assertAlmostEqual(costs["NVDA"], 100.0)               # 120 - 200/10 (no lots)
        self.assertNotIn("FLAT", costs)


class AttachAvgCost(unittest.TestCase):
    def test_attaches_avg_cost_by_symbol_to_working_orders(self):
        orders = [
            {"orderId": "o-1", "ticker": "AMD", "side": "SELL", "status": "Submitted"},
            {"orderId": "o-2", "ticker": "AMD", "side": "SELL", "status": "Filled"},     # terminal -> skipped
            {"orderId": "o-3", "ticker": "ZZZ", "side": "SELL", "status": "Submitted"},  # not held
        ]
        with mock.patch.object(trade_service, "_held_avg_cost", return_value={"AMD": 95.0}):
            out = trade_service._attach_avg_cost(orders)
        self.assertEqual(out[0]["avg_cost"], 95.0)
        self.assertNotIn("avg_cost", out[1])
        self.assertNotIn("avg_cost", out[2])

    def test_no_holdings_leaves_orders_untouched(self):
        orders = [{"orderId": "o", "ticker": "AMD", "side": "SELL", "status": "Submitted"}]
        with mock.patch.object(trade_service, "_held_avg_cost", return_value={}):
            out = trade_service._attach_avg_cost(orders)
        self.assertNotIn("avg_cost", out[0])


class TradeQuotes(unittest.TestCase):
    """The quotes are now their own endpoint (fetched async by the client) rather
    than folded into the orders list, so the working list can paint before the
    ~2s market-snapshot round-trip returns."""

    def test_returns_bid_ask_last_keyed_by_conid(self):
        snap = {
            111: {"conid": 111, "31": "100.20", "84": "100.00", "86": "100.50"},
            222: {"conid": 222, "31": "8.40"},   # last only -> still emitted
        }
        with mock.patch.object(ibkr_trade, "trading_enabled", return_value=True), \
             mock.patch.object(ibkr_trade, "market_snapshot", return_value=snap) as ms:
            out = trade_service._trade_quotes([222, 111, 111])   # deduped + sorted
        ms.assert_called_once_with([111, 222])
        self.assertEqual(out["111"], {"last": 100.20, "bid": 100.00, "ask": 100.50})
        self.assertEqual(out["222"], {"last": 8.40, "bid": None, "ask": None})

    def test_snapshot_failure_is_swallowed(self):
        with mock.patch.object(ibkr_trade, "trading_enabled", return_value=True), \
             mock.patch.object(ibkr_trade, "market_snapshot",
                               side_effect=ibkr_trade.CPAPIError("cold feed")):
            self.assertEqual(trade_service._trade_quotes([111]), {})

    def test_empty_conids_skips_the_snapshot_call(self):
        with mock.patch.object(ibkr_trade, "trading_enabled", return_value=True), \
             mock.patch.object(ibkr_trade, "market_snapshot") as ms:
            self.assertEqual(trade_service._trade_quotes([]), {})
        ms.assert_not_called()

    def test_forbidden_when_trading_disabled(self):
        from apierror import Forbidden
        with mock.patch.object(ibkr_trade, "trading_enabled", return_value=False):
            with self.assertRaises(Forbidden):
                trade_service._trade_quotes([111])


class DropBlockedBuys(unittest.TestCase):
    """KID/PRIIPs-blocked names (US-domiciled ETFs) can't be bought directly, so
    their BUY orders are stripped before preview/placement. SELLs must survive --
    closing an existing position is always allowed."""

    ORDERS = [
        {"conid": 1, "side": "BUY", "symbol": "AMD"},
        {"conid": 2, "side": "BUY", "symbol": "XSD"},    # blocked buy -> dropped
        {"conid": 3, "side": "SELL", "symbol": "SOXX"},  # blocked SELL -> kept
    ]

    def test_drops_blocked_buys_keeps_sells_and_others(self):
        kept = trade_service._drop_blocked_buys(self.ORDERS, {"XSD", "SOXX"})
        self.assertEqual([o["symbol"] for o in kept], ["AMD", "SOXX"])

    def test_empty_blocked_set_is_a_noop(self):
        kept = trade_service._drop_blocked_buys(self.ORDERS, set())
        self.assertIs(kept, self.ORDERS)

    def test_symbol_match_is_case_insensitive(self):
        orders = [{"conid": 9, "side": "BUY", "symbol": "xsd"}]
        self.assertEqual(trade_service._drop_blocked_buys(orders, {"XSD"}), [])


class WorkingOrderReconciliation(unittest.TestCase):
    PROPOSED = [{
        "symbol": "AMD", "conid": 1, "side": "BUY", "quantity": 10,
        "orderType": "MKT", "tif": "DAY", "cOID": "assay-token-AMD-10",
    }]
    BASKET = [{"symbol": "AMD", "delta_czk": 10000}]

    def test_same_side_partial_fill_uses_only_remaining_and_reduces_new_order(self):
        working = trade_service._normalized_working_orders([{
            "orderId": "7", "ticker": "AMD", "side": "BUY", "totalSize": 8,
            "filledQuantity": 5, "remainingQuantity": 3, "status": "Submitted",
            "orderType": "LMT", "price": 95, "tif": "GTC",
        }], {"AMD"})
        residual, ctx, effective = trade_service._reconcile_working_orders(
            self.PROPOSED, self.BASKET, working,
        )
        self.assertEqual(residual[0]["quantity"], 7)
        self.assertEqual(residual[0]["cOID"], "assay-token-AMD-7")
        self.assertEqual(ctx[0]["classification"], "same_side_partial")
        self.assertEqual(ctx[0]["working_same_qty"], 3)
        self.assertEqual(effective, [{"symbol": "AMD", "delta_czk": 10000.0}])

    def test_same_side_full_coverage_omits_new_order(self):
        working = trade_service._normalized_working_orders([{
            "orderId": "7", "ticker": "AMD", "side": "BUY",
            "remainingQuantity": 12, "status": "PreSubmitted",
        }], {"AMD"})
        residual, ctx, effective = trade_service._reconcile_working_orders(
            self.PROPOSED, self.BASKET, working,
        )
        self.assertEqual(residual, [])
        self.assertEqual(ctx[0]["classification"], "fully_covered")
        self.assertFalse(ctx[0]["placeable"])
        self.assertEqual(effective, [{"symbol": "AMD", "delta_czk": 12000.0}])

    def test_opposite_side_suppresses_new_order_and_recommends_resolution(self):
        working = trade_service._normalized_working_orders([{
            "orderId": "8", "ticker": "AMD", "side": "SELL",
            "remainingQuantity": 4, "status": "Submitted",
        }], {"AMD"})
        residual, ctx, effective = trade_service._reconcile_working_orders(
            self.PROPOSED, self.BASKET, working,
        )
        self.assertEqual(residual, [])
        self.assertEqual(ctx[0]["classification"], "opposite_side")
        self.assertIn("Cancel or modify", ctx[0]["next_step"])
        self.assertEqual(effective, [{"symbol": "AMD", "delta_czk": -4000.0}])

    def test_terminal_and_zero_remaining_orders_do_not_reconcile(self):
        working = trade_service._normalized_working_orders([
            {"orderId": "1", "ticker": "AMD", "side": "BUY",
             "remainingQuantity": 10, "status": "Filled"},
            {"orderId": "2", "ticker": "AMD", "side": "BUY",
             "remainingQuantity": 0, "status": "Submitted"},
        ], {"AMD"})
        self.assertEqual(working, [])

    def test_missing_remaining_quantity_is_total_minus_filled(self):
        working = trade_service._normalized_working_orders([{
            "orderId": "3", "ticker": "AMD", "side": "BUY", "totalSize": 10,
            "filledQuantity": 6, "status": "Submitted",
        }], {"AMD"})
        self.assertEqual(working[0]["remaining_qty"], 4)

    def test_position_projection_distinguishes_order_remainder_from_shares_left(self):
        proposed = [{
            "symbol": "ARM", "side": "SELL", "quantity": 206,
            "_current_position_qty": 300, "_estimate_price": 250,
            "_estimate_fx_to_base": 25,
        }]
        working = [{"order_id": "1", "symbol": "ARM", "side": "SELL",
                    "remaining_qty": 100, "status": "Submitted"}]
        residual, ctx, _effective = trade_service._reconcile_working_orders(
            proposed, [{"symbol": "ARM", "delta_czk": -1287500}], working)
        self.assertEqual(residual[0]["quantity"], 106)
        self.assertEqual(ctx[0]["current_position_qty"], 300)
        self.assertEqual(ctx[0]["projected_position_qty"], 94)

    def test_fingerprint_changes_with_remaining_quantity(self):
        a = [{"order_id": "1", "symbol": "AMD", "side": "BUY",
              "remaining_qty": 3, "status": "Submitted"}]
        b = [{**a[0], "remaining_qty": 2}]
        self.assertNotEqual(
            trade_service._working_fingerprint(a),
            trade_service._working_fingerprint(b),
        )


if __name__ == "__main__":
    unittest.main()
