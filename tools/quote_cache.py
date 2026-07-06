#!/usr/bin/env python3
"""Fresh-quote cache for gated names.

A tiny leaf so the rebalance overlay can read a recent close *without* importing
the scheduler that writes it. Shape of ``data/cache/quotes.json``::

    {"SYM": {"price": float, "currency": str|None, "at": "ISO-8601 UTC"}}

Read-only market data -- regenerable and gitignored (under ``data/cache/``).
Stdlib only.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
import store
import timeutil

QUOTES_JSON = config.DATA_DIR / "cache" / "quotes.json"
# A quote older than this loses to the holdings mark in the gate overlay: it is
# no longer "fresh" enough to flip a locked level on its own.
FRESH_MAX_AGE_SECONDS = 4 * 3600

# Sparkline series: how much recent shape to show and how coarse to draw it.
SPARK_TAIL_DAYS = 90
SPARK_MAX_POINTS = 30


def load() -> dict[str, dict]:
    """Every cached quote, keyed by upper-case symbol. Missing/corrupt → {}."""
    data = store.load(QUOTES_JSON, {})
    return data if isinstance(data, dict) else {}


def save(quotes: dict[str, dict]) -> None:
    """Atomic write of the whole quote map (single writer: the scheduler)."""
    store.write_json(QUOTES_JSON, quotes)


def age_seconds(at: Any, now: dt.datetime | None = None) -> float | None:
    """Seconds since an ISO timestamp (Z-tolerant); None if unparsable."""
    return timeutil.age_seconds(at, now)


def fresh_price(
    symbol: str,
    *,
    quotes: dict[str, dict] | None = None,
    now: dt.datetime | None = None,
    max_age: float = FRESH_MAX_AGE_SECONDS,
) -> dict | None:
    """``{price, currency, at, age_s}`` for ``symbol`` when a cached quote exists
    and is younger than ``max_age``; else ``None``. Pure when ``quotes`` is
    injected (no disk read), which is what the overlay tests rely on."""
    table = quotes if quotes is not None else load()
    q = table.get((symbol or "").strip().upper())
    if not isinstance(q, dict):
        return None
    price = q.get("price")
    if not isinstance(price, (int, float)) or not price:
        return None
    age = age_seconds(q.get("at"), now)
    if age is None or age > max_age:
        return None
    return {"price": float(price), "currency": q.get("currency"), "at": q.get("at"), "age_s": age}


def _downsample(vals: list[float], k: int) -> list[float]:
    """At most ``k`` values spanning the full range, endpoints always kept.
    Evenly-strided index pick; duplicate indices (when k ~ n) collapse."""
    n = len(vals)
    if n <= k:
        return [round(float(v), 4) for v in vals]
    out: list[float] = []
    seen: set[int] = set()
    for i in range(k):
        j = round(i * (n - 1) / (k - 1))
        if j not in seen:
            seen.add(j)
            out.append(round(float(vals[j]), 4))
    return out


def _dossier_closes(symbol: str, research_dir: Path) -> tuple[list[float], Any]:
    """Ordered (oldest->newest) daily closes from a cached dossier's
    ``price_history.points``; ``([], None)`` when the dossier or field is
    missing. Never fetches -- reads one JSON file."""
    rec = store.load(research_dir / f"{symbol}.json", None)
    if not isinstance(rec, dict):
        return [], None
    ph = rec.get("price_history")
    if not isinstance(ph, dict):
        return [], None
    pts = ph.get("points")
    if not isinstance(pts, list):
        return [], None
    closes = [
        float(p["close"]) for p in pts
        if isinstance(p, dict) and isinstance(p.get("close"), (int, float))
    ]
    return closes, ph.get("currency")


def spark_series(
    symbols: Any,
    *,
    research_dir: Path | None = None,
    tail_days: int = SPARK_TAIL_DAYS,
    max_points: int = SPARK_MAX_POINTS,
) -> dict[str, dict]:
    """Per-symbol sparkline payloads from cached data only (never fetches).

    ``{SYM: {"points": [float], "change": float|None, "currency": str|None}}``
    where ``points`` is the last ``tail_days`` closes downsampled to
    ``<=max_points`` and ``change`` is the fractional move across that window.
    Symbols passed in are treated as provider (dossier) symbols; a symbol with
    fewer than two cached closes is omitted (a single point can't draw a line).
    """
    rd = research_dir or config.RESEARCH_DIR
    out: dict[str, dict] = {}
    for raw in symbols or []:
        sym = (raw or "").strip().upper()
        if not sym or sym in out:
            continue
        closes, currency = _dossier_closes(sym, rd)
        if len(closes) < 2:
            continue
        window = closes[-tail_days:]
        first, last = window[0], window[-1]
        change = (last - first) / first if first else None
        out[sym] = {
            "points": _downsample(window, max_points),
            "change": round(change, 4) if change is not None else None,
            "currency": currency,
        }
    return out
