#!/usr/bin/env python3
"""Locked per-symbol price triggers.

A price level is a human-confirmed condition on the *current* instrument price
(in the instrument's own trading currency, matching the dossier ``price.value``
and the holdings ``mark_price``):

* ``buy_below``  -- only accumulate while price <= this; the BUY becomes a LIMIT here.
* ``trim_above`` -- only trim while price >= this; the SELL becomes a LIMIT here.

The weight band in the target model still decides *how much*; the level decides
*whether / at what price*. This module owns the small JSON store and the pure
gate evaluation; the HTTP server, the rebalance overlay, and the order
synthesis all read through it. Stdlib only, depends just on config + store, so
it stays a leaf with no import cycle.
"""

from __future__ import annotations

import datetime as dt

from config import DATA_DIR
from store import load as _load, safe_symbol as _safe_symbol, write_json as _write_json

# Keyed by provider symbol; gitignored only insofar as data/ is private. Small,
# rarely written, read on every rebalance and trade preview.
LEVELS_JSON = DATA_DIR / "price-levels.json"


def _coerce_price(value) -> float | None:
    """A positive float, or None for blank/absent. Raises ValueError on a value
    that is present but not a sane price (negative, zero, non-numeric)."""
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"not a number: {value!r}")
    if out <= 0 or out != out or out in (float("inf"), float("-inf")):
        raise ValueError(f"price must be a positive, finite number: {value!r}")
    return out


def load_all() -> dict[str, dict]:
    """Every locked level, keyed by provider symbol. Forgiving: missing/corrupt
    store yields an empty map (the planner and trade desk just see no gates)."""
    data = _load(LEVELS_JSON)
    return data if isinstance(data, dict) else {}


def get(symbol: str) -> dict | None:
    """The locked level for *symbol* (already a provider symbol), or None."""
    try:
        sym = _safe_symbol(symbol)
    except ValueError:
        return None
    entry = load_all().get(sym)
    return entry if isinstance(entry, dict) else None


def lock(symbol: str, *, buy_below=None, trim_above=None,
         currency: str = "", source: dict | None = None) -> dict:
    """Persist (upsert) a level for *symbol*. At least one of buy_below /
    trim_above must be set; if both are, buy_below must be below trim_above
    (you accumulate low and trim high -- the reverse is a fat-finger)."""
    sym = _safe_symbol(symbol)
    buy = _coerce_price(buy_below)
    trim = _coerce_price(trim_above)
    if buy is None and trim is None:
        raise ValueError("provide at least one of buy_below / trim_above")
    if buy is not None and trim is not None and buy >= trim:
        raise ValueError(f"buy_below ({buy}) must be below trim_above ({trim})")
    entry = {
        "symbol": sym,
        "currency": (currency or "").upper(),
        "buy_below": buy,
        "trim_above": trim,
        "locked_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "status": "active",
    }
    if isinstance(source, dict):
        entry["source"] = source
    store = load_all()
    store[sym] = entry
    _write_json(LEVELS_JSON, store)
    return entry


def clear(symbol: str) -> dict:
    """Remove a symbol's level. Idempotent: clearing an absent level is fine."""
    sym = _safe_symbol(symbol)
    store = load_all()
    existed = store.pop(sym, None) is not None
    if existed:
        _write_json(LEVELS_JSON, store)
    return {"symbol": sym, "cleared": existed}


def evaluate(level: dict | None, current_price: float | None) -> dict | None:
    """Pure gate check. Given a level and the current instrument price, report
    whether the trigger currently *blocks* a buy or a trim:

    * blocks_buy  -- a buy_below exists and price is still above it (too dear).
    * blocks_trim -- a trim_above exists and price is still below it (too cheap).

    An unknown price never blocks (the human confirms every order anyway) but is
    flagged via price_known=False so the UI can warn. Returns None when there is
    no level to evaluate."""
    if not isinstance(level, dict):
        return None
    buy = level.get("buy_below")
    trim = level.get("trim_above")
    known = isinstance(current_price, (int, float))
    blocks_buy = bool(known and isinstance(buy, (int, float)) and current_price > buy)
    blocks_trim = bool(known and isinstance(trim, (int, float)) and current_price < trim)
    return {
        "buy_below": buy,
        "trim_above": trim,
        "current": float(current_price) if known else None,
        "currency": level.get("currency") or "",
        "price_known": known,
        "blocks_buy": blocks_buy,
        "blocks_trim": blocks_trim,
    }


def limit_price_for(level: dict | None, side: str) -> float | None:
    """The limit price a given order side should use under this level: buy_below
    for a BUY, trim_above for a SELL. None when the level doesn't constrain that
    side (the caller then leaves the order at market)."""
    if not isinstance(level, dict):
        return None
    if side.upper() == "BUY":
        px = level.get("buy_below")
    elif side.upper() == "SELL":
        px = level.get("trim_above")
    else:
        px = None
    return float(px) if isinstance(px, (int, float)) else None
