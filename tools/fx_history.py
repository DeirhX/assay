#!/usr/bin/env python3
"""Daily FX panel -- the currency spine for FX-clean attribution.

A CZK-base investor holding mostly USD/EUR assets sees a chunk of every month's
NAV move that is pure USDCZK/EURCZK noise, not stock picking. Splitting the two
apart (local-price effect vs FX effect), and converting a USD benchmark curve
into CZK correctly, needs a *time series* of daily rates -- not the single
point-in-time ``fx_rate_to_base`` the IBKR trade/holdings records already carry.
This module is that series.

Shape (``data/cache/fx-history.json``, gitignored like every cache)::

    {
      "base": "CZK",
      "fetched_at": "2026-07-07T00:00:00+00:00",
      "series": {
        "USDCZK": {"2026-07-01": 23.11, "2026-07-02": 23.04, ...},
        "EURCZK": {"2026-07-01": 25.02, ...}
      }
    }

A pair ``FOOCZK`` maps to the amount of base (CZK) one unit of FOO buys, so a
foreign amount times the rate is the base amount -- the same convention as
``fx_rate_to_base``. Keys are ISO dates, so lexical order *is* chronological and
a dict de-dupes overlapping re-fetches for free.

Incremental, like ``ibkr_history.extend_history``: each top-up requests only a
window covering the gap since the last cached day (plus overlap), and merges by
date. Fetching is behind an injectable ``fetch`` seam (default: Yahoo FX pairs),
so the engine is unit-tested offline; a failed live pull keeps the stale panel
rather than wiping it (``risk.load_price_series``' "stale beats nothing").

Read-only with respect to the market; pure data. Nothing here trades.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
import store  # noqa: E402

FX_HISTORY_JSON = config.DATA_DIR / "cache" / "fx-history.json"

DEFAULT_BASE = "CZK"
# Foreign->base pairs to track. The book is CZK-base and USD/EUR-heavy; extend
# here (and the panel tops up the new pair from scratch on the next run).
DEFAULT_PAIRS: tuple[str, ...] = ("USDCZK", "EURCZK")

# Empty-cache seed: enough history to attribute a multi-year book against.
SEED_RANGE = "5y"

FetchFn = Callable[[str, str], list[dict[str, Any]] | None]


# --------------------------------------------------------------------------- #
# Yahoo fetch seam
# --------------------------------------------------------------------------- #
def _yahoo_symbol(pair: str) -> str:
    """``USDCZK`` -> Yahoo's ``USDCZK=X`` spot-FX ticker."""
    return f"{pair.upper()}=X"


def _yahoo_fetch(pair: str, rng: str) -> list[dict[str, Any]] | None:
    """Daily closes for one FX pair as ``[{date, close}, ...]``, or None on a
    provider miss. Lazy import keeps the network dependency off pure callers
    (attribution math consumes the cached panel, never this)."""
    from providers import yahoo  # noqa: E402 - lazy, mirrors risk._yahoo_fetch

    result = yahoo.chart(_yahoo_symbol(pair), rng=rng, interval="1d")
    ph = yahoo.price_history_from_chart(result, rng=rng, interval="1d")
    return ph.get("points") if ph else None


# --------------------------------------------------------------------------- #
# Incremental top-up
# --------------------------------------------------------------------------- #
def _parse_date(value: Any) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _range_for(last: dt.date | None, today: dt.date) -> str:
    """Smallest Yahoo range that covers the gap since *last*, deliberately
    over-reaching so the re-fetched window overlaps the cached tail (the merge
    de-dupes it). None (empty cache) seeds the full history."""
    if last is None:
        return SEED_RANGE
    gap = (today - last).days
    if gap <= 20:
        return "1mo"
    if gap <= 80:
        return "3mo"
    if gap <= 170:
        return "6mo"
    if gap <= 350:
        return "1y"
    if gap <= 700:
        return "2y"
    return SEED_RANGE


def _empty_panel(base: str) -> dict[str, Any]:
    return {"base": base, "fetched_at": None, "series": {}}


def load_panel(path: Path = FX_HISTORY_JSON) -> dict[str, Any]:
    """The cached panel, or an empty one when absent/corrupt. Always well-shaped
    (``base``/``series`` present) so callers never guard for missing keys."""
    raw = store.load(path)
    if not isinstance(raw, dict):
        return _empty_panel(DEFAULT_BASE)
    series = raw.get("series")
    return {
        "base": str(raw.get("base") or DEFAULT_BASE),
        "fetched_at": raw.get("fetched_at"),
        "series": series if isinstance(series, dict) else {},
    }


def pair_series(panel: dict[str, Any], pair: str) -> dict[str, float]:
    """The ``{date: rate}`` map for one pair (empty if untracked)."""
    series = panel.get("series") if isinstance(panel, dict) else None
    got = series.get(pair.upper()) if isinstance(series, dict) else None
    return got if isinstance(got, dict) else {}


def rate_on(panel: dict[str, Any], pair: str, on: str | dt.date) -> float | None:
    """As-of rate: the most recent quote on or before *on* (FX doesn't quote on
    weekends/holidays, so an exact-day match can't be required). None when the
    pair is untracked or *on* precedes the first quote."""
    series = pair_series(panel, pair)
    if not series:
        return None
    target = on.isoformat() if isinstance(on, dt.date) else str(on)[:10]
    best_key: str | None = None
    for key in series:  # ISO keys sort chronologically
        if key <= target and (best_key is None or key > best_key):
            best_key = key
    if best_key is None:
        return None
    try:
        return float(series[best_key])
    except (TypeError, ValueError):
        return None


def update_panel(
    pairs: tuple[str, ...] | list[str] = DEFAULT_PAIRS,
    *,
    base: str = DEFAULT_BASE,
    today: dt.date | None = None,
    fetch: FetchFn | None = None,
    path: Path = FX_HISTORY_JSON,
) -> dict[str, Any]:
    """Top up each pair to *today* and persist. Incremental: only the gap since
    each pair's last cached day is requested. A pair whose fetch fails or returns
    nothing keeps its existing series (stale beats nothing); other pairs still
    update. Returns the merged panel."""
    today = today or dt.datetime.now(dt.timezone.utc).date()
    fetch = fetch or _yahoo_fetch

    panel = load_panel(path)
    panel["base"] = base
    series: dict[str, Any] = panel["series"]

    for pair in pairs:
        key = pair.upper()
        existing = series.get(key)
        existing = existing if isinstance(existing, dict) else {}
        last = _latest_date(existing)
        rng = _range_for(last, today)
        try:
            points = fetch(key, rng)
        except Exception:  # noqa: BLE001 - one bad pair must not sink the panel
            points = None
        merged = dict(existing)
        for pt in points or []:
            d = _parse_date(pt.get("date"))
            close = pt.get("close")
            if d is None or not isinstance(close, (int, float)):
                continue
            merged[d.isoformat()] = round(float(close), 6)
        series[key] = merged

    panel["fetched_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    store.write_json(path, panel)
    return panel


def _latest_date(series: dict[str, Any]) -> dt.date | None:
    latest: dt.date | None = None
    for key in series:
        d = _parse_date(key)
        if d is not None and (latest is None or d > latest):
            latest = d
    return latest


def main() -> int:
    """CLI: top up the panel from Yahoo and print a one-line coverage summary."""
    panel = update_panel()
    for pair in DEFAULT_PAIRS:
        s = pair_series(panel, pair)
        if s:
            lo, hi = min(s), max(s)
            print(f"{pair}: {len(s)} days [{lo} .. {hi}] latest={s[hi]:g}")
        else:
            print(f"{pair}: no data")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
