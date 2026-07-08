#!/usr/bin/env python3
"""Symbol resolution, aliasing, and lookup.

Extracted from serve.py. The UI deals in input symbols (what you typed or what
IBKR reported); the providers deal in their own canonical tickers. This module
bridges the two: a filtered alias map, input->provider resolution, record
annotation with a resolved instrument type, alias persistence, and best-effort
ticker candidate validation / company-name search via Yahoo. Pure data + provider
calls -- no HTTP or job machinery.

Public names are intentionally underscore-free; serve.py imports them aliased to
its existing private call-site names.
"""

from __future__ import annotations

import re

from config import SYMBOL_ALIASES_JSON
import instruments
from portfolio import provider_symbol_for, symbol_aliases
from providers import yahoo
from store import safe_symbol, write_json


def aliases() -> dict[str, str]:
    return {
        src: dst
        for src, dst in symbol_aliases().items()
        if len(src) <= 16 and len(dst) <= 16
        and re.match(r"^[A-Z0-9.=\- ]+$", src)
        and re.match(r"^[A-Z0-9.=\- ]+$", dst)
    }


def resolve_symbol(symbol: str) -> str:
    sym = safe_symbol(symbol)
    return provider_symbol_for(sym, aliases())


def annotate_record(rec: dict, input_symbol: str, provider_symbol: str) -> dict:
    if input_symbol != provider_symbol:
        rec = dict(rec)
        rec["input_symbol"] = input_symbol
        rec["provider_symbol"] = provider_symbol
    # Always hand the UI a resolved instrument type, even for older cached
    # records that predate quote_type capture (classify() falls back to
    # symbol/profile heuristics in that case).
    rec["instrument_type"] = instruments.classify(
        provider_symbol,
        quote_type=rec.get("quote_type") or rec.get("instrument_type"),
        profile=rec.get("profile"),
    )
    return rec


def save_alias(body: dict) -> dict:
    src = safe_symbol(str(body.get("input_symbol") or body.get("input") or ""))
    dst = safe_symbol(str(body.get("provider_symbol") or body.get("provider") or ""))
    current = aliases()
    if src == dst:
        current.pop(src, None)
    else:
        current[src] = dst
    write_json(SYMBOL_ALIASES_JSON, current)
    return {"aliases": current, "input_symbol": src, "provider_symbol": current.get(src, src)}


def candidates(body: dict) -> dict:
    src = safe_symbol(str(body.get("input_symbol") or body.get("symbol") or ""))
    raw_candidates = body.get("candidates") or []
    if not isinstance(raw_candidates, list):
        raise ValueError("candidates must be a list")

    seen: set[str] = set()
    valid: list[dict[str, str]] = []
    invalid: list[dict[str, str]] = []
    for raw in raw_candidates[:16]:
        try:
            candidate = safe_symbol(str(raw))
        except ValueError as exc:
            invalid.append({"symbol": str(raw), "error": str(exc)})
            continue
        if candidate in seen or candidate == src:
            continue
        seen.add(candidate)
        try:
            result = yahoo.chart(candidate, rng="5d", interval="1d")
            meta = result.get("meta") or {}
            valid.append({
                "symbol": candidate,
                "exchange": str(meta.get("exchangeName") or meta.get("fullExchangeName") or ""),
                "currency": str(meta.get("currency") or ""),
            })
        except Exception as exc:  # noqa: BLE001 - candidate failed validation
            invalid.append({"symbol": candidate, "error": str(exc)})
    return {"input_symbol": src, "candidates": valid, "invalid": invalid}


def search(query: str, *, limit: int = 8) -> dict:
    """Substring / company-name ticker search via Yahoo. Best-effort: a provider
    hiccup returns an empty list rather than an error so the UI degrades cleanly."""
    q = (query or "").strip()
    if not q:
        return {"query": "", "results": []}
    try:
        results = yahoo.search(q, limit=limit)
    except Exception as exc:  # noqa: BLE001 - search is a nicety, never fatal
        return {"query": q, "results": [], "error": str(exc)}
    return {"query": q, "results": results}
