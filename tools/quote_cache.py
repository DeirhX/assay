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
