#!/usr/bin/env python3
"""Single source of truth for what *kind* of thing a ticker is.

Yahoo's ``quoteType`` is authoritative when we captured it at pull time. For
records pulled before we started storing it (or anything Yahoo never tagged) we
fall back to symbol-shape and profile heuristics so the UI still gets a sensible
label instead of a shrug. Re-pulling a ticker upgrades it to the real type.

Canonical labels are intentionally small and stable -- the frontend keys badge
styling off these exact strings, so don't rename them on a whim.
"""

from __future__ import annotations

STOCK = "stock"
ETF = "etf"
FUTURES = "futures"
INDEX = "index"
FUND = "fund"
CRYPTO = "crypto"
FX = "fx"
OTHER = "other"

CANONICAL = (STOCK, ETF, FUTURES, INDEX, FUND, CRYPTO, FX, OTHER)

LABELS = {
    STOCK: "Stock",
    ETF: "ETF",
    FUTURES: "Futures",
    INDEX: "Index",
    FUND: "Fund",
    CRYPTO: "Crypto",
    FX: "FX",
    OTHER: "Other",
}

# Yahoo quoteType -> our canonical label.
_YAHOO_MAP = {
    "EQUITY": STOCK,
    "ETF": ETF,
    "FUTURE": FUTURES,
    "FUTURES": FUTURES,
    "INDEX": INDEX,
    "MUTUALFUND": FUND,
    "CRYPTOCURRENCY": CRYPTO,
    "CURRENCY": FX,
}


def _from_symbol(symbol: str) -> str | None:
    """Yahoo's symbol conventions leak the instrument kind: ``^`` prefixes an
    index, ``=F`` a future, ``=X`` an FX pair, ``-USD`` a crypto cross."""
    s = (symbol or "").upper().strip()
    if not s:
        return None
    if s.startswith("^"):
        return INDEX
    if s.endswith("=F"):
        return FUTURES
    if s.endswith("=X"):
        return FX
    if s.endswith("-USD") or s.endswith("-USDT") or s.endswith("-USDC"):
        return CRYPTO
    return None


def classify(symbol: str = "", quote_type=None, profile=None) -> str:
    """Best-effort instrument type. Precedence: an explicit Yahoo ``quoteType``
    (or already-canonical value) -> symbol shape -> profile shape -> ``other``."""
    raw = str(quote_type or "").strip()
    if raw:
        up = raw.upper()
        if up in _YAHOO_MAP:
            return _YAHOO_MAP[up]
        low = raw.lower()
        if low in LABELS:  # already one of ours; pass through
            return low

    by_sym = _from_symbol(symbol)
    if by_sym:
        return by_sym

    if isinstance(profile, dict):
        # An operating company carries a sector/industry tag; a fund wrapper
        # typically has only a prose summary. Imperfect, but only matters for
        # stale records that predate quoteType capture.
        if profile.get("sector") or profile.get("industry"):
            return STOCK
        if profile.get("summary"):
            return ETF

    return OTHER


def label(kind: str) -> str:
    return LABELS.get((kind or "").lower(), LABELS[OTHER])
