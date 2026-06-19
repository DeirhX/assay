"""Tests for the gated live-trading client (ibkr_trade) and serve.py's trade
guards. No network: every gateway call is mocked. The point is to prove the
SAFETY logic -- flag gating, paper/live detection, preview-token binding, the
confirmation-reply loop, and the CZK->shares sizing -- holds without ever
touching the real Client Portal Gateway."""

from __future__ import annotations

import os
import unittest
from unittest import mock

import _support  # noqa: F401
import apierror
import ibkr_trade as ibt
import trade_service


def _env(**overrides):
    """Context manager: a clean os.environ with only the given keys, plus an
    empty secrets.env, so flag resolution is deterministic."""
    base = {k: v for k, v in os.environ.items()
            if not k.startswith("IBKR_")}
    base.update(overrides)
    return mock.patch.dict(os.environ, base, clear=True)


class SharesSizing(unittest.TestCase):
    def test_buy_rounds_to_whole_shares(self):
        # 23000 CZK / (100 USD * 23 CZK/USD) = 10 shares.
        self.assertEqual(ibt.shares_for(23000, 100, 23.0), 10)

    def test_sell_keeps_sign(self):
        self.assertEqual(ibt.shares_for(-23000, 100, 23.0), -10)

    def test_rounds_to_nearest(self):
        self.assertEqual(ibt.shares_for(2300 * 1.04, 100, 23.0), 1)

    def test_zero_price_is_unsized(self):
        self.assertEqual(ibt.shares_for(1000, 0, 23.0), 0)

    def test_non_base_currency_at_par(self):
        self.assertEqual(ibt.shares_for(1000, 100, 1.0), 10)


class PaperLiveDetection(unittest.TestCase):
    def test_paper_prefix(self):
        self.assertTrue(ibt.is_paper_account("DU1234567"))
        self.assertEqual(ibt.account_kind("DU1234567"), "paper")

    def test_live_prefix(self):
        # Synthetic 6-digit id (not a real Uxxxxxxx) so the personal-data
        # pre-commit hook doesn't flag the fixture; the prefix is what matters.
        self.assertFalse(ibt.is_paper_account("U999000"))
        self.assertEqual(ibt.account_kind("U999000"), "live")


class FlagGating(unittest.TestCase):
    def test_trading_disabled_by_default(self):
        with _env(), mock.patch.object(ibt, "load_env_file", return_value={}):
            self.assertFalse(ibt.trading_enabled())
            self.assertFalse(ibt.live_allowed())

    def test_trading_enabled_via_env(self):
        with _env(IBKR_TRADING_ENABLED="1"), mock.patch.object(ibt, "load_env_file", return_value={}):
            self.assertTrue(ibt.trading_enabled())

    def test_live_allowed_via_env(self):
        with _env(IBKR_ALLOW_LIVE="true"), mock.patch.object(ibt, "load_env_file", return_value={}):
            self.assertTrue(ibt.live_allowed())

    def test_gateway_base_default_and_override(self):
        with _env(), mock.patch.object(ibt, "load_env_file", return_value={}):
            self.assertEqual(ibt.gateway_base(), ibt.DEFAULT_GATEWAY_BASE)
        with _env(IBKR_GATEWAY_BASE="https://localhost:9999/v1/api/"), \
                mock.patch.object(ibt, "load_env_file", return_value={}):
            self.assertEqual(ibt.gateway_base(), "https://localhost:9999/v1/api")


class ConidResolution(unittest.TestCase):
    def setUp(self):
        ibt._conid_cache.clear()

    def test_picks_exact_stock_match(self):
        rows = [
            {"conid": 111, "symbol": "AMDX", "sections": [{"secType": "OPT"}]},
            {"conid": 222, "symbol": "AMD", "sections": [{"secType": "STK"}]},
        ]
        with mock.patch.object(ibt, "_request", return_value=rows) as req:
            self.assertEqual(ibt.resolve_conid("AMD"), 222)
            # Second call is served from cache -- no extra request.
            self.assertEqual(ibt.resolve_conid("AMD"), 222)
            self.assertEqual(req.call_count, 1)

    def test_returns_none_when_empty(self):
        with mock.patch.object(ibt, "_request", return_value=[]):
            self.assertIsNone(ibt.resolve_conid("NOPE"))


class BuildOrders(unittest.TestCase):
    def _lookups(self, prices, conids):
        return (lambda s: prices.get(s), lambda s: conids.get(s))

    def test_sizes_and_signs(self):
        prices = {"AMD": {"price": 100, "fx_to_base": 23.0}}
        conids = {"AMD": 222}
        pl, cl = self._lookups(prices, conids)
        orders, warns = ibt.build_orders(
            [{"symbol": "AMD", "delta_czk": 23000}],
            price_lookup=pl, conid_lookup=cl, account_id="DU1")
        self.assertEqual(len(orders), 1)
        o = orders[0]
        self.assertEqual((o["side"], o["quantity"], o["conid"]), ("BUY", 10, 222))
        self.assertEqual(o["symbol"], "AMD")
        self.assertFalse(warns)

    def test_skips_unresolved_unpriced_and_zero(self):
        prices = {"AMD": {"price": 100, "fx_to_base": 23.0}, "BAD": None}
        conids = {"AMD": 222, "BAD": 999, "NOID": None}
        pl, cl = self._lookups(prices, conids)
        orders, warns = ibt.build_orders(
            [
                {"symbol": "AMD", "delta_czk": 5},      # rounds to 0 shares
                {"symbol": "BAD", "delta_czk": 23000},  # no price
                {"symbol": "NOID", "delta_czk": 23000}, # no conid
            ],
            price_lookup=pl, conid_lookup=cl, account_id="DU1")
        self.assertEqual(orders, [])
        self.assertEqual(len(warns), 3)

    def test_buy_limit_uses_buy_below_and_gtc(self):
        prices = {"AMD": {"price": 100, "fx_to_base": 23.0}}
        conids = {"AMD": 222}
        pl, cl = self._lookups(prices, conids)
        # A BUY gets a limit at buy_below; a SELL would get trim_above.
        limit = lambda sym, side: 92.0 if (sym == "AMD" and side == "BUY") else None
        orders, _ = ibt.build_orders(
            [{"symbol": "AMD", "delta_czk": 23000}],
            price_lookup=pl, conid_lookup=cl, account_id="DU1", limit_lookup=limit)
        o = orders[0]
        self.assertEqual(o["orderType"], "LMT")
        self.assertEqual(o["price"], 92.0)
        self.assertEqual(o["tif"], "GTC")
        self.assertEqual(o["side"], "BUY")

    def test_sell_limit_uses_trim_above(self):
        prices = {"NVDA": {"price": 130, "fx_to_base": 23.0}}
        conids = {"NVDA": 333}
        pl, cl = self._lookups(prices, conids)
        limit = lambda sym, side: 145.0 if side == "SELL" else None
        orders, _ = ibt.build_orders(
            [{"symbol": "NVDA", "delta_czk": -23000}],
            price_lookup=pl, conid_lookup=cl, account_id="DU1", limit_lookup=limit)
        o = orders[0]
        self.assertEqual((o["side"], o["orderType"], o["price"], o["tif"]),
                         ("SELL", "LMT", 145.0, "GTC"))

    def test_no_limit_stays_market(self):
        prices = {"AMD": {"price": 100, "fx_to_base": 23.0}}
        conids = {"AMD": 222}
        pl, cl = self._lookups(prices, conids)
        orders, _ = ibt.build_orders(
            [{"symbol": "AMD", "delta_czk": 23000}],
            price_lookup=pl, conid_lookup=cl, account_id="DU1",
            limit_lookup=lambda sym, side: None)
        o = orders[0]
        self.assertEqual(o["orderType"], "MKT")
        self.assertNotIn("price", o)
        self.assertEqual(o["tif"], "DAY")


class LockedLimitLookup(unittest.TestCase):
    """trade_service._locked_limit resolves the basket symbol to a provider
    symbol and reads the locked level server-side (never from the client)."""

    def test_buy_and_sell_sides(self):
        import price_levels
        level = {"buy_below": 92.0, "trim_above": 145.0}
        with mock.patch.object(trade_service, "provider_symbol_for", lambda s: s), \
                mock.patch.object(price_levels, "get", lambda s: level if s == "AMD" else None):
            self.assertEqual(trade_service._locked_limit("AMD", "BUY"), 92.0)
            self.assertEqual(trade_service._locked_limit("AMD", "SELL"), 145.0)
            self.assertIsNone(trade_service._locked_limit("ZZZ", "BUY"))


class ReplyLoop(unittest.TestCase):
    def test_confirms_prompts_until_accepted(self):
        prompt = [{"id": "abc", "message": ["Are you sure?"]}]
        ack = [{"order_id": "1", "order_status": "Submitted"}]
        with mock.patch.object(ibt, "_request", side_effect=[prompt, ack]) as req:
            out = ibt.place_orders("DU1", [{"conid": 222, "side": "BUY", "quantity": 1,
                                            "orderType": "MKT", "tif": "DAY", "symbol": "AMD"}])
        self.assertEqual(out, ack)
        # Second call must be the reply to the prompt id.
        self.assertIn("/iserver/reply/abc", req.call_args_list[1].args[1])

    def test_strips_display_fields_before_sending(self):
        ack = [{"order_id": "1"}]
        with mock.patch.object(ibt, "_request", return_value=ack) as req:
            ibt.place_orders("DU1", [{"conid": 222, "side": "BUY", "quantity": 1,
                                      "orderType": "MKT", "tif": "DAY", "symbol": "AMD"}])
        sent = req.call_args.args[2]["orders"][0]
        self.assertNotIn("symbol", sent)
        self.assertIn("conid", sent)


class TradeServiceGuards(unittest.TestCase):
    def setUp(self):
        ibt._conid_cache.clear()

    def test_basket_token_is_stable_and_account_bound(self):
        basket = trade_service._normalize_basket([{"symbol": "amd", "delta_czk": 100}])
        t1 = trade_service._basket_token("DU1", basket)
        t2 = trade_service._basket_token("DU1", basket)
        t3 = trade_service._basket_token("DU2", basket)
        self.assertEqual(t1, t2)
        self.assertNotEqual(t1, t3)

    def test_place_refused_when_disabled(self):
        with mock.patch.object(ibt, "trading_enabled", return_value=False):
            with self.assertRaises(apierror.Forbidden):
                trade_service._trade_place({"trades": [{"symbol": "AMD", "delta_czk": 1000}],
                                            "confirm": True, "token": "x"})

    def test_place_rejects_token_mismatch(self):
        with mock.patch.object(ibt, "trading_enabled", return_value=True), \
                mock.patch.object(ibt, "accounts", return_value=[{"accountId": "DU1"}]):
            with self.assertRaises(ValueError):
                trade_service._trade_place({"trades": [{"symbol": "AMD", "delta_czk": 1000}],
                                            "confirm": True, "token": "wrong"})

    def test_place_locks_live_account(self):
        basket = trade_service._normalize_basket([{"symbol": "AMD", "delta_czk": 1000}])
        token = trade_service._basket_token("U777", basket)
        with mock.patch.object(ibt, "trading_enabled", return_value=True), \
                mock.patch.object(ibt, "live_allowed", return_value=False), \
                mock.patch.object(ibt, "accounts", return_value=[{"accountId": "U777"}]):
            with self.assertRaises(apierror.Forbidden):
                trade_service._trade_place({"trades": [{"symbol": "AMD", "delta_czk": 1000}],
                                            "account": "U777", "confirm": True, "token": token})

    def test_place_requires_confirm(self):
        with mock.patch.object(ibt, "trading_enabled", return_value=True):
            with self.assertRaises(ValueError):
                trade_service._trade_place({"trades": [{"symbol": "AMD", "delta_czk": 1000}], "token": "x"})

    def test_happy_path_places_on_paper(self):
        basket = trade_service._normalize_basket([{"symbol": "AMD", "delta_czk": 1000}])
        token = trade_service._basket_token("DU1", basket)
        order = {"symbol": "AMD", "conid": 222, "side": "BUY", "quantity": 1,
                 "orderType": "MKT", "tif": "DAY"}
        with mock.patch.object(ibt, "trading_enabled", return_value=True), \
                mock.patch.object(ibt, "accounts", return_value=[{"accountId": "DU1"}]), \
                mock.patch.object(trade_service, "_prepare_trade_orders", return_value=([order], [])), \
                mock.patch.object(ibt, "place_orders",
                                  return_value=[{"order_id": "1"}]) as place:
            res = trade_service._trade_place({"trades": [{"symbol": "AMD", "delta_czk": 1000}],
                                              "account": "DU1", "confirm": True, "token": token})
        self.assertEqual(res["account"], "DU1")
        self.assertEqual(res["placed"], [{"order_id": "1"}])
        place.assert_called_once()


if __name__ == "__main__":
    unittest.main()
