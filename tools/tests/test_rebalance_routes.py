import datetime as dt
import sys
import tempfile
from pathlib import Path
from unittest import TestCase, mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import rebalance_routes  # noqa: E402
import trade_service  # noqa: E402

NOW = dt.datetime(2026, 7, 1, 12, tzinfo=dt.timezone.utc)


def _holdings(cash=1_000_000):
    return {
        "cash": [{"currency": "BASE_SUMMARY", "ending_cash": cash}],
        "positions": [{
            "symbol": "NVDA",
            "asset_class": "STK",
            "quantity": 300,
            "mark_price": 100.0,
            "market_value": 30_000.0,
            "base_market_value": 690_000.0,
            "currency": "USD",
            "fx_rate_to_base": 23.0,
        }],
    }


def _chain():
    return {
        "source": "ibkr",
        "currency": "USD",
        "underlying_price": 100.0,
        "quote_timestamp": NOW.isoformat(),
        "expiries": [{
            "expiry": "2026-08-07",
            "calls": [{
                "strike": 105.0, "bid": 2.4, "ask": 2.6, "last": 2.5,
                "conid": 555, "delta": 0.35, "open_interest": 500, "volume": 50,
            }],
            "puts": [{
                "strike": 93.0, "bid": 1.8, "ask": 2.0, "last": 1.9,
                "conid": 556, "delta": -0.25, "open_interest": 500, "volume": 50,
            }],
        }],
    }


def test_contract_sizing_matches_exit_bounded_round_up():
    assert rebalance_routes.contracts_for_shares(90) == 1
    assert rebalance_routes.contracts_for_shares(80) == 0
    assert rebalance_routes.contracts_for_shares(250) == 2
    assert rebalance_routes.contracts_for_shares(290, capacity=2) == 2


def test_sub_contract_trade_skips_chain_session_and_rate_fetches():
    with mock.patch.object(
        rebalance_routes.option_market, "cached_option_chain",
    ) as chain, mock.patch.object(
        rebalance_routes.option_market, "session_ready",
    ) as session, mock.patch.object(
        rebalance_routes.option_market, "cached_risk_free_rate",
    ) as rate, mock.patch.object(
        trade_service,
        "cash_secured_put_capacity",
        return_value={
            "cash_czk": 1_000_000,
            "held_short_put_collateral_czk": 0,
            "available_cash_czk": 1_000_000,
        },
    ), mock.patch.object(trade_service, "margin_account_enabled") as margin:
        route = rebalance_routes.build_route(
            _holdings(), "NVDA", 100_000, now=NOW,
        )

    assert route["planned_shares"] == 43
    assert route["option"]["contracts"] == 0
    assert "43-share trade" in route["option"]["reasons"][0]
    chain.assert_not_called()
    session.assert_not_called()
    rate.assert_not_called()
    margin.assert_not_called()


def test_route_fetches_only_the_option_side_needed_for_its_direction():
    with mock.patch.object(
        rebalance_routes.option_market, "cached_option_chain", return_value=_chain(),
    ) as chain, mock.patch.object(
        rebalance_routes.option_market, "session_ready", return_value=False,
    ), mock.patch.object(
        rebalance_routes.option_market, "cached_risk_free_rate", return_value=0.04,
    ), mock.patch.object(
        trade_service,
        "cash_secured_put_capacity",
        return_value={"available_cash_czk": 1_000_000},
    ), mock.patch.object(
        trade_service, "margin_account_enabled", return_value=False,
    ), mock.patch.object(trade_service, "load_basket", return_value=[]):
        rebalance_routes.build_route(_holdings(), "NVDA", 230_000, now=NOW)

    chain.assert_called_once_with("NVDA", right="P")


@mock.patch.object(trade_service, "load_basket", return_value=[])
def test_increase_offers_exact_cash_secured_put_route(_basket):
    with mock.patch.object(
        trade_service, "cash_secured_put_capacity",
        return_value={"available_cash_czk": 1_000_000},
    ):
        route = rebalance_routes.build_route(
            _holdings(), "NVDA", 230_000, chain=_chain(), now=NOW,
        )
    assert route["direction"] == "increase"
    assert route["planned_shares"] == 100
    assert route["direct"]["kind"] == "buy_shares"
    assert route["option"]["kind"] == "cash_secured_put"
    assert route["option"]["contracts"] == 1
    assert route["option"]["stageable"] is True
    assert route["ladder"][0]["conid"] == 556
    assert route["ladder"][0]["cash_secured_czk"] == 213_900


@mock.patch.object(trade_service, "load_basket", return_value=[])
def test_reduction_offers_covered_call_bounded_by_held_shares(_basket):
    with mock.patch.object(
        trade_service, "covered_call_capacity",
        return_value={"capacity_contracts": 3, "current_shares": 300},
    ):
        route = rebalance_routes.build_route(
            _holdings(), "NVDA", -230_000, chain=_chain(), now=NOW,
        )
    assert route["direction"] == "reduce"
    assert route["option"]["kind"] == "covered_call"
    assert route["option"]["contracts"] == 1
    assert route["ladder"][0]["conid"] == 555


@mock.patch.object(trade_service, "load_basket", return_value=[])
def test_fallback_ladder_is_visible_but_not_stageable(_basket):
    chain = _chain()
    chain["source"] = "yahoo"
    chain["expiries"][0]["puts"][0].pop("conid")
    route = rebalance_routes.build_route(
        _holdings(), "NVDA", 230_000, chain=chain, now=NOW,
    )
    assert route["option"]["eligible"] is True
    assert route["option"]["stageable"] is False
    assert "exact IBKR" in route["option"]["reasons"][0]


@mock.patch.object(trade_service, "load_basket", return_value=[])
def test_zero_cash_explains_held_put_collateral_without_ladder_cascade(_basket):
    with mock.patch.object(
        trade_service,
        "cash_secured_put_capacity",
        return_value={
            "cash_czk": 1_000_000,
            "held_short_put_collateral_czk": 1_200_000,
            "available_cash_czk": 0,
        },
    ), mock.patch.object(trade_service, "margin_account_enabled", return_value=False):
        route = rebalance_routes.build_route(
            _holdings(), "NVDA", 230_000, chain=_chain(), now=NOW,
        )
    reasons = route["option"]["reasons"]
    assert any("held short puts" in reason and "working or queued obligations" in reason for reason in reasons)
    assert not any("strike ladder" in reason for reason in reasons)
    assert not any("Indicative" in reason for reason in reasons)
    assert route["option"]["snapshot_cash_czk"] == 1_000_000
    assert route["option"]["held_short_put_collateral_czk"] == 1_200_000


@mock.patch.object(trade_service, "load_basket", return_value=[])
def test_partial_cash_reports_cost_of_one_put_contract(_basket):
    with mock.patch.object(
        trade_service,
        "cash_secured_put_capacity",
        return_value={
            "cash_czk": 500_000,
            "held_short_put_collateral_czk": 400_000,
            "available_cash_czk": 100_000,
        },
    ), mock.patch.object(trade_service, "margin_account_enabled", return_value=False):
        route = rebalance_routes.build_route(
            _holdings(), "NVDA", 230_000, chain=_chain(), now=NOW,
        )
    assert route["option"]["contracts"] == 0
    assert "needs about 230,000 CZK" in route["option"]["reasons"][0]
    assert "100,000 CZK remains" in route["option"]["reasons"][0]


@mock.patch.object(trade_service, "load_basket", return_value=[])
def test_margin_account_put_route_is_not_capped_by_snapshot_cash(_basket):
    with mock.patch.object(
        trade_service,
        "cash_secured_put_capacity",
        return_value={
            "cash_czk": 0,
            "held_short_put_collateral_czk": 0,
            "available_cash_czk": 0,
        },
    ), mock.patch.object(trade_service, "margin_account_enabled", return_value=True):
        route = rebalance_routes.build_route(
            _holdings(), "NVDA", 230_000, chain=_chain(), now=NOW,
        )
    assert route["option"]["contracts"] == 1
    assert route["option"]["eligible"] is True
    assert route["option"]["collateral_mode"] == "margin"
    assert route["option"]["label"] == "Sell put (margin)"
    assert route["option"]["available_cash_czk"] is None


def test_route_capacity_reserves_working_puts_and_unrelated_staged_buys():
    working = [{
        "orderId": "p-other",
        "ticker": "AMD260807P00093000",
        "side": "SELL",
        "remainingQuantity": 1,
        "status": "Submitted",
        "secType": "OPT",
        "currency": "USD",
    }]
    with mock.patch.object(rebalance_routes.option_market, "session_ready", return_value=True), \
            mock.patch("ibkr_trade.live_orders", return_value=working), \
            mock.patch.object(
                trade_service, "load_basket",
                return_value=[{"type": "stock", "symbol": "MSFT", "delta_czk": 100_000}],
            ), \
            mock.patch.object(trade_service, "_fx_by_currency", return_value={"USD": 23.0}), \
            mock.patch.object(trade_service, "margin_account_enabled", return_value=False):
        route = rebalance_routes.build_route(
            _holdings(), "NVDA", 690_000, chain=_chain(), now=NOW,
        )
    # 1m cash - 213.9k working put - 100k staged buy leaves room for two,
    # not the three contracts implied by the 300-share plan.
    assert route["planned_shares"] == 300
    assert route["option"]["contracts"] == 2
    assert route["option"]["available_cash_czk"] == 686_100


@mock.patch.object(trade_service, "load_basket", return_value=[])
def test_stage_put_replaces_stock_leg_and_records_conditional_provenance(_basket):
    route = rebalance_routes.build_route(
        _holdings(), "NVDA", 230_000, chain=_chain(), now=NOW,
    )
    exact = {
        "conid": 556, "expiry": "2026-08-07", "strike": 93.0,
        "right": "P", "bid": 1.8, "ask": 2.0, "limit_price": 1.9,
        "quote_timestamp": NOW.isoformat(),
    }
    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(trade_service, "STAGED_BASKET_JSON", Path(tmp) / "basket.json"), \
            mock.patch.object(rebalance_routes, "build_route", return_value=route), \
            mock.patch("ibkr_trade.resolve_executable_put", return_value=exact), \
            mock.patch("ibkr_trade.live_orders", return_value=[]), \
            mock.patch.object(
                trade_service, "cash_secured_put_capacity",
                return_value={"available_cash_czk": 1_000_000},
            ), \
            mock.patch.object(trade_service, "margin_account_enabled", return_value=False):
        out = rebalance_routes.stage_routes(
            _holdings(),
            [{"symbol": "NVDA", "delta_czk": 230_000}],
            [{
                "symbol": "NVDA", "route": "cash_secured_put", "conid": 556,
                "expiry": "2026-08-07", "strike": 93.0, "contracts": 1,
                "limit_price": 1.85,
            }],
        )
    assert [leg["type"] for leg in out["basket"]] == ["cash_secured_put"]
    leg = out["basket"][0]
    assert leg["right"] == "P"
    assert leg["limit_price"] == 1.85
    assert leg["provenance"][0]["source"] == "rebalance_routes"
    assert leg["provenance"][0]["intended_assigned_shares"] == 100


@mock.patch.object(trade_service, "load_basket", return_value=[])
def test_stage_rejects_aggregate_put_collateral_above_cash(_basket):
    route = rebalance_routes.build_route(
        _holdings(), "NVDA", 230_000, chain=_chain(), now=NOW,
    )
    exact = {
        "conid": 556, "expiry": "2026-08-07", "strike": 93.0,
        "right": "P", "bid": 1.8, "ask": 2.0, "limit_price": 1.9,
        "quote_timestamp": NOW.isoformat(),
    }
    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(trade_service, "STAGED_BASKET_JSON", Path(tmp) / "basket.json"), \
            mock.patch.object(rebalance_routes, "build_route", return_value=route), \
            mock.patch("ibkr_trade.resolve_executable_put", return_value=exact), \
            mock.patch("ibkr_trade.live_orders", return_value=[]), \
            mock.patch.object(
                trade_service, "cash_secured_put_capacity",
                return_value={"available_cash_czk": 100_000},
            ), \
            mock.patch.object(trade_service, "margin_account_enabled", return_value=False):
        try:
            rebalance_routes.stage_routes(
                _holdings(),
                [{"symbol": "NVDA", "delta_czk": 230_000}],
                [{
                    "symbol": "NVDA", "route": "cash_secured_put", "conid": 556,
                    "expiry": "2026-08-07", "strike": 93.0, "contracts": 1,
                }],
            )
        except ValueError as exc:
            assert "only 100,000 CZK" in str(exc)
        else:
            raise AssertionError("insufficient cash must reject staging")


@mock.patch.object(trade_service, "load_basket", return_value=[])
def test_stage_margin_put_does_not_require_assignment_cash(_basket):
    route = rebalance_routes.build_route(
        _holdings(), "NVDA", 230_000, chain=_chain(), now=NOW,
    )
    route["option"]["collateral_mode"] = "margin"
    route["option"]["label"] = "Sell put (margin)"
    exact = {
        "conid": 556, "expiry": "2026-08-07", "strike": 93.0,
        "right": "P", "bid": 1.8, "ask": 2.0, "limit_price": 1.9,
        "quote_timestamp": NOW.isoformat(),
    }
    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(trade_service, "STAGED_BASKET_JSON", Path(tmp) / "basket.json"), \
            mock.patch.object(rebalance_routes, "build_route", return_value=route), \
            mock.patch("ibkr_trade.resolve_executable_put", return_value=exact), \
            mock.patch("ibkr_trade.live_orders", return_value=[]), \
            mock.patch.object(
                trade_service, "cash_secured_put_capacity",
                return_value={"available_cash_czk": 0},
            ), \
            mock.patch.object(trade_service, "margin_account_enabled", return_value=True):
        out = rebalance_routes.stage_routes(
            _holdings(),
            [{"symbol": "NVDA", "delta_czk": 230_000}],
            [{
                "symbol": "NVDA", "route": "cash_secured_put", "conid": 556,
                "expiry": "2026-08-07", "strike": 93.0, "contracts": 1,
            }],
        )
    assert out["collateral_mode"] == "margin"
    assert out["basket"][0]["collateral_mode"] == "margin"


def test_append_mode_keeps_existing_queue_and_adds_new_stock_amounts():
    existing = [
        {
            "type": "stock", "symbol": "AAPL", "delta_czk": 100_000,
            "provenance": [{"source": "rebalance_routes"}],
        },
        {
            "type": "stock", "symbol": "MSFT", "delta_czk": -40_000,
            "provenance": [{"source": "exit_plan"}],
        },
    ]
    with mock.patch.object(trade_service, "load_basket", return_value=existing), \
            mock.patch.object(
                trade_service, "save_basket",
                side_effect=lambda rows: trade_service._normalize_basket(rows),
            ), \
            mock.patch.object(
                trade_service, "cash_secured_put_capacity",
                return_value={"available_cash_czk": 1_000_000},
            ):
        out = rebalance_routes.stage_routes(
            _holdings(),
            [{"symbol": "NVDA", "delta_czk": 230_000}],
            [],
            mode="append",
        )
    stocks = {
        row["symbol"]: row["delta_czk"]
        for row in out["basket"] if row["type"] == "stock"
    }
    assert stocks == {"AAPL": 100_000.0, "MSFT": -40_000.0, "NVDA": 230_000.0}
    assert out["mode"] == "append"


def test_replace_mode_removes_prior_rebalance_legs_but_keeps_exit_routes():
    existing = [
        {
            "type": "stock", "symbol": "AAPL", "delta_czk": 100_000,
            "provenance": [{"source": "rebalance_routes"}],
        },
        {
            "type": "stock", "symbol": "MSFT", "delta_czk": -40_000,
            "provenance": [{"source": "exit_plan"}],
        },
    ]
    with mock.patch.object(trade_service, "load_basket", return_value=existing), \
            mock.patch.object(
                trade_service, "save_basket",
                side_effect=lambda rows: trade_service._normalize_basket(rows),
            ), \
            mock.patch.object(
                trade_service, "cash_secured_put_capacity",
                return_value={"available_cash_czk": 1_000_000},
            ):
        out = rebalance_routes.stage_routes(
            _holdings(),
            [{"symbol": "NVDA", "delta_czk": 230_000}],
            [],
            mode="replace",
        )
    stocks = {
        row["symbol"]: row["delta_czk"]
        for row in out["basket"] if row["type"] == "stock"
    }
    assert stocks == {"MSFT": -40_000.0, "NVDA": 230_000.0}
    assert out["mode"] == "replace"


def _put_leg(**overrides):
    return {
        "type": "cash_secured_put",
        "symbol": "NVDA",
        "conid": 556,
        "expiry": "2026-08-07",
        "strike": 93.0,
        "contracts": 1,
        "multiplier": 100,
        "fx_to_base": 23.0,
        **overrides,
    }


def test_canonical_put_leg_preserves_explicit_right_and_type():
    leg = trade_service._normalize_basket([_put_leg()])[0]
    assert leg["type"] == "cash_secured_put"
    assert leg["route"] == "cash_secured_put"
    assert leg["right"] == "P"
    assert leg["leg_id"] == "cash_secured_put:NVDA:556"


def test_prepare_put_builds_exact_sell_limit_and_secured_cash():
    exact = {
        "conid": 556, "expiry": "2026-08-07", "strike": 93.0,
        "right": "P", "bid": 1.8, "ask": 2.0, "last": 1.9,
        "limit_price": 1.9, "quote_timestamp": NOW.isoformat(),
    }
    basket = trade_service._normalize_basket([_put_leg()])
    with mock.patch.object(trade_service, "_trade_price_map", return_value={}), \
            mock.patch.object(trade_service, "_position_quantity_map", return_value={}), \
            mock.patch.object(trade_service, "_held_call_capacity", return_value={}), \
            mock.patch.object(trade_service, "_fx_by_currency", return_value={"USD": 23.0}), \
            mock.patch.object(
                trade_service, "cash_secured_put_capacity",
                return_value={"available_cash_czk": 1_000_000},
            ), \
            mock.patch.object(trade_service, "margin_account_enabled", return_value=False), \
            mock.patch("ibkr_trade.resolve_executable_put", return_value=exact):
        orders, warnings = trade_service._prepare_trade_orders("DU1", basket)
    assert warnings == []
    assert len(orders) == 1
    order = orders[0]
    assert order["instrument_type"] == "cash_secured_put"
    assert order["side"] == "SELL"
    assert order["right"] == "P"
    assert order["price"] == 1.9
    assert order["cash_secured_czk"] == 213_900


def test_prepare_put_on_margin_account_defers_capacity_to_ibkr_preview():
    exact = {
        "conid": 556, "expiry": "2026-08-07", "strike": 93.0,
        "right": "P", "bid": 1.8, "ask": 2.0, "last": 1.9,
        "limit_price": 1.9, "quote_timestamp": NOW.isoformat(),
    }
    basket = trade_service._normalize_basket([_put_leg(collateral_mode="margin")])
    with mock.patch.object(trade_service, "_trade_price_map", return_value={}), \
            mock.patch.object(trade_service, "_position_quantity_map", return_value={}), \
            mock.patch.object(trade_service, "_held_call_capacity", return_value={}), \
            mock.patch.object(trade_service, "_fx_by_currency", return_value={"USD": 23.0}), \
            mock.patch.object(trade_service, "margin_account_enabled", return_value=True), \
            mock.patch.object(trade_service, "cash_secured_put_capacity") as cash_capacity, \
            mock.patch("ibkr_trade.resolve_executable_put", return_value=exact):
        orders, warnings = trade_service._prepare_trade_orders("DU1", basket)
    assert warnings == []
    assert orders[0]["collateral_mode"] == "margin"
    assert orders[0]["cash_secured_czk"] == 213_900
    cash_capacity.assert_not_called()


def test_working_short_put_is_not_mislabeled_as_covered_call():
    raw = [{
        "orderId": "p1",
        "ticker": "NVDA260807P00093000",
        "side": "SELL",
        "remainingQuantity": 1,
        "status": "Submitted",
        "secType": "OPT",
    }]
    rows = trade_service._normalized_working_orders(
        raw, set(), {"NVDA"}, {556},
    )
    assert rows[0]["instrument_type"] == "cash_secured_put"
    assert rows[0]["right"] == "P"
    assert rows[0]["strike"] == 93.0


def test_place_time_put_revalidation_counts_working_put_collateral():
    order = {
        "instrument_type": "cash_secured_put",
        "symbol": "NVDA",
        "conid": 556,
        "expiry": "2026-08-07",
        "strike": 93.0,
        "side": "SELL",
        "quantity": 1,
        "price": 1.9,
        "_estimate_fx_to_base": 23.0,
    }
    working = [{
        "instrument_type": "cash_secured_put",
        "symbol": "NVDA",
        "side": "SELL",
        "remaining_qty": 1,
        "strike": 93.0,
    }]
    exact = {
        "conid": 556, "expiry": "2026-08-07", "strike": 93.0,
        "bid": 1.8, "ask": 2.0, "last": 1.9, "limit_price": 1.9,
        "tick": 0.05, "quote_timestamp": NOW.isoformat(),
    }
    with mock.patch("ibkr_trade.resolve_executable_put", return_value=exact), \
            mock.patch.object(
                trade_service, "cash_secured_put_capacity",
                return_value={"available_cash_czk": 300_000},
            ), \
            mock.patch.object(trade_service, "margin_account_enabled", return_value=False), \
            mock.patch.object(trade_service, "_fx_by_currency", return_value={"USD": 23.0}):
        try:
            trade_service._revalidate_cash_secured_put_orders("DU1", [order], working)
        except Exception as exc:
            assert "cash coverage changed" in str(exc)
        else:
            raise AssertionError("working and proposed puts must share the cash-capacity gate")


def test_place_time_margin_put_revalidation_skips_local_cash_gate():
    order = {
        "instrument_type": "cash_secured_put",
        "symbol": "NVDA",
        "conid": 556,
        "expiry": "2026-08-07",
        "strike": 93.0,
        "side": "SELL",
        "quantity": 1,
        "price": 1.9,
        "_estimate_fx_to_base": 23.0,
    }
    exact = {
        "conid": 556, "expiry": "2026-08-07", "strike": 93.0,
        "bid": 1.8, "ask": 2.0, "last": 1.9, "limit_price": 1.9,
        "tick": 0.05, "quote_timestamp": NOW.isoformat(),
    }
    with mock.patch("ibkr_trade.resolve_executable_put", return_value=exact), \
            mock.patch.object(trade_service, "margin_account_enabled", return_value=True), \
            mock.patch.object(trade_service, "cash_secured_put_capacity") as cash_capacity:
        trade_service._revalidate_cash_secured_put_orders("DU1", [order], [])
    cash_capacity.assert_not_called()


class RebalanceRouteUnittestCoverage(TestCase):
    """Mirror the pytest-style cases into the repository's unittest CI runner."""

    def setUp(self):
        self._session = mock.patch.object(
            rebalance_routes.option_market, "session_ready", return_value=False,
        )
        self._basket = mock.patch.object(trade_service, "load_basket", return_value=[])
        self._session.start()
        self._basket.start()
        self.addCleanup(self._session.stop)
        self.addCleanup(self._basket.stop)

    def test_contract_sizing(self):
        test_contract_sizing_matches_exit_bounded_round_up()

    def test_increase_route(self):
        test_increase_offers_exact_cash_secured_put_route()

    def test_reduction_route(self):
        test_reduction_offers_covered_call_bounded_by_held_shares()

    def test_fallback_route(self):
        test_fallback_ladder_is_visible_but_not_stageable()

    def test_zero_cash_reason(self):
        test_zero_cash_explains_held_put_collateral_without_ladder_cascade()

    def test_partial_cash_reason(self):
        test_partial_cash_reports_cost_of_one_put_contract()

    def test_aggregate_route_capacity(self):
        test_route_capacity_reserves_working_puts_and_unrelated_staged_buys()

    def test_atomic_put_staging(self):
        test_stage_put_replaces_stock_leg_and_records_conditional_provenance()

    def test_stage_cash_rejection(self):
        test_stage_rejects_aggregate_put_collateral_above_cash()

    def test_append_mode(self):
        test_append_mode_keeps_existing_queue_and_adds_new_stock_amounts()

    def test_replace_mode(self):
        test_replace_mode_removes_prior_rebalance_legs_but_keeps_exit_routes()

    def test_canonical_put(self):
        test_canonical_put_leg_preserves_explicit_right_and_type()

    def test_prepare_put(self):
        test_prepare_put_builds_exact_sell_limit_and_secured_cash()

    def test_working_put_classification(self):
        test_working_short_put_is_not_mislabeled_as_covered_call()

    def test_place_time_cash_gate(self):
        test_place_time_put_revalidation_counts_working_put_collateral()
