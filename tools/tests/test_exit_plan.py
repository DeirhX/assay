import datetime as dt
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import exit_plan  # noqa: E402
import ibkr_trade  # noqa: E402
import tax_lots  # noqa: E402
from providers import yahoo  # noqa: E402

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
    assert pos["tax"]["defer_czk"] == 370_000
    assert pos["schedule"]["tranches"], "expected a scale-out schedule"


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
# Option-chain source selection: IBKR first when the gateway is authenticated,
# Yahoo fallback otherwise / on an IBKR miss.
# --------------------------------------------------------------------------- #
def _ibkr_chain():
    return {"source": "ibkr", "symbol": "NVDA",
            "expiries": [{"expiry": "2026-08-21", "calls": [], "puts": []}]}


def _yahoo_chain():
    return {"source": "yahoo", "symbol": "NVDA",
            "expiries": [{"expiry": "2026-08-21", "calls": [], "puts": []}]}


def test_session_ready_reads_auth_status():
    with mock.patch.object(ibkr_trade, "auth_status", return_value={"authenticated": True}):
        assert exit_plan._ibkr_session_ready() is True
    with mock.patch.object(ibkr_trade, "auth_status", return_value={}):
        assert exit_plan._ibkr_session_ready() is False
    with mock.patch.object(ibkr_trade, "auth_status", side_effect=RuntimeError("down")):
        assert exit_plan._ibkr_session_ready() is False


def test_fetch_prefers_ibkr_when_authenticated():
    with mock.patch.object(exit_plan, "_ibkr_session_ready", return_value=True), \
            mock.patch.object(ibkr_trade, "option_chain", return_value=_ibkr_chain()) as ibkr_fn, \
            mock.patch.object(yahoo, "option_chain") as yahoo_fn:
        out = exit_plan._fetch_option_chain("NVDA")
    assert out["source"] == "ibkr"
    ibkr_fn.assert_called_once()
    yahoo_fn.assert_not_called()


def test_fetch_falls_back_to_yahoo_when_not_authenticated():
    with mock.patch.object(exit_plan, "_ibkr_session_ready", return_value=False), \
            mock.patch.object(ibkr_trade, "option_chain") as ibkr_fn, \
            mock.patch.object(yahoo, "option_chain", return_value=_yahoo_chain()) as yahoo_fn:
        out = exit_plan._fetch_option_chain("NVDA")
    assert out["source"] == "yahoo"
    ibkr_fn.assert_not_called()
    yahoo_fn.assert_called_once()


def test_fetch_falls_back_when_ibkr_resolves_nothing():
    with mock.patch.object(exit_plan, "_ibkr_session_ready", return_value=True), \
            mock.patch.object(ibkr_trade, "option_chain", return_value=None), \
            mock.patch.object(yahoo, "option_chain", return_value=_yahoo_chain()) as yahoo_fn:
        out = exit_plan._fetch_option_chain("NVDA")
    assert out["source"] == "yahoo"
    yahoo_fn.assert_called_once()


def test_fetch_falls_back_when_ibkr_raises():
    with mock.patch.object(exit_plan, "_ibkr_session_ready", return_value=True), \
            mock.patch.object(ibkr_trade, "option_chain", side_effect=RuntimeError("gateway boom")), \
            mock.patch.object(yahoo, "option_chain", return_value=_yahoo_chain()) as yahoo_fn:
        out = exit_plan._fetch_option_chain("NVDA")
    assert out["source"] == "yahoo"
    yahoo_fn.assert_called_once()


def test_cached_option_chain_persists_and_serves(tmp_path, monkeypatch):
    monkeypatch.setattr(exit_plan, "_OPT_CACHE_DIR", tmp_path)
    calls = {"n": 0}

    def fake_fetch(sym):
        calls["n"] += 1
        return _ibkr_chain()

    monkeypatch.setattr(exit_plan, "_fetch_option_chain", fake_fetch)
    first = exit_plan._cached_option_chain("NVDA")
    second = exit_plan._cached_option_chain("NVDA")
    assert first["source"] == "ibkr"
    assert second["source"] == "ibkr"
    assert calls["n"] == 1  # second call served from the fresh cache entry
