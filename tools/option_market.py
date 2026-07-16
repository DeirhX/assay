#!/usr/bin/env python3
"""Option-market acquisition and caching for advisory exit analysis.

This module owns the slow, provider-specific boundary: IBKR session probing,
time-boxed chain retrieval, Alpaca/Yahoo only when the gateway is disconnected,
disk caches, and the cached risk-free rate. Exit planning consumes these values
but does not decide how they are fetched.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import ibkr_trade
import options_overlay
import portfolio
import store
import timeutil
from config import REPO_ROOT

OPT_CACHE_DIR = REPO_ROOT / "data" / "cache" / "options"
OPT_CACHE_TTL_SECONDS = 3 * 3600
IBKR_QUOTE_CACHE_TTL_SECONDS = 60
FALLBACK_CACHE_TTL_SECONDS = 60
RATE_CACHE_PATH = OPT_CACHE_DIR / "risk-free-rate.json"
RATE_CACHE_TTL_SECONDS = 6 * 3600
IBKR_CHAIN_BUDGET_SECONDS = 8.0
SESSION_READY_TTL_SECONDS = 20.0
ROUTE_CHAIN_MAX_EXPIRIES = 2
ROUTE_CHAIN_STRIKES_PER_SIDE = 4
ROUTE_CHAIN_STRIKES_NEAREST = 8

_session_ready_cache: tuple[float, bool] | None = None
_session_ready_lock = threading.Lock()


def _ibkr_chain_strikes_coherent(chain: dict[str, Any] | None) -> bool:
    """False when cached contracts sit on the wrong side of the underlying mark.

    A spot-less IBKR sample used to keep the high end of the put catalog; quote
    refresh later filled ``underlying_price`` without rebuilding contracts, so
    ADI at ~380 kept 570–630 puts. Those caches must be discarded.
    """
    if not isinstance(chain, dict):
        return False
    spot_raw = chain.get("underlying_price")
    if not isinstance(spot_raw, (int, float)) or spot_raw <= 0:
        nested = chain.get("underlying_quote")
        if isinstance(nested, dict) and isinstance(nested.get("last"), (int, float)):
            spot_raw = nested["last"]
        else:
            spot_raw = chain.get("underlying_last")
    try:
        spot = float(spot_raw or 0)
    except (TypeError, ValueError):
        spot = 0.0
    expiries = chain.get("expiries") or []
    has_contracts = False
    for exp in expiries:
        if not isinstance(exp, dict):
            continue
        puts = [
            float(row["strike"])
            for row in (exp.get("puts") or [])
            if isinstance(row, dict) and isinstance(row.get("strike"), (int, float))
        ]
        calls = [
            float(row["strike"])
            for row in (exp.get("calls") or [])
            if isinstance(row, dict) and isinstance(row.get("strike"), (int, float))
        ]
        if puts or calls:
            has_contracts = True
        if spot <= 0:
            continue
        if puts and all(strike >= spot for strike in puts):
            return False
        if calls and all(strike <= spot for strike in calls):
            return False
    # Contracts without a spot were sampled blindly — rebuild rather than serve.
    if has_contracts and spot <= 0:
        return False
    return True


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


def chain_within_budget(
    symbol: str,
    budget: float,
    *,
    max_expiries: int = 4,
    strikes_per_side: int = 6,
    rights: tuple[str, ...] = ("C", "P"),
    target_dte: int = 37,
) -> dict[str, Any] | None:
    """Fetch a cooperative, deadline-bounded IBKR chain.

    The old daemon-thread timeout returned after five seconds but left the worker
    issuing dozens of CPAPI requests in the background. Passing the deadline into
    the request pipeline stops new work and bounds each in-flight HTTP timeout.
    """
    deadline = time.monotonic() + max(0.05, budget)
    try:
        return ibkr_trade.option_chain(
            symbol,
            max_expiries=max_expiries,
            strikes_per_side=strikes_per_side,
            rights=rights,
            target_dte=target_dte,
            deadline_monotonic=deadline,
        )
    except Exception:  # noqa: BLE001 -- caller falls through to other providers
        return None


def fetch_option_chain(
    symbol: str,
    *,
    max_expiries: int = 4,
    strikes_per_side: int = 6,
    rights: tuple[str, ...] = ("C", "P"),
    target_dte: int = 37,
) -> dict[str, Any] | None:
    """Live chain from IBKR when connected; Alpaca/Yahoo only when disconnected.

    A connected gateway that returns nothing (timeout, empty chain, quote miss)
    must not silently hand the UI an Alpaca/Yahoo book — those lack IBKR conids
    and look like “quotes” while staging is impossible.
    """
    if session_ready():
        return chain_within_budget(
            symbol,
            IBKR_CHAIN_BUDGET_SECONDS,
            max_expiries=max_expiries,
            strikes_per_side=strikes_per_side,
            rights=rights,
            target_dte=target_dte,
        )
    try:
        from providers import alpaca

        if alpaca.enabled():
            chain = alpaca.option_chain(
                portfolio.provider_symbol_for(symbol),
                max_expiries=max_expiries,
            )
            if chain and chain.get("expiries"):
                return chain
    except Exception:  # noqa: BLE001 -- provider hiccup falls through
        pass
    try:
        from providers import yahoo

        return yahoo.option_chain(
            portfolio.provider_symbol_for(symbol),
            max_expiries=max_expiries,
        )
    except Exception:  # noqa: BLE001 -- Black-Scholes fallback is downstream
        return None


def _write_option_chain_cache(
    path: Path,
    symbol: str,
    chain: dict[str, Any] | None,
) -> None:
    fetched_at = timeutil.now_iso()
    store.write_json(path, {
        "symbol": symbol.upper(),
        "fetched_at": fetched_at,
        "reference_fetched_at": (
            fetched_at if chain and chain.get("source") == "ibkr" else None
        ),
        "chain": chain,
    })


def _route_strikes_per_side(expiry_mode: str) -> int:
    mode = options_overlay.normalize_expiry_mode(expiry_mode)
    return (
        ROUTE_CHAIN_STRIKES_NEAREST
        if mode == "nearest"
        else ROUTE_CHAIN_STRIKES_PER_SIDE
    )


def _fetch_route_option_chain(
    symbol: str,
    requested_right: str,
    *,
    target_dte: int,
    expiry_mode: str = "monthly",
) -> dict[str, Any] | None:
    if requested_right:
        return fetch_option_chain(
            symbol,
            max_expiries=ROUTE_CHAIN_MAX_EXPIRIES,
            strikes_per_side=_route_strikes_per_side(expiry_mode),
            rights=(requested_right,),
            target_dte=target_dte,
        )
    return fetch_option_chain(symbol, target_dte=target_dte)


def _serve_cached_ibkr_chain(
    *,
    path: Path,
    symbol: str,
    cached: dict[str, Any],
    cached_chain: dict[str, Any],
    force_quotes: bool,
) -> dict[str, Any] | None:
    """Serve a coherent IBKR cache entry, optionally refreshing quotes."""
    reference_at = cached.get("reference_fetched_at") or cached.get("fetched_at")
    if not (
        timeutil.cache_fresh(reference_at, OPT_CACHE_TTL_SECONDS)
        and _ibkr_chain_strikes_coherent(cached_chain)
    ):
        return None
    if not force_quotes and timeutil.cache_fresh(
        cached_chain.get("quote_timestamp"),
        IBKR_QUOTE_CACHE_TTL_SECONDS,
    ):
        return cached_chain
    if not session_ready():
        return cached_chain
    try:
        refreshed = ibkr_trade.refresh_option_chain_quotes(
            cached_chain,
            deadline_monotonic=time.monotonic() + IBKR_CHAIN_BUDGET_SECONDS,
        )
    except Exception:  # noqa: BLE001 -- stale references still beat a total miss
        refreshed = cached_chain
    if not _ibkr_chain_strikes_coherent(refreshed):
        return None
    if refreshed is not cached_chain:
        store.write_json(path, {
            "symbol": symbol.upper(),
            "fetched_at": timeutil.now_iso(),
            "reference_fetched_at": reference_at,
            "chain": refreshed,
        })
    return refreshed


def cached_option_chain(
    symbol: str,
    *,
    cache_dir: Path | None = None,
    force_quotes: bool = False,
    force_refresh: bool = False,
    right: str | None = None,
    expiry_mode: str = "monthly",
) -> dict[str, Any] | None:
    """Cached provider-selected option chain for a canonical ticker.

    ``force_quotes`` bypasses only the short quote TTL.  Fresh IBKR security
    definitions are retained, so an instrument-level refresh updates every
    contract snapshot without repeating the expensive secdef discovery.

    ``force_refresh`` attempts a full provider rebuild (used when the option
    route table is opened). On failure, a coherent cache entry is returned.

    ``expiry_mode`` selects month/expiry targeting and a separate cache file so
    monthly and nearest-weekly ladders do not clobber each other.
    """
    requested_right = str(right or "").strip().upper()
    if requested_right not in {"", "C", "P"}:
        raise ValueError("right must be C or P")
    mode = options_overlay.normalize_expiry_mode(expiry_mode)
    target_dte = options_overlay.expiry_target_dte(mode)
    directory = cache_dir or OPT_CACHE_DIR
    safe = "".join(ch for ch in symbol.upper() if ch.isalnum() or ch in "-._=")
    if requested_right:
        suffix = f"-route-{requested_right.lower()}-{mode}"
    else:
        suffix = f"-{mode}" if mode != "monthly" else ""
    path = directory / f"{safe}{suffix}.json"
    cached = store.load(path)
    cached_chain = cached.get("chain") if isinstance(cached, dict) else None

    if force_refresh and session_ready():
        try:
            chain = _fetch_route_option_chain(
                symbol, requested_right, target_dte=target_dte, expiry_mode=mode,
            )
        except Exception:  # noqa: BLE001 -- fall back to coherent cache below
            chain = None
        if (
            isinstance(chain, dict)
            and (
                chain.get("source") != "ibkr"
                or _ibkr_chain_strikes_coherent(chain)
            )
        ):
            _write_option_chain_cache(path, symbol, chain)
            return chain
        if isinstance(cached, dict) and isinstance(cached_chain, dict):
            if cached_chain.get("source") == "ibkr":
                fallback = _serve_cached_ibkr_chain(
                    path=path,
                    symbol=symbol,
                    cached=cached,
                    cached_chain=cached_chain,
                    force_quotes=True,
                )
                if fallback is not None:
                    return fallback
            # Connected: never revive Alpaca/Yahoo disk leftovers.
        return chain if isinstance(chain, dict) else None

    if isinstance(cached, dict) and isinstance(cached_chain, dict):
        if cached_chain.get("source") == "ibkr":
            served = _serve_cached_ibkr_chain(
                path=path,
                symbol=symbol,
                cached=cached,
                cached_chain=cached_chain,
                force_quotes=force_quotes,
            )
            if served is not None:
                return served
        elif (
            not session_ready()
            and timeutil.cache_fresh(cached.get("fetched_at"), FALLBACK_CACHE_TTL_SECONDS)
        ):
            return cached_chain
    chain = _fetch_route_option_chain(
        symbol, requested_right, target_dte=target_dte, expiry_mode=mode,
    )
    _write_option_chain_cache(path, symbol, chain)
    return chain
