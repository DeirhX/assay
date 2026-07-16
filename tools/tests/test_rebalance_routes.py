import datetime as dt
import sys
import tempfile
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import rebalance_routes  # noqa: E402
import trade_service  # noqa: E402

NOW = dt.datetime(2026, 7, 1, 12, tzinfo=dt.timezone.utc)

_EXACT_PUT = {
    "conid": 556, "expiry": "2026-08-07", "strike": 93.0,
    "right": "P", "bid": 1.8, "ask": 2.0, "limit_price": 1.9,
    "quote_timestamp": NOW.isoformat(),
}


@contextmanager
def _stage_put_patches(
    route,
    *,
    available_cash_czk: float,
    margin_enabled: bool,
):
    with ExitStack() as stack:
        tmp = stack.enter_context(tempfile.TemporaryDirectory())
        stack.enter_context(mock.patch.object(
            trade_service, "STAGED_BASKET_JSON", Path(tmp) / "basket.json",
        ))
        stack.enter_context(mock.patch.object(
            rebalance_routes, "build_route", return_value=route,
        ))
        stack.enter_context(mock.patch(
            "ibkr_trade.resolve_executable_put", return_value=_EXACT_PUT,
        ))
        stack.enter_context(mock.patch("ibkr_trade.live_orders", return_value=[]))
        stack.enter_context(mock.patch.object(
            trade_service, "cash_secured_put_capacity",
            return_value={"available_cash_czk": available_cash_czk},
        ))
        stack.enter_context(mock.patch.object(
            trade_service, "margin_account_enabled", return_value=margin_enabled,
        ))
        yield


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
    ), mock.patch.object(
        trade_service, "margin_account_enabled", return_value=False,
    ) as margin:
        route = rebalance_routes.build_route(
            _holdings(), "NVDA", 100_000, now=NOW,
        )

    assert route["planned_shares"] == 43
    assert route["option"]["contracts"] == 0
    assert "43-share trade" in route["option"]["reasons"][0]
    assert route["option"]["collateral_mode"] == "cash"
    chain.assert_not_called()
    session.assert_not_called()
    rate.assert_not_called()
    margin.assert_called_once_with()


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

    chain.assert_called_once_with("NVDA", right="P", force_refresh=True)


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
def test_rebalance_put_route_refuses_synthetic_strikes(_basket):
    """Wrong-side IBKR puts must not be replaced with Black-Scholes inventions."""
    chain = {
        "source": "ibkr",
        "currency": "USD",
        "underlying_price": 100.0,
        "quote_timestamp": NOW.isoformat(),
        "expiries": [{
            "expiry": "2026-08-07",
            "calls": [],
            "puts": [
                {"strike": 150.0, "conid": 1, "bid": 50.0, "ask": 52.0},
                {"strike": 160.0, "conid": 2, "bid": 60.0, "ask": 62.0},
            ],
        }],
    }
    with mock.patch.object(
        trade_service,
        "cash_secured_put_capacity",
        return_value={"available_cash_czk": 5_000_000},
    ), mock.patch.object(trade_service, "margin_account_enabled", return_value=True):
        route = rebalance_routes.build_route(
            _holdings(), "NVDA", 230_000, chain=chain, now=NOW,
        )
    assert route["planned_shares"] == 100
    assert route["option"]["eligible"] is False
    assert route["ladder"] == []
    assert any("IBKR listed no usable OTM" in reason
               for reason in route["option"]["reasons"])


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


@mock.patch.object(trade_service, "load_basket", return_value=[])
def test_missing_mark_keeps_margin_label_and_explains_quote_gap(_basket):
    """Unheld names with a quote-less chain must not masquerade as cash fails."""
    chain = {
        "source": "ibkr",
        "currency": None,
        "underlying_price": None,
        "expiries": [],
    }
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
            _holdings(), "ADI", 1_122_062, chain=chain, now=NOW,
        )
    assert route["planned_shares"] == 0
    assert route["option"]["eligible"] is False
    assert route["option"]["collateral_mode"] == "margin"
    assert route["option"]["label"] == "Sell put (margin)"
    assert any("underlying quote" in reason for reason in route["option"]["reasons"])
    assert not any("cash-secured" in reason.lower() for reason in route["option"]["reasons"])


@mock.patch.object(trade_service, "load_basket", return_value=[])
def test_unheld_ibkr_chain_assumes_usd_fx_from_book(_basket):
    chain = _chain()
    chain["currency"] = None
    with mock.patch.object(
        trade_service,
        "cash_secured_put_capacity",
        return_value={"available_cash_czk": 5_000_000},
    ), mock.patch.object(trade_service, "margin_account_enabled", return_value=True):
        route = rebalance_routes.build_route(
            _holdings(), "ADI", 230_000, chain=chain, now=NOW,
        )
    assert route["currency"] == "USD"
    assert route["fx_to_base"] == 23.0
    assert route["planned_shares"] == 100
    assert route["option"]["contracts"] == 1
    assert route["option"]["collateral_mode"] == "margin"


def test_chain_spot_falls_back_to_bid_ask_mid():
    assert rebalance_routes._chain_spot({
        "underlying_bid": 99.0,
        "underlying_ask": 101.0,
    }) == 100.0


def test_route_table_access_requests_live_ibkr_refresh():
    warm = _chain()
    warm["currency"] = None
    with mock.patch.object(
        rebalance_routes.option_market, "session_ready", return_value=True,
    ), mock.patch.object(
        rebalance_routes.option_market, "cached_option_chain", return_value=warm,
    ) as chain, mock.patch.object(
        rebalance_routes.option_market, "cached_risk_free_rate", return_value=0.04,
    ), mock.patch.object(
        trade_service,
        "cash_secured_put_capacity",
        return_value={"available_cash_czk": 5_000_000},
    ), mock.patch.object(
        trade_service, "margin_account_enabled", return_value=True,
    ), mock.patch.object(trade_service, "load_basket", return_value=[]):
        route = rebalance_routes.build_route(_holdings(), "ADI", 230_000, now=NOW)

    chain.assert_called_once_with("ADI", right="P", force_refresh=True)
    assert route["planned_shares"] == 100
    assert route["option"]["eligible"] is True


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
    with _stage_put_patches(route, available_cash_czk=1_000_000, margin_enabled=False):
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
    with _stage_put_patches(route, available_cash_czk=100_000, margin_enabled=False):
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
    with _stage_put_patches(route, available_cash_czk=0, margin_enabled=True):
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


def test_margin_account_can_append_a_covered_call_despite_existing_stock_buys():
    existing = [{
        "type": "stock", "symbol": "ADI", "delta_czk": 1_000_000,
    }]
    route = {
        "planned_shares": 100,
        "currency": "USD",
        "fx_to_base": 23.0,
        "option": {"eligible": True, "contracts": 1},
        "ladder": [{
            "conid": 555, "expiry": "2026-08-07", "strike": 105.0,
            "stageable": True,
        }],
    }
    exact = {
        "conid": 555, "expiry": "2026-08-07", "strike": 105.0,
        "right": "C", "bid": 2.4, "ask": 2.6, "limit_price": 2.5,
        "quote_timestamp": NOW.isoformat(),
    }
    with mock.patch.object(trade_service, "load_basket", return_value=existing), \
            mock.patch.object(
                trade_service, "save_basket",
                side_effect=lambda rows: trade_service._normalize_basket(rows),
            ), \
            mock.patch.object(rebalance_routes, "build_route", return_value=route), \
            mock.patch("ibkr_trade.resolve_executable_call", return_value=exact), \
            mock.patch("ibkr_trade.live_orders", return_value=[]), \
            mock.patch.object(trade_service, "_resolve_trade_account", return_value="U1"), \
            mock.patch.object(
                trade_service, "covered_call_capacity",
                return_value={
                    "current_shares": 300,
                    "held_short_calls": 0,
                    "working_short_calls": 0,
                },
            ), \
            mock.patch.object(
                trade_service, "cash_secured_put_capacity",
                return_value={"available_cash_czk": 0},
            ), \
            mock.patch.object(trade_service, "margin_account_enabled", return_value=True):
        out = rebalance_routes.stage_routes(
            _holdings(),
            [{"symbol": "NVDA", "delta_czk": -230_000}],
            [{
                "symbol": "NVDA", "route": "covered_call", "conid": 555,
                "expiry": "2026-08-07", "strike": 105.0, "contracts": 1,
            }],
            mode="append",
        )

    assert out["collateral_mode"] == "margin"
    assert any(
        leg["type"] == "covered_call" and leg["symbol"] == "NVDA"
        for leg in out["basket"]
    )


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


def test_append_option_is_saved_when_an_unrelated_existing_symbol_needs_reconciliation():
    holdings = _holdings()
    holdings["positions"].extend([
        {
            "symbol": "EEFT", "asset_class": "STK", "quantity": 1_000,
            "mark_price": 100.0, "market_value": 100_000.0,
            "base_market_value": 2_300_000.0, "currency": "USD",
            "fx_rate_to_base": 23.0,
        },
        {
            "symbol": "PYPL", "asset_class": "STK", "quantity": 1_000,
            "mark_price": 50.0, "market_value": 50_000.0,
            "base_market_value": 1_150_000.0, "currency": "USD",
            "fx_rate_to_base": 23.0,
        },
    ])
    existing = [
        {"type": "stock", "symbol": "PYPL", "delta_czk": -115_000},
        {
            "type": "covered_call", "symbol": "PYPL", "conid": 900,
            "expiry": "2026-08-07", "strike": 55.0, "contracts": 10,
            "multiplier": 100,
        },
    ]
    route = {
        "planned_shares": 100,
        "currency": "USD",
        "fx_to_base": 23.0,
        "option": {"eligible": True, "contracts": 1},
        "ladder": [{
            "conid": 777, "expiry": "2026-08-07", "strike": 105.0,
            "stageable": True,
        }],
    }
    exact = {
        "conid": 777, "expiry": "2026-08-07", "strike": 105.0,
        "right": "C", "bid": 2.4, "ask": 2.6, "limit_price": 2.5,
        "quote_timestamp": NOW.isoformat(),
    }

    def capacity(symbol, *_args, **_kwargs):
        return {
            "current_shares": 1_000,
            "held_short_calls": 0,
            "working_short_calls": 0,
        }

    with mock.patch.object(trade_service, "load_basket", return_value=existing), \
            mock.patch.object(
                trade_service, "save_basket",
                side_effect=lambda rows: trade_service._normalize_basket(rows),
            ), \
            mock.patch.object(rebalance_routes, "build_route", return_value=route), \
            mock.patch("ibkr_trade.resolve_executable_call", return_value=exact), \
            mock.patch("ibkr_trade.live_orders", return_value=[]), \
            mock.patch.object(trade_service, "_resolve_trade_account", return_value="U1"), \
            mock.patch.object(trade_service, "covered_call_capacity", side_effect=capacity), \
            mock.patch.object(
                trade_service, "cash_secured_put_capacity",
                return_value={"available_cash_czk": 1_000_000},
            ):
        out = rebalance_routes.stage_routes(
            holdings,
            [{"symbol": "EEFT", "delta_czk": -230_000}],
            [{
                "symbol": "EEFT", "route": "covered_call", "conid": 777,
                "expiry": "2026-08-07", "strike": 105.0, "contracts": 1,
            }],
            mode="append",
        )

    assert any(
        leg["type"] == "covered_call" and leg["symbol"] == "EEFT"
        for leg in out["basket"]
    )
    assert out["coverage_violations"] == [{
        "symbol": "PYPL",
        "current_shares": 1_000,
        "planned_stock_sell_shares": 100,
        "working_stock_sell_shares": 0,
        "working_stock_order_ids": [],
        "working_call_order_ids": [],
        "selected_call_contracts": 10,
        "held_short_call_contracts": 0,
        "working_short_call_contracts": 0,
        "required_shares": 1_100,
        "excess_shares": 100,
        "stock_leg_ids": ["stock:PYPL"],
        "call_leg_ids": ["covered_call:PYPL:900"],
    }]


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
