#!/usr/bin/env python3
"""Regime context strip -- deliberately dumb macro backdrop for the segment view.

"Most promising segment" is regime-conditional: momentum chase works in easing
regimes and gets punished in tightening ones. This surfaces a tiny, descriptive
macro strip (rates, credit, USD, volatility) above the segment leaderboard so the
human reads the ranking in context.

Explicitly **not a signal generator**: three or four FRED series, a trend arrow
from the 1-year change, and a one-line note each. No scores, no predictions, no
"buy/sell" -- context for a person, nothing the app acts on. FRED is already the
macro provider (:mod:`providers.fred`); this just picks a compact panel and
caches it (macro data barely moves intraday) so the segment view isn't gated on
four CSV round-trips per load.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import store  # noqa: E402
import timeutil  # noqa: E402
from config import DATA_DIR  # noqa: E402

EPS = 1e-9
CACHE_FILE = DATA_DIR / "cache" / "regime.json"
CACHE_TTL_SECONDS = 6 * 3600

# The compact panel. ``rising`` is the plain word for an up-move so the strip can
# describe the trend without implying it's good or bad -- that judgement is the
# human's. Order is the display order.
PANEL: tuple[dict[str, str], ...] = (
    {"id": "DGS10", "label": "Rates", "note": "10-year Treasury yield", "rising": "higher"},
    {"id": "BAMLH0A0HYM2", "label": "Credit", "note": "High-yield spread (OAS)", "rising": "wider"},
    {"id": "DTWEXBGS", "label": "USD", "note": "Broad dollar index", "rising": "stronger"},
    {"id": "VIXCLS", "label": "Volatility", "note": "Equity vol regime (VIX)", "rising": "higher"},
)

CAPTION = "Descriptive macro context — arrows are the 1-year trend, not a signal. The read is yours."


def _trend(change: Any) -> str:
    """Up / down / flat from a signed 1-year change. Flat when tiny or missing."""
    if not isinstance(change, (int, float)):
        return "flat"
    if change > EPS:
        return "up"
    if change < -EPS:
        return "down"
    return "flat"


def build_strip(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Pure: turn a FRED-style ``{series: {id: node}}`` snapshot into the ordered
    strip. Series absent from the snapshot (a failed CSV) are skipped rather than
    faked -- a partial strip still gives context."""
    series = snapshot.get("series") if isinstance(snapshot, dict) else None
    series = series if isinstance(series, dict) else {}
    out: list[dict[str, Any]] = []
    for spec in PANEL:
        node = series.get(spec["id"])
        if not isinstance(node, dict):
            continue
        change = node.get("change_1y")
        out.append({
            "id": spec["id"],
            "label": spec["label"],
            "note": spec["note"],
            "value": node.get("value"),
            "display": node.get("display"),
            "as_of": node.get("as_of"),
            "trend": _trend(change),
            "rising": spec["rising"],
            "change_display": node.get("change_1y_display"),
            "url": node.get("url"),
        })
    return out


def build_regime(*, fetch: Any = None, use_cache: bool = True) -> dict[str, Any]:
    """The strip plus metadata, cached ``CACHE_TTL_SECONDS``. ``fetch`` is an
    injectable ``ids -> snapshot`` callable (defaults to FRED) so tests never
    touch the network. A cache hit short-circuits the fan-out entirely."""
    if use_cache:
        cached = store.load(CACHE_FILE)
        if isinstance(cached, dict) and timeutil.cache_fresh(cached.get("fetched_at"), CACHE_TTL_SECONDS):
            return {**cached, "cached": True}

    if fetch is None:
        from providers import fred
        fetch = fred.snapshot_for
    snapshot = fetch([spec["id"] for spec in PANEL])
    strip = build_strip(snapshot)
    payload = {
        "as_of": snapshot.get("as_of") if isinstance(snapshot, dict) else None,
        "source": "fred",
        "caption": CAPTION,
        "strip": strip,
        "errors": snapshot.get("errors") if isinstance(snapshot, dict) else [],
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }
    if strip:  # don't cache a total failure -- retry next load
        store.write_json(CACHE_FILE, payload)
    return {**payload, "cached": False}


def _main() -> int:
    import json
    print(json.dumps(build_regime(use_cache=False), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
