#!/usr/bin/env python3
"""Registry of symbols this account can't BUY directly for want of a PRIIPs KID.

EU retail clients can't be *sold* a packaged retail product (most US-domiciled
ETFs) without an approved Key Information Document, so a direct buy is rejected by
IBKR every time. The exposure is still reachable via options -- sell a put or buy
a call and take assignment/exercise into the shares -- so the trade desk converts
these buys to "options-only" rather than emitting a guaranteed-reject order.

The restriction is one-directional: it blocks the broker selling the product to
the client (a buy), never liquidating one you already hold (a sell/close), so
only BUY orders are ever suppressed.

Self-populating: a whatif KID rejection marks the symbol (see
trade_service._trade_preview). The backing file (gitignored, under data/cache) is
also hand-editable -- add a ticker to pre-empt the first rejection, or remove one
if your entitlements change.
"""

from __future__ import annotations

import datetime as dt
import threading
from typing import Any

from config import KID_BLOCK_JSON
from store import load as _load, write_json as _write_json

_lock = threading.Lock()


def _read() -> dict[str, Any]:
    data = _load(KID_BLOCK_JSON)
    return data if isinstance(data, dict) else {}


def blocked_symbols() -> set[str]:
    """The current set of direct-buy-blocked tickers (upper-cased)."""
    return {str(s).upper() for s in (_read().get("symbols") or {})}


def is_blocked(symbol: str | None) -> bool:
    return bool(symbol) and str(symbol).upper() in blocked_symbols()


def mark_blocked(symbol: str, reason: str = "") -> bool:
    """Record ``symbol`` as direct-buy-blocked. Returns True if newly added
    (idempotent: a symbol already present is left untouched)."""
    sym = str(symbol or "").strip().upper()
    if not sym:
        return False
    with _lock:
        data = _read()
        syms = data.setdefault("symbols", {})
        if sym in syms:
            return False
        syms[sym] = {"reason": reason,
                     "learned_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
        _write_json(KID_BLOCK_JSON, data)
    return True
