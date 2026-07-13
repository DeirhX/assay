import datetime as dt
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import exit_plan  # noqa: E402
import ibkr_trade  # noqa: E402
import tax_lots  # noqa: E402


AS_OF = dt.datetime(2026, 7, 1, tzinfo=dt.timezone.utc)


def _lot(symbol, mv, cost, days_ago):
    open_dt = (AS_OF - dt.timedelta(days=days_ago)).isoformat()
    return {
        "symbol": symbol,
        "quantity": mv / 100.0,          # 100 CZK/share for tidy share math
        "base_market_value": mv,
        "market_value": mv,              # CZK lot: local == base, fx = 1
        "cost_basis_money": cost,
        "open_datetime": open_dt,
    }


def _holdings():
    # EXITME: 800k across three lots (exempt gain, taxable loss, near-exempt gain).
    lots = [
        _lot("EXITME", 300_000, 100_000, days_ago=4 * 365),           # exempt gain
        _lot("EXITME", 100_000, 150_000, days_ago=365),               # taxable loss
        _lot("EXITME", 400_000, 100_000, days_ago=3 * 365 - 90),      # taxable gain, ~90d to exempt
    ]
    positions = [
        {"symbol": "EXITME", "asset_class": "STK", "currency": "CZK", "quantity": 8000.0,
         "mark_price": 100.0, "market_value": 800_000, "base_market_value": 800_000},
        {"symbol": "KEEP", "asset_class": "STK", "currency": "CZK", "quantity": 2000.0,
         "mark_price": 100.0, "market_value": 200_000, "base_market_value": 200_000},
    ]
    return {"base_currency": "CZK", "net_asset_value": 1_000_000, "positions": positions, "lots": lots}


def _model(rule="reduce", low=1.0, high=3.0):
    return {"as_of": "2026-07-01", "targets": {"EXITME": {"low": low, "high": high, "rule": rule}}}


# --------------------------------------------------------------------------- #
# average_daily_volume
# --------------------------------------------------------------------------- #
def test_adv_averages_trailing_window():
    points = [{"date": f"d{i}", "close": 10.0, "volume": v} for i, v in enumerate([100, 200, 300, 400])]
    assert exit_plan.average_daily_volume(points, window=2) == 350.0
    assert exit_plan.average_daily_volume(points, window=20) == 250.0


def test_adv_none_without_volume():
    assert exit_plan.average_daily_volume([{"date": "d", "close": 10.0}]) is None
    assert exit_plan.average_daily_volume([]) is None
    assert exit_plan.average_daily_volume(None) is None


# --------------------------------------------------------------------------- #
# tax layering
# --------------------------------------------------------------------------- #
def test_tax_layers_defers_near_exempt_gain():
    lots = tax_lots.classify_lots(_holdings(), "EXITME", as_of=AS_OF)
    layers = exit_plan._tax_layers(lots, 770_000, near_exempt_days=120, tax_rate=0.15)
    # exempt gain (300k) + harvestable loss lot (100k) fill now; near-exempt 400k
    # gain lot is held back.
    assert layers["sell_now_czk"] == 400_000
    assert layers["defer_czk"] == 370_000
    assert layers["defer_lots"] and layers["defer_lots"][0]["bucket"] == "taxable_gain"
    # tax saved ~ 300k gain * (370/400 sold-portion) * 15%.
    assert 41_000 < layers["tax_saved_by_waiting"] < 42_000
    # no taxable gain realized in the sell-now leg (only exempt gain + loss).
    assert layers["taxable_gain_now"] == 0.0
    assert layers["tax_cost_now"] == 0.0


def test_tax_layers_small_exit_stays_in_no_cost_pool():
    lots = tax_lots.classify_lots(_holdings(), "EXITME", as_of=AS_OF)
    layers = exit_plan._tax_layers(lots, 250_000, near_exempt_days=120, tax_rate=0.15)
    assert layers["sell_now_czk"] == 250_000
    assert layers["defer_czk"] == 0.0
    assert layers["tax_saved_by_waiting"] == 0.0


def test_tax_layers_far_taxable_gain_is_sold_not_deferred():
    # A taxable-gain lot well beyond the near-exempt window is sold now.
    lots = [{
        "bucket": "taxable_gain", "qty": 100.0, "mv": 100_000, "cost": 60_000,
        "gain": 40_000, "exempt": False, "days_to_exempt": 500, "exempt_on": "2028-01-01",
        "open_datetime": "2024-01-01",
    }]
    layers = exit_plan._tax_layers(lots, 100_000, near_exempt_days=120, tax_rate=0.15)
    assert layers["sell_now_czk"] == 100_000
    assert layers["defer_czk"] == 0.0
    assert layers["taxable_gain_now"] == 40_000
    assert layers["tax_cost_now"] == 6_000


# --------------------------------------------------------------------------- #
# schedule + ladder
# --------------------------------------------------------------------------- #
def test_schedule_even_slices_without_adv():
    sched = exit_plan._schedule(
        1000.0, 100.0, 100.0, "CZK", None, None,
        horizon_days=10, slice_pct=0.12, default_tranches=4, start=dt.date(2026, 7, 1),
    )
    assert sched["n"] == 4
    assert [t["shares"] for t in sched["tranches"]] == [250.0, 250.0, 250.0, 250.0]
    assert sched["tranches"][0]["date"] == "2026-07-01"
    assert sched["tranches"][-1]["date"] == "2026-07-10"


def test_schedule_caps_slices_to_adv():
    # 1000 shares, ADV 1000, 12% cap = 120/day -> ceil(1000/120)=9 tranches.
    sched = exit_plan._schedule(
        1000.0, 100.0, 100.0, "USD", 1000.0, None,
        horizon_days=10, slice_pct=0.12, default_tranches=4, start=dt.date(2026, 7, 1),
    )
    assert sched["n"] == 9
    assert sched["max_shares_per_day"] == 120.0
    assert all(not t["over_adv_cap"] for t in sched["tranches"])


def test_schedule_rounds_to_whole_shares_and_preserves_total():
    sched = exit_plan._schedule(
        695.54, 946.32, 42.0, "USD", None, None,
        horizon_days=10, slice_pct=0.12, default_tranches=4,
        start=dt.date(2026, 7, 1),
    )
    shares = [row["shares"] for row in sched["tranches"]]
    assert sum(shares) == 696
    assert all(isinstance(value, int) for value in shares)
    assert max(shares) - min(shares) <= 1


def test_schedule_clamps_to_horizon_when_illiquid():
    # Tiny ADV would want many days; clamp to the horizon and flag over-cap.
    sched = exit_plan._schedule(
        1000.0, 100.0, 100.0, "USD", 100.0, None,
        horizon_days=5, slice_pct=0.10, default_tranches=4, start=dt.date(2026, 7, 1),
    )
    assert sched["n"] == 5
    assert any(t["over_adv_cap"] for t in sched["tranches"])


def test_ladder_uses_locked_trim_rungs():
    level = {"trim_ladder": [{"price": 110.0, "size_pct": 0.5}, {"price": 120.0, "size_pct": 0.5}]}
    prices = exit_plan._ladder_prices(level, 100.0, 3)
    assert prices == [110.0, 120.0, 120.0]


def test_ladder_default_rungs_step_above_mark():
    prices = exit_plan._ladder_prices(None, 100.0, 3)
    assert prices == [100.0, 102.0, 104.0]


def test_ladder_anchors_top_rung_to_fair_value():
    prices = exit_plan._ladder_prices({"fair_value": 130.0}, 100.0, 3)
    assert prices[-1] == 130.0


# --------------------------------------------------------------------------- #
# build_exit_plan integration
# --------------------------------------------------------------------------- #
def test_build_exit_plan_ceiling_end_state():
    plan = exit_plan.build_exit_plan(_model("reduce", 1.0, 3.0), _holdings(),
                                     as_of=AS_OF, fetch=False, with_options=False)
    assert len(plan["positions"]) == 1
    pos = plan["positions"][0]
    assert pos["symbol"] == "EXITME"
    assert pos["end_state"] == "ceiling"
    assert pos["target_pct"] == 3.0
    # 800k held, trim to 3% of 1M invested = 30k -> exit 770k.
    assert pos["exit_czk"] == 770_000
    assert pos["exit_shares"] == 7_700
    assert pos["tax"]["defer_czk"] == 370_000
    assert pos["schedule"]["tranches"], "expected a scale-out schedule"
    assert all(isinstance(row["shares"], int) for row in pos["schedule"]["tranches"])


def test_build_exit_plan_full_exit_to_zero():
    plan = exit_plan.build_exit_plan(_model("reduce", 1.0, 3.0), _holdings(),
                                     full_exit=["EXITME"], as_of=AS_OF, fetch=False, with_options=False)
    pos = plan["positions"][0]
    assert pos["end_state"] == "zero"
    assert pos["target_pct"] == 0.0
    assert pos["exit_czk"] == 800_000


def test_build_exit_plan_avoid_rule_defaults_to_zero():
    plan = exit_plan.build_exit_plan(_model("avoid", 0.0, 0.0), _holdings(),
                                     as_of=AS_OF, fetch=False, with_options=False)
    pos = plan["positions"][0]
    assert pos["end_state"] == "zero"
    assert pos["exit_czk"] == 800_000


def test_build_exit_plan_skips_in_band_names():
    # Band comfortably above the 80% holding? No -- make the holding within band.
    plan = exit_plan.build_exit_plan(_model("reduce", 70.0, 90.0), _holdings(),
                                     as_of=AS_OF, fetch=False, with_options=False)
    assert plan["positions"] == []


# --------------------------------------------------------------------------- #
# stage_covered_call — server validation, idempotence, provenance
# --------------------------------------------------------------------------- #
def test_indicative_ladder_keeps_covered_call_route_selectable(monkeypatch):
    import trade_service

    monkeypatch.setattr(
        trade_service,
        "covered_call_capacity",
        lambda _symbol: {
            "capacity_contracts": 4,
            "current_shares": 400,
            "held_short_calls": 0,
        },
    )
    routes = exit_plan._execution_routes({
        "symbol": "EXITME",
        "exit_shares": 400,
        "schedule": {"tranches": []},
        "options": {
            "covered_call_ladder": [{
                "strike": 105,
                "executable": False,
                "source": "yahoo",
            }],
        },
    }, now=AS_OF)

    assert routes["covered_call"]["eligible"] is True
    assert routes["covered_call"]["stageable"] is False
    assert "Indicative covered-call levels from" in routes["covered_call"]["reasons"][0]
    assert routes["recommended"] == "covered_call"


def test_covered_call_route_rounds_89_planned_shares_to_one_contract(monkeypatch):
    import trade_service

    monkeypatch.setattr(
        trade_service,
        "covered_call_capacity",
        lambda _symbol: {
            "capacity_contracts": 3,
            "current_shares": 300,
            "held_short_calls": 0,
        },
    )
    routes = exit_plan._execution_routes({
        "symbol": "ARM",
        "exit_shares": 89,
        "schedule": {"tranches": []},
        "options": {
            "covered_call_ladder": [{
                "conid": 555,
                "strike": 340,
                "bid": 30.3,
                "ask": 33.95,
                "stageable": True,
                "executable": True,
                "quote_timestamp": AS_OF.isoformat(),
            }],
        },
    }, now=AS_OF)

    route = routes["covered_call"]
    assert route["eligible"] is True
    assert route["stageable"] is True
    assert route["capacity_contracts"] == 1
    assert route["assignment_shares"] == 100
    assert route["share_deviation"] == 11
    assert route["rounded_up"] is True


def test_covered_call_round_up_rejects_large_deviation_or_negative_position():
    assert exit_plan._covered_call_exit_contracts(80, 300) == 0
    assert exit_plan._covered_call_exit_contracts(89, 95) == 0
    assert exit_plan._covered_call_exit_contracts(89, 300) == 1


def test_missing_bid_ask_warns_but_remains_stageable(monkeypatch):
    import trade_service

    monkeypatch.setattr(
        trade_service,
        "covered_call_capacity",
        lambda _symbol: {
            "capacity_contracts": 1,
            "current_shares": 100,
            "held_short_calls": 0,
        },
    )
    entry = {
        "symbol": "EXITME",
        "exit_shares": 100,
        "schedule": {"tranches": []},
        "options": {
            "covered_call_ladder": [{
                "conid": 555,
                "strike": 105,
                "bid": None,
                "ask": None,
                "stageable": True,
                "executable": False,
                "quote_timestamp": AS_OF.isoformat(),
            }],
        },
    }
    routes = exit_plan._execution_routes(entry, now=AS_OF)
    rung = entry["options"]["covered_call_ladder"][0]
    assert routes["covered_call"]["eligible"] is True
    assert routes["covered_call"]["stageable"] is True
    assert routes["covered_call"]["reasons"] == []
    assert rung["limit_price"] is None
    assert "Staging is allowed" in rung["staging_warning"]


def test_stale_quote_warns_and_refreshes_during_staging(monkeypatch):
    import trade_service

    monkeypatch.setattr(
        trade_service,
        "covered_call_capacity",
        lambda _symbol: {
            "capacity_contracts": 1,
            "current_shares": 100,
            "held_short_calls": 0,
        },
    )
    entry = {
        "symbol": "EXITME",
        "exit_shares": 100,
        "schedule": {"tranches": []},
        "options": {
            "covered_call_ladder": [{
                "conid": 555,
                "strike": 105,
                "bid": 2.40,
                "ask": 2.60,
                "stageable": True,
                "executable": True,
                "quote_timestamp": "2020-01-01T00:00:00+00:00",
            }],
        },
    }
    routes = exit_plan._execution_routes(entry, now=AS_OF)
    rung = entry["options"]["covered_call_ladder"][0]
    assert routes["covered_call"]["stageable"] is True
    assert rung["limit_price"] is None
    assert "Staging will refresh it from IBKR" in rung["staging_warning"]


def _fresh_quote_ts():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _exit_plan_with_cc(*, eligible=True, exit_shares=400, capacity_contracts=4,
                       rung_overrides=None, route_overrides=None):
    rung = {
        "conid": 555,
        "expiry": "2026-08-21",
        "strike": 105.0,
        "bid": 2.40,
        "ask": 2.60,
        "stageable": True,
        "executable": True,
        "quote_timestamp": _fresh_quote_ts(),
    }
    if rung_overrides:
        rung.update(rung_overrides)
    route = {
        "eligible": eligible,
        "reasons": [] if eligible else ["covered-call route is not eligible"],
        "capacity_contracts": capacity_contracts if eligible else 0,
    }
    if route_overrides:
        route.update(route_overrides)
    return {
        "as_of": "2026-07-01",
        "snapshot": "test-snapshot",
        "positions": [{
            "symbol": "EXITME",
            "exit_shares": exit_shares,
            "exit_czk": 400_000,
            "schedule": {"tranches": [{"index": 0, "shares": 100.0, "czk": 50_000}]},
            "options": {"covered_call_ladder": [rung]},
            "routes": {"covered_call": route},
        }],
    }, rung


def _stage_mocks(
    monkeypatch, tmp_path, *, holdings_shares=500, held_short_calls=0,
    working=None, exact=None,
):
    import store
    import trade_service
    from config import HOLDINGS_JSON

    staged_path = tmp_path / "staged-basket.json"
    monkeypatch.setattr(trade_service, "STAGED_BASKET_JSON", staged_path)
    holdings = {
        "positions": [{
            "symbol": "EXITME", "asset_class": "STK", "quantity": holdings_shares,
            "mark_price": 100.0, "market_value": holdings_shares * 100.0,
            "base_market_value": holdings_shares * 100.0, "currency": "CZK",
        }],
        "lots": [],
    }
    if held_short_calls:
        holdings["positions"].append({
            "symbol": "EXITME  260821C00105000",
            "asset_class": "OPT",
            "quantity": -held_short_calls,
            "mark_price": 2.5,
            "market_value": -250 * held_short_calls,
            "base_market_value": -250 * held_short_calls,
            "currency": "USD",
        })

    def _load(path):
        if path == HOLDINGS_JSON:
            return holdings
        return store.load(path)

    monkeypatch.setattr(trade_service, "_load", _load)
    monkeypatch.setattr(ibkr_trade, "accounts", lambda: [{"accountId": "DU1"}])
    monkeypatch.setattr(
        ibkr_trade,
        "positions",
        lambda account, page=0: ([{
            "assetClass": "STK",
            "contractDesc": "EXITME",
            "position": holdings_shares,
        }] if page == 0 else []),
    )
    monkeypatch.setattr(ibkr_trade, "live_orders", lambda: working or [])
    monkeypatch.setattr(
        ibkr_trade, "resolve_exact_call",
        lambda sym, expiry, strike: exact or {
            "conid": 555,
            "expiry": expiry,
            "strike": strike,
            "bid": 2.40,
            "ask": 2.60,
            "quote_timestamp": _fresh_quote_ts(),
            "rules": {"increment": 0.05},
        },
    )


def test_stage_covered_call_validates_and_stages_with_provenance(tmp_path, monkeypatch):
    import trade_service

    plan, rung = _exit_plan_with_cc()
    _stage_mocks(monkeypatch, tmp_path)
    out = exit_plan.stage_covered_call(
        plan, "EXITME", conid=555, expiry=rung["expiry"],
        strike=rung["strike"], contracts=2,
    )
    assert out["staged"] is True
    assert out["route"] == "covered_call"
    leg = out["leg"]
    assert leg["leg_id"] == "covered_call:EXITME:555"
    assert leg["contracts"] == 2
    assert leg["limit_price"] == 2.50
    prov = leg["provenance"][0]
    assert prov["source"] == "exit_plan"
    assert prov["route"] == "covered_call"
    assert prov["plan_fingerprint"]
    assert prov["rung"] == {"conid": 555, "expiry": "2026-08-21", "strike": 105.0}
    assert prov["intended_assigned_shares"] == 200
    assert trade_service.load_basket() == out["basket"]


def test_stage_covered_call_accepts_bounded_round_up(tmp_path, monkeypatch):
    plan, rung = _exit_plan_with_cc(exit_shares=89, capacity_contracts=1)
    _stage_mocks(monkeypatch, tmp_path, holdings_shares=300)
    out = exit_plan.stage_covered_call(
        plan,
        "EXITME",
        conid=555,
        expiry=rung["expiry"],
        strike=rung["strike"],
        contracts=1,
    )
    assert out["leg"]["contracts"] == 1
    assert out["leg"]["provenance"][0]["intended_assigned_shares"] == 100


def test_stage_covered_call_allows_missing_quote_with_warning(tmp_path, monkeypatch):
    plan, rung = _exit_plan_with_cc(rung_overrides={
        "bid": None,
        "ask": None,
        "executable": False,
        "staging_warning": "No live bid/ask right now.",
    })
    _stage_mocks(monkeypatch, tmp_path, exact={
        "conid": 555,
        "expiry": rung["expiry"],
        "strike": rung["strike"],
        "bid": None,
        "ask": None,
        "quote_timestamp": _fresh_quote_ts(),
        "rules": {"increment": 0.05},
    })
    out = exit_plan.stage_covered_call(
        plan,
        "EXITME",
        conid=555,
        expiry=rung["expiry"],
        strike=rung["strike"],
        contracts=1,
    )
    assert out["leg"]["limit_price"] is None
    assert "cannot be previewed or placed" in out["leg"]["staging_warning"]


def test_stage_covered_call_round_up_cannot_exceed_live_shares(tmp_path, monkeypatch):
    plan, rung = _exit_plan_with_cc(exit_shares=89, capacity_contracts=1)
    _stage_mocks(monkeypatch, tmp_path, holdings_shares=95)
    with pytest.raises(ValueError, match="only 0 covered-call contract"):
        exit_plan.stage_covered_call(
            plan,
            "EXITME",
            conid=555,
            expiry=rung["expiry"],
            strike=rung["strike"],
            contracts=1,
        )


def test_stage_covered_call_idempotent_by_leg_id(tmp_path, monkeypatch):
    import trade_service

    plan, rung = _exit_plan_with_cc()
    _stage_mocks(monkeypatch, tmp_path)
    exit_plan.stage_covered_call(
        plan, "EXITME", conid=555, expiry=rung["expiry"],
        strike=rung["strike"], contracts=1,
    )
    out = exit_plan.stage_covered_call(
        plan, "EXITME", conid=555, expiry=rung["expiry"],
        strike=rung["strike"], contracts=3,
    )
    basket = trade_service.load_basket()
    assert len(basket) == 1
    assert basket[0]["contracts"] == 3
    assert out["leg"]["contracts"] == 3


def test_stage_covered_call_rejects_ineligible_route(tmp_path, monkeypatch):
    plan, rung = _exit_plan_with_cc(eligible=False)
    _stage_mocks(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="not eligible"):
        exit_plan.stage_covered_call(
            plan, "EXITME", conid=555, expiry=rung["expiry"],
            strike=rung["strike"], contracts=1,
        )


def test_stage_covered_call_rejects_rung_not_in_ladder(tmp_path, monkeypatch):
    plan, rung = _exit_plan_with_cc()
    _stage_mocks(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="not in the current server-built Exit ladder"):
        exit_plan.stage_covered_call(
            plan, "EXITME", conid=999, expiry=rung["expiry"],
            strike=rung["strike"], contracts=1,
        )


def test_stage_covered_call_revalidates_stale_ladder_quote(tmp_path, monkeypatch):
    plan, rung = _exit_plan_with_cc(rung_overrides={
        "quote_timestamp": "2020-01-01T00:00:00+00:00",
    })
    _stage_mocks(monkeypatch, tmp_path)
    out = exit_plan.stage_covered_call(
        plan, "EXITME", conid=555, expiry=rung["expiry"],
        strike=rung["strike"], contracts=1,
    )
    assert out["leg"]["limit_price"] == 2.50


def test_stage_covered_call_rejects_stale_revalidated_quote(tmp_path, monkeypatch):
    plan, rung = _exit_plan_with_cc()
    _stage_mocks(monkeypatch, tmp_path, exact={
        "conid": 555,
        "expiry": rung["expiry"],
        "strike": rung["strike"],
        "bid": 2.40,
        "ask": 2.60,
        "quote_timestamp": "2020-01-01T00:00:00+00:00",
        "rules": {"increment": 0.05},
    })
    with pytest.raises(ValueError, match="option quote is stale"):
        exit_plan.stage_covered_call(
            plan, "EXITME", conid=555, expiry=rung["expiry"],
            strike=rung["strike"], contracts=1,
        )


def test_stage_covered_call_rejects_contracts_above_route_capacity(tmp_path, monkeypatch):
    plan, rung = _exit_plan_with_cc(capacity_contracts=1, exit_shares=400)
    _stage_mocks(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="exceed uncovered exit capacity"):
        exit_plan.stage_covered_call(
            plan, "EXITME", conid=555, expiry=rung["expiry"],
            strike=rung["strike"], contracts=2,
        )


def test_stage_covered_call_rejects_when_working_orders_shrink_capacity(tmp_path, monkeypatch):
    plan, rung = _exit_plan_with_cc(exit_shares=400, capacity_contracts=4)
    working = [{
        "orderId": "w1",
        "ticker": "EXITME  260821C00105000",
        "secType": "OPT",
        "side": "SELL",
        "remainingQuantity": 3,
        "status": "Submitted",
    }]
    _stage_mocks(monkeypatch, tmp_path, holdings_shares=500, working=working)
    with pytest.raises(ValueError, match="remain after held/working calls"):
        exit_plan.stage_covered_call(
            plan, "EXITME", conid=555, expiry=rung["expiry"],
            strike=rung["strike"], contracts=4,
        )


def test_stage_covered_call_rejects_unresolvable_exact_contract(tmp_path, monkeypatch):
    plan, rung = _exit_plan_with_cc()
    _stage_mocks(monkeypatch, tmp_path, exact=None)
    monkeypatch.setattr(ibkr_trade, "resolve_exact_call", lambda *a, **k: None)
    with pytest.raises(ValueError, match="exact call contract no longer resolves"):
        exit_plan.stage_covered_call(
            plan, "EXITME", conid=555, expiry=rung["expiry"],
            strike=rung["strike"], contracts=1,
        )


def test_stage_covered_call_rejects_crossed_live_quote(tmp_path, monkeypatch):
    plan, rung = _exit_plan_with_cc()
    _stage_mocks(monkeypatch, tmp_path, exact={
        "conid": 555, "expiry": "2026-08-21", "strike": 105.0,
        "bid": 2.80, "ask": 2.60,
        "quote_timestamp": _fresh_quote_ts(),
        "rules": {"increment": 0.05},
    })
    with pytest.raises(ValueError, match="bid/ask is missing or crossed"):
        exit_plan.stage_covered_call(
            plan, "EXITME", conid=555, expiry=rung["expiry"],
            strike=rung["strike"], contracts=1,
        )


def test_stage_covered_call_rejects_when_working_orders_unavailable(tmp_path, monkeypatch):
    plan, rung = _exit_plan_with_cc()
    _stage_mocks(monkeypatch, tmp_path)
    monkeypatch.setattr(
        ibkr_trade, "live_orders",
        mock.Mock(side_effect=ibkr_trade.CPAPIError("orders bridge down")),
    )
    with pytest.raises(ValueError, match="working option orders could not be verified"):
        exit_plan.stage_covered_call(
            plan, "EXITME", conid=555, expiry=rung["expiry"],
            strike=rung["strike"], contracts=1,
        )


def test_stage_covered_call_capacity_includes_staged_share_sales(tmp_path, monkeypatch):
    import trade_service

    plan, rung = _exit_plan_with_cc(exit_shares=500, capacity_contracts=5)
    _stage_mocks(monkeypatch, tmp_path, holdings_shares=500)
    trade_service.save_basket([{
        "type": "stock",
        "symbol": "EXITME",
        "delta_czk": -25_000,
        "provenance": {
            "source": "exit_plan",
            "route": "sell_shares",
            "intended_shares": 250,
        },
    }])
    with pytest.raises(ValueError) as exc:
        exit_plan.stage_covered_call(
            plan, "EXITME", conid=555, expiry=rung["expiry"],
            strike=rung["strike"], contracts=3,
        )
    assert "250 staged share sales" in str(exc.value)


def test_stage_share_tranche_cannot_uncover_staged_calls(tmp_path, monkeypatch):
    import trade_service

    plan, _rung = _exit_plan_with_cc(exit_shares=500, capacity_contracts=5)
    _stage_mocks(monkeypatch, tmp_path, holdings_shares=500)
    trade_service.save_basket([{
        "type": "covered_call",
        "symbol": "EXITME",
        "route": "covered_call",
        "conid": 555,
        "expiry": "2026-08-21",
        "strike": 105.0,
        "contracts": 5,
    }])
    with pytest.raises(ValueError) as exc:
        exit_plan.stage_tranche(plan, "EXITME", 0)
    assert "covering 5 held/staged short call" in str(exc.value)


def test_stage_share_tranche_cannot_uncover_held_calls(tmp_path, monkeypatch):
    plan, _rung = _exit_plan_with_cc(exit_shares=250, capacity_contracts=0)
    _stage_mocks(
        monkeypatch, tmp_path, holdings_shares=250, held_short_calls=2,
    )
    with pytest.raises(ValueError) as exc:
        exit_plan.stage_tranche(plan, "EXITME", 0)
    assert "covering 2 held/staged short call" in str(exc.value)


def test_stage_share_tranche_rejects_aggregate_sell_above_holdings(tmp_path, monkeypatch):
    import trade_service

    plan, _rung = _exit_plan_with_cc()
    _stage_mocks(monkeypatch, tmp_path, holdings_shares=500)
    trade_service.save_basket([{
        "type": "stock",
        "symbol": "EXITME",
        "delta_czk": -25_000,
    }])
    with pytest.raises(ValueError, match="staged stock sells exceed holdings"):
        exit_plan.stage_tranche(plan, "EXITME", 0)
    assert trade_service.load_basket()[0]["delta_czk"] == -25_000


def test_stage_share_tranche_is_idempotent_for_same_plan_tranche(tmp_path, monkeypatch):
    import trade_service

    plan, _rung = _exit_plan_with_cc()
    _stage_mocks(monkeypatch, tmp_path, holdings_shares=500)
    first = exit_plan.stage_tranche(plan, "EXITME", 0)
    second = exit_plan.stage_tranche(plan, "EXITME", 0)
    assert first["basket"][0]["delta_czk"] == -50_000
    assert second["already_staged"] is True
    assert trade_service.load_basket()[0]["delta_czk"] == -50_000
