import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import options_math as om  # noqa: E402


def test_norm_cdf_reference_points():
    assert abs(om.norm_cdf(0.0) - 0.5) < 1e-9
    assert abs(om.norm_cdf(1.96) - 0.975) < 1e-3
    assert abs(om.norm_cdf(-1.96) - 0.025) < 1e-3


def test_put_call_parity_zero_dividend():
    # C - P == S - K e^{-rT} for European options with q=0.
    spot, strike, t, vol, rate = 100.0, 105.0, 0.5, 0.30, 0.04
    call = om.bs_price(spot, strike, t, vol, rate=rate, kind="call")
    put = om.bs_price(spot, strike, t, vol, rate=rate, kind="put")
    parity = spot - strike * math.exp(-rate * t)
    assert abs((call - put) - parity) < 1e-6


def test_atm_call_price_is_positive_and_reasonable():
    # ATM 1y 20% vol call ~ 0.4*S*sigma*sqrt(T) rule of thumb (~8 on 100).
    px = om.bs_price(100.0, 100.0, 1.0, 0.20, rate=0.0, kind="call")
    assert px is not None and 6.0 < px < 10.0


def test_call_delta_between_zero_and_one():
    d = om.bs_delta(100.0, 100.0, 0.25, 0.35, rate=0.04, kind="call")
    assert d is not None and 0.4 < d < 0.65
    dp = om.bs_delta(100.0, 100.0, 0.25, 0.35, rate=0.04, kind="put")
    assert dp is not None and -0.6 < dp < -0.35


def test_deep_otm_call_delta_is_small():
    d = om.bs_delta(100.0, 200.0, 0.1, 0.30, kind="call")
    assert d is not None and d < 0.05


def test_degenerate_inputs_return_none():
    assert om.bs_price(0.0, 100.0, 1.0, 0.2) is None
    assert om.bs_price(100.0, 100.0, 0.0, 0.2) is None
    assert om.bs_price(100.0, 100.0, 1.0, 0.0) is None
    assert om.bs_delta(100.0, 100.0, -1.0, 0.2) is None


def test_annualized_vol_flat_series_is_zero():
    assert om.annualized_vol([100.0, 100.0, 100.0, 100.0]) == 0.0


def test_annualized_vol_needs_two_returns():
    assert om.annualized_vol([100.0]) is None
    assert om.annualized_vol([]) is None


def test_annualized_vol_positive_for_moving_series():
    closes = [100.0 * (1.01 if i % 2 else 0.99) for i in range(40)]
    v = om.annualized_vol(closes)
    assert v is not None and v > 0.0


def test_risk_free_rate_from_snapshot():
    snap = {"series": {"DGS10": {"value": 4.25}}}
    assert abs(om.risk_free_rate(snapshot=snap) - 0.0425) < 1e-9


def test_risk_free_rate_falls_back_on_missing():
    assert om.risk_free_rate(snapshot={"series": {}}) == om.DEFAULT_RISK_FREE
    assert om.risk_free_rate(snapshot={}) == om.DEFAULT_RISK_FREE
