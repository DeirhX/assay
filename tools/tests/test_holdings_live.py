import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import holdings_live as hl  # noqa: E402


def _snapshot():
    # Mixed book: a USD name, a foreign name whose snapshot symbol carries an
    # exchange suffix, a short, and an option. base = CZK.
    return {
        "net_asset_value": 1_000_000.0,
        "positions": [
            {"symbol": "ACN", "currency": "USD", "asset_class": "STK", "quantity": 20.0,
             "mark_price": 100.0, "market_value": 2000.0, "base_market_value": 46_000.0},
            {"symbol": "005930.KS", "currency": "KRW", "asset_class": "STK", "quantity": 170.0,
             "mark_price": 280_000.0, "market_value": 47_600_000.0, "base_market_value": 800_000.0},
            {"symbol": "SPCX", "currency": "USD", "asset_class": "STK", "quantity": -10.0,
             "mark_price": 50.0, "market_value": -500.0, "base_market_value": -11_500.0},
            {"symbol": "KLAC  260717P00238000", "currency": "USD", "asset_class": "OPT", "quantity": -2.0,
             "mark_price": 3.0, "market_value": -600.0, "base_market_value": -13_800.0},
        ],
    }


def _live(overrides=None):
    rows = [
        {"contractDesc": "ACN", "assetClass": "STK", "currency": "USD", "mktPrice": 110.0, "position": 20.0},
        {"contractDesc": "005930", "assetClass": "STK", "currency": "KRW", "mktPrice": 294_000.0, "position": 170.0},
        {"contractDesc": "SPCX", "assetClass": "STK", "currency": "USD", "mktPrice": 45.0, "position": -10.0},
    ]
    return overrides if overrides is not None else rows


def test_stk_mark_scales_base_value_and_flags_live():
    res = hl.apply_live_marks(_snapshot(), _live())
    assert res["available"] is True
    by = {p["symbol"]: p for p in res["payload"]["positions"]}
    # ACN +10% -> 46,000 * 1.1
    assert round(by["ACN"]["base_market_value"], 2) == 50_600.0
    assert by["ACN"]["live_mark"] is True


def test_exchange_suffix_is_normalized():
    # snapshot 005930.KS must match live 005930.
    res = hl.apply_live_marks(_snapshot(), _live())
    by = {p["symbol"]: p for p in res["payload"]["positions"]}
    assert by["005930.KS"]["live_mark"] is True
    # 800,000 * (294000/280000) = 840,000
    assert round(by["005930.KS"]["base_market_value"]) == 840_000


def test_short_position_keeps_sign():
    res = hl.apply_live_marks(_snapshot(), _live())
    by = {p["symbol"]: p for p in res["payload"]["positions"]}
    # SPCX -10%: -11,500 * (45/50) = -10,350 (still negative)
    assert round(by["SPCX"]["base_market_value"], 2) == -10_350.0
    assert by["SPCX"]["live_mark"] is True


def test_options_and_unmatched_stay_delayed():
    res = hl.apply_live_marks(_snapshot(), _live())
    by = {p["symbol"]: p for p in res["payload"]["positions"]}
    opt = by["KLAC  260717P00238000"]
    assert opt["live_mark"] is False
    assert opt["base_market_value"] == -13_800.0  # untouched


def test_currency_guard_blocks_cross_listing():
    # A USD 'ACN' snapshot must NOT match a EUR 'ACN' live row.
    live = [{"contractDesc": "ACN", "assetClass": "STK", "currency": "EUR", "mktPrice": 999.0, "position": 20.0}]
    res = hl.apply_live_marks(_snapshot(), live)
    by = {p["symbol"]: p for p in res["payload"]["positions"]}
    assert by["ACN"]["live_mark"] is False
    assert by["ACN"]["base_market_value"] == 46_000.0


def test_nav_shifts_by_invested_delta():
    snap = _snapshot()
    res = hl.apply_live_marks(snap, _live())
    # deltas: ACN +4600, 005930 +40000, SPCX +1150 (short recovered) = +45750
    assert round(res["payload"]["net_asset_value"], 2) == 1_000_000.0 + 45_750.0


def test_coverage_counts_only_eligible_stk():
    res = hl.apply_live_marks(_snapshot(), _live())
    cov = res["coverage"]
    assert cov["live"] == 3
    assert cov["eligible"] == 3   # 3 STK, option excluded
    assert cov["total"] == 4


def test_ignores_live_rows_with_bad_price():
    live = [{"contractDesc": "ACN", "assetClass": "STK", "currency": "USD", "mktPrice": 0.0, "position": 20.0}]
    res = hl.apply_live_marks(_snapshot(), live)
    by = {p["symbol"]: p for p in res["payload"]["positions"]}
    assert by["ACN"]["live_mark"] is False


def test_refresh_marks_returns_none_when_gateway_unavailable():
    with mock.patch.object(hl, "_fetch_live_positions", return_value=None):
        assert hl.refresh_marks(_snapshot()) is None


def test_refresh_marks_none_on_empty_snapshot():
    assert hl.refresh_marks({}) is None
    assert hl.refresh_marks(None) is None


def test_refresh_marks_uses_fetched_positions():
    with mock.patch.object(hl, "_fetch_live_positions", return_value=_live()):
        res = hl.refresh_marks(_snapshot())
    assert res["available"] is True
    assert res["coverage"]["live"] == 3


def test_live_snapshot_replaces_book_and_drops_mismatched_tax_lots():
    snap = _snapshot()
    snap.update({
        "base_currency": "CZK",
        "lots": [
            {"symbol": "ACN", "quantity": 20.0},
            {"symbol": "005930.KS", "quantity": 170.0},
        ],
        "tax_lot_summary": [
            {"symbol": "ACN", "total_quantity": 20.0},
            {"symbol": "005930.KS", "total_quantity": 170.0},
        ],
        "top_positions": [],
    })
    live = [
        {
            "ticker": "ACN", "contractDesc": "ACN", "assetClass": "STK",
            "currency": "USD", "mktPrice": 110.0, "mktValue": 2750.0,
            "position": 25.0, "unrealizedPnl": 500.0,
        },
        {
            "ticker": "005930", "contractDesc": "005930", "assetClass": "STK",
            "currency": "KRW", "mktPrice": 294_000.0, "mktValue": 49_980_000.0,
            "position": 170.0, "unrealizedPnl": 1000.0,
        },
        {
            "ticker": "PYPL", "contractDesc": "PYPL AUG2026 47 C",
            "assetClass": "OPT", "currency": "USD", "mktPrice": 1.0,
            "mktValue": -100.0, "position": -1.0, "expiry": "20260814",
            "putOrCall": "C", "strike": "47", "multiplier": 100.0,
        },
    ]
    summary = {
        "netliquidation": {"amount": 1_100_000.0, "currency": "CZK"},
        "totalcashvalue": {"amount": 20_000.0, "currency": "CZK"},
    }

    before = datetime.now(timezone.utc)
    out = hl.merge_live_snapshot(snap, live, summary, {"currency": "CZK"})
    after = datetime.now(timezone.utc)
    by = {row["symbol"]: row for row in out["positions"]}

    assert set(by) == {"ACN", "005930.KS", "PYPL  260814C00047000"}
    assert by["ACN"]["quantity"] == 25.0
    assert by["ACN"]["base_market_value"] == 63_250.0
    assert by["PYPL  260814C00047000"]["base_market_value"] == -2300.0
    assert out["net_asset_value"] == 1_100_000.0
    assert out["cash"][0]["ending_cash"] == 20_000.0
    assert [lot["symbol"] for lot in out["lots"]] == ["005930.KS"]
    assert [row["symbol"] for row in out["tax_lot_summary"]] == ["005930.KS"]
    assert "Client Portal API live snapshot" in out["source"]
    generated = datetime.fromisoformat(out["generated_at"])
    assert before.replace(microsecond=0) <= generated <= after
