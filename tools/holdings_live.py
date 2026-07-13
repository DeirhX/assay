#!/usr/bin/env python3
"""Live-mark overlay for the holdings view.

The committed portfolio snapshot (``data/current-holdings.json``) comes from the
delayed IBKR **Flex** query, which is the source of truth for structure the live
feed can't give us: tax lots, cost basis, open dates (the Czech 3-year exit
clock), issuer country. But its *marks* are stale. When the Client Portal
Gateway holds an authenticated session we can refresh just the marks from the
live CPAPI ``/portfolio/{acct}/positions`` feed.

This module is a thin, read-only overlay: it never trades, never writes the
snapshot, and degrades to "unavailable" (not an error) whenever the gateway is
down/unauthenticated or a fetch times out. Only equity (STK) marks are
refreshed; options and any unmatched name keep their Flex mark (flagged
``live_mark: false``) so the UI can be honest about coverage.

Currency note: CPAPI ``mktPrice``/``mktValue`` are in each instrument's *local*
currency, while the snapshot's ``base_market_value`` is in the account base
(CZK). Rather than pull a second live FX quote, we scale each position's base
value by the *price ratio* ``live_price / snapshot_mark_price`` — this refreshes
the equity mark (the thing that's actually delayed) while holding the snapshot's
FX constant (FX moves far less intraday than the equity print you're staring at).
"""

from __future__ import annotations

import copy
import datetime as dt
import math
import re
import threading
import time
from typing import Any

import portfolio

# The live fetch is auth_status (~2s) + accounts (primes the portfolio session,
# ~2s) + paged position reads (~3s) + a trailing empty page to detect the end, so
# a healthy round trip is ~9-10s. It runs OFF the holdings view's critical path
# (the delayed snapshot paints first, this overlays after), so the budget only
# needs to be generous enough not to clip a healthy gateway while still capping a
# wedged one. Cache briefly so re-navigating the Positions tab doesn't re-hit it.
FETCH_BUDGET_SECONDS = 15.0
_CACHE_TTL_SECONDS = 15.0
_MAX_POSITION_PAGES = 8
_cache: tuple[float, list[dict[str, Any]] | None] | None = None
_cache_lock = threading.Lock()


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        out = float(value)
    except ValueError:
        return None
    return out if math.isfinite(out) else None


def _summary_amount(summary: dict[str, Any], key: str) -> float | None:
    field = summary.get(key)
    return _number(field.get("amount")) if isinstance(field, dict) else None


def _live_option_symbol(row: dict[str, Any]) -> str:
    """Canonical OCC symbol for a CPAPI option position."""
    ticker = str(row.get("ticker") or "").strip().upper()
    expiry = re.sub(r"\D", "", str(row.get("expiry") or ""))
    right = str(row.get("putOrCall") or "").strip().upper()[:1]
    strike = _number(row.get("strike"))
    if ticker and len(expiry) == 8 and right in {"C", "P"} and strike is not None:
        return f"{ticker[:6]:<6}{expiry[2:]}{right}{round(strike * 1000):08d}"
    desc = str(row.get("contractDesc") or "")
    bracketed = re.search(r"\[([A-Z0-9 ]{6}\d{6}[CP]\d{8})\s+\d+\]", desc)
    return bracketed.group(1) if bracketed else ticker


def _position_key(row: dict[str, Any], *, live: bool) -> tuple[str, str, str]:
    asset = str(row.get("assetClass") if live else row.get("asset_class") or "").upper()
    currency = str(row.get("currency") or "").upper()
    if asset == "OPT":
        symbol = _live_option_symbol(row) if live else str(row.get("symbol") or "").strip().upper()
    else:
        symbol = _norm_ticker(
            row.get("ticker") or row.get("contractDesc")
            if live else row.get("symbol")
        )
    return asset, symbol, currency


def _fx_rates(snapshot: dict[str, Any]) -> dict[str, float]:
    """Snapshot-local currency to base rates inferred from existing positions."""
    numerators: dict[str, float] = {}
    denominators: dict[str, float] = {}
    base = str(snapshot.get("base_currency") or "").upper()
    if base:
        numerators[base] = denominators[base] = 1.0
    for row in snapshot.get("positions") or []:
        currency = str(row.get("currency") or "").upper()
        local = _number(row.get("market_value"))
        base_value = _number(row.get("base_market_value"))
        if not currency or local is None or local == 0 or base_value is None:
            continue
        numerators[currency] = numerators.get(currency, 0.0) + abs(base_value)
        denominators[currency] = denominators.get(currency, 0.0) + abs(local)
    return {
        currency: numerators[currency] / denominator
        for currency, denominator in denominators.items()
        if denominator > 0
    }


def merge_live_snapshot(
    snapshot: dict[str, Any],
    live_positions: list[dict[str, Any]],
    summary: dict[str, Any],
    account: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Replace portfolio quantities and values from CPAPI, preserving safe metadata.

    Flex remains the tax-lot source because CPAPI's portfolio endpoint does not
    expose acquisition dates. Lots are retained only when their summed quantity
    still matches the live position; otherwise they are dropped rather than
    silently presenting stale tax eligibility after a fill.
    """
    existing = {
        _position_key(row, live=False): row
        for row in snapshot.get("positions") or []
        if isinstance(row, dict)
    }
    fx = _fx_rates(snapshot)
    nav = _summary_amount(summary, "netliquidation")
    base_currency = str(
        (summary.get("netliquidation") or {}).get("currency")
        or (account or {}).get("currency")
        or snapshot.get("base_currency")
        or ""
    ).upper()
    if base_currency:
        fx[base_currency] = 1.0

    positions: list[dict[str, Any]] = []
    position_by_symbol: dict[str, dict[str, Any]] = {}
    for live in live_positions:
        if not isinstance(live, dict):
            continue
        quantity = _number(live.get("position"))
        if quantity is None or abs(quantity) < 1e-12:
            continue
        asset, live_symbol, currency = _position_key(live, live=True)
        if not live_symbol:
            continue
        prior = existing.get((asset, live_symbol, currency))
        symbol = str((prior or {}).get("symbol") or live_symbol)
        mark = _number(live.get("mktPrice"))
        market_value = _number(live.get("mktValue"))
        multiplier = _number(live.get("multiplier")) or (100.0 if asset == "OPT" else 1.0)
        if market_value is None and mark is not None:
            market_value = quantity * mark * multiplier
        rate = fx.get(currency)
        base_value = market_value * rate if market_value is not None and rate is not None else None
        row = {
            "symbol": symbol,
            "description": (prior or {}).get("description")
            or live.get("fullName") or live.get("name") or live.get("contractDesc"),
            "asset_class": asset,
            "quantity": quantity,
            "mark_price": mark,
            "market_value": market_value,
            "base_market_value": base_value,
            "percent_of_nav": (
                round(base_value / nav * 100, 6)
                if base_value is not None and nav is not None and nav != 0
                else None
            ),
            "currency": currency,
            "unrealized_pnl": _number(live.get("unrealizedPnl")),
            "issuer_country_code": (prior or {}).get("issuer_country_code")
            or live.get("countryCode"),
            "listing_exchange": (prior or {}).get("listing_exchange")
            or live.get("listingExchange"),
            "sub_category": (prior or {}).get("sub_category")
            or live.get("sector"),
        }
        positions.append(row)
        position_by_symbol[symbol] = row

    positions.sort(key=lambda row: abs(_number(row.get("base_market_value")) or 0), reverse=True)
    out = copy.deepcopy(snapshot)
    out["base_currency"] = base_currency or snapshot.get("base_currency")
    out["net_asset_value"] = nav if nav is not None else snapshot.get("net_asset_value")
    out["generated_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    out["source"] = (
        "IBKR Client Portal API live snapshot; Flex tax lots retained only "
        "where quantities still match"
    )
    out["positions"] = positions

    cash = copy.deepcopy(snapshot.get("cash") or [])
    total_cash = _summary_amount(summary, "totalcashvalue")
    if total_cash is not None:
        base_row = next(
            (row for row in cash if row.get("currency") == "BASE_SUMMARY"),
            None,
        )
        if base_row is None:
            cash.insert(0, {"currency": "BASE_SUMMARY", "ending_cash": total_cash})
        else:
            base_row["ending_cash"] = total_cash
    out["cash"] = cash

    live_quantities = {
        symbol: _number(row.get("quantity"))
        for symbol, row in position_by_symbol.items()
    }
    lot_totals: dict[str, float] = {}
    for lot in snapshot.get("lots") or []:
        if isinstance(lot, dict):
            symbol = str(lot.get("symbol") or "")
            lot_totals[symbol] = lot_totals.get(symbol, 0.0) + (_number(lot.get("quantity")) or 0)
    safe_lot_symbols = {
        symbol for symbol, total in lot_totals.items()
        if live_quantities.get(symbol) is not None
        and math.isclose(total, live_quantities[symbol] or 0, abs_tol=1e-8)
    }
    out["lots"] = [
        copy.deepcopy(lot) for lot in snapshot.get("lots") or []
        if isinstance(lot, dict) and str(lot.get("symbol") or "") in safe_lot_symbols
    ]
    out["tax_lot_summary"] = [
        copy.deepcopy(row) for row in snapshot.get("tax_lot_summary") or []
        if isinstance(row, dict) and str(row.get("symbol") or "") in safe_lot_symbols
    ]
    top_fields = {
        "symbol", "description", "quantity", "percent_of_nav",
        "base_market_value", "currency", "unrealized_pnl",
    }
    out["top_positions"] = [
        {key: value for key, value in row.items() if key in top_fields}
        for row in positions
    ]
    return out


def fetch_live_portfolio(
    *,
    include_summary: bool = True,
    assume_authenticated: bool = False,
) -> dict[str, Any] | None:
    """Return an authenticated CPAPI portfolio, or None when not connected."""
    import ibkr_trade

    if not assume_authenticated and not ibkr_trade.auth_status().get("authenticated"):
        return None
    accounts = ibkr_trade.accounts()
    if not accounts:
        raise ValueError("IBKR is authenticated but returned no portfolio account")
    account = accounts[0]
    account_id = account.get("accountId") or account.get("id") or account.get("account")
    if not account_id:
        raise ValueError("IBKR portfolio account has no account id")
    rows: list[dict[str, Any]] = []
    for page in range(_MAX_POSITION_PAGES):
        page_rows = ibkr_trade.positions(str(account_id), page)
        if not page_rows:
            break
        rows.extend(page_rows)
        if len(page_rows) < 30:
            break
    return {
        "account": account,
        "positions": rows,
        "summary": (
            ibkr_trade.account_summary(str(account_id))
            if include_summary else {}
        ),
    }


def _norm_ticker(sym: str | None) -> str:
    """Join key for equities: uppercase, drop any exchange suffix. The snapshot
    carries ``005930.KS`` where the live feed says ``005930``; US names are
    identical on both sides."""
    return (sym or "").split(".")[0].strip().upper()


def _build_live_index(live_positions: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    """(normalized ticker, currency) -> live position, for STK rows with a usable
    positive mark. Currency is part of the key so a same-ticker cross-listing
    can't cross-map. First occurrence wins (accounts rarely list a name twice)."""
    idx: dict[tuple[str, str], dict[str, Any]] = {}
    for r in live_positions or []:
        if str(r.get("assetClass") or "").upper() != "STK":
            continue
        px = r.get("mktPrice")
        if not isinstance(px, (int, float)) or px <= 0:
            continue
        key = (_norm_ticker(r.get("contractDesc")), str(r.get("currency") or "").upper())
        idx.setdefault(key, r)
    return idx


def apply_live_marks(snapshot: dict[str, Any], live_positions: list[dict[str, Any]]) -> dict[str, Any]:
    """Pure overlay: refresh STK marks in a *copy* of ``snapshot`` from
    ``live_positions`` and return the holdings-view response (payload + coverage).
    No network — the fetch lives in :func:`refresh_marks` so this stays testable."""
    idx = _build_live_index(live_positions)
    snap = copy.deepcopy(snapshot)
    positions = snap.get("positions") or []
    old_invested = portfolio.invested_value(positions)

    live_n = 0
    eligible = 0
    for p in positions:
        is_stk = str(p.get("asset_class") or "").upper() == "STK"
        if is_stk:
            eligible += 1
        snap_px = p.get("mark_price")
        lv = idx.get((_norm_ticker(p.get("symbol")), str(p.get("currency") or "").upper())) if is_stk else None
        if not lv or not isinstance(snap_px, (int, float)) or snap_px <= 0:
            p["live_mark"] = False
            continue
        scale = float(lv["mktPrice"]) / float(snap_px)
        for f in ("base_market_value", "market_value"):
            if isinstance(p.get(f), (int, float)):
                p[f] = p[f] * scale
        p["mark_price"] = float(lv["mktPrice"])
        p["live_mark"] = True
        live_n += 1

    # Cash is unchanged, so NAV shifts by exactly the invested-value delta.
    new_invested = portfolio.invested_value(positions)
    nav = snap.get("net_asset_value")
    if isinstance(nav, (int, float)):
        snap["net_asset_value"] = nav + (new_invested - old_invested)

    payload = portfolio.holdings_payload(snap)
    # holdings_payload rebuilds each position with a fixed field set, dropping our
    # transient live_mark flag; re-attach it by symbol so the UI can mark the
    # rows that are still on the delayed Flex mark.
    live_by_sym = {p["symbol"]: bool(p.get("live_mark"))
                   for p in positions if isinstance(p.get("symbol"), str)}
    for pp in payload["positions"]:
        pp["live_mark"] = live_by_sym.get(pp["symbol"], False)

    return {
        "available": True,
        "as_of": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "coverage": {"live": live_n, "eligible": eligible, "total": len(positions)},
        "payload": payload,
    }


def _fetch_live_positions_uncached() -> list[dict[str, Any]] | None:
    """All CPAPI positions for the first account, or None when the gateway is
    unauthenticated / unreachable. Best-effort and quiet — any failure just means
    'no live overlay this time', never an exception to the caller."""
    try:
        portfolio_data = fetch_live_portfolio(include_summary=False)
        return portfolio_data["positions"] if portfolio_data else None
    except Exception:  # noqa: BLE001 -- gateway hiccup: fall back to delayed-only
        return None


def _fetch_live_positions(budget_s: float) -> list[dict[str, Any]] | None:
    """Time-boxed, briefly-cached fetch. Runs on a daemon thread joined with a
    wall-clock budget so a wedged gateway can't hang the request; a fresh cache
    entry short-circuits the whole thing."""
    global _cache
    now = time.monotonic()
    with _cache_lock:
        if _cache is not None and (now - _cache[0]) < _CACHE_TTL_SECONDS:
            return _cache[1]

    box: dict[str, Any] = {}

    def _run() -> None:
        box["r"] = _fetch_live_positions_uncached()

    th = threading.Thread(target=_run, name="holdings-live", daemon=True)
    th.start()
    th.join(budget_s)
    if th.is_alive():
        return None  # too slow; leave the cache untouched so a retry can try again
    result = box.get("r")
    with _cache_lock:
        _cache = (time.monotonic(), result)
    return result


def refresh_marks(snapshot: dict[str, Any] | None, *, budget_s: float = FETCH_BUDGET_SECONDS) -> dict[str, Any] | None:
    """Live-mark overlay for the holdings view, or None when unavailable.

    None means "no live data right now" (gateway down/unauthenticated, timeout,
    or an empty snapshot) — the caller should serve the delayed snapshot as-is."""
    if not snapshot or not (snapshot.get("positions")):
        return None
    live = _fetch_live_positions(budget_s)
    if live is None:
        return None
    return apply_live_marks(snapshot, live)
