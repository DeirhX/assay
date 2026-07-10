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
        import ibkr_trade
        if not ibkr_trade.auth_status().get("authenticated"):
            return None
        accts = ibkr_trade.accounts()  # also primes the /portfolio session
        if not accts:
            return None
        acct = accts[0].get("accountId") or accts[0].get("id") or accts[0].get("account")
        if not acct:
            return None
        rows: list[dict[str, Any]] = []
        for page in range(_MAX_POSITION_PAGES):
            pg = ibkr_trade.positions(str(acct), page)
            if not pg:
                break
            rows.extend(pg)
            if len(pg) < 30:  # CPAPI pages positions in 30s; a short page is the last
                break
        return rows
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
