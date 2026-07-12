"""Tests for the gated live-trading client (ibkr_trade) and serve.py's trade
guards. No network: every gateway call is mocked. The point is to prove the
SAFETY logic -- flag gating, paper/live detection, preview-token binding, the
confirmation-reply loop, and the CZK->shares sizing -- holds without ever
touching the real Client Portal Gateway."""

from __future__ import annotations

import datetime as dt
import os
import time
import unittest
import urllib.parse
from unittest import mock

import _support  # noqa: F401
import apierror
import config
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
        with _env(), mock.patch.object(config, "read_env_file", return_value={}):
            self.assertFalse(ibt.trading_enabled())
            self.assertFalse(ibt.live_allowed())

    def test_trading_enabled_via_env(self):
        with _env(IBKR_TRADING_ENABLED="1"), mock.patch.object(config, "read_env_file", return_value={}):
            self.assertTrue(ibt.trading_enabled())

    def test_live_allowed_via_env(self):
        with _env(IBKR_ALLOW_LIVE="true"), mock.patch.object(config, "read_env_file", return_value={}):
            self.assertTrue(ibt.live_allowed())

    def test_gateway_base_default_and_override(self):
        with _env(), mock.patch.object(config, "read_env_file", return_value={}):
            self.assertEqual(ibt.gateway_base(), ibt.DEFAULT_GATEWAY_BASE)
        with _env(IBKR_GATEWAY_BASE="https://localhost:9999/v1/api/"), \
                mock.patch.object(config, "read_env_file", return_value={}):
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
        def limit(sym, side):
            return 92.0 if (sym == "AMD" and side == "BUY") else None
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
        def limit(sym, side):
            return 145.0 if side == "SELL" else None
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

    def test_ladder_uses_outermost_tranche(self):
        # A multi-tranche ladder resolves the BUY limit to the highest (first to
        # trigger) buy price and the SELL limit to the lowest trim price.
        import price_levels
        level = {
            "fair_value": 400.0,
            "buy_ladder": [{"price": 360.0, "size_pct": 0.5}, {"price": 320.0, "size_pct": 0.5}],
            "trim_ladder": [{"price": 500.0, "size_pct": 0.5}, {"price": 600.0, "size_pct": 0.5}],
        }
        with mock.patch.object(trade_service, "provider_symbol_for", lambda s: s), \
                mock.patch.object(price_levels, "get", lambda s: level if s == "AVGO" else None):
            self.assertEqual(trade_service._locked_limit("AVGO", "BUY"), 360.0)
            self.assertEqual(trade_service._locked_limit("AVGO", "SELL"), 500.0)


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

    def test_places_each_basket_order_in_its_own_request(self):
        # A basket must NOT go in one array (CPAPI would read it as a bracket and
        # demand parentId on the children); each order is its own POST.
        a1 = [{"order_id": "1", "order_status": "Submitted"}]
        a2 = [{"order_id": "2", "order_status": "Submitted"}]
        orders = [
            {"conid": 1, "side": "BUY", "quantity": 1, "orderType": "MKT", "tif": "DAY", "symbol": "AMD"},
            {"conid": 2, "side": "BUY", "quantity": 1, "orderType": "MKT", "tif": "DAY", "symbol": "NVDA"}]
        with mock.patch.object(ibt, "_request", side_effect=[a1, a2]) as req:
            out = ibt.place_orders("DU1", orders)
        self.assertEqual(out, [a1[0], a2[0]])          # both acks collected
        self.assertEqual(len(req.call_args_list), 2)   # one request per order
        for call in req.call_args_list:
            self.assertEqual(len(call.args[2]["orders"]), 1)


class PreviewAggregation(unittest.TestCase):
    ORDERS = [
        {"conid": 1, "side": "BUY", "quantity": 2, "orderType": "MKT", "tif": "DAY", "symbol": "AMD"},
        {"conid": 2, "side": "BUY", "quantity": 1, "orderType": "MKT", "tif": "DAY", "symbol": "NVDA"}]

    # Previews fan out on a thread pool, so a positional side_effect list would be
    # racy -- key the mock responses by the order's conid instead.
    @staticmethod
    def _by_conid(mapping):
        def _fn(_method, _endpoint, body):
            conid = body["orders"][0]["conid"]
            resp = mapping[conid]
            if isinstance(resp, Exception):
                raise resp
            return resp
        return _fn

    def test_previews_each_order_separately_and_recombines(self):
        r1 = {"amount": {"amount": "1,000 USD", "commission": "1.0 USD"},
              "initial": {"current": "5,000 USD", "change": "200 USD", "after": "5,200 USD"},
              "maintenance": {"current": "4,000 USD", "change": "150 USD", "after": "4,150 USD"}}
        r2 = {"amount": {"amount": "500 USD", "commission": "1.0 USD"},
              "initial": {"current": "5,000 USD", "change": "100 USD", "after": "5,100 USD"},
              "maintenance": {"current": "4,000 USD", "change": "50 USD", "after": "4,050 USD"}}
        with mock.patch.object(ibt, "_request", side_effect=self._by_conid({1: r1, 2: r2})) as req:
            out = ibt.preview_orders("DU1", self.ORDERS)
        # One whatif POST per order, each a single-element array (never a bracket).
        self.assertEqual(len(req.call_args_list), 2)
        for call in req.call_args_list:
            self.assertTrue(call.args[1].endswith("/orders/whatif"))
            self.assertEqual(len(call.args[2]["orders"]), 1)
        # Order value + commission are additive and summed exactly.
        self.assertEqual(out["amount"]["amount"], "1500 USD")
        self.assertEqual(out["amount"]["commission"], "2 USD")
        # Margin: shared pre-basket current, summed change, after = current+change.
        self.assertEqual(out["initial"], {"current": "5000 USD", "change": "300 USD", "after": "5300 USD"})
        self.assertEqual(out["maintenance"]["after"], "4200 USD")
        self.assertEqual(out["basket_orders"], 2)

    def test_single_order_preview_is_returned_verbatim(self):
        r1 = {"amount": {"amount": "1,000 USD", "commission": "1.0 USD"}}
        with mock.patch.object(ibt, "_request", return_value=r1) as req:
            out = ibt.preview_orders("DU1", self.ORDERS[:1])
        self.assertEqual(out, r1)
        self.assertEqual(len(req.call_args_list), 1)

    def test_a_per_order_error_is_surfaced_not_swallowed(self):
        good = {"amount": {"amount": "1,000 USD"}}
        bad = {"error": "no market data permissions for this contract"}
        with mock.patch.object(ibt, "_request", side_effect=self._by_conid({1: good, 2: bad})):
            with self.assertRaises(ibt.CPAPIError) as ctx:
                ibt.preview_orders("DU1", self.ORDERS)
        self.assertIn("NVDA", str(ctx.exception))
        self.assertIn("market data", str(ctx.exception))

    def test_hard_gateway_rejection_is_attributed_and_explained(self):
        # The PRIIPs/KID block on a US ETF comes back as a raised 500, not a body
        # error. It must be caught, tied to the symbol, and translated -- and it
        # must NOT abort the sibling order's preview (both are still attempted).
        good = {"amount": {"amount": "1,000 USD"}}
        kid = ibt.CPAPIError('gateway HTTP 500: {"error":"No Trading Permission, '
                             'Customer Ineligible; Ineligibility reasons: \\nThis product '
                             'does not have a KID in English ..."}', status=500)
        with mock.patch.object(ibt, "_request", side_effect=self._by_conid({1: good, 2: kid})) as req:
            with self.assertRaises(ibt.CPAPIError) as ctx:
                ibt.preview_orders("DU1", self.ORDERS)
        msg = str(ctx.exception)
        self.assertIn("NVDA", msg)          # attributed to the offending symbol
        self.assertIn("UCITS", msg)          # translated to the actionable gist
        self.assertIn("options", msg.lower())  # names the real path to exposure
        self.assertNotIn("Ineligibility reasons", msg)  # raw legal wall dropped
        self.assertEqual(len(req.call_args_list), 2)     # sibling still previewed
        # The blocked symbol is carried out-of-band so the service layer can learn
        # it and convert future buys to options-only (the good sibling stays off it).
        self.assertEqual(getattr(ctx.exception, "kid_symbols", None), ["NVDA"])


class ModifyOrder(unittest.TestCase):
    def test_reprices_and_clears_prompts(self):
        prompt = [{"id": "z9", "message": ["Price cap?"]}]
        ack = [{"order_id": "5", "order_status": "Submitted"}]
        with mock.patch.object(ibt, "_request", side_effect=[prompt, ack]) as req:
            out = ibt.modify_order("DU1", "5", {"conid": 222, "orderType": "LMT",
                                                "side": "SELL", "price": 9.94, "tif": "GTC",
                                                "symbol": "AMD"})
        self.assertEqual(out, ack)
        # First POST hits the order's own path; the second is the reply.
        self.assertIn("/iserver/account/DU1/order/5", req.call_args_list[0].args[1])
        self.assertIn("/iserver/reply/z9", req.call_args_list[1].args[1])
        # Display-only fields are stripped; the new price is sent.
        self.assertNotIn("symbol", req.call_args_list[0].args[2])
        self.assertEqual(req.call_args_list[0].args[2]["price"], 9.94)


class TickForPrice(unittest.TestCase):
    def test_banded_increment_picks_the_applicable_band(self):
        rules = {"incrementRules": [{"lowerEdge": "0", "increment": "0.0001"},
                                    {"lowerEdge": "1", "increment": "0.01"}]}
        self.assertAlmostEqual(ibt.tick_for_price(rules, 0.5), 0.0001)
        self.assertAlmostEqual(ibt.tick_for_price(rules, 5.0), 0.01)

    def test_accepts_nested_rules_and_flat_increment(self):
        self.assertAlmostEqual(ibt.tick_for_price({"rules": {"increment": "0.05"}}, 5.0), 0.05)

    def test_default_when_absent_or_zero(self):
        self.assertAlmostEqual(ibt.tick_for_price({}, 5.0), 0.01)
        self.assertAlmostEqual(ibt.tick_for_price({"incrementRules": []}, 5.0), 0.01)
        self.assertAlmostEqual(ibt.tick_for_price({"increment": 0}, 5.0), 0.01)


class LiveOrders(unittest.TestCase):
    """A half-up CPAPI session answers the orders endpoint with either 500
    'Please query /accounts first' (never primed) or 400 'Bad Request: no bridge'
    (auth is fine but the orders bridge isn't connected yet). live_orders must
    self-heal both: prime the session and retry once, rather than bubbling a 502."""

    def test_returns_orders_on_the_happy_path(self):
        with mock.patch.object(ibt, "_request", return_value={"orders": [{"orderId": "1"}]}) as req:
            self.assertEqual(ibt.live_orders(), [{"orderId": "1"}])
        req.assert_called_once_with("GET", "/iserver/account/orders")

    def test_primes_an_already_authenticated_session_then_retries(self):
        # Brokerage session is up but was never queried: a GET /iserver/accounts
        # is enough, no reauth needed.
        unprimed = ibt.CPAPIError('gateway HTTP 500: {"error":"Please query /accounts first"}', status=500)
        with mock.patch.object(ibt, "_request", side_effect=[
            unprimed,                       # orders: unprimed
            {"authenticated": True},        # auth_status
            {"accounts": ["U1"]},           # /iserver/accounts primer
            {"orders": [{"orderId": "9"}]},  # retry succeeds
        ]) as req:
            self.assertEqual(ibt.live_orders(), [{"orderId": "9"}])
        self.assertEqual(
            [c.args for c in req.call_args_list],
            [("GET", "/iserver/account/orders"),
             ("POST", "/iserver/auth/status"),
             ("GET", "/iserver/accounts"),
             ("GET", "/iserver/account/orders")],
        )

    def test_reauthenticates_when_the_brokerage_session_is_down_then_retries(self):
        # Only the SSO/web session is up (the real failure seen live): iserver
        # is unauthenticated, so ssodh/init must bring it up before querying
        # accounts and re-fetching orders.
        unprimed = ibt.CPAPIError('gateway HTTP 500: {"error":"Please query /accounts first"}', status=500)
        with mock.patch.object(ibt, "_request", side_effect=[
            unprimed,                        # orders: unprimed
            {"authenticated": False},        # auth_status -> session down
            {"authenticated": True},         # ssodh/init (reauthenticate)
            {"accounts": ["U1"]},            # /iserver/accounts primer
            {"orders": [{"orderId": "7"}]},  # retry succeeds
        ]) as req:
            self.assertEqual(ibt.live_orders(), [{"orderId": "7"}])
        self.assertEqual(
            [c.args for c in req.call_args_list],
            [("GET", "/iserver/account/orders"),
             ("POST", "/iserver/auth/status"),
             ("POST", "/iserver/auth/ssodh/init", {"publish": True, "compete": True}),
             ("GET", "/iserver/accounts"),
             ("GET", "/iserver/account/orders")],
        )

    def test_primes_and_retries_on_no_bridge(self):
        # Auth/status is healthy but the orders subsystem's backend bridge isn't
        # up yet (the live 502 the user hit): a prime nudge (/iserver/accounts)
        # + retry clears it without a reconnect.
        no_bridge = ibt.CPAPIError('gateway HTTP 400: {"error":"Bad Request: no bridge","statusCode":400}', status=400)
        with mock.patch.object(ibt, "_request", side_effect=[
            no_bridge,                       # orders: bridge not up
            {"authenticated": True},         # auth_status -> already authed
            {"accounts": ["U1"]},            # /iserver/accounts primer
            {"orders": [{"orderId": "3"}]},  # retry succeeds
        ]) as req:
            self.assertEqual(ibt.live_orders(), [{"orderId": "3"}])
        self.assertEqual(
            [c.args for c in req.call_args_list],
            [("GET", "/iserver/account/orders"),
             ("POST", "/iserver/auth/status"),
             ("GET", "/iserver/accounts"),
             ("GET", "/iserver/account/orders")],
        )

    def test_does_not_retry_on_an_unrelated_error(self):
        boom = ibt.CPAPIError("gateway HTTP 503: service unavailable", status=503)
        with mock.patch.object(ibt, "_request", side_effect=boom) as req:
            with self.assertRaises(ibt.CPAPIError):
                ibt.live_orders()
        req.assert_called_once_with("GET", "/iserver/account/orders")


class TradeServiceGuards(unittest.TestCase):
    def setUp(self):
        ibt._conid_cache.clear()
        trade_service._preview_issued.clear()

    @staticmethod
    def _arm_preview(token: str, age_s: float = 0.0) -> None:
        """Register a preview time for a token, as _trade_preview would."""
        trade_service._preview_issued[token] = {
            "issued_at": time.time() - age_s,
            "orders": [{"symbol": "AMD", "conid": 222, "side": "BUY", "quantity": 1,
                        "orderType": "MKT", "tif": "DAY"}],
            "working_fingerprint": trade_service._working_fingerprint([]),
            "working_symbols": ["AMD"],
            "working_available": True,
        }

    def test_basket_token_is_stable_and_account_bound(self):
        basket = trade_service._normalize_basket([{"symbol": "amd", "delta_czk": 100}])
        t1 = trade_service._basket_token("DU1", basket)
        t2 = trade_service._basket_token("DU1", basket)
        t3 = trade_service._basket_token("DU2", basket)
        self.assertEqual(t1, t2)
        self.assertNotEqual(t1, t3)

    def test_projection_review_is_bound_to_exact_queue_revision(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(trade_service, "STAGED_BASKET_JSON",
                                  Path(tmp) / "staged-basket.json"):
            first = [{"symbol": "AMD", "delta_czk": 1000}]
            trade_service.save_basket(first)
            state = trade_service.basket_state()
            self.assertFalse(state["reviewed"])
            reviewed = trade_service.review_basket(state["revision"])
            self.assertTrue(reviewed["reviewed"])

            # Re-saving identical content is not a mutation; changing even one
            # amount invalidates review and any already-issued IBKR preview.
            trade_service._preview_issued["old"] = {"issued_at": time.time()}
            trade_service.save_basket(first)
            self.assertTrue(trade_service.basket_state()["reviewed"])
            self.assertIn("old", trade_service._preview_issued)
            trade_service.save_basket([{"symbol": "AMD", "delta_czk": 2000}])
            self.assertFalse(trade_service.basket_state()["reviewed"])
            self.assertFalse(trade_service._preview_issued)
            with self.assertRaises(apierror.Conflict):
                trade_service.review_basket(state["revision"])

    def test_queue_leg_can_be_excluded_and_included_without_deletion(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(trade_service, "STAGED_BASKET_JSON",
                                  Path(tmp) / "staged-basket.json"):
            trades = [
                {"symbol": "AMD", "delta_czk": 1000},
                {"symbol": "ARM", "delta_czk": 500},
            ]
            trade_service.save_basket(trades)
            initial = trade_service.basket_state()
            trade_service.review_basket(initial["revision"])

            trade_service.set_basket_leg_included("stock:AMD", False)
            excluded = trade_service.basket_state()
            self.assertEqual([leg["symbol"] for leg in excluded["trades"]], ["ARM"])
            self.assertEqual(
                {leg["symbol"]: leg["included"] for leg in excluded["queue_trades"]},
                {"AMD": False, "ARM": True},
            )
            self.assertEqual(excluded["excluded_leg_ids"], ["stock:AMD"])
            self.assertFalse(excluded["reviewed"])
            self.assertEqual(
                [leg["symbol"] for leg in trade_service.load_basket()],
                ["ARM"],
            )

            reviewed = trade_service.review_basket(excluded["revision"])
            self.assertTrue(reviewed["reviewed"])
            self.assertEqual(len(reviewed["queue_trades"]), 2)

            trade_service.set_basket_leg_included("stock:AMD", True)
            restored = trade_service.basket_state()
            self.assertEqual(
                [leg["symbol"] for leg in restored["trades"]],
                ["AMD", "ARM"],
            )
            self.assertEqual(restored["excluded_leg_ids"], [])
            self.assertFalse(restored["reviewed"])

    def test_basket_state_reads_persisted_queue_once(self):
        raw = {"trades": [{"symbol": "AMD", "delta_czk": 1000}]}
        with mock.patch.object(trade_service, "_load", return_value=raw) as load:
            state = trade_service.basket_state()
        self.assertEqual(
            sum(
                call.args == (trade_service.STAGED_BASKET_JSON,)
                for call in load.call_args_list
            ),
            1,
        )
        self.assertEqual(state["trades"][0]["symbol"], "AMD")

    def test_preview_gate_rejects_unreviewed_or_changed_queue(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(trade_service, "STAGED_BASKET_JSON",
                                  Path(tmp) / "staged-basket.json"):
            trades = [{"symbol": "AMD", "delta_czk": 1000}]
            trade_service.save_basket(trades)
            state = trade_service.basket_state()
            body = {"trades": trades, "queue_revision": state["revision"]}
            with self.assertRaises(apierror.Conflict):
                trade_service._reviewed_preview_basket(body)
            trade_service.review_basket(state["revision"])
            self.assertEqual(trade_service._reviewed_preview_basket(body),
                             trade_service._normalize_basket(trades))
            with self.assertRaises(apierror.Conflict):
                trade_service._reviewed_preview_basket(
                    {"trades": trades, "queue_revision": "stale"})

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

    def test_happy_path_places_on_paper_and_clears_staged_basket(self):
        import tempfile
        from pathlib import Path
        basket = trade_service._normalize_basket([{"symbol": "AMD", "delta_czk": 1000}])
        token = trade_service._basket_token("DU1", basket)
        order = {"symbol": "AMD", "conid": 222, "side": "BUY", "quantity": 1,
                 "orderType": "MKT", "tif": "DAY"}
        with tempfile.TemporaryDirectory() as tmp:
            staged = Path(tmp) / "staged-basket.json"
            with mock.patch.object(trade_service, "STAGED_BASKET_JSON", staged), \
                    mock.patch.object(ibt, "trading_enabled", return_value=True), \
                    mock.patch.object(ibt, "accounts", return_value=[{"accountId": "DU1"}]), \
                    mock.patch.object(trade_service, "_prepare_trade_orders", return_value=([order], [])), \
                    mock.patch.object(ibt, "live_orders", return_value=[]), \
                    mock.patch.object(ibt, "place_orders",
                                      return_value=[{"order_id": "1"}]) as place:
                # The planner staged this basket; a successful place must retire it
                # so the desk can't re-offer an already-submitted basket.
                trade_service.save_basket([{"symbol": "AMD", "delta_czk": 1000}])
                self.assertTrue(staged.exists())
                self._arm_preview(token)
                res = trade_service._trade_place({"trades": [{"symbol": "AMD", "delta_czk": 1000}],
                                                  "account": "DU1", "confirm": True, "token": token})
                self.assertFalse(staged.exists())
                self.assertEqual(trade_service.load_basket(), [])
                # The preview token is consumed with the basket.
                self.assertNotIn(token, trade_service._preview_issued)
        self.assertEqual(res["account"], "DU1")
        self.assertEqual(res["placed"], [{"order_id": "1"}])
        self.assertTrue(res["staged_basket_cleared"])
        place.assert_called_once()

    def test_failed_place_keeps_staged_basket(self):
        import tempfile
        from pathlib import Path
        basket = trade_service._normalize_basket([{"symbol": "AMD", "delta_czk": 1000}])
        token = trade_service._basket_token("DU1", basket)
        order = {"symbol": "AMD", "conid": 222, "side": "BUY", "quantity": 1,
                 "orderType": "MKT", "tif": "DAY"}
        with tempfile.TemporaryDirectory() as tmp:
            staged = Path(tmp) / "staged-basket.json"
            with mock.patch.object(trade_service, "STAGED_BASKET_JSON", staged), \
                    mock.patch.object(ibt, "trading_enabled", return_value=True), \
                    mock.patch.object(ibt, "accounts", return_value=[{"accountId": "DU1"}]), \
                    mock.patch.object(trade_service, "_prepare_trade_orders", return_value=([order], [])), \
                    mock.patch.object(ibt, "live_orders", return_value=[]), \
                    mock.patch.object(ibt, "place_orders",
                                      side_effect=ibt.CPAPIError("gateway down")):
                trade_service.save_basket([{"symbol": "AMD", "delta_czk": 1000}])
                self._arm_preview(token)
                with self.assertRaises(apierror.BadGateway):
                    trade_service._trade_place({"trades": [{"symbol": "AMD", "delta_czk": 1000}],
                                                "account": "DU1", "confirm": True, "token": token})
                # Placement never reached IBKR — the basket stays available to retry.
                self.assertTrue(staged.exists())

    def test_place_rejects_expired_or_unknown_preview(self):
        basket = trade_service._normalize_basket([{"symbol": "AMD", "delta_czk": 1000}])
        token = trade_service._basket_token("DU1", basket)
        body = {"trades": [{"symbol": "AMD", "delta_czk": 1000}],
                "account": "DU1", "confirm": True, "token": token}
        with mock.patch.object(ibt, "trading_enabled", return_value=True), \
                mock.patch.object(ibt, "accounts", return_value=[{"accountId": "DU1"}]):
            # Never previewed on this server run (e.g. a restart) -> expired.
            with self.assertRaises(ValueError) as ctx:
                trade_service._trade_place(dict(body))
            self.assertIn("expired", str(ctx.exception))
            # Previewed too long ago -> expired.
            self._arm_preview(token, age_s=trade_service.PREVIEW_TTL_S + 1)
            with self.assertRaises(ValueError) as ctx2:
                trade_service._trade_place(dict(body))
            self.assertIn("expired", str(ctx2.exception))

    def test_preview_registers_token_and_flags_stale_snapshot(self):
        order = {"symbol": "AMD", "conid": 222, "side": "BUY", "quantity": 1,
                 "orderType": "MKT", "tif": "DAY"}
        stale = {"generated_at": "2020-01-01T00:00:00+00:00", "positions": []}
        with mock.patch.object(ibt, "trading_enabled", return_value=True), \
                mock.patch.object(ibt, "accounts", return_value=[{"accountId": "DU1"}]), \
                mock.patch.object(trade_service, "_reviewed_preview_basket",
                                  return_value=[{"symbol": "AMD", "delta_czk": 1000.0}]), \
                mock.patch.object(trade_service, "_prepare_trade_orders", return_value=([order], [])), \
                mock.patch.object(ibt, "live_orders", return_value=[]), \
                mock.patch.object(ibt, "preview_orders", return_value={}), \
                mock.patch.object(trade_service, "_load", return_value=stale):
            res = trade_service._trade_preview({"trades": [{"symbol": "AMD", "delta_czk": 1000}],
                                                "account": "DU1"})
        self.assertEqual(res["preview_ttl_s"], trade_service.PREVIEW_TTL_S)
        self.assertIn(res["token"], trade_service._preview_issued)
        self.assertTrue(any("snapshot is" in w and "days old" in w for w in res["warnings"]))

    def test_preview_surfaces_unavailable_working_orders_as_safety_blocker(self):
        order = {"symbol": "AMD", "conid": 222, "side": "BUY", "quantity": 1,
                 "orderType": "MKT", "tif": "DAY"}
        with mock.patch.object(ibt, "trading_enabled", return_value=True), \
                mock.patch.object(ibt, "accounts", return_value=[{"accountId": "DU1"}]), \
                mock.patch.object(trade_service, "_reviewed_preview_basket",
                                  return_value=[{"symbol": "AMD", "delta_czk": 1000.0}]), \
                mock.patch.object(trade_service, "_prepare_trade_orders", return_value=([order], [])), \
                mock.patch.object(ibt, "live_orders",
                                  side_effect=ibt.CPAPIError("orders bridge unavailable")), \
                mock.patch.object(ibt, "preview_orders", return_value={}), \
                mock.patch.object(trade_service, "_load", return_value={}):
            res = trade_service._trade_preview({
                "trades": [{"symbol": "AMD", "delta_czk": 1000}], "account": "DU1",
            })
        self.assertFalse(res["working_orders_available"])
        self.assertIn("bridge unavailable", res["working_orders_error"])
        self.assertFalse(trade_service._preview_issued[res["token"]]["working_available"])

    def test_preview_keeps_unquoted_call_visible_and_blocks_placement(self):
        basket = trade_service._normalize_basket([{
            "type": "covered_call",
            "symbol": "PYPL",
            "conid": 555,
            "expiry": "2026-08-21",
            "strike": 75,
            "contracts": 1,
        }])
        blocked = {
            "instrument_type": "covered_call",
            "leg_id": basket[0]["leg_id"],
            "symbol": "PYPL",
            "conid": 555,
            "side": "SELL",
            "classification": "quote_blocked",
            "proposed_qty": 1,
            "residual_qty": 0,
            "placeable": False,
        }

        def prepare(_account, _basket, *, blocked_calls):
            blocked_calls.append(blocked)
            return [], ["PYPL: covered call needs a live bid/ask"]

        with mock.patch.object(ibt, "trading_enabled", return_value=True), \
                mock.patch.object(ibt, "accounts", return_value=[{"accountId": "DU1"}]), \
                mock.patch.object(
                    trade_service, "_reviewed_preview_basket", return_value=basket,
                ), \
                mock.patch.object(
                    trade_service, "_prepare_trade_orders", side_effect=prepare,
                ), \
                mock.patch.object(ibt, "live_orders", return_value=[]), \
                mock.patch.object(ibt, "preview_orders") as ibkr_preview, \
                mock.patch.object(trade_service, "_load", return_value={}):
            res = trade_service._trade_preview({
                "trades": basket, "account": "DU1",
            })
        self.assertTrue(res["placement_blocked"])
        self.assertEqual(res["order_context"][0]["classification"], "quote_blocked")
        self.assertEqual(res["orders"], [])
        ibkr_preview.assert_not_called()

    def test_preview_sends_only_residual_to_ibkr_and_effective_book_to_local_whatif(self):
        proposed = {"symbol": "AMD", "conid": 222, "side": "BUY", "quantity": 10,
                    "orderType": "MKT", "tif": "DAY", "cOID": "assay-x-AMD-10"}
        working = [{"orderId": "4", "ticker": "AMD", "side": "BUY",
                    "remainingQuantity": 4, "status": "Submitted"}]
        holdings = {"generated_at": "2026-07-10T00:00:00+00:00", "positions": []}
        model = {"targets": {}}
        with mock.patch.object(ibt, "trading_enabled", return_value=True), \
                mock.patch.object(ibt, "accounts", return_value=[{"accountId": "DU1"}]), \
                mock.patch.object(trade_service, "_reviewed_preview_basket",
                                  return_value=[{"symbol": "AMD", "delta_czk": 10000.0}]), \
                mock.patch.object(trade_service, "_prepare_trade_orders",
                                  return_value=([proposed], [])), \
                mock.patch.object(ibt, "live_orders", return_value=working), \
                mock.patch.object(ibt, "preview_orders", return_value={}) as ibkr_preview, \
                mock.patch.object(trade_service, "_load", return_value=holdings), \
                mock.patch("target_staging.active_model", return_value=model), \
                mock.patch.object(trade_service.whatif, "simulate",
                                  return_value={"after": {"rows": []}}) as local:
            res = trade_service._trade_preview({
                "trades": [{"symbol": "AMD", "delta_czk": 10000}], "account": "DU1",
            })
        self.assertEqual(res["orders"][0]["quantity"], 6)
        self.assertEqual(res["order_context"][0]["classification"], "same_side_partial")
        ibkr_preview.assert_called_once()
        self.assertEqual(ibkr_preview.call_args.args[1][0]["quantity"], 6)
        local.assert_called_once_with(
            holdings, model, [{"symbol": "AMD", "delta_czk": 10000.0}],
        )

    def test_place_rejects_when_relevant_working_orders_changed(self):
        basket = trade_service._normalize_basket([{"symbol": "AMD", "delta_czk": 1000}])
        token = trade_service._basket_token("DU1", basket)
        self._arm_preview(token)
        changed = [{"orderId": "9", "ticker": "AMD", "side": "BUY",
                    "remainingQuantity": 1, "status": "Submitted"}]
        with mock.patch.object(ibt, "trading_enabled", return_value=True), \
                mock.patch.object(ibt, "accounts", return_value=[{"accountId": "DU1"}]), \
                mock.patch.object(ibt, "live_orders", return_value=changed), \
                mock.patch.object(ibt, "place_orders") as place:
            with self.assertRaises(apierror.Conflict) as ctx:
                trade_service._trade_place({
                    "trades": basket, "account": "DU1", "confirm": True, "token": token,
                })
        self.assertIn("changed", str(ctx.exception))
        place.assert_not_called()


class SessionLifecycle(unittest.TestCase):
    """Reconnect + keepalive are read-only session operations. They remain
    available when order execution is disabled and never turn a gateway hiccup
    into a 5xx."""

    def test_reconnect_available_when_trading_disabled(self):
        with mock.patch.object(ibt, "trading_enabled", return_value=False), \
                mock.patch.object(ibt, "reauthenticate",
                                  return_value={"authenticated": True}) as reauth, \
                mock.patch.object(ibt, "auth_status",
                                  return_value={"authenticated": True, "connected": True}), \
                mock.patch.object(ibt, "accounts", return_value=[]):
            res = trade_service._trade_reconnect()
        reauth.assert_called_once()
        self.assertFalse(res["trading_enabled"])
        self.assertTrue(res["authenticated"])

    def test_reconnect_reauths_and_returns_status(self):
        with mock.patch.object(ibt, "trading_enabled", return_value=True), \
                mock.patch.object(ibt, "reauthenticate", return_value={"authenticated": True}) as reauth, \
                mock.patch.object(ibt, "auth_status",
                                  return_value={"authenticated": True, "connected": True}), \
                mock.patch.object(ibt, "accounts", return_value=[{"accountId": "DU1"}]) as accounts:
            res = trade_service._trade_reconnect()
        reauth.assert_called_once()
        accounts.assert_called_once()  # display + default selection share one response
        self.assertTrue(res["authenticated"])
        self.assertIsNone(res["reconnect_error"])
        self.assertEqual(res["accounts"], [{"id": "DU1", "kind": "paper"}])

    def test_reconnect_reports_failure_in_band(self):
        # A gateway that can't re-init (expired SSO) must surface as a clean
        # 'not connected' + reconnect_error, never as an exception.
        with mock.patch.object(ibt, "trading_enabled", return_value=True), \
                mock.patch.object(ibt, "reauthenticate",
                                  side_effect=ibt.CPAPIError("cannot reach the gateway")), \
                mock.patch.object(ibt, "auth_status", return_value={}):
            res = trade_service._trade_reconnect()
        self.assertFalse(res["authenticated"])
        self.assertIn("cannot reach the gateway", res["reconnect_error"])

    def test_tickle_keeps_data_session_alive_when_trading_disabled(self):
        payload = {"iserver": {"authStatus": {"authenticated": True, "connected": True,
                                              "competing": False}}}
        with mock.patch.object(ibt, "trading_enabled", return_value=False), \
                mock.patch.object(ibt, "tickle", return_value=payload) as tickle:
            res = trade_service._trade_tickle()
        tickle.assert_called_once()
        self.assertEqual(res, {"trading_enabled": False, "authenticated": True,
                               "connected": True, "competing": False})

    def test_tickle_parses_session_from_response(self):
        payload = {"iserver": {"authStatus": {"authenticated": True, "connected": True,
                                              "competing": False}}}
        with mock.patch.object(ibt, "trading_enabled", return_value=True), \
                mock.patch.object(ibt, "tickle", return_value=payload) as t:
            res = trade_service._trade_tickle()
        t.assert_called_once()
        self.assertTrue(res["authenticated"])
        self.assertTrue(res["connected"])
        self.assertFalse(res["competing"])

    def test_tickle_survives_gateway_error(self):
        with mock.patch.object(ibt, "trading_enabled", return_value=True), \
                mock.patch.object(ibt, "tickle", side_effect=ibt.CPAPIError("down")):
            res = trade_service._trade_tickle()
        self.assertFalse(res["authenticated"])
        self.assertTrue(res["trading_enabled"])


class _FakeGateway:
    """A minimal in-memory CPAPI, standing in for ``ibkr_trade._request`` so the
    option-chain builder is exercised entirely offline. Answers secdef/search
    (STK + optional OPT months), secdef/strikes, secdef/info (deterministic option
    conids), contract info-and-rules, and marketdata/snapshot. An option conid
    absent from ``quotes`` returns an empty snapshot row -- exactly the
    no-subscription case."""

    def __init__(self, *, months="AUG26", strikes=None, spot="100.0",
                 quotes=None, has_opt=True, underlying=500, maturity="20260821",
                 maturities=None, rules=None, warm_snapshots=False):
        self.months = months
        self.strikes = strikes or {"call": [95, 100, 105, 110], "put": [90, 95, 100, 105]}
        self.spot = spot
        self.quotes = quotes or {}
        self.has_opt = has_opt
        self.underlying = underlying
        self.maturities = maturities or [maturity]
        self.rules = rules or {"incrementRules": [{"lowerEdge": "0", "increment": "0.05"}]}
        self.warm_snapshots = warm_snapshots
        self.snapshot_calls: dict[str, int] = {}
        self.calls: list[tuple[str, str]] = []

    @staticmethod
    def opt_conid(strike, right):
        """Deterministic synthetic option conid so a test can address a quote."""
        return int(round(float(strike) * 100)) * 10 + (1 if str(right).upper().startswith("C") else 2)

    def __call__(self, method, endpoint, body=None, **_kwargs):
        self.calls.append((method, endpoint))
        path, _, qs = endpoint.partition("?")
        q = {k: v[0] for k, v in urllib.parse.parse_qs(qs).items()}
        if path == "/iserver/secdef/search":
            sections = [{"secType": "STK"}]
            if self.has_opt:
                sections.append({"secType": "OPT", "months": self.months})
            return [{"conid": self.underlying, "symbol": q.get("symbol"), "sections": sections}]
        if path == "/iserver/secdef/strikes":
            return {"call": list(self.strikes["call"]), "put": list(self.strikes["put"])}
        if path == "/iserver/secdef/info":
            ocid = self.opt_conid(float(q["strike"]), q["right"])
            return [
                {"conid": ocid + index, "maturityDate": maturity}
                for index, maturity in enumerate(self.maturities)
            ]
        if path.startswith("/iserver/contract/") and path.endswith("/info-and-rules"):
            return {"rules": self.rules}
        if path == "/iserver/marketdata/snapshot":
            snapshot_key = q["conids"]
            self.snapshot_calls[snapshot_key] = self.snapshot_calls.get(snapshot_key, 0) + 1
            warming = self.warm_snapshots and self.snapshot_calls[snapshot_key] == 1
            rows = []
            for c in (int(x) for x in q["conids"].split(",")):
                if warming:
                    rows.append({"conid": c})
                    continue
                if c == self.underlying:
                    if isinstance(self.spot, dict):
                        rows.append({"conid": c, **self.spot})
                    else:
                        rows.append({"conid": c, "31": self.spot})
                elif c in self.quotes:
                    rows.append({"conid": c, **self.quotes[c]})
                else:
                    rows.append({"conid": c})  # no market-data subscription
            return rows
        return {}


class SnapNum(unittest.TestCase):
    def test_plain_and_close_prefixed(self):
        self.assertEqual(ibt._snap_num("123.45"), 123.45)
        self.assertEqual(ibt._snap_num("C123.45"), 123.45)  # prior close when shut

    def test_percent_becomes_decimal(self):
        self.assertAlmostEqual(ibt._snap_num("25.3%"), 0.253)

    def test_empty_and_junk_are_none(self):
        self.assertIsNone(ibt._snap_num(""))
        self.assertIsNone(ibt._snap_num("n/a"))
        self.assertIsNone(ibt._snap_num(None))


class SnapCount(unittest.TestCase):
    def test_plain_integer(self):
        self.assertEqual(ibt._snap_count("73"), 73)

    def test_multiplier_suffixes(self):
        self.assertEqual(ibt._snap_count("1.2K"), 1200)
        self.assertEqual(ibt._snap_count("3.4M"), 3_400_000)
        self.assertEqual(ibt._snap_count("2B"), 2_000_000_000)

    def test_empty_is_none(self):
        self.assertIsNone(ibt._snap_count(""))
        self.assertIsNone(ibt._snap_count(None))


class OptionMonths(unittest.TestCase):
    def test_orders_from_current_month_and_drops_past(self):
        got = ibt._months_by_date(["JUL26", "AUG26", "JAN26", "DEC26"], as_of=dt.date(2026, 7, 9))
        self.assertEqual(got, ["JUL26", "AUG26", "DEC26"])  # JAN26 is past, dropped


class OptionChain(unittest.TestCase):
    AS_OF = dt.date(2026, 7, 9)

    def setUp(self):
        ibt._conid_cache.clear()

    def test_builds_ibkr_shaped_chain_with_quotes(self):
        gw = _FakeGateway(spot="100.0")
        gw.quotes = {gw.opt_conid(105, "C"): {"31": "2.50", "84": "2.40", "86": "2.60", "7283": "25.0%"}}
        with mock.patch.object(ibt, "_request", gw):
            chain = ibt.option_chain("NVDA", as_of=self.AS_OF)
        self.assertIsNotNone(chain)
        self.assertEqual(chain["source"], "ibkr")
        self.assertEqual(chain["symbol"], "NVDA")
        self.assertEqual(chain["underlying_price"], 100.0)
        self.assertEqual(len(chain["expiries"]), 1)
        exp = chain["expiries"][0]
        self.assertEqual(exp["expiry"], "2026-08-21")
        call105 = next(c for c in exp["calls"] if c["strike"] == 105.0)
        self.assertAlmostEqual(call105["bid"], 2.40)
        self.assertAlmostEqual(call105["ask"], 2.60)
        self.assertAlmostEqual(call105["last"], 2.50)
        self.assertAlmostEqual(call105["implied_vol"], 0.25)

    def test_enriched_fields_delta_volume_open_interest(self):
        # 87 = volume ("1.2K" -> 1200), 7308 = delta, 7638 = open interest.
        gw = _FakeGateway(spot="100.0")
        gw.quotes = {gw.opt_conid(105, "C"): {
            "31": "2.50", "84": "2.40", "86": "2.60", "7283": "25.0%",
            "87": "1.2K", "7308": "0.35", "7638": "4210"}}
        with mock.patch.object(ibt, "_request", gw):
            chain = ibt.option_chain("NVDA", as_of=self.AS_OF)
        call105 = next(c for c in chain["expiries"][0]["calls"] if c["strike"] == 105.0)
        self.assertAlmostEqual(call105["delta"], 0.35)
        self.assertEqual(call105["volume"], 1200)
        self.assertEqual(call105["open_interest"], 4210)
        # A strike with no quote row carries None for the enriched fields, not 0.
        other = next(c for c in chain["expiries"][0]["calls"] if c["strike"] == 110.0)
        self.assertIsNone(other["delta"])
        self.assertIsNone(other["volume"])
        self.assertIsNone(other["open_interest"])

    def test_no_subscription_leaves_prices_none_but_keeps_strikes(self):
        # quotes empty -> every option snapshot row is bare; strikes/expiry still
        # resolve off reference data, so the overlay can estimate the premium.
        gw = _FakeGateway(spot="100.0", quotes={})
        with mock.patch.object(ibt, "_request", gw):
            chain = ibt.option_chain("NVDA", as_of=self.AS_OF)
        self.assertIsNotNone(chain)
        exp = chain["expiries"][0]
        self.assertTrue(exp["calls"])
        for c in exp["calls"]:
            self.assertIsNone(c["bid"])
            self.assertIsNone(c["ask"])
            self.assertIsNone(c["last"])

    def test_name_without_options_returns_none(self):
        gw = _FakeGateway(has_opt=True)
        gw.has_opt = False
        with mock.patch.object(ibt, "_request", gw):
            self.assertIsNone(ibt.option_chain("KO", as_of=self.AS_OF))

    def test_strike_window_bounds_resolution(self):
        # Calls are targeted above spot and remain within the configured window.
        gw = _FakeGateway(spot="100.0", strikes={
            "call": [40, 60, 90, 100, 110, 140, 200],
            "put": [40, 60, 90, 100, 110, 140, 200],
        })
        with mock.patch.object(ibt, "_request", gw):
            chain = ibt.option_chain("NVDA", as_of=self.AS_OF, strike_window_pct=0.25)
        strikes = [c["strike"] for c in chain["expiries"][0]["calls"]]
        self.assertTrue(all(75.0 <= s <= 125.0 for s in strikes))
        self.assertIn(110.0, strikes)
        self.assertNotIn(90.0, strikes)
        self.assertNotIn(40.0, strikes)
        self.assertNotIn(200.0, strikes)

    def test_preserves_weekly_expiries_and_drops_expired_rows(self):
        gw = _FakeGateway(
            months="JUL26",
            maturities=["20260710", "20260717", "20260724", "20260731"],
        )
        with mock.patch.object(ibt, "_request", gw):
            chain = ibt.option_chain("NVDA", as_of=dt.date(2026, 7, 11))
        self.assertEqual(
            [expiry["expiry"] for expiry in chain["expiries"]],
            ["2026-07-17", "2026-07-24", "2026-07-31"],
        )

    def test_repeats_bare_market_snapshot_after_preflight(self):
        gw = _FakeGateway(
            spot="100.0",
            quotes={_FakeGateway.opt_conid(105, "C"): {
                "84": "2.40", "86": "2.60",
            }},
            warm_snapshots=True,
        )
        with mock.patch.object(ibt, "_request", gw), \
                mock.patch.object(ibt, "OPTION_SNAPSHOT_WARMUP_SECONDS", 0):
            chain = ibt.option_chain("NVDA", as_of=self.AS_OF)
        call = next(c for c in chain["expiries"][0]["calls"] if c["strike"] == 105.0)
        self.assertEqual((call["bid"], call["ask"]), (2.4, 2.6))
        self.assertTrue(any(count == 2 for count in gw.snapshot_calls.values()))


class QuotesAreValid(unittest.TestCase):
    def test_accepts_two_sided_positive_non_crossed(self):
        self.assertTrue(ibt.quotes_are_valid(2.40, 2.60))
        self.assertTrue(ibt.quotes_are_valid(2.40, 2.40))  # locked

    def test_rejects_crossed_missing_or_non_positive(self):
        self.assertFalse(ibt.quotes_are_valid(2.60, 2.40))
        self.assertFalse(ibt.quotes_are_valid(0, 2.60))
        self.assertFalse(ibt.quotes_are_valid(2.40, -1))
        self.assertFalse(ibt.quotes_are_valid(None, 2.60))
        self.assertFalse(ibt.quotes_are_valid(2.40, None))


class RoundSellLimitMidpoint(unittest.TestCase):
    RULES = {"incrementRules": [{"lowerEdge": "0", "increment": "0.05"}]}

    def test_floors_midpoint_to_tick_without_crossing(self):
        # mid = 2.50 -> floor to 2.50 on 0.05 grid
        self.assertAlmostEqual(ibt.round_sell_limit_midpoint(2.40, 2.60, self.RULES), 2.50)
        # mid = 2.47 -> floor to 2.45
        self.assertAlmostEqual(ibt.round_sell_limit_midpoint(2.44, 2.50, self.RULES), 2.45)

    def test_returns_none_on_invalid_quotes(self):
        self.assertIsNone(ibt.round_sell_limit_midpoint(2.60, 2.40, self.RULES))


class ResolveExecutableCall(unittest.TestCase):
    NOW = dt.datetime(2026, 7, 9, 12, tzinfo=dt.timezone.utc)

    def _resolved(self, **overrides):
        row = {
            "symbol": "NVDA",
            "conid": 555,
            "expiry": "2026-08-21",
            "strike": 105.0,
            "bid": 2.40,
            "ask": 2.60,
            "quote_timestamp": self.NOW.isoformat(),
            "rules": {"incrementRules": [{"lowerEdge": "0", "increment": "0.05"}]},
            "tick": 0.05,
        }
        row.update(overrides)
        return row

    def test_returns_normalized_executable_contract(self):
        with mock.patch.object(ibt, "resolve_exact_call", return_value=self._resolved()):
            out = ibt.resolve_executable_call(
                "NVDA", "2026-08-21", 105,
                expected_conid=555, now=self.NOW,
            )
        self.assertEqual(out["limit_price"], 2.50)
        self.assertEqual(out["tick"], 0.05)
        self.assertIsInstance(out["bid"], float)

    def test_put_uses_same_supervised_sell_limit_boundary(self):
        resolved = self._resolved(right="P", strike=95.0, conid=556)
        with mock.patch.object(ibt, "resolve_exact_put", return_value=resolved):
            out = ibt.resolve_executable_put(
                "NVDA", "2026-08-21", 95,
                expected_conid=556, now=self.NOW,
            )
        self.assertEqual(out["limit_price"], 2.50)
        self.assertEqual(out["right"], "P")

    def test_put_reports_put_specific_error_type(self):
        with mock.patch.object(ibt, "resolve_exact_put", return_value=None):
            with self.assertRaises(ibt.ExecutablePutError) as ctx:
                ibt.resolve_executable_put("NVDA", "2026-08-21", 95)
        self.assertEqual(ctx.exception.reason, "contract_missing")

    def test_allows_staging_exact_contract_while_quote_is_missing(self):
        with mock.patch.object(
            ibt,
            "resolve_exact_call",
            return_value=self._resolved(bid=None, ask=None),
        ):
            out = ibt.resolve_executable_call(
                "NVDA",
                "2026-08-21",
                105,
                expected_conid=555,
                now=self.NOW,
                allow_missing_quote=True,
            )
        self.assertIsNone(out["limit_price"])
        self.assertEqual(out["quote_status"], "missing")
        self.assertIn("cannot be previewed or placed", out["staging_warning"])

    def test_allow_missing_quote_does_not_allow_crossed_market(self):
        with mock.patch.object(
            ibt,
            "resolve_exact_call",
            return_value=self._resolved(bid=2.80, ask=2.60),
        ):
            with self.assertRaises(ibt.ExecutableCallError) as ctx:
                ibt.resolve_executable_call(
                    "NVDA",
                    "2026-08-21",
                    105,
                    expected_conid=555,
                    now=self.NOW,
                    allow_missing_quote=True,
                )
        self.assertEqual(ctx.exception.reason, "quote_invalid")

    def test_reports_stable_failure_reasons(self):
        cases = [
            (None, "contract_missing"),
            (self._resolved(conid=777), "contract_changed"),
            (self._resolved(bid=None), "quote_invalid"),
            (
                self._resolved(
                    quote_timestamp=(self.NOW - dt.timedelta(seconds=121)).isoformat(),
                ),
                "quote_stale",
            ),
        ]
        for resolved, reason in cases:
            with self.subTest(reason=reason), \
                    mock.patch.object(ibt, "resolve_exact_call", return_value=resolved):
                with self.assertRaises(ibt.ExecutableCallError) as ctx:
                    ibt.resolve_executable_call(
                        "NVDA", "2026-08-21", 105,
                        expected_conid=555, now=self.NOW,
                    )
                self.assertEqual(ctx.exception.reason, reason)


class ResolveExactCall(unittest.TestCase):
    EXPIRY = "2026-08-21"

    def setUp(self):
        ibt._conid_cache.clear()

    def test_resolves_exact_call_with_quotes_rules_and_metadata(self):
        gw = _FakeGateway(
            spot={"31": "100.0", "84": "99.8", "86": "100.2"},
            quotes={_FakeGateway.opt_conid(105, "C"): {
                "31": "2.50", "84": "2.40", "86": "2.60",
            }},
            rules={"incrementRules": [{"lowerEdge": "0", "increment": "0.05"}]},
        )
        fixed = "2026-07-09T10:15:00+00:00"
        with mock.patch.object(ibt, "_request", gw), \
                mock.patch.object(ibt, "_utc_now_iso", return_value=fixed):
            out = ibt.resolve_exact_call("NVDA", self.EXPIRY, 105)
        self.assertIsNotNone(out)
        self.assertEqual(out["symbol"], "NVDA")
        self.assertEqual(out["conid"], gw.opt_conid(105, "C"))
        self.assertEqual(out["underlying_conid"], gw.underlying)
        self.assertEqual(out["expiry"], self.EXPIRY)
        self.assertEqual(out["strike"], 105.0)
        self.assertEqual(out["right"], "C")
        self.assertEqual(out["multiplier"], 100)
        self.assertAlmostEqual(out["bid"], 2.40)
        self.assertAlmostEqual(out["ask"], 2.60)
        self.assertAlmostEqual(out["last"], 2.50)
        self.assertAlmostEqual(out["underlying_last"], 100.0)
        self.assertAlmostEqual(out["underlying_bid"], 99.8)
        self.assertAlmostEqual(out["underlying_ask"], 100.2)
        self.assertEqual(out["quote_timestamp"], fixed)
        self.assertAlmostEqual(out["tick"], 0.05)
        self.assertIn("incrementRules", out["rules"])

    def test_resolves_exact_put_with_right_and_quotes(self):
        gw = _FakeGateway(
            quotes={_FakeGateway.opt_conid(95, "P"): {
                "31": "1.90", "84": "1.80", "86": "2.00",
            }},
        )
        with mock.patch.object(ibt, "_request", gw):
            out = ibt.resolve_exact_put("NVDA", self.EXPIRY, 95)
        self.assertIsNotNone(out)
        self.assertEqual(out["conid"], gw.opt_conid(95, "P"))
        self.assertEqual(out["right"], "P")
        self.assertAlmostEqual(out["bid"], 1.80)

    def test_rejects_expiry_mismatch(self):
        gw = _FakeGateway(maturity="20260918")
        with mock.patch.object(ibt, "_request", gw):
            self.assertIsNone(ibt.resolve_exact_call("NVDA", self.EXPIRY, 105))

    def test_rejects_unlisted_strike(self):
        def _no_info(method, endpoint, body=None, **_kwargs):
            path, _, _ = endpoint.partition("?")
            if path == "/iserver/secdef/search":
                return [{"conid": 500, "symbol": "NVDA",
                         "sections": [{"secType": "STK"}, {"secType": "OPT", "months": "AUG26"}]}]
            if path == "/iserver/secdef/info":
                return []
            return {}
        with mock.patch.object(ibt, "_request", side_effect=_no_info):
            self.assertIsNone(ibt.resolve_exact_call("NVDA", self.EXPIRY, 999))

    def test_resolves_later_weekly_expiry_from_info_array(self):
        gw = _FakeGateway(maturities=["20260807", "20260814", "20260828"])
        with mock.patch.object(ibt, "_request", gw):
            out = ibt.resolve_exact_call("NVDA", "2026-08-28", 105)
        self.assertIsNotNone(out)
        self.assertEqual(out["expiry"], "2026-08-28")
        self.assertEqual(out["conid"], gw.opt_conid(105, "C") + 2)


class OptionChainMetadata(unittest.TestCase):
    AS_OF = dt.date(2026, 7, 9)

    def setUp(self):
        ibt._conid_cache.clear()

    def test_chain_includes_underlying_quote_metadata(self):
        gw = _FakeGateway(spot={"31": "100.0", "84": "99.5", "86": "100.5"})
        fixed = "2026-07-09T10:15:00+00:00"
        with mock.patch.object(ibt, "_request", gw), \
                mock.patch.object(ibt, "_utc_now_iso", return_value=fixed):
            chain = ibt.option_chain("NVDA", as_of=self.AS_OF)
        self.assertEqual(chain["underlying_price"], 100.0)
        self.assertAlmostEqual(chain["underlying_bid"], 99.5)
        self.assertAlmostEqual(chain["underlying_ask"], 100.5)
        self.assertAlmostEqual(chain["underlying_last"], 100.0)
        self.assertEqual(chain["quote_timestamp"], fixed)

    def test_refreshes_quotes_without_repeating_secdef_discovery(self):
        option_conid = _FakeGateway.opt_conid(105, "C")
        gw = _FakeGateway(
            spot={"31": "101.0", "84": "100.5", "86": "101.5"},
            quotes={option_conid: {"31": "2.50", "84": "2.40", "86": "2.60"}},
            warm_snapshots=True,
        )
        chain = {
            "source": "ibkr",
            "symbol": "NVDA",
            "underlying_conid": gw.underlying,
            "quote_timestamp": "2000-01-01T00:00:00+00:00",
            "expiries": [{
                "expiry": "2026-08-21",
                "calls": [{"conid": option_conid, "strike": 105.0}],
                "puts": [],
            }],
        }
        fixed = "2026-07-09T10:15:00+00:00"
        with mock.patch.object(ibt, "_request", gw), \
                mock.patch.object(ibt, "OPTION_SNAPSHOT_WARMUP_SECONDS", 0), \
                mock.patch.object(ibt, "_utc_now_iso", return_value=fixed):
            refreshed = ibt.refresh_option_chain_quotes(chain)
        call = refreshed["expiries"][0]["calls"][0]
        self.assertEqual((call["bid"], call["ask"]), (2.4, 2.6))
        self.assertEqual(refreshed["underlying_price"], 101.0)
        self.assertEqual(refreshed["quote_timestamp"], fixed)
        self.assertFalse(any("/secdef/" in endpoint for _, endpoint in gw.calls))


if __name__ == "__main__":
    unittest.main()
