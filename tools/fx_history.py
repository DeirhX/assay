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


# --------------------------------------------------------------------------- #
# Currency lens: turn the panel + a holdings snapshot into "how much of the
# window's CZK move was FX, not stock-picking?" Consumed by risk.risk_report.
# --------------------------------------------------------------------------- #
# Display-range -> lookback days, so the FX window lines up with the risk view's
# range selector (serve.PRICE_HISTORY_RANGES).
RANGE_DAYS = {"3mo": 90, "6mo": 180, "1y": 365, "2y": 730, "5y": 1825}


def exposure_by_currency(holdings: dict[str, Any], base: str = DEFAULT_BASE) -> list[dict[str, Any]]:
    """Non-base currency exposure from the current snapshot: each foreign
    currency's share of invested (base-valued) equity. Exact -- no panel needed."""
    positions = holdings.get("positions") or []
    by_ccy: dict[str, float] = {}
    invested = 0.0
    for p in positions:
        if not isinstance(p, dict):
            continue
        bmv = p.get("base_market_value")
        if not isinstance(bmv, (int, float)):
            continue
        invested += bmv
        ccy = str(p.get("currency") or "").upper()
        if ccy and ccy != base.upper():
            by_ccy[ccy] = by_ccy.get(ccy, 0.0) + bmv
    rows: list[dict[str, Any]] = [
        {
            "currency": ccy,
            "base_value": round(val, 2),
            "weight_pct": round(val / invested * 100.0, 2) if invested else 0.0,
        }
        for ccy, val in by_ccy.items()
    ]
    rows.sort(key=lambda r: -float(r["base_value"]))
    return rows


def _asof_key(series: dict[str, float], target_iso: str) -> str | None:
    """The latest ISO key on or before ``target_iso`` (weekends/holidays don't
    quote), or None if every quote is newer."""
    best: str | None = None
    for key in series:  # ISO keys sort chronologically
        if key <= target_iso and (best is None or key > best):
            best = key
    return best


def window_move(panel: dict[str, Any], pair: str, *, days: int, today: dt.date | None = None) -> dict[str, Any] | None:
    """Fractional FX move for ``pair`` over the trailing window: ``end/start-1``,
    both ends taken as-of (tolerating non-trading days). Positive means the
    foreign currency strengthened vs base -- a tailwind for that sleeve's CZK
    value. None when the panel can't cover two distinct points in the window."""
    series = pair_series(panel, pair)
    if not series:
        return None
    today = today or dt.datetime.now(dt.timezone.utc).date()
    end_key = _asof_key(series, today.isoformat()) or max(series)
    start_key = _asof_key(series, (today - dt.timedelta(days=days)).isoformat())
    if start_key is None or start_key == end_key:
        return None
    try:
        start_rate, end_rate = float(series[start_key]), float(series[end_key])
    except (TypeError, ValueError):
        return None
    if not start_rate or not end_rate:
        return None
    return {"return": end_rate / start_rate - 1.0, "from": start_key, "to": end_key}


def window_report(
    holdings: dict[str, Any],
    *,
    rng: str = "1y",
    base: str | None = None,
    today: dt.date | None = None,
    panel: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The `fx` block for GET /api/risk: current-snapshot exposure by currency,
    plus each currency's FX move over the window and an (approximate) contribution
    to CZK NAV.

    Read-only over the cached panel (``holdings_sync`` keeps it warm on every
    history top-up), so it adds no network to the risk path and degrades to
    exposure-only for any currency the panel doesn't yet cover. ``panel`` is
    injectable for tests."""
    base = (base or holdings.get("base_currency") or DEFAULT_BASE).upper()
    exposure = exposure_by_currency(holdings, base)
    foreign_pct = round(sum(float(e["weight_pct"]) for e in exposure), 2)
    caveats: list[str] = []
    window: list[dict[str, Any]] = []

    if panel is None:
        panel = load_panel()
    days = RANGE_DAYS.get(rng, 365)
    by_weight = {str(e["currency"]): float(e["weight_pct"]) for e in exposure}
    for e in exposure:
        ccy = str(e["currency"])
        pair = f"{ccy}{base}"
        mv = window_move(panel, pair, days=days, today=today)
        if mv is None:
            caveats.append(f"No FX history for {pair}=X; its FX effect is omitted.")
            continue
        w = by_weight.get(ccy, 0.0)
        window.append({
            "currency": ccy,
            "fx_return_pct": round(mv["return"] * 100.0, 2),
            "contribution_pct": round(w / 100.0 * mv["return"] * 100.0, 2),
            "from": mv["from"],
            "to": mv["to"],
        })
    if window:
        caveats.append(
            "FX contribution assumes the current book was held across the whole "
            "window (it ignores intra-window trades); it is a context estimate, "
            "not a realized-return statement.")

    return {
        "base": base,
        "exposure": exposure,
        "foreign_pct": foreign_pct,
        "window": window,
        "range": rng,
        "updated_at": panel.get("fetched_at"),
        "caveats": caveats,
    }


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
