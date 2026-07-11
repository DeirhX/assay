#!/usr/bin/env python3
"""Option-market acquisition and caching for advisory exit analysis.

This module owns the slow, provider-specific boundary: IBKR session probing,
time-boxed chain retrieval, Alpaca/Yahoo fallback, disk caches, and the cached
risk-free rate. Exit planning consumes these values but does not decide how they
are fetched.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import ibkr_trade
import portfolio
import store
import timeutil
from config import REPO_ROOT

OPT_CACHE_DIR = REPO_ROOT / "data" / "cache" / "options"
OPT_CACHE_TTL_SECONDS = 3 * 3600
RATE_CACHE_PATH = OPT_CACHE_DIR / "risk-free-rate.json"
RATE_CACHE_TTL_SECONDS = 6 * 3600
IBKR_CHAIN_BUDGET_SECONDS = 5.0
SESSION_READY_TTL_SECONDS = 20.0

_session_ready_cache: tuple[float, bool] | None = None
_session_ready_lock = threading.Lock()


def cached_risk_free_rate(*, cache_path: Path | None = None) -> float | None:
    """Risk-free rate as a decimal, cached for six hours.

    Only the DGS10 series is fetched; total failure returns ``None`` so the pure
    options overlay can use its neutral default.
    """
    path = cache_path or RATE_CACHE_PATH
    cached = store.load(path)
    if (
        isinstance(cached, dict)
        and timeutil.cache_fresh(cached.get("fetched_at"), RATE_CACHE_TTL_SECONDS)
    ):
        val = cached.get("rate")
        if isinstance(val, (int, float)):
            return float(val)
    try:
        import options_math
        from providers import fred

        rate = options_math.risk_free_rate(snapshot=fred.series_snapshot("DGS10"))
    except Exception:  # noqa: BLE001 -- advisory data degrades to overlay default
        rate = None
    if isinstance(rate, (int, float)):
        store.write_json(path, {
            "rate": rate,
            "fetched_at": timeutil.now_iso(),
        })
    return rate


def session_ready() -> bool:
    """Whether CPAPI has an authenticated session, memoized briefly."""
    global _session_ready_cache
    with _session_ready_lock:
        cached = _session_ready_cache
        if cached is not None and (time.monotonic() - cached[0]) < SESSION_READY_TTL_SECONDS:
            return cached[1]
    try:
        ready = bool(ibkr_trade.auth_status().get("authenticated"))
    except Exception:  # noqa: BLE001 -- any gateway failure selects a fallback
        ready = False
    with _session_ready_lock:
        _session_ready_cache = (time.monotonic(), ready)
    return ready


def reset_session_cache() -> None:
    """Clear the readiness memo, primarily for deterministic tests/reconnects."""
    global _session_ready_cache
    with _session_ready_lock:
        _session_ready_cache = None


def chain_within_budget(symbol: str, budget: float) -> dict[str, Any] | None:
    """Fetch an IBKR chain on a daemon thread, waiting at most ``budget`` seconds."""
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["chain"] = ibkr_trade.option_chain(symbol)
        except Exception:  # noqa: BLE001 -- caller falls through to other providers
            box["chain"] = None

    thread = threading.Thread(target=run, name=f"ibkr-chain-{symbol}", daemon=True)
    thread.start()
    thread.join(budget)
    return None if thread.is_alive() else box.get("chain")


def fetch_option_chain(symbol: str) -> dict[str, Any] | None:
    """Live chain from IBKR, then Alpaca, then Yahoo; ``None`` on total miss."""
    if session_ready():
        chain = chain_within_budget(symbol, IBKR_CHAIN_BUDGET_SECONDS)
        if chain and chain.get("expiries"):
            return chain
    try:
        from providers import alpaca

        if alpaca.enabled():
            chain = alpaca.option_chain(portfolio.provider_symbol_for(symbol))
            if chain and chain.get("expiries"):
                return chain
    except Exception:  # noqa: BLE001 -- provider hiccup falls through
        pass
    try:
        from providers import yahoo

        return yahoo.option_chain(portfolio.provider_symbol_for(symbol))
    except Exception:  # noqa: BLE001 -- Black-Scholes fallback is downstream
        return None


def cached_option_chain(
    symbol: str,
    *,
    cache_dir: Path | None = None,
) -> dict[str, Any] | None:
    """Cached provider-selected option chain for a canonical ticker."""
    directory = cache_dir or OPT_CACHE_DIR
    safe = "".join(ch for ch in symbol.upper() if ch.isalnum() or ch in "-._=")
    path = directory / f"{safe}.json"
    cached = store.load(path)
    if (
        isinstance(cached, dict)
        and "chain" in cached
        and timeutil.cache_fresh(cached.get("fetched_at"), OPT_CACHE_TTL_SECONDS)
    ):
        return cached.get("chain")
    chain = fetch_option_chain(symbol)
    store.write_json(path, {
        "symbol": symbol.upper(),
        "fetched_at": timeutil.now_iso(),
        "chain": chain,
    })
    return chain
