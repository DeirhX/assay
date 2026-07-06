#!/usr/bin/env python3
"""Locked per-symbol price triggers, as a valuation-anchored ladder.

A price level is a human-confirmed condition on the *current* instrument price
(in the instrument's own trading currency, matching the dossier ``price.value``
and the holdings ``mark_price``). Instead of a single buy/trim threshold, a
level is now a small ladder anchored on a fair-value estimate:

* ``fair_value``  -- the analyst's intrinsic-value anchor (optional).
* ``buy_ladder``  -- tranches ``{discount_pct, price, size_pct}`` sorted by price
  DESCENDING (the shallowest discount triggers first as price falls). You only
  accumulate while price <= a tranche's price; the active fraction is the sum of
  ``size_pct`` for every triggered tranche.
* ``trim_ladder`` -- tranches ``{premium_pct, price, size_pct}`` sorted by price
  ASCENDING. You only trim while price >= a tranche's price.

The weight band in the target model still sets the *target* delta; the ladder
*grades* it (active fraction) and decides *at what price*. A single-tranche
ladder at ``size_pct=1`` is exactly the old binary buy_below/trim_above, so
legacy ``{buy_below, trim_above}`` records are read transparently via
``_normalize``. Each record also mirrors ``buy_below``/``trim_above`` (the
outermost tranche of each side) for back-compat with older consumers.

This module owns the small JSON store and the pure gate evaluation; the HTTP
server, the rebalance overlay, and the order synthesis all read through it.
Stdlib only, depends just on config + store, so it stays a leaf with no import
cycle.
"""

from __future__ import annotations

import datetime as dt

from config import DATA_DIR
from store import load as _load, safe_symbol as _safe_symbol, write_json as _write_json

# Keyed by provider symbol; gitignored only insofar as data/ is private. Small,
# rarely written, read on every rebalance and trade preview.
LEVELS_JSON = DATA_DIR / "price-levels.json"


_SIZE_EPS = 1e-6


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


def _opt_price(value) -> float | None:
    """Forgiving price coercion for reads: a positive finite float, else None."""
    try:
        return _coerce_price(value)
    except ValueError:
        return None


def _opt_float(value) -> float | None:
    """Any finite float (margins can be 0), else None. Forgiving."""
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and out not in (float("inf"), float("-inf")) else None


def _coerce_size(value) -> float | None:
    """A positive finite fraction, or None for absent (caller fills a default)."""
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"size not a number: {value!r}")
    if out <= 0 or out != out or out in (float("inf"), float("-inf")):
        raise ValueError(f"size_pct must be a positive, finite fraction: {value!r}")
    return out


def _build_ladder(raw, side: str, fair_value: float | None) -> list[dict]:
    """Normalize a raw ladder (a list of tranche dicts) into sorted, fully-sized
    tranches. ``side`` is "buy" or "trim". A tranche may give an absolute
    ``price`` or a margin (``discount_pct``/``premium_pct``) resolved against
    ``fair_value``. Missing sizes are split across the gap to 1.0; sizes are then
    normalized to sum to 1. Buy tranches sort by price descending (shallowest
    first), trim ascending. Tranches with no resolvable price are dropped."""
    if not isinstance(raw, list):
        return []
    margin_key = "discount_pct" if side == "buy" else "premium_pct"
    tranches: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        margin = _opt_float(item.get(margin_key))
        price = _opt_price(item.get("price"))
        if price is None and margin is not None and fair_value:
            derived = fair_value * (1.0 - margin) if side == "buy" else fair_value * (1.0 + margin)
            price = _opt_price(derived)
        if price is None:
            continue
        if margin is None and fair_value:
            margin = (fair_value - price) / fair_value if side == "buy" else (price - fair_value) / fair_value
        tranches.append({margin_key: margin, "price": price, "size_pct": _coerce_size(item.get("size_pct"))})
    if not tranches:
        return []
    missing = [t for t in tranches if t["size_pct"] is None]
    if missing:
        known = sum(t["size_pct"] for t in tranches if t["size_pct"] is not None)
        remaining = 1.0 - known
        share = (remaining / len(missing)) if remaining > _SIZE_EPS else (1.0 / len(tranches))
        for t in tranches:
            if t["size_pct"] is None:
                t["size_pct"] = share
    total = sum(t["size_pct"] for t in tranches)
    if total > 0 and abs(total - 1.0) > _SIZE_EPS:
        # Normalize to sum 1 (kept at full precision so the fractions stay exact;
        # the UI rounds for display). Margins are display-only, so round those.
        for t in tranches:
            t["size_pct"] = t["size_pct"] / total
    for t in tranches:
        if t.get(margin_key) is not None:
            t[margin_key] = round(t[margin_key], 6)
    tranches.sort(key=lambda t: t["price"], reverse=(side == "buy"))
    return tranches


def _assemble(fair_value, buy_ladder, trim_ladder, buy_below=None, trim_above=None):
    """Resolve a fair value plus both ladders from either the new ladder payload
    or the legacy ``buy_below``/``trim_above`` single levels. Forgiving (drops
    bad tranches); cross-side and fair-value validation is the caller's job."""
    fv = _opt_price(fair_value)
    if buy_ladder is not None or trim_ladder is not None:
        buy = _build_ladder(buy_ladder or [], "buy", fv)
        trim = _build_ladder(trim_ladder or [], "trim", fv)
    else:
        buy = _build_ladder([{"price": buy_below}] if buy_below not in (None, "") else [], "buy", fv)
        trim = _build_ladder([{"price": trim_above}] if trim_above not in (None, "") else [], "trim", fv)
    return fv, buy, trim


def normalize_suggested(*, fair_value=None, buy_ladder=None, trim_ladder=None,
                        buy_below=None, trim_above=None, currency: str = "") -> dict:
    """The canonical *suggested* shape written to an analysis .meta.json and read
    by the deep-dive editor: fair value, both ladders, and the mirrored outermost
    buy_below/trim_above for back-compat. Pure; never raises."""
    fv, buy, trim = _assemble(fair_value, buy_ladder, trim_ladder, buy_below, trim_above)
    return {
        "currency": (currency or "").upper(),
        "fair_value": fv,
        "buy_ladder": buy,
        "trim_ladder": trim,
        "buy_below": buy[0]["price"] if buy else None,
        "trim_above": trim[0]["price"] if trim else None,
    }


def _normalize(level: dict | None) -> dict | None:
    """Return *level* in the canonical ladder shape, upgrading a legacy
    ``{buy_below, trim_above}`` record to single-tranche ladders. Idempotent and
    forgiving; used on every read so all consumers see one shape."""
    if not isinstance(level, dict):
        return None
    fv, buy, trim = _assemble(level.get("fair_value"), level.get("buy_ladder"),
                              level.get("trim_ladder"), level.get("buy_below"),
                              level.get("trim_above"))
    out = dict(level)
    out["fair_value"] = fv
    out["buy_ladder"] = buy
    out["trim_ladder"] = trim
    out["buy_below"] = buy[0]["price"] if buy else None
    out["trim_above"] = trim[0]["price"] if trim else None
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


def lock(symbol: str, *, fair_value=None, buy_ladder=None, trim_ladder=None,
         buy_below=None, trim_above=None, currency: str = "",
         source: dict | None = None) -> dict:
    """Persist (upsert) a level for *symbol*. Accepts either the new ladder
    payload (``fair_value`` + ``buy_ladder``/``trim_ladder``) or the legacy
    single ``buy_below``/``trim_above``. At least one tranche must survive
    normalization; every buy price must sit below every trim price (you
    accumulate low and trim high -- the reverse is a fat-finger); and when a
    fair value is given, buys must be at/below it and trims at/above it."""
    sym = _safe_symbol(symbol)
    fv, buy, trim = _assemble(fair_value, buy_ladder, trim_ladder, buy_below, trim_above)
    if not buy and not trim:
        raise ValueError("provide at least one buy or trim tranche")
    if buy and trim and max(t["price"] for t in buy) >= min(t["price"] for t in trim):
        raise ValueError("every buy price must be below every trim price")
    if fv is not None:
        if buy and max(t["price"] for t in buy) > fv:
            raise ValueError(f"buy prices must be at or below fair value ({fv})")
        if trim and min(t["price"] for t in trim) < fv:
            raise ValueError(f"trim prices must be at or above fair value ({fv})")
    entry = {
        "symbol": sym,
        "currency": (currency or "").upper(),
        "fair_value": fv,
        "buy_ladder": buy,
        "trim_ladder": trim,
        "buy_below": buy[0]["price"] if buy else None,
        "trim_above": trim[0]["price"] if trim else None,
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


def _side_state(ladder: list[dict], current: float | None, is_buy: bool) -> dict:
    """Graded state for one side of the ladder at the current price: how many
    tranches are live, the cumulative active size fraction, and the next tranche
    that would trigger (with distance to the current price)."""
    live = 0
    fraction = 0.0
    next_trigger = None
    known = current is not None
    for t in ladder:
        triggered = known and (current <= t["price"] if is_buy else current >= t["price"])
        if triggered:
            live += 1
            fraction += t["size_pct"]
        elif known:
            # Not yet triggered: the nearest such tranche is the "next" one.
            d = (current - t["price"]) if is_buy else (t["price"] - current)
            if d > 0 and (next_trigger is None or d < next_trigger["distance"]):
                next_trigger = {
                    "price": t["price"], "distance": round(d, 6),
                    "distance_pct": round(d / current, 6) if current else None,
                }
    return {
        "live": live, "total": len(ladder),
        "fraction": round(min(1.0, fraction), 6),
        "next": next_trigger,
    }


def evaluate(level: dict | None, current_price: float | None) -> dict | None:
    """Pure gate check. Given a level and the current instrument price, report,
    per side, how much of the move the ladder currently unlocks:

    * blocks_buy  -- a buy ladder exists and no tranche is triggered yet (too dear).
    * blocks_trim -- a trim ladder exists and no tranche is triggered (too cheap).
    * buy_fraction / trim_fraction -- cumulative active size (0..1); the rebalance
      overlay scales the suggested delta by this so the gate is graded, not binary.
    * buy_live/buy_total (+ trim) and next_buy/next_trim -- for the UI.

    An unknown price never blocks (the human confirms every order anyway) but is
    flagged via price_known=False. Returns None when there is no level."""
    norm = _normalize(level)
    if norm is None:
        return None
    known = isinstance(current_price, (int, float))
    cur = float(current_price) if isinstance(current_price, (int, float)) else None
    buy = _side_state(norm["buy_ladder"], cur, True)
    trim = _side_state(norm["trim_ladder"], cur, False)
    return {
        "buy_below": norm["buy_below"],
        "trim_above": norm["trim_above"],
        "current": cur,
        "currency": norm.get("currency") or "",
        "fair_value": norm.get("fair_value"),
        "price_known": known,
        "blocks_buy": bool(known and buy["total"] and buy["live"] == 0),
        "blocks_trim": bool(known and trim["total"] and trim["live"] == 0),
        "buy_fraction": buy["fraction"],
        "trim_fraction": trim["fraction"],
        "buy_live": buy["live"], "buy_total": buy["total"],
        "trim_live": trim["live"], "trim_total": trim["total"],
        "next_buy": buy["next"],
        "next_trim": trim["next"],
        "buy_ladder": norm["buy_ladder"],
        "trim_ladder": norm["trim_ladder"],
    }


def limit_price_for(level: dict | None, side: str) -> float | None:
    """The limit price a given order side should use under this level: the
    outermost (shallowest) tranche price -- the highest acceptable buy, or the
    lowest acceptable trim. With one order per symbol this fills the currently
    triggered size at/through the market; the size grading lives in the gate's
    fraction. None when the level doesn't constrain that side."""
    norm = _normalize(level)
    if norm is None:
        return None
    if side.upper() == "BUY":
        ladder = norm["buy_ladder"]
    elif side.upper() == "SELL":
        ladder = norm["trim_ladder"]
    else:
        return None
    return float(ladder[0]["price"]) if ladder else None
