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
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config

QUOTES_JSON = config.DATA_DIR / "cache" / "quotes.json"
# A quote older than this loses to the holdings mark in the gate overlay: it is
# no longer "fresh" enough to flip a locked level on its own.
FRESH_MAX_AGE_SECONDS = 4 * 3600


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def load() -> dict[str, dict]:
    """Every cached quote, keyed by upper-case symbol. Missing/corrupt → {}."""
    try:
        data = json.loads(QUOTES_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(quotes: dict[str, dict]) -> None:
    """Atomic write of the whole quote map (single writer: the scheduler)."""
    QUOTES_JSON.parent.mkdir(parents=True, exist_ok=True)
    tmp = QUOTES_JSON.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(quotes, indent=2), encoding="utf-8")
    tmp.replace(QUOTES_JSON)


def age_seconds(at: Any, now: dt.datetime | None = None) -> float | None:
    """Seconds since an ISO timestamp (Z-tolerant); None if unparsable."""
    if not at:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return max(0.0, ((now or _now()) - parsed).total_seconds())


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
