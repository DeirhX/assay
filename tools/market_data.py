#!/usr/bin/env python3
"""Shared market-data seam: the one place a daily price series is fetched from
the provider, and the one thread-pool fan-out that pulls many at once.

Three modules used to each carry a near-identical ``_yahoo_fetch``
(``yahoo.chart`` -> ``price_history_from_chart`` -> ``points``): ``risk`` (per-
symbol series), ``attribution`` (via ``risk``), and ``fx_history`` (FX pairs).
Two of them (``risk``, ``attribution``) also each carried the same
``ThreadPoolExecutor`` fan-out, differing only in whether a per-symbol miss was
dropped or recorded as ``None``; the lookback-range and worker-count constants
were copied alongside. This centralizes all of it so the fetch pipeline and its
tuning live in exactly one spot.

Stdlib only. ``providers`` and ``concurrent.futures`` are imported lazily so
pure-math callers -- and importers that never actually fetch -- pay nothing.
"""

from __future__ import annotations

from typing import Any, Callable

# Display range -> trailing lookback in days: the shared window vocabulary that
# risk, attribution, and fx_history all size against.
RANGE_DAYS: dict[str, int] = {"3mo": 90, "6mo": 180, "1y": 365, "2y": 730, "5y": 1825}

# Cold cache: fan per-symbol Yahoo pulls out across this many threads instead of
# going serial (~30 names is 10-30s serially). Warm, every call is a disk hit and
# the pool is trivial.
FETCH_WORKERS = 8

# A price fetch takes (provider_symbol, range) and returns [{date, close}] | None.
Fetch = Callable[[str, str], "list[dict[str, Any]] | None"]


def range_for_days(days: int) -> str:
    """Smallest display range whose lookback covers ``days`` (caps at 5y)."""
    for key in ("3mo", "6mo", "1y", "2y", "5y"):
        if days <= RANGE_DAYS[key]:
            return key
    return "5y"


def daily_closes(symbol: str, rng: str) -> "list[dict[str, Any]] | None":
    """Daily-close points for ``symbol`` as ``[{date, close}, ...]``, or ``None``
    on a provider miss. The single Yahoo seam; the lazy import keeps the network
    dependency off pure callers."""
    from providers import yahoo  # lazy: keeps import cost off pure-math callers
    result = yahoo.chart(symbol, rng=rng, interval="1d")
    ph = yahoo.price_history_from_chart(result, rng=rng, interval="1d")
    return ph.get("points") if ph else None


def _safe_fetch(fetch: Fetch, symbol: str, rng: str) -> "list[dict[str, Any]] | None":
    if not symbol:
        return None
    try:
        return fetch(symbol, rng)
    except Exception:  # noqa: BLE001 -- a provider miss is a caveat, not an error
        return None


def fetch_series_many(
    symbols: list[str],
    *,
    rng: str,
    fetch: Fetch,
    workers: int = FETCH_WORKERS,
    keep_misses: bool = False,
) -> "dict[str, list[dict[str, Any]] | None]":
    """Fetch a set of provider symbols in one parallel batch, de-duplicated.

    Cold cache: turns N serial round-trips into a handful of concurrent ones;
    warm, each call is just a disk read. A per-symbol miss (``None``/empty, or a
    ``fetch`` that raises) is swallowed -- recorded as ``None`` when
    ``keep_misses`` (attribution's counterfactuals want the gap noted), or dropped
    otherwise (risk only wants usable series). Each symbol writes its own cache,
    so there is no shared mutable state and order does not matter.
    """
    uniq = list(dict.fromkeys(s for s in symbols if s))
    if not uniq:
        return {}
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(workers, len(uniq))) as pool:
        series = list(pool.map(lambda s: _safe_fetch(fetch, s, rng), uniq))
    out: dict[str, list[dict[str, Any]] | None] = {}
    for sym, pts in zip(uniq, series):
        if keep_misses or pts:
            out[sym] = pts
    return out
