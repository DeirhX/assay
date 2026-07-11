"""Tests for trade_service pure helpers. The gateway-touching entry points are
gated and exercised elsewhere; here we pin the per-order band context that feeds
the preview's "effect on band" tracks, since a wrong before/after weight at the
confirmation moment is worse than none."""

from __future__ import annotations

import unittest
from unittest import mock

import datetime as dt
import tempfile
import time
from pathlib import Path

import _support  # noqa: F401
import apierror
import ibkr_trade
import kid_block
import portfolio
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
            portfolio.position_fx_to_base(position), 21.25, places=6,
        )
        with mock.patch.object(trade_service, "_load",
                               return_value={"positions": [position]}):
            prices = trade_service._trade_price_map()
            fx = trade_service._fx_by_currency()
        self.assertAlmostEqual(prices["ARM"]["fx_to_base"], 21.25, places=6)
        self.assertAlmostEqual(fx["USD"], 21.25, places=6)

    def test_explicit_fx_wins_and_one_is_last_resort(self):
        self.assertEqual(
            portfolio.position_fx_to_base({
                "fx_rate_to_base": 22.0,
                "market_value": 100.0,
                "base_market_value": 2125.0,
            }),
            22.0,
        )
        self.assertEqual(portfolio.position_fx_to_base({}), 1.0)


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


def _cc_leg(**overrides):
    base = {
        "type": "covered_call",
        "symbol": "NVDA",
        "conid": 555,
        "expiry": "2026-08-21",
        "strike": 105.0,
        "contracts": 2,
    }
    base.update(overrides)
    return base


def _resolved_call(**overrides):
    base = {
        "conid": 555,
        "expiry": "2026-08-21",
        "strike": 105.0,
        "bid": 2.40,
        "ask": 2.60,
        "last": 2.50,
        "quote_timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        "rules": {"increment": 0.05},
    }
    base.update(overrides)
    return base


def _nvda_holdings(*, shares=500, short_calls=0):
    positions = [
        {"symbol": "NVDA", "asset_class": "STK", "quantity": shares,
         "mark_price": 100.0, "market_value": shares * 100.0,
         "base_market_value": shares * 100.0, "currency": "USD"},
    ]
    if short_calls:
        positions.append({
            "symbol": "NVDA  260821C00105000",
            "asset_class": "OPT",
            "quantity": -short_calls,
        })
    return {"positions": positions, "lots": []}


class NormalizeBasket(unittest.TestCase):
    def test_legacy_stock_row_canonicalizes(self):
        out = trade_service._normalize_basket([{"symbol": "amd", "delta_czk": 1000}])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["type"], "stock")
        self.assertEqual(out[0]["leg_id"], "stock:AMD")
        self.assertEqual(out[0]["symbol"], "AMD")
        self.assertEqual(out[0]["delta_czk"], 1000.0)

    def test_explicit_stock_type_gets_canonical_leg_id(self):
        out = trade_service._normalize_basket([
            {"type": "stock", "symbol": "NVDA", "delta_czk": -5000},
        ])
        self.assertEqual(out[0], {
            "type": "stock",
            "leg_id": "stock:NVDA",
            "symbol": "NVDA",
            "delta_czk": -5000.0,
        })

    def test_stock_rows_net_by_symbol_and_merge_provenance(self):
        prov_a = {"source": "exit_plan", "tranche_index": 0}
        prov_b = {"source": "exit_plan", "tranche_index": 1}
        out = trade_service._normalize_basket([
            {"type": "stock", "symbol": "AMD", "delta_czk": -1000, "provenance": prov_a},
            {"type": "stock", "symbol": "AMD", "delta_czk": -500, "provenance": prov_b},
        ])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["delta_czk"], -1500.0)
        self.assertEqual(out[0]["provenance"], [prov_a, prov_b])

    def test_covered_call_canonicalization_discards_unknown_fields(self):
        out = trade_service._normalize_basket([_cc_leg(
            symbol="nvda",
            limit_price=2.55,
            quote_timestamp="2026-07-10T12:00:00+00:00",
            client_only="must-not-survive",
        )])
        row = out[0]
        self.assertEqual(row["type"], "covered_call")
        self.assertEqual(row["leg_id"], "covered_call:NVDA:555")
        self.assertEqual(row["route"], "covered_call")
        self.assertEqual(row["right"], "C")
        self.assertEqual(row["multiplier"], 100)
        self.assertEqual(row["limit_price"], 2.55)
        self.assertNotIn("client_only", row)

    def test_covered_call_custom_leg_id_is_preserved(self):
        out = trade_service._normalize_basket([_cc_leg(leg_id="covered_call:NVDA:custom")])
        self.assertEqual(out[0]["leg_id"], "covered_call:NVDA:custom")

    def test_save_replaces_same_leg_id_instead_of_doubling(self):
        with tempfile.TemporaryDirectory() as tmp:
            staged = Path(tmp) / "staged-basket.json"
            with mock.patch.object(trade_service, "STAGED_BASKET_JSON", staged):
                trade_service.save_basket([_cc_leg(contracts=1)])
                trade_service.save_basket(trade_service.load_basket() + [_cc_leg(contracts=3)])
                basket = trade_service.load_basket()
        self.assertEqual(len(basket), 1)
        self.assertEqual(basket[0]["contracts"], 3)

    def test_replace_stock_basket_preserves_server_staged_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            staged = Path(tmp) / "staged-basket.json"
            with mock.patch.object(trade_service, "STAGED_BASKET_JSON", staged):
                trade_service.save_basket([_cc_leg()])
                basket = trade_service.replace_stock_basket([
                    {"symbol": "AMD", "delta_czk": 1000},
                ])
        self.assertEqual(
            {(row["type"], row["symbol"]) for row in basket},
            {("stock", "AMD"), ("covered_call", "NVDA")},
        )

    def test_replace_stock_basket_rejects_client_option_definition(self):
        with self.assertRaisesRegex(ValueError, "must be staged from the Exit plan"):
            trade_service.replace_stock_basket([_cc_leg()])

    def test_remove_leg_invalidates_review_and_rejects_stale_identifier(self):
        with tempfile.TemporaryDirectory() as tmp:
            staged = Path(tmp) / "staged-basket.json"
            with mock.patch.object(trade_service, "STAGED_BASKET_JSON", staged):
                trade_service.save_basket([
                    {"symbol": "AMD", "delta_czk": 1000},
                    _cc_leg(),
                ])
                state = trade_service.basket_state()
                trade_service.review_basket(state["revision"])
                trade_service._preview_issued["token"] = {"test": True}

                basket = trade_service.remove_basket_leg("covered_call:NVDA:555")
                self.assertEqual([row["symbol"] for row in basket], ["AMD"])
                self.assertFalse(trade_service.basket_state()["reviewed"])
                self.assertEqual(trade_service._preview_issued, {})

                with self.assertRaises(apierror.Conflict):
                    trade_service.remove_basket_leg("covered_call:NVDA:555")

    def test_basket_token_sensitive_to_every_canonical_field(self):
        base = trade_service._normalize_basket([_cc_leg(), {"symbol": "AMD", "delta_czk": 100}])
        account = "DU1"
        token = trade_service._basket_token(account, base)
        variants = [
            trade_service._normalize_basket([_cc_leg(contracts=3), {"symbol": "AMD", "delta_czk": 100}]),
            trade_service._normalize_basket([_cc_leg(strike=106.0), {"symbol": "AMD", "delta_czk": 100}]),
            trade_service._normalize_basket([_cc_leg(expiry="2026-09-19"), {"symbol": "AMD", "delta_czk": 100}]),
            trade_service._normalize_basket([
                {**_cc_leg(), "provenance": [{"source": "exit_plan"}]},
                {"symbol": "AMD", "delta_czk": 100},
            ]),
            trade_service._normalize_basket([_cc_leg(), {"symbol": "AMD", "delta_czk": 200}]),
        ]
        for variant in variants:
            self.assertNotEqual(trade_service._basket_token(account, variant), token)

    def test_invalid_and_tampered_fields_raise(self):
        cases = [
            ("not-a-list", ValueError, "trades must be a list"),
            ([{"type": "put", "symbol": "AMD"}], ValueError, "unsupported trade leg type"),
            ([_cc_leg(expiry="08/21/2026")], ValueError, "expiry must be YYYY-MM-DD"),
            ([_cc_leg(conid=0)], ValueError, "invalid covered-call contract"),
            ([_cc_leg(multiplier=50)], ValueError, "invalid covered-call contract"),
            ([_cc_leg(contracts=0)], ValueError, "invalid covered-call contract"),
            ([_cc_leg(provenance="bad")], ValueError, "provenance must be"),
            ([_cc_leg(leg_id="   ")], ValueError, "leg_id is required"),
            ([_cc_leg(limit_price="nope")], ValueError, "limit_price must be numeric"),
        ]
        for trades, exc, fragment in cases:
            with self.subTest(trades=trades):
                with self.assertRaises(exc) as ctx:
                    trade_service._normalize_basket(trades)
                self.assertIn(fragment, str(ctx.exception))


class PrepareCoveredCallOrders(unittest.TestCase):
    def setUp(self):
        live = mock.patch.object(
            trade_service,
            "_live_positions",
            return_value=[{
                "assetClass": "STK", "contractDesc": "NVDA", "position": 500,
            }],
        )
        live.start()
        self.addCleanup(live.stop)

    def test_prepare_appends_exact_option_order_from_resolver(self):
        basket = trade_service._normalize_basket([_cc_leg()])
        resolved = _resolved_call()
        with mock.patch.object(trade_service, "_load", return_value=_nvda_holdings()), \
             mock.patch.object(ibkr_trade, "build_orders", return_value=([], [])), \
             mock.patch.object(ibkr_trade, "market_snapshot", return_value={}), \
             mock.patch.object(ibkr_trade, "resolve_exact_call", return_value=resolved) as exact, \
             mock.patch.object(kid_block, "blocked_symbols", return_value=set()):
            orders, _warnings = trade_service._prepare_trade_orders("DU1", basket)
        exact.assert_called_once_with("NVDA", "2026-08-21", 105.0)
        self.assertEqual(len(orders), 1)
        order = orders[0]
        self.assertEqual(order["instrument_type"], "covered_call")
        self.assertEqual(order["leg_id"], "covered_call:NVDA:555")
        self.assertEqual(order["conid"], 555)
        self.assertEqual(order["side"], "SELL")
        self.assertEqual(order["quantity"], 2)
        self.assertEqual(order["orderType"], "LMT")
        self.assertAlmostEqual(order["price"], 2.50)
        self.assertEqual(order["provenance"], [])
        self.assertIn("assay-", order["cOID"])

    def test_prepare_carries_provenance_onto_order(self):
        prov = [{"source": "exit_plan", "route": "covered_call"}]
        basket = trade_service._normalize_basket([_cc_leg(provenance=prov)])
        with mock.patch.object(trade_service, "_load", return_value=_nvda_holdings()), \
             mock.patch.object(ibkr_trade, "build_orders", return_value=([], [])), \
             mock.patch.object(ibkr_trade, "market_snapshot", return_value={}), \
             mock.patch.object(ibkr_trade, "resolve_exact_call", return_value=_resolved_call()), \
             mock.patch.object(kid_block, "blocked_symbols", return_value=set()):
            orders, _ = trade_service._prepare_trade_orders("DU1", basket)
        self.assertEqual(orders[0]["provenance"], prov)

    def test_prepare_rejects_resolver_conid_mismatch(self):
        basket = trade_service._normalize_basket([_cc_leg()])
        with mock.patch.object(trade_service, "_load", return_value=_nvda_holdings()), \
             mock.patch.object(ibkr_trade, "build_orders", return_value=([], [])), \
             mock.patch.object(ibkr_trade, "market_snapshot", return_value={}), \
             mock.patch.object(ibkr_trade, "resolve_exact_call",
                               return_value=_resolved_call(conid=999)), \
             mock.patch.object(kid_block, "blocked_symbols", return_value=set()):
            with self.assertRaises(ValueError) as ctx:
                trade_service._prepare_trade_orders("DU1", basket)
        self.assertIn("contract changed", str(ctx.exception))

    def test_prepare_rejects_crossed_quote(self):
        basket = trade_service._normalize_basket([_cc_leg()])
        with mock.patch.object(trade_service, "_load", return_value=_nvda_holdings()), \
             mock.patch.object(ibkr_trade, "build_orders", return_value=([], [])), \
             mock.patch.object(ibkr_trade, "market_snapshot", return_value={}), \
             mock.patch.object(ibkr_trade, "resolve_exact_call",
                               return_value=_resolved_call(bid=2.80, ask=2.60)), \
             mock.patch.object(kid_block, "blocked_symbols", return_value=set()):
            with self.assertRaises(ValueError) as ctx:
                trade_service._prepare_trade_orders("DU1", basket)
        self.assertIn("live, uncrossed IBKR bid/ask", str(ctx.exception))

    def test_prepare_rejects_stale_quote(self):
        basket = trade_service._normalize_basket([_cc_leg()])
        stale = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=3)).isoformat()
        with mock.patch.object(trade_service, "_load", return_value=_nvda_holdings()), \
             mock.patch.object(ibkr_trade, "build_orders", return_value=([], [])), \
             mock.patch.object(ibkr_trade, "market_snapshot", return_value={}), \
             mock.patch.object(ibkr_trade, "resolve_exact_call",
                               return_value=_resolved_call(quote_timestamp=stale)), \
             mock.patch.object(kid_block, "blocked_symbols", return_value=set()):
            with self.assertRaises(ValueError) as ctx:
                trade_service._prepare_trade_orders("DU1", basket)
        self.assertIn("quote is stale", str(ctx.exception))

    def test_prepare_rejects_when_no_covered_capacity(self):
        basket = trade_service._normalize_basket([_cc_leg()])
        with mock.patch.object(trade_service, "_load",
                               return_value=_nvda_holdings(shares=50)), \
             mock.patch.object(ibkr_trade, "build_orders", return_value=([], [])), \
             mock.patch.object(ibkr_trade, "market_snapshot", return_value={}), \
             mock.patch.object(ibkr_trade, "resolve_exact_call", return_value=_resolved_call()), \
             mock.patch.object(trade_service, "_live_positions", return_value=[{
                 "assetClass": "STK", "contractDesc": "NVDA", "position": 50,
             }]), \
             mock.patch.object(kid_block, "blocked_symbols", return_value=set()):
            with self.assertRaises(ValueError) as ctx:
                trade_service._prepare_trade_orders("DU1", basket)
        self.assertIn("no covered-call capacity", str(ctx.exception))


class CoveredCallCapacity(unittest.TestCase):
    def test_capacity_accounts_for_held_short_and_working_orders(self):
        raw_working = [{
            "orderId": "w1",
            "ticker": "NVDA  260821C00110000",
            "secType": "OPT",
            "side": "SELL",
            "remainingQuantity": 1,
            "status": "Submitted",
        }]
        with mock.patch.object(trade_service, "_load",
                               return_value=_nvda_holdings(shares=350, short_calls=1)):
            cap = trade_service.covered_call_capacity("NVDA", raw_working)
        # 350 shares -> 3 contracts, minus 1 held short -> 2, minus 1 working -> 1
        self.assertEqual(cap["current_shares"], 350)
        self.assertEqual(cap["held_short_calls"], 1)
        self.assertEqual(cap["working_short_calls"], 1)
        self.assertEqual(cap["capacity_contracts"], 1)
        self.assertEqual(cap["available_shares"], 100)

    def test_live_capacity_counts_short_call_positions(self):
        rows = [
            {"assetClass": "STK", "contractDesc": "NVDA", "position": 300},
            {
                "assetClass": "OPT", "contractDesc": "NVDA AUG 21 '26 105 Call",
                "putOrCall": "CALL", "position": -1,
            },
        ]
        with mock.patch.object(trade_service, "_live_positions", return_value=rows):
            cap = trade_service.covered_call_capacity(
                "NVDA", live_account_id="DU1",
            )
        self.assertEqual(cap["current_shares"], 300)
        self.assertEqual(cap["held_short_calls"], 1)
        self.assertEqual(cap["capacity_contracts"], 2)

    def test_working_put_does_not_consume_call_coverage(self):
        working = [{
            "ticker": "NVDA  260821P00105000",
            "side": "SELL",
            "remainingQuantity": 2,
            "status": "Submitted",
        }]
        with mock.patch.object(
            trade_service, "_load", return_value=_nvda_holdings(shares=300),
        ):
            cap = trade_service.covered_call_capacity("NVDA", working)
        self.assertEqual(cap["working_short_calls"], 0)
        self.assertEqual(cap["capacity_contracts"], 3)


class OptionWorkingNormalization(unittest.TestCase):
    def test_occ_ticker_is_recognized_without_sectype(self):
        out = trade_service._normalized_working_orders(
            [{
                "orderId": "occ",
                "ticker": "NVDA  260821C00110000",
                "side": "SELL",
                "remainingQuantity": 1,
                "status": "Submitted",
            }],
            {"NVDA"},
            {"NVDA"},
            set(),
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["instrument_type"], "covered_call")

    def test_filters_options_by_conid_when_symbol_set_is_empty(self):
        raw = [
            {"orderId": "1", "ticker": "NVDA  260821C00105000", "conid": 555,
             "secType": "OPT", "side": "SELL", "remainingQuantity": 1, "status": "Submitted"},
            {"orderId": "2", "ticker": "NVDA  260821C00110000", "conid": 666,
             "secType": "OPT", "side": "SELL", "remainingQuantity": 2, "status": "Submitted"},
        ]
        out = trade_service._normalized_working_orders(raw, set(), set(), {555})
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["conid"], 555)
        self.assertEqual(out[0]["instrument_type"], "covered_call")

    def test_stock_symbol_filter_does_not_pick_unrelated_option(self):
        raw = [
            {"orderId": "1", "ticker": "AMD", "side": "BUY",
             "remainingQuantity": 5, "status": "Submitted"},
            {"orderId": "2", "ticker": "NVDA  260821C00105000", "conid": 555,
             "secType": "OPT", "side": "SELL", "remainingQuantity": 1, "status": "Submitted"},
        ]
        out = trade_service._normalized_working_orders(raw, {"AMD"}, set(), set())
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["symbol"], "AMD")
        self.assertEqual(out[0]["instrument_type"], "stock")


class OptionWorkingReconciliation(unittest.TestCase):
    PROPOSED_CC = [{
        "symbol": "NVDA",
        "instrument_type": "covered_call",
        "leg_id": "covered_call:NVDA:555",
        "conid": 555,
        "side": "SELL",
        "quantity": 3,
        "price": 2.5,
        "coverage_capacity_contracts": 5,
        "held_short_calls": 0,
        "current_shares": 500,
        "expiry": "2026-08-21",
        "strike": 105.0,
    }]
    BASKET = [_cc_leg()]

    def test_same_conid_partial_fill_reduces_residual(self):
        working = [{
            "order_id": "w1", "symbol": "NVDA", "instrument_type": "covered_call",
            "conid": 555, "side": "SELL", "remaining_qty": 1,
        }]
        residual, ctx, _ = trade_service._reconcile_working_orders(
            self.PROPOSED_CC, self.BASKET, working,
        )
        self.assertEqual(residual[0]["quantity"], 2)
        self.assertEqual(ctx[0]["classification"], "same_side_partial")

    def test_other_conid_on_same_symbol_does_not_reduce_proposal(self):
        working = [{
            "order_id": "w2", "symbol": "NVDA", "instrument_type": "covered_call",
            "conid": 666, "side": "SELL", "remaining_qty": 2,
        }]
        residual, ctx, _ = trade_service._reconcile_working_orders(
            self.PROPOSED_CC, self.BASKET, working,
        )
        self.assertEqual(residual[0]["quantity"], 3)
        self.assertEqual(ctx[0]["classification"], "none")

    def test_stock_working_order_does_not_reconcile_covered_call(self):
        working = [{
            "order_id": "s1", "symbol": "NVDA", "instrument_type": "stock",
            "side": "SELL", "remaining_qty": 100,
        }]
        residual, ctx, _ = trade_service._reconcile_working_orders(
            self.PROPOSED_CC, self.BASKET, working,
        )
        self.assertEqual(residual[0]["quantity"], 3)
        self.assertEqual(ctx[0]["working_same_qty"], 0)

    def test_coverage_blocked_when_residual_exceeds_capacity(self):
        proposed = [{**self.PROPOSED_CC[0], "quantity": 2, "coverage_capacity_contracts": 1}]
        residual, ctx, _ = trade_service._reconcile_working_orders(
            proposed, self.BASKET, [],
        )
        self.assertEqual(residual, [])
        self.assertEqual(ctx[0]["classification"], "coverage_blocked")
        self.assertFalse(ctx[0]["placeable"])

    def test_combined_strikes_cannot_reuse_the_same_covered_shares(self):
        proposed = [
            {**self.PROPOSED_CC[0], "quantity": 2, "current_shares": 300,
             "coverage_capacity_contracts": 3},
            {
                **self.PROPOSED_CC[0],
                "leg_id": "covered_call:NVDA:666",
                "conid": 666,
                "strike": 110.0,
                "quantity": 2,
                "current_shares": 300,
                "coverage_capacity_contracts": 3,
            },
        ]
        basket = trade_service._normalize_basket([
            _cc_leg(contracts=2),
            _cc_leg(leg_id="covered_call:NVDA:666", conid=666, strike=110.0, contracts=2),
        ])
        residual, ctx, _ = trade_service._reconcile_working_orders(proposed, basket, [])
        self.assertEqual(residual, [])
        self.assertEqual([c["classification"] for c in ctx], ["coverage_blocked", "coverage_blocked"])
        self.assertIn("4 short call contract(s)", ctx[0]["next_step"])

    def test_stock_sell_in_same_basket_reduces_call_coverage(self):
        stock = {
            "symbol": "NVDA", "side": "SELL", "quantity": 150,
            "_estimate_price": 100.0, "_estimate_fx_to_base": 1.0,
            "_current_position_qty": 300, "cOID": "stock-150",
        }
        call = {
            **self.PROPOSED_CC[0],
            "quantity": 2,
            "current_shares": 300,
            "coverage_capacity_contracts": 3,
        }
        basket = trade_service._normalize_basket([
            {"symbol": "NVDA", "delta_czk": -15000},
            _cc_leg(contracts=2),
        ])
        residual, ctx, _ = trade_service._reconcile_working_orders([stock, call], basket, [])
        self.assertEqual(residual, [])
        stock_ctx = next(c for c in ctx if c["instrument_type"] == "stock")
        self.assertEqual(stock_ctx["classification"], "coverage_blocked")
        option_ctx = next(c for c in ctx if c["instrument_type"] == "covered_call")
        self.assertEqual(option_ctx["classification"], "coverage_blocked")
        self.assertEqual(option_ctx["coverage_capacity_contracts"], 1)
        self.assertEqual(option_ctx["future_stock_sell_shares"], 150)

    def test_stock_sell_cannot_uncover_held_short_calls(self):
        stock = {
            "symbol": "NVDA", "side": "SELL", "quantity": 150,
            "_estimate_price": 100.0, "_estimate_fx_to_base": 1.0,
            "_current_position_qty": 250,
            "_coverage_current_shares": 250,
            "_coverage_held_short_calls": 2,
            "cOID": "stock-150",
        }
        basket = trade_service._normalize_basket([
            {"symbol": "NVDA", "delta_czk": -15000},
        ])
        residual, ctx, effective = trade_service._reconcile_working_orders(
            [stock], basket, [],
        )
        self.assertEqual(residual, [])
        self.assertEqual(effective, [])
        self.assertEqual(ctx[0]["classification"], "coverage_blocked")
        self.assertIn("200 shares", ctx[0]["next_step"])

    def test_provenance_flows_into_option_order_context(self):
        prov = [{"source": "exit_plan", "route": "covered_call"}]
        basket = trade_service._normalize_basket([_cc_leg(provenance=prov)])
        residual, ctx, _ = trade_service._reconcile_working_orders(
            self.PROPOSED_CC, basket, [],
        )
        self.assertEqual(ctx[0]["provenance"], prov)


class PlaceTimeCoveredCallRevalidation(unittest.TestCase):
    def setUp(self):
        trade_service._preview_issued.clear()
        live = mock.patch.object(
            trade_service,
            "_live_positions",
            return_value=[{
                "assetClass": "STK", "contractDesc": "NVDA", "position": 500,
            }],
        )
        live.start()
        self.addCleanup(live.stop)

    @staticmethod
    def _cc_order(**overrides):
        base = {
            "instrument_type": "covered_call",
            "symbol": "NVDA",
            "conid": 555,
            "expiry": "2026-08-21",
            "strike": 105.0,
            "side": "SELL",
            "quantity": 2,
            "orderType": "LMT",
            "price": 2.50,
            "tif": "GTC",
        }
        base.update(overrides)
        return base

    def _arm_cc_preview(self, token: str, order: dict) -> None:
        trade_service._preview_issued[token] = {
            "issued_at": time.time(),
            "orders": [order],
            "working_fingerprint": trade_service._working_fingerprint([]),
            "working_symbols": [],
            "working_option_symbols": ["NVDA"],
            "working_option_conids": [555],
            "working_available": True,
        }

    def test_place_revalidates_contract_quote_and_coverage(self):
        basket = trade_service._normalize_basket([_cc_leg()])
        token = trade_service._basket_token("DU1", basket)
        order = self._cc_order()
        with tempfile.TemporaryDirectory() as tmp:
            staged = Path(tmp) / "staged-basket.json"
            with mock.patch.object(trade_service, "STAGED_BASKET_JSON", staged), \
                    mock.patch.object(ibkr_trade, "trading_enabled", return_value=True), \
                    mock.patch.object(ibkr_trade, "accounts", return_value=[{"accountId": "DU1"}]), \
                    mock.patch.object(ibkr_trade, "live_orders", return_value=[]), \
                    mock.patch.object(ibkr_trade, "resolve_exact_call",
                                      return_value=_resolved_call()) as exact, \
                    mock.patch.object(trade_service, "_held_call_capacity",
                                      return_value={"NVDA": {
                                          "current_shares": 500,
                                          "held_short_calls": 0,
                                          "capacity_contracts": 5,
                                      }}), \
                    mock.patch.object(ibkr_trade, "place_orders",
                                      return_value=[{"order_id": "9"}]) as place:
                trade_service.save_basket(basket)
                self._arm_cc_preview(token, order)
                res = trade_service._trade_place({
                    "trades": basket, "account": "DU1", "confirm": True, "token": token,
                })
        exact.assert_called_once_with("NVDA", "2026-08-21", 105.0)
        place.assert_called_once()
        self.assertEqual(res["placed"], [{"order_id": "9"}])

    def test_place_refreshes_limit_within_current_spread(self):
        basket = trade_service._normalize_basket([_cc_leg()])
        token = trade_service._basket_token("DU1", basket)
        self._arm_cc_preview(token, self._cc_order(quantity=2, price=2.50))
        with mock.patch.object(ibkr_trade, "trading_enabled", return_value=True), \
                mock.patch.object(ibkr_trade, "accounts", return_value=[{"accountId": "DU1"}]), \
                mock.patch.object(ibkr_trade, "live_orders", return_value=[]), \
                mock.patch.object(
                    ibkr_trade, "resolve_exact_call",
                    return_value=_resolved_call(bid=2.45, ask=2.65),
                ), \
                mock.patch.object(trade_service, "save_basket"), \
                mock.patch.object(ibkr_trade, "place_orders",
                                  return_value=[{"order_id": "10"}]) as place:
            trade_service._trade_place({
                "trades": basket, "account": "DU1", "confirm": True, "token": token,
            })
        placed_order = place.call_args.args[1][0]
        self.assertEqual(placed_order["price"], 2.55)
        self.assertEqual(placed_order["premium_credit"], 510.0)

    def test_place_limit_drift_uses_resolver_tick_for_nested_rules(self):
        basket = trade_service._normalize_basket([_cc_leg()])
        token = trade_service._basket_token("DU1", basket)
        self._arm_cc_preview(token, self._cc_order(quantity=2, price=2.51))
        resolved = _resolved_call(
            bid=2.54,
            ask=2.56,
            tick=0.05,
            rules={"incrementRules": [{"lowerEdge": "0", "increment": "0.05"}]},
        )
        with mock.patch.object(ibkr_trade, "trading_enabled", return_value=True), \
                mock.patch.object(ibkr_trade, "accounts", return_value=[{"accountId": "DU1"}]), \
                mock.patch.object(ibkr_trade, "live_orders", return_value=[]), \
                mock.patch.object(ibkr_trade, "resolve_exact_call", return_value=resolved), \
                mock.patch.object(trade_service, "save_basket"), \
                mock.patch.object(ibkr_trade, "place_orders",
                                  return_value=[{"order_id": "tick"}]) as place:
            trade_service._trade_place({
                "trades": basket, "account": "DU1", "confirm": True, "token": token,
            })
        self.assertEqual(place.call_args.args[1][0]["price"], 2.55)

    def test_mixed_place_submits_covered_call_before_stock_sale(self):
        basket = trade_service._normalize_basket([
            {"symbol": "NVDA", "delta_czk": -10_000},
            _cc_leg(contracts=1),
        ])
        token = trade_service._basket_token("DU1", basket)
        trade_service._preview_issued[token] = {
            "issued_at": time.time(),
            "orders": [
                {"symbol": "NVDA", "side": "SELL", "quantity": 100},
                self._cc_order(quantity=1),
            ],
            "working_fingerprint": trade_service._working_fingerprint([]),
            "working_symbols": ["NVDA"],
            "working_option_symbols": ["NVDA"],
            "working_option_conids": [555],
            "working_available": True,
        }
        with mock.patch.object(ibkr_trade, "trading_enabled", return_value=True), \
                mock.patch.object(ibkr_trade, "accounts", return_value=[{"accountId": "DU1"}]), \
                mock.patch.object(ibkr_trade, "live_orders", return_value=[]), \
                mock.patch.object(ibkr_trade, "resolve_exact_call",
                                  return_value=_resolved_call()), \
                mock.patch.object(trade_service, "_live_positions", return_value=[{
                    "assetClass": "STK", "contractDesc": "NVDA", "position": 300,
                }]), \
                mock.patch.object(trade_service, "save_basket"), \
                mock.patch.object(
                    ibkr_trade, "place_orders", return_value=[{"order_id": "11"}],
                ) as place:
            trade_service._trade_place({
                "trades": basket, "account": "DU1", "confirm": True, "token": token,
            })
        submitted = place.call_args.args[1]
        self.assertEqual(submitted[0]["instrument_type"], "covered_call")
        self.assertNotEqual(submitted[1].get("instrument_type"), "covered_call")

    def test_place_rejects_large_limit_move(self):
        basket = trade_service._normalize_basket([_cc_leg()])
        token = trade_service._basket_token("DU1", basket)
        self._arm_cc_preview(token, self._cc_order(quantity=2, price=2.50))
        with mock.patch.object(ibkr_trade, "trading_enabled", return_value=True), \
                mock.patch.object(ibkr_trade, "accounts", return_value=[{"accountId": "DU1"}]), \
                mock.patch.object(ibkr_trade, "live_orders", return_value=[]), \
                mock.patch.object(
                    ibkr_trade, "resolve_exact_call",
                    return_value=_resolved_call(bid=3.00, ask=3.20),
                ), \
                mock.patch.object(ibkr_trade, "place_orders") as place:
            with self.assertRaises(apierror.Conflict) as ctx:
                trade_service._trade_place({
                    "trades": basket, "account": "DU1", "confirm": True, "token": token,
                })
        self.assertIn("limit moved", str(ctx.exception))
        place.assert_not_called()

    def test_place_time_coverage_is_aggregated_across_strikes(self):
        orders = [
            self._cc_order(conid=555, strike=105.0, quantity=2),
            self._cc_order(conid=666, strike=110.0, quantity=2),
        ]
        with mock.patch.object(
            ibkr_trade,
            "resolve_exact_call",
            side_effect=[_resolved_call(), _resolved_call(conid=666, strike=110.0)],
        ), mock.patch.object(
            trade_service, "_live_positions", return_value=[{
                "assetClass": "STK", "contractDesc": "NVDA", "position": 300,
            }],
        ):
            with self.assertRaises(apierror.Conflict) as ctx:
                trade_service._revalidate_covered_call_orders("DU1", orders, [])
        self.assertIn("4 contract(s) need shares", str(ctx.exception))
        self.assertIn("300 shares would remain", str(ctx.exception))

    def test_place_time_coverage_accounts_for_stock_sells_in_same_basket(self):
        orders = [
            {"symbol": "NVDA", "side": "SELL", "quantity": 150},
            self._cc_order(quantity=2),
        ]
        with mock.patch.object(
            ibkr_trade, "resolve_exact_call", return_value=_resolved_call(),
        ), mock.patch.object(
            trade_service, "_live_positions", return_value=[{
                "assetClass": "STK", "contractDesc": "NVDA", "position": 300,
            }],
        ):
            with self.assertRaises(apierror.Conflict) as ctx:
                trade_service._revalidate_covered_call_orders("DU1", orders, [])
        self.assertIn("2 contract(s) need shares", str(ctx.exception))
        self.assertIn("150 shares would remain", str(ctx.exception))

    def test_place_blocks_stock_sell_that_would_uncover_held_calls(self):
        orders = [{"symbol": "NVDA", "side": "SELL", "quantity": 150}]
        rows = [
            {"assetClass": "STK", "contractDesc": "NVDA", "position": 250},
            {
                "assetClass": "OPT", "contractDesc": "NVDA AUG 21 '26 105 Call",
                "putOrCall": "CALL", "position": -2,
            },
        ]
        with mock.patch.object(trade_service, "_live_positions", return_value=rows):
            with self.assertRaises(apierror.Conflict) as ctx:
                trade_service._revalidate_covered_call_orders("DU1", orders, [])
        self.assertIn("2 contract(s) need shares", str(ctx.exception))
        self.assertIn("100 shares would remain", str(ctx.exception))

    def test_place_rejects_contract_revalidation_failure(self):
        basket = trade_service._normalize_basket([_cc_leg()])
        token = trade_service._basket_token("DU1", basket)
        self._arm_cc_preview(token, self._cc_order())
        with mock.patch.object(ibkr_trade, "trading_enabled", return_value=True), \
                mock.patch.object(ibkr_trade, "accounts", return_value=[{"accountId": "DU1"}]), \
                mock.patch.object(ibkr_trade, "live_orders", return_value=[]), \
                mock.patch.object(ibkr_trade, "resolve_exact_call", return_value=None), \
                mock.patch.object(ibkr_trade, "place_orders") as place:
            with self.assertRaises(apierror.Conflict) as ctx:
                trade_service._trade_place({
                    "trades": basket, "account": "DU1", "confirm": True, "token": token,
                })
        self.assertIn("could not be revalidated", str(ctx.exception))
        place.assert_not_called()

    def test_place_rejects_crossed_quote_at_place_time(self):
        basket = trade_service._normalize_basket([_cc_leg()])
        token = trade_service._basket_token("DU1", basket)
        self._arm_cc_preview(token, self._cc_order())
        with mock.patch.object(ibkr_trade, "trading_enabled", return_value=True), \
                mock.patch.object(ibkr_trade, "accounts", return_value=[{"accountId": "DU1"}]), \
                mock.patch.object(ibkr_trade, "live_orders", return_value=[]), \
                mock.patch.object(ibkr_trade, "resolve_exact_call",
                                  return_value=_resolved_call(bid=2.80, ask=2.60)), \
                mock.patch.object(ibkr_trade, "place_orders") as place:
            with self.assertRaises(apierror.Conflict) as ctx:
                trade_service._trade_place({
                    "trades": basket, "account": "DU1", "confirm": True, "token": token,
                })
        self.assertIn("quote is missing or crossed", str(ctx.exception))
        place.assert_not_called()

    def test_place_rejects_coverage_shrink_after_preview(self):
        basket = trade_service._normalize_basket([_cc_leg(contracts=2)])
        token = trade_service._basket_token("DU1", basket)
        order = self._cc_order(quantity=2)
        working_raw = [{
            "orderId": "w1", "ticker": "NVDA  260821C00105000", "conid": 555,
            "secType": "OPT", "side": "SELL", "remainingQuantity": 1, "status": "Submitted",
        }]
        fresh_working = trade_service._normalized_working_orders(
            working_raw, set(), {"NVDA"}, {555},
        )
        trade_service._preview_issued[token] = {
            "issued_at": time.time(),
            "orders": [order],
            "working_fingerprint": trade_service._working_fingerprint(fresh_working),
            "working_symbols": [],
            "working_option_symbols": ["NVDA"],
            "working_option_conids": [555],
            "working_available": True,
        }
        with mock.patch.object(ibkr_trade, "trading_enabled", return_value=True), \
                mock.patch.object(ibkr_trade, "accounts", return_value=[{"accountId": "DU1"}]), \
                mock.patch.object(ibkr_trade, "live_orders", return_value=working_raw), \
                mock.patch.object(ibkr_trade, "resolve_exact_call",
                                  return_value=_resolved_call()), \
                mock.patch.object(trade_service, "_live_positions", return_value=[{
                    "assetClass": "STK", "contractDesc": "NVDA", "position": 200,
                }]), \
                mock.patch.object(ibkr_trade, "place_orders") as place:
            with self.assertRaises(apierror.Conflict) as ctx:
                trade_service._trade_place({
                    "trades": basket, "account": "DU1", "confirm": True, "token": token,
                })
        self.assertIn("coverage changed", str(ctx.exception))
        place.assert_not_called()

    def test_tampered_place_body_rejected_by_token_mismatch(self):
        basket = trade_service._normalize_basket([_cc_leg(contracts=2)])
        token = trade_service._basket_token("DU1", basket)
        self._arm_cc_preview(token, self._cc_order(quantity=2))
        tampered = trade_service._normalize_basket([_cc_leg(contracts=5)])
        with mock.patch.object(ibkr_trade, "trading_enabled", return_value=True), \
                mock.patch.object(ibkr_trade, "accounts", return_value=[{"accountId": "DU1"}]), \
                mock.patch.object(ibkr_trade, "live_orders", return_value=[]), \
                mock.patch.object(ibkr_trade, "place_orders") as place:
            with self.assertRaises(ValueError) as ctx:
                trade_service._trade_place({
                    "trades": tampered, "account": "DU1", "confirm": True, "token": token,
                })
        self.assertIn("token mismatch", str(ctx.exception))
        place.assert_not_called()


if __name__ == "__main__":
    unittest.main()
