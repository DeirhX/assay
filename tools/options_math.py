#!/usr/bin/env python3
"""Black-Scholes fallback for the options overlay -- estimates, not quotes.

Yahoo's ``/v7/finance/options`` covers US-listed names well but returns nothing
for most foreign tickers (the very ones this book is full of). When there is no
live chain, the exit planner still wants a *ballpark* covered-call premium or
protective-put cost so the overlay isn't blank. This module supplies that from
first principles: European Black-Scholes with a continuous-dividend yield, fed
by realized volatility (``risk.py``) and the risk-free rate (FRED ``DGS10``).

Everything here is clearly labelled ``estimate`` upstream. It is NOT a pricing
engine: no smile, no American early-exercise premium, no term-structure. Pure
stdlib ``math`` so it stays a safe, unit-testable leaf.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

TRADING_DAYS = 252
# Fallback risk-free when FRED is unreachable: a neutral long-rate placeholder.
DEFAULT_RISK_FREE = 0.04


def norm_cdf(x: float) -> float:
    """Standard-normal CDF via the error function (stdlib, no scipy)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _d1_d2(
    spot: float, strike: float, t_years: float, vol: float, rate: float, div_yield: float
) -> tuple[float, float]:
    denom = vol * math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (rate - div_yield + 0.5 * vol * vol) * t_years) / denom
    return d1, d1 - denom


def bs_price(
    spot: float,
    strike: float,
    t_years: float,
    vol: float,
    *,
    rate: float = DEFAULT_RISK_FREE,
    div_yield: float = 0.0,
    kind: str = "call",
) -> float | None:
    """European option price. Returns None on degenerate inputs (non-positive
    spot/strike/time/vol) rather than raising, so callers can skip cleanly."""
    if spot <= 0 or strike <= 0 or t_years <= 0 or vol <= 0:
        return None
    d1, d2 = _d1_d2(spot, strike, t_years, vol, rate, div_yield)
    disc_r = math.exp(-rate * t_years)
    disc_q = math.exp(-div_yield * t_years)
    if kind == "call":
        px = spot * disc_q * norm_cdf(d1) - strike * disc_r * norm_cdf(d2)
    elif kind == "put":
        px = strike * disc_r * norm_cdf(-d2) - spot * disc_q * norm_cdf(-d1)
    else:
        raise ValueError(f"kind must be 'call' or 'put', got {kind!r}")
    return max(0.0, px)


def bs_delta(
    spot: float,
    strike: float,
    t_years: float,
    vol: float,
    *,
    rate: float = DEFAULT_RISK_FREE,
    div_yield: float = 0.0,
    kind: str = "call",
) -> float | None:
    """Option delta -- also the risk-neutral probability proxy for finishing ITM
    (|delta| ~= P(assignment) for calls), which the assignment guard leans on."""
    if spot <= 0 or strike <= 0 or t_years <= 0 or vol <= 0:
        return None
    d1, _ = _d1_d2(spot, strike, t_years, vol, rate, div_yield)
    disc_q = math.exp(-div_yield * t_years)
    if kind == "call":
        return disc_q * norm_cdf(d1)
    if kind == "put":
        return -disc_q * norm_cdf(-d1)
    raise ValueError(f"kind must be 'call' or 'put', got {kind!r}")


def annualized_vol(closes: list[float]) -> float | None:
    """Annualized volatility from a daily close series (stdev of log returns *
    sqrt(252)). None when there aren't enough usable observations."""
    rets: list[float] = []
    for i in range(1, len(closes)):
        prev, cur = closes[i - 1], closes[i]
        if prev and cur and prev > 0 and cur > 0:
            rets.append(math.log(cur / prev))
    n = len(rets)
    if n < 2:
        return None
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / (n - 1)
    return math.sqrt(var) * math.sqrt(TRADING_DAYS)


def risk_free_rate(*, snapshot: dict[str, Any] | None = None) -> float:
    """Risk-free rate as a decimal from FRED ``DGS10`` (a percent print), or the
    neutral default when FRED is unreachable. Accepts a pre-fetched
    ``macro_snapshot()`` dict to avoid a network hit in the hot path/tests."""
    if snapshot is None:
        try:
            from providers import fred  # lazy: keep network off pure-math callers
            snapshot = fred.macro_snapshot()
        except Exception:  # noqa: BLE001 -- FRED hiccup falls back to the default
            return DEFAULT_RISK_FREE
    series = (snapshot or {}).get("series") or {}
    node = series.get("DGS10") or {}
    val = node.get("value")
    if isinstance(val, (int, float)) and val > 0:
        return float(val) / 100.0  # DGS10 is quoted in percent
    return DEFAULT_RISK_FREE
