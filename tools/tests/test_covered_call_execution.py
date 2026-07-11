"""Backend tests for covered-call exit execution (typed basket, resolution, staging)."""

from __future__ import annotations

import datetime as dt
import time
import unittest
import urllib.parse
from unittest import mock

import _support  # noqa: F401
import apierror
import exit_plan
import ibkr_trade as ibt
import options_overlay as ov
import overview
import trade_service
import whatif


class _OptGw:
    """Minimal gateway for resolve_option_contract tests."""

    def __init__(self, *, months="AUG26", strikes=None, spot="100.0", quotes=None, underlying=500):
        self.months = months
        self.strikes = strikes or {"call": [100, 105, 110], "put": []}
        self.spot = spot
        self.quotes = quotes or {}
        self.underlying = underlying

    @staticmethod
    def opt_conid(strike, right="C"):
        return int(round(float(strike) * 100)) * 10 + (1 if str(right).upper().startswith("C") else 2)

    def __call__(self, method, endpoint, body=None):
        path, _, qs = endpoint.partition("?")
        q = {k: v[0] for k, v in urllib.parse.parse_qs(qs).items()}
        if path == "/iserver/secdef/search":
            return [{
                "conid": self.underlying,
                "symbol": q.get("symbol"),
                "sections": [
                    {"secType": "STK"},
                    {"secType": "OPT", "months": self.months},
                ],
            }]
        if path == "/iserver/secdef/strikes":
            return dict(self.strikes)
        if path == "/iserver/secdef/info":
            ocid = self.opt_conid(float(q["strike"]), q["right"])
            return [{"conid": ocid, "maturityDate": "20260821"}]
        if path == "/iserver/marketdata/snapshot":
            rows = []
            for c in (int(x) for x in q["conids"].split(",")):
                if c == self.underlying:
                    rows.append({"conid": c, "31": self.spot, "84": "99.5", "86": "100.5"})
                elif c in self.quotes:
                    rows.append({"conid": c, **self.quotes[c]})
                else:
                    rows.append({"conid": c})
            return rows
        if path.startswith("/iserver/contract/") and path.endswith("/info-and-rules"):
            return {"rules": {"increment": "0.05"}}
        return {}


class ResolveOptionContract(unittest.TestCase):
    def setUp(self):
        ibt._conid_cache.clear()

    def test_exact_resolve_with_quotes(self):
        gw = _OptGw()
        ocid = gw.opt_conid(105)
        gw.quotes = {ocid: {"31": "2.50", "84": "2.40", "86": "2.60"}}
        with mock.patch.object(ibt, "_request", gw):
            row = ibt.resolve_option_contract("NVDA", "2026-08-21", 105, "C")
        self.assertEqual(row["conid"], ocid)
        self.assertEqual(row["expiry"], "2026-08-21")
        self.assertAlmostEqual(row["bid"], 2.40)
        self.assertAlmostEqual(row["ask"], 2.60)
        self.assertEqual(row["multiplier"], 100)
        self.assertEqual(row["underlying_conid"], gw.underlying)

    def test_for_execution_rejects_crossed_quote(self):
        gw = _OptGw()
        ocid = gw.opt_conid(105)
        gw.quotes = {ocid: {"84": "2.70", "86": "2.60"}}
        with mock.patch.object(ibt, "_request", gw):
            with self.assertRaises(ibt.QuoteError):
                ibt.resolve_option_contract("NVDA", "2026-08-21", 105, for_execution=True)

    def test_for_execution_rejects_missing_two_sided_quote(self):
        gw = _OptGw()
        ocid = gw.opt_conid(105)
        gw.quotes = {ocid: {"31": "2.50", "84": "2.40"}}
        with mock.patch.object(ibt, "_request", gw):
            with self.assertRaisesRegex(ibt.QuoteError, "missing bid/ask"):
                ibt.resolve_option_contract("NVDA", "2026-08-21", 105, for_execution=True)

    def test_exact_resolve_rejects_mismatched_expiry(self):
        with mock.patch.object(ibt, "option_months", return_value=(500, ["AUG26"])), \
                mock.patch.object(ibt, "option_strikes", return_value={"call": [105.0], "put": []}), \
                mock.patch.object(ibt, "option_info", return_value={
                    "conid": 42, "expiry": "2026-08-28", "strike": 105.0, "right": "C",
                }):
            with self.assertRaisesRegex(ibt.CPAPIError, "resolved expiry"):
                ibt.resolve_option_contract("NVDA", "2026-08-21", 105)

    def test_round_sell_limit_midpoint_ticks(self):
        rules = {"incrementRules": [{"lowerEdge": "0", "increment": "0.05"}]}
        px = ibt.round_sell_limit_midpoint(2.40, 2.60, rules)
        self.assertAlmostEqual(px, 2.50)


class TypedBasket(unittest.TestCase):
    def test_legacy_stock_canonicalizes(self):
        basket = trade_service._normalize_basket([{"symbol": "amd", "delta_czk": 100}])
        self.assertEqual(len(basket), 1)
        leg = basket[0]
        self.assertEqual(leg["leg_type"], "stock")
        self.assertEqual(leg["leg_id"], "stock:AMD")
        self.assertEqual(leg["delta_czk"], 100.0)

    def test_stock_legs_net_and_keep_provenance(self):
        p1 = {"route": "exit", "tranche_index": 1}
        p2 = {"route": "exit", "tranche_index": 2}
        basket = trade_service._normalize_basket([
            {"symbol": "AMD", "delta_czk": -100, "provenance": p1},
            {"symbol": "AMD", "delta_czk": -50, "provenance": p2},
        ])
        self.assertEqual(len(basket), 1)
        self.assertEqual(basket[0]["delta_czk"], -150.0)
        self.assertEqual(len(basket[0]["provenance"]), 2)

    def test_covered_call_restaging_replaces_not_doubles(self):
        leg_a = trade_service.make_covered_call_leg({
            "symbol": "AMD", "conid": 111, "expiry": "2026-08-21", "strike": 105.0,
            "right": "C", "bid": 2.4, "ask": 2.6, "last": 2.5, "quote_at": "t",
            "underlying_conid": 1, "rules": {},
        }, 1, [{"route": "exit"}])
        leg_b = trade_service.make_covered_call_leg({
            "symbol": "AMD", "conid": 111, "expiry": "2026-08-21", "strike": 105.0,
            "right": "C", "bid": 2.5, "ask": 2.7, "last": 2.6, "quote_at": "t2",
            "underlying_conid": 1, "rules": {},
        }, 2, [{"route": "exit", "note": "restage"}])
        merged = trade_service._normalize_basket([leg_a, leg_b])
        cc = [x for x in merged if x["leg_type"] == "covered_call"]
        self.assertEqual(len(cc), 1)
        self.assertEqual(cc[0]["contracts"], 2)

    def test_token_includes_full_canonical_leg(self):
        a = trade_service._normalize_basket([{"symbol": "AMD", "delta_czk": 100}])
        b = trade_service._normalize_basket([{
            "leg_type": "stock", "leg_id": "stock:AMD", "symbol": "AMD",
            "delta_czk": 100.0, "provenance": [],
        }])
        self.assertEqual(
            trade_service._basket_token("DU1", a),
            trade_service._basket_token("DU1", b),
        )
        tampered = [{"leg_type": "stock", "leg_id": "stock:AMD", "symbol": "AMD",
                     "delta_czk": 200.0, "provenance": []}]
        self.assertNotEqual(
            trade_service._basket_token("DU1", a),
            trade_service._basket_token("DU1", tampered),
        )

    def test_option_token_changes_with_contract_or_limit_tampering(self):
        leg = trade_service.make_covered_call_leg({
            "symbol": "AMD", "conid": 111, "expiry": "2026-08-21", "strike": 105,
            "right": "C", "bid": 2.4, "ask": 2.6, "last": 2.5,
            "quote_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "underlying_conid": 1, "rules": {},
        }, 1)
        token = trade_service._basket_token("DU1", [leg])
        for field, value in (("conid", 222), ("strike", 110), ("limit_price", 2.6),
                             ("contracts", 2)):
            changed = {**leg, field: value}
            self.assertNotEqual(token, trade_service._basket_token("DU1", [changed]), field)

    def test_malformed_non_object_leg_is_rejected_not_silently_dropped(self):
        with self.assertRaisesRegex(ValueError, "each trade must be an object"):
            trade_service._normalize_basket([{"symbol": "AMD", "delta_czk": 100}, "oops"])

    def test_rebalance_stock_replace_preserves_server_staged_options(self):
        option = trade_service._canonical_covered_call_leg(
            symbol="AMD", contracts=1, conid=999, expiry="2026-08-21",
            strike=105, right="C", limit_price=2.5, quote={}, underlying_quote={})
        with mock.patch.object(trade_service, "load_basket", return_value=[option]), \
                mock.patch.object(trade_service, "save_basket",
                                  side_effect=lambda rows: trade_service._normalize_basket(rows)):
            out = trade_service.replace_stock_basket([
                {"symbol": "NVDA", "delta_czk": 1000}])
        self.assertEqual({leg["leg_type"] for leg in out}, {"stock", "covered_call"})

    def test_direct_option_basket_post_shape_is_rejected(self):
        option = trade_service._canonical_covered_call_leg(
            symbol="AMD", contracts=1, conid=999, expiry="2026-08-21",
            strike=105, right="C", limit_price=2.5, quote={}, underlying_quote={})
        with self.assertRaisesRegex(ValueError, "must be staged from the Exit plan"):
            trade_service.replace_stock_basket([option])

    def test_remove_basket_leg_deletes_server_known_option(self):
        stock = trade_service._canonical_stock_leg("NVDA", 1000)
        option = trade_service._canonical_covered_call_leg(
            symbol="AMD", contracts=1, conid=999, expiry="2026-08-21",
            strike=105, right="C", limit_price=2.5, quote={}, underlying_quote={})
        with mock.patch.object(trade_service, "load_basket", return_value=[stock, option]), \
                mock.patch.object(trade_service, "save_basket", side_effect=lambda rows: rows):
            out = trade_service.remove_basket_leg(option["leg_id"])
        self.assertEqual(out, [stock])

    def test_remove_basket_leg_rejects_stale_identifier(self):
        stock = trade_service._canonical_stock_leg("NVDA", 1000)
        with mock.patch.object(trade_service, "load_basket", return_value=[stock]):
            with self.assertRaises(apierror.Conflict):
                trade_service.remove_basket_leg("covered_call:AMD:2026-08-21:105:C")

    def test_preview_option_must_exactly_match_server_staged_leg(self):
        option = trade_service._canonical_covered_call_leg(
            symbol="AMD", contracts=1, conid=999, expiry="2026-08-21",
            strike=105, right="C", limit_price=2.5, quote={}, underlying_quote={},
            provenance=[{"route": "covered_call", "plan_fingerprint": "abc"}])
        with mock.patch.object(trade_service, "load_basket", return_value=[option]):
            trade_service._assert_server_staged_option_legs([option])
            mutated = {**option, "contracts": 2}
            with self.assertRaisesRegex(ValueError, "exact server-staged Exit leg"):
                trade_service._assert_server_staged_option_legs([mutated])


class CoverageHelpers(unittest.TestCase):
    HOLDINGS = {
        "positions": [
            {"symbol": "AMD", "asset_class": "STK", "quantity": 300.0},
            {"symbol": "AMD  260821C00105000", "asset_class": "OPT", "quantity": -1.0,
             "mark_price": 2.5, "market_value": -250.0},
        ],
    }

    def test_available_shares_subtracts_held_and_working_short_calls(self):
        working = [{
            "order_id": "1",
            "symbol": "AMD  260821C00110000",
            "side": "SELL",
            "remaining_qty": 1,
            "conid": 999,
            "status": "Submitted",
        }]
        avail = trade_service.covered_shares_available("AMD", self.HOLDINGS, working)
        self.assertEqual(avail, 100)

    def test_conid_only_working_option_is_not_misclassified_as_stock(self):
        rows = trade_service._normalized_working_orders([{
            "orderId": "1", "ticker": "AMD", "conid": 999, "side": "SELL",
            "remainingQuantity": 1, "status": "Submitted",
        }], {"AMD"}, {999})
        self.assertEqual(rows[0]["instrument_type"], "option")
        self.assertEqual(rows[0]["option_right"], "C")
        self.assertEqual(
            trade_service.covered_shares_available("AMD", self.HOLDINGS, rows), 100)

    def test_same_contract_working_order_counts_toward_staged_total(self):
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        leg = trade_service._canonical_covered_call_leg(
            symbol="AMD", contracts=2, conid=999, expiry="2026-08-21",
            strike=105, right="C", limit_price=2.5,
            quote={"bid": 2.4, "ask": 2.6, "last": 2.5, "quote_at": now},
            underlying_quote={"last": 100, "quote_at": now},
        )
        working = [{
            "order_id": "1", "symbol": "AMD", "instrument_type": "option",
            "option_right": "C", "conid": 999, "side": "SELL",
            "remaining_qty": 1, "status": "Submitted",
        }]
        resolved = {
            "symbol": "AMD", "conid": 999, "expiry": "2026-08-21",
            "strike": 105, "right": "C", "bid": 2.4, "ask": 2.6, "last": 2.5,
            "quote_at": now, "multiplier": 100, "underlying_conid": 1,
            "underlying_bid": 99.9, "underlying_ask": 100.1,
            "underlying_last": 100, "underlying_quote_at": now, "rules": {},
        }
        with mock.patch.object(ibt, "resolve_option_contract", return_value=resolved):
            refreshed = trade_service._validate_covered_call_leg(
                leg, self.HOLDINGS, working)
        self.assertEqual(refreshed["contracts"], 2)

    def test_different_contract_working_order_does_not_reduce_residual(self):
        working = [{
            "order_id": "1", "symbol": "AMD", "instrument_type": "option",
            "option_right": "C", "conid": 1110, "side": "SELL",
            "remaining_qty": 1, "status": "Submitted",
        }]
        self.assertEqual(
            trade_service._working_short_call_contracts(
                "AMD", working, conid=999),
            0,
        )

    def test_fully_working_contract_can_revalidate_with_no_free_shares(self):
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        leg = trade_service._canonical_covered_call_leg(
            symbol="AMD", contracts=1, conid=999, expiry="2026-08-21",
            strike=105, right="C", limit_price=2.5,
            quote={"bid": 2.4, "ask": 2.6, "last": 2.5, "quote_at": now},
            underlying_quote={"last": 100, "quote_at": now},
        )
        holdings = {"positions": [
            {"symbol": "AMD", "asset_class": "STK", "quantity": 100.0},
        ]}
        working = [{
            "order_id": "1", "symbol": "AMD", "instrument_type": "option",
            "option_right": "C", "conid": 999, "side": "SELL",
            "remaining_qty": 1, "status": "Submitted",
        }]
        resolved = {
            "symbol": "AMD", "conid": 999, "expiry": "2026-08-21",
            "strike": 105, "right": "C", "bid": 2.4, "ask": 2.6, "last": 2.5,
            "quote_at": now, "multiplier": 100, "underlying_conid": 1,
            "underlying_bid": 99.9, "underlying_ask": 100.1,
            "underlying_last": 100, "underlying_quote_at": now, "rules": {},
        }
        with mock.patch.object(ibt, "resolve_option_contract", return_value=resolved):
            refreshed = trade_service._validate_covered_call_leg(
                leg, holdings, working)
        self.assertEqual(refreshed["contracts"], 1)

    @staticmethod
    def _resolved(symbol, expiry, strike, right="C", **_kwargs):
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        conid = 1000 + int(strike)
        return {
            "symbol": symbol, "conid": conid, "expiry": expiry, "strike": strike,
            "right": right, "bid": 2.4, "ask": 2.6, "last": 2.5,
            "quote_at": now, "multiplier": 100, "underlying_conid": 1,
            "underlying_bid": 99.9, "underlying_ask": 100.1,
            "underlying_last": 100, "underlying_quote_at": now, "rules": {},
        }

    def _leg(self, strike, contracts):
        return trade_service.make_covered_call_leg(
            self._resolved("AMD", "2026-08-21", strike), contracts)

    def test_multiple_strikes_cannot_double_spend_coverage(self):
        holdings = {"positions": [
            {"symbol": "AMD", "asset_class": "STK", "quantity": 300.0},
        ]}
        basket = [self._leg(105, 2), self._leg(110, 2)]
        with mock.patch.object(ibt, "resolve_option_contract", side_effect=self._resolved):
            with self.assertRaisesRegex(ValueError, "mixed basket leaves only 100 shares"):
                trade_service._prepare_covered_call_orders(
                    basket, holdings, [], "assay-test", [])

    def test_stock_sell_and_calls_cannot_overcommit_same_shares(self):
        holdings = {"positions": [
            {"symbol": "AMD", "asset_class": "STK", "quantity": 300.0},
        ]}
        basket = [self._leg(105, 2)]
        stock_orders = [{"leg_type": "stock", "symbol": "AMD", "side": "SELL", "quantity": 200}]
        with mock.patch.object(ibt, "resolve_option_contract", side_effect=self._resolved):
            with self.assertRaisesRegex(ValueError, "mixed basket leaves only 100 shares"):
                trade_service._prepare_covered_call_orders(
                    basket, holdings, [], "assay-test", stock_orders)

    def test_working_call_on_other_strike_does_not_cover_staged_contracts(self):
        holdings = {"positions": [
            {"symbol": "AMD", "asset_class": "STK", "quantity": 200.0},
        ]}
        basket = [self._leg(105, 2)]
        working = [{
            "order_id": "1", "symbol": "AMD", "instrument_type": "option",
            "option_right": "C", "conid": 1110, "side": "SELL",
            "remaining_qty": 1, "status": "Submitted",
        }]
        with mock.patch.object(ibt, "resolve_option_contract", side_effect=self._resolved):
            with self.assertRaisesRegex(ValueError, "mixed basket leaves only 100 shares"):
                trade_service._prepare_covered_call_orders(
                    basket, holdings, working, "assay-test", [])


class QuoteFreshness(unittest.TestCase):
    def test_stale_and_future_quotes_fail_closed(self):
        stale = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=61)).isoformat()
        future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=5)).isoformat()
        self.assertFalse(trade_service._quote_fresh(stale))
        self.assertFalse(trade_service._quote_fresh(future))


class OptionsOverlayExecutable(unittest.TestCase):
    AS_OF = dt.datetime(2026, 7, 1, tzinfo=dt.timezone.utc)

    def test_ibkr_rung_marked_executable_with_metadata(self):
        chain = {
            "source": "ibkr",
            "underlying_price": 100.0,
            "quote_at": "2026-07-01T12:00:00+00:00",
            "expiries": [{
                "expiry": "2026-08-07",
                "calls": [{
                    "conid": 12345,
                    "strike": 105.0,
                    "bid": 3.0,
                    "ask": 3.2,
                    "last": 3.1,
                    "quote_at": "2026-07-01T12:00:00+00:00",
                    "multiplier": 100,
                }],
                "puts": [],
            }],
        }
        pos = {"qty": 200.0, "price_local": 100.0, "fx": 1.0}
        out = ov.suggest_for_position("TEST", pos, {"defer_lots": []}, as_of=self.AS_OF,
                                      chain=chain, rate=0.04)
        rung = out["covered_call_ladder"][0]
        self.assertTrue(rung["executable"])
        self.assertEqual(rung["conid"], 12345)
        self.assertEqual(rung["bid"], 3.0)
        self.assertFalse(rung["estimate"])


class StageCoveredCall(unittest.TestCase):
    def _plan(self):
        return {
            "as_of": "2026-07-10", "snapshot": "2026-07-10T12:00:00+00:00",
            "config": {},
            "positions": [{
                "symbol": "AMD", "quantity": 300, "current_czk": 30000,
                "options": {
                    "route_contracts": 2,
                    "covered_call_ladder": [{
                        "source": "ibkr", "estimate": False, "executable": True,
                        "conid": 1105, "expiry": "2026-08-21", "strike": 105,
                    }],
                },
            }],
        }

    @staticmethod
    def _resolved():
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        return {
            "symbol": "AMD", "conid": 1105, "expiry": "2026-08-21",
            "strike": 105, "right": "C", "bid": 2.4, "ask": 2.6, "last": 2.5,
            "quote_at": now, "multiplier": 100, "underlying_conid": 1,
            "underlying_bid": 99.9, "underlying_ask": 100.1,
            "underlying_last": 100, "underlying_quote_at": now, "rules": {},
        }

    def test_staging_rejects_coverage_consumed_by_another_staged_strike(self):
        holdings = {"positions": [
            {"symbol": "AMD", "asset_class": "STK", "quantity": 300},
        ]}
        other = trade_service._canonical_covered_call_leg(
            symbol="AMD", contracts=2, conid=1110, expiry="2026-08-21",
            strike=110, right="C", limit_price=2.0, quote={}, underlying_quote={})
        with mock.patch.object(exit_plan.store, "load", return_value=holdings), \
                mock.patch.object(ibt, "auth_status", return_value={"authenticated": True}), \
                mock.patch.object(ibt, "live_orders", return_value=[]), \
                mock.patch.object(ibt, "resolve_option_contract", return_value=self._resolved()), \
                mock.patch.object(trade_service, "load_basket", return_value=[other]):
            with self.assertRaisesRegex(ValueError, "existing staged legs leave only 100 shares"):
                exit_plan.stage_covered_call(self._plan(), "AMD", 0)


class WhatifExcludesOptions(unittest.TestCase):
    def test_typed_basket_ignores_covered_call_legs(self):
        holdings = {
            "base_currency": "CZK",
            "net_asset_value": 1_000_000,
            "positions": [
                {"symbol": "AMD", "base_market_value": 600_000},
                {"symbol": "REST", "base_market_value": 400_000},
            ],
        }
        model = {"targets": {"AMD": {"low": 5, "high": 10, "rule": "hold"}}}
        trades = [
            {"leg_type": "stock", "leg_id": "stock:AMD", "symbol": "AMD",
             "delta_czk": -100_000, "provenance": []},
            {"leg_type": "covered_call", "leg_id": "cc", "symbol": "AMD",
             "contracts": 1, "conid": 1, "expiry": "2026-08-21", "strike": 105,
             "right": "C", "limit_price": 3.0, "multiplier": 100,
             "quote": {}, "underlying_quote": {}, "provenance": []},
        ]
        sim = whatif.simulate(holdings, model, trades)
        amd = next(r for r in sim["after"]["rows"] if r.get("name") == "AMD")
        self.assertAlmostEqual(amd["current_pct"], 55.56, places=1)

    def test_overview_counts_option_legs_without_inventing_czk_value(self):
        summary = overview.staged_basket_summary([
            {"leg_type": "stock", "symbol": "AMD", "delta_czk": -1000},
            {"leg_type": "covered_call", "symbol": "AMD", "contracts": 2},
        ])
        self.assertEqual(summary["count"], 2)
        self.assertEqual(summary["sells"], 2)
        self.assertEqual(summary["option_legs"], 1)
        self.assertEqual(summary["option_contracts"], 2)
        self.assertEqual(summary["total_abs_czk"], 1000)


class PreviewPlaceSafety(unittest.TestCase):
    def setUp(self):
        ibt._conid_cache.clear()
        trade_service._preview_issued.clear()

    def test_place_rejects_token_tamper(self):
        basket = trade_service._normalize_basket([{"symbol": "AMD", "delta_czk": 1000}])
        token = trade_service._basket_token("DU1", basket)
        with mock.patch.object(ibt, "trading_enabled", return_value=True), \
                mock.patch.object(ibt, "accounts", return_value=[{"accountId": "DU1"}]):
            with self.assertRaises(ValueError):
                trade_service._trade_place({
                    "trades": [{"symbol": "AMD", "delta_czk": 2000}],
                    "account": "DU1", "confirm": True, "token": token,
                })

    def test_place_rejects_option_reprice_after_preview(self):
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        leg = trade_service._canonical_covered_call_leg(
            symbol="AMD", contracts=1, conid=999, expiry="2026-08-21",
            strike=105, right="C", limit_price=2.5,
            quote={"bid": 2.4, "ask": 2.6, "last": 2.5, "quote_at": now},
            underlying_quote={"last": 100, "quote_at": now},
        )
        token = trade_service._basket_token("DU1", [leg])
        preview_order = {
            "leg_type": "covered_call", "conid": 999, "side": "SELL",
            "quantity": 1, "orderType": "LMT", "price": 2.5,
        }
        fresh_order = {**preview_order, "price": 2.6}
        trade_service._preview_issued[token] = {
            "issued_at": time.time(), "orders": [preview_order], "basket": [leg],
            "working_fingerprint": trade_service._working_fingerprint([]),
            "working_symbols": ["AMD"], "working_conids": [999],
            "working_available": True,
        }
        with mock.patch.object(ibt, "trading_enabled", return_value=True), \
                mock.patch.object(ibt, "accounts", return_value=[{"accountId": "DU1"}]), \
                mock.patch.object(ibt, "live_orders", return_value=[]), \
                mock.patch.object(trade_service, "_load", return_value={"positions": []}), \
                mock.patch.object(trade_service, "_validate_covered_call_leg", return_value=leg), \
                mock.patch.object(trade_service, "_prepare_trade_orders",
                                  return_value=([fresh_order], [])), \
                mock.patch.object(trade_service, "_reconcile_working_orders",
                                  return_value=([fresh_order], [], [])), \
                mock.patch.object(ibt, "place_orders") as place:
            with self.assertRaises(apierror.Conflict):
                trade_service._trade_place({
                    "trades": [leg], "account": "DU1", "confirm": True, "token": token,
                })
        place.assert_not_called()


if __name__ == "__main__":
    unittest.main()
