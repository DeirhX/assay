"""Alpaca options market-data provider -- a structured fallback for the exit
overlay's option chain when IBKR can't resolve one.

Enabled when ``ALPACA_API_KEY_ID`` / ``ALPACA_API_SECRET_KEY`` are set (live env
or a ``secrets.env``). The options *snapshots* endpoint gives a bid/ask/last quote
for every listed contract and a daily traded ``volume`` for most; it computes
greeks + implied vol only for the liquid near-the-money strikes (which is exactly
where a covered-call ladder lives), so ``delta``/``implied_vol`` are best-effort
and fall through to a Black-Scholes estimate downstream when absent.

Open interest is deliberately ``None`` here: it is not on the market-data feed at
all (it lives on the Trading API's ``get_option_contract`` with a one-day lag), so
liquidity gating on an Alpaca-sourced chain leans on spread + daily volume.

Default feed is ``indicative`` (free, ~15-min delayed); override with
``ALPACA_OPTIONS_FEED=opra`` if the account carries the realtime OPRA entitlement.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import urllib.parse
from typing import Any

from .common import ProviderError, get_json

_DATA = "https://data.alpaca.markets"
# Alpaca's compact OSI symbol: ROOT + YYMMDD + C|P + strike*1000 (8 digits).
_OCC = re.compile(r"^(?P<root>[A-Z0-9]+?)(?P<ymd>\d{6})(?P<cp>[CP])(?P<strike>\d{8})$")
_MAX_PAGES = 4  # bound the fan-out; a windowed request rarely spills past one.
# DTE targets the returned expiries should cover. Alpaca lists weeklies, so the
# naive "N soonest" would bunch inside a fortnight and starve the exit overlay's
# 30-45 DTE covered-call window; spanning these targets guarantees a monthly-ish
# expiry in that band plus a couple of longer-dated ones for the collar path.
_TARGET_DTES = (35, 75, 120, 165)


def _cfg(key: str, default: str = "") -> str:
    """Resolve a key from live env first, then ``secrets.env`` (via config), so
    Alpaca creds follow the same precedence as the IBKR credentials."""
    val = os.environ.get(key)
    if val is None:
        try:
            import config
            val = config.config_value(key) or None
        except Exception:  # noqa: BLE001 -- config not importable: env-only.
            val = None
    return (val if val is not None else default).strip()


def api_keys() -> tuple[str | None, str | None]:
    return (_cfg("ALPACA_API_KEY_ID") or None, _cfg("ALPACA_API_SECRET_KEY") or None)


def enabled() -> bool:
    kid, sec = api_keys()
    return bool(kid and sec)


def _feed() -> str:
    return _cfg("ALPACA_OPTIONS_FEED", "indicative") or "indicative"


def _headers() -> dict[str, str] | None:
    kid, sec = api_keys()
    if not (kid and sec):
        return None
    return {"APCA-API-KEY-ID": kid, "APCA-API-SECRET-KEY": sec, "Accept": "application/json"}


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _parse_occ(sym: str) -> tuple[str, str, float] | None:
    """``AAPL260710C00315000`` -> ``("2026-07-10", "C", 315.0)``; None if it isn't
    a well-formed OSI symbol."""
    m = _OCC.match(sym or "")
    if not m:
        return None
    ymd = m.group("ymd")
    try:
        expiry = dt.date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:6])).isoformat()
    except ValueError:
        return None
    return expiry, m.group("cp"), int(m.group("strike")) / 1000.0


def _contract(strike: float, snap: dict[str, Any]) -> dict[str, Any]:
    """A Yahoo-shaped chain row from one Alpaca option snapshot."""
    quote = snap.get("latestQuote") or {}
    trade = snap.get("latestTrade") or {}
    greeks = snap.get("greeks") or {}
    bar = snap.get("dailyBar") or {}
    vol = bar.get("v")
    return {
        "strike": float(strike),
        "bid": _num(quote.get("bp")),
        "ask": _num(quote.get("ap")),
        "last": _num(trade.get("p")),
        "implied_vol": _num(snap.get("impliedVolatility")),
        "delta": _num(greeks.get("delta")),
        "volume": int(vol) if isinstance(vol, (int, float)) and not isinstance(vol, bool) else None,
        # Never on the data feed -- Trading API only, one-day lag.
        "open_interest": None,
    }


def _select_expiries(expiries: list[str], as_of: dt.date, max_n: int) -> list[str]:
    """Choose up to ``max_n`` expiries spanning ``_TARGET_DTES`` rather than the
    ``max_n`` soonest -- so a 30-45 DTE covered-call expiry is always present even
    though Alpaca lists dense weeklies. Tops up with the nearest remaining dates
    if the targets don't fill the quota."""
    dated = [(e, (dt.date.fromisoformat(e) - as_of).days) for e in expiries]
    dated = [(e, d) for e, d in dated if d >= 0]
    if not dated:
        return []
    chosen: list[str] = []
    for target in _TARGET_DTES[:max_n]:
        best = min((ed for ed in dated if ed[0] not in chosen),
                   key=lambda ed: abs(ed[1] - target), default=None)
        if best is not None:
            chosen.append(best[0])
    for e, _ in dated:
        if len(chosen) >= max_n:
            break
        if e not in chosen:
            chosen.append(e)
    return sorted(chosen)


def _spot(symbol: str, headers: dict[str, str]) -> float | None:
    """Underlying last trade (IEX feed, free) to center the strike window. Best
    effort: None just means we skip the strike filter."""
    try:
        res = get_json(f"{_DATA}/v2/stocks/{symbol}/trades/latest?feed=iex", headers=headers)
    except ProviderError:
        return None
    return _num(((res or {}).get("trade") or {}).get("p"))


def option_chain(symbol: str, *, max_expiries: int = 4, strike_window_pct: float = 0.25,
                 horizon_days: int = 200, as_of: dt.date | None = None) -> dict[str, Any] | None:
    """Yahoo-shaped option chain from Alpaca, or None when disabled / the name has
    no options / the feed returns nothing.

    Targeted like the IBKR reader: only expiries inside ``horizon_days`` and, when
    a spot is resolvable, strikes within ``strike_window_pct`` of it, so the
    snapshot payload stays small. ``source`` is ``"alpaca"``."""
    sym = str(symbol or "").strip().upper()
    headers = _headers()
    if not sym or headers is None:
        return None
    today = as_of or dt.datetime.now(dt.timezone.utc).date()

    params: dict[str, str] = {
        "feed": _feed(),
        "limit": "1000",
        "expiration_date_gte": today.isoformat(),
        "expiration_date_lte": (today + dt.timedelta(days=horizon_days)).isoformat(),
    }
    spot = _spot(sym, headers)
    if spot and spot > 0:
        params["strike_price_gte"] = f"{spot * (1.0 - strike_window_pct):.2f}"
        params["strike_price_lte"] = f"{spot * (1.0 + strike_window_pct):.2f}"

    snapshots: dict[str, Any] = {}
    page_token: str | None = None
    try:
        for _ in range(_MAX_PAGES):
            q = dict(params)
            if page_token:
                q["page_token"] = page_token
            res = get_json(f"{_DATA}/v1beta1/options/snapshots/{sym}?"
                           + urllib.parse.urlencode(q), headers=headers)
            if not isinstance(res, dict):
                break
            snapshots.update(res.get("snapshots") or {})
            page_token = res.get("next_page_token")
            if not page_token:
                break
    except ProviderError:
        return None
    if not snapshots:
        return None

    # Group contracts by expiry into calls/puts.
    by_expiry: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for osi, snap in snapshots.items():
        parsed = _parse_occ(osi)
        if not parsed or not isinstance(snap, dict):
            continue
        expiry, cp, strike = parsed
        bucket = by_expiry.setdefault(expiry, {"calls": [], "puts": []})
        bucket["calls" if cp == "C" else "puts"].append(_contract(strike, snap))
    if not by_expiry:
        return None

    expiries: list[dict[str, Any]] = []
    for expiry in _select_expiries(sorted(by_expiry), today, max_expiries):
        block = by_expiry[expiry]
        expiries.append({
            "expiry": expiry,
            "calls": sorted(block["calls"], key=lambda c: c["strike"]),
            "puts": sorted(block["puts"], key=lambda p: p["strike"]),
        })
    return {
        "source": "alpaca",
        "symbol": sym,
        "currency": "USD",
        "underlying_price": round(spot, 4) if isinstance(spot, (int, float)) else None,
        "expiries": expiries,
    }
