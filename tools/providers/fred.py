"""FRED macro provider (free, keyless CSV path).

FRED's official JSON API requires a free API key, but the graph CSV endpoint is
stable enough for a small, low-frequency macro context panel:

    https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10

This provider deliberately pulls a compact set of series. Macro data is context,
not a ticker fundamental, so it is exposed as its own record instead of being
jammed into per-symbol metrics like some kind of cursed spreadsheet casserole.
"""

from __future__ import annotations

import csv
import concurrent.futures
import datetime as dt
import io
import urllib.parse
from typing import Any

from .common import ProviderError, http_get

_BASE = "https://fred.stlouisfed.org/graph/fredgraph.csv"

SERIES: tuple[dict[str, str], ...] = (
    {
        "id": "DGS10",
        "label": "10Y Treasury",
        "unit": "%",
        "category": "rates",
        "note": "Long-rate discount-rate anchor.",
    },
    {
        "id": "DGS2",
        "label": "2Y Treasury",
        "unit": "%",
        "category": "rates",
        "note": "Policy-sensitive rate anchor.",
    },
    {
        "id": "T10Y2Y",
        "label": "10Y-2Y spread",
        "unit": "pp",
        "category": "rates",
        "note": "Yield-curve slope; inversion flags recession risk.",
    },
    {
        "id": "DFF",
        "label": "Effective Fed Funds",
        "unit": "%",
        "category": "rates",
        "note": "Actual overnight policy-rate print.",
    },
    {
        "id": "CPIAUCSL",
        "label": "CPI index",
        "unit": "index",
        "category": "inflation",
        "note": "All-items CPI; 1Y change is the useful read.",
    },
    {
        "id": "UNRATE",
        "label": "Unemployment",
        "unit": "%",
        "category": "labor",
        "note": "Labor-market slack.",
    },
    {
        "id": "INDPRO",
        "label": "Industrial production",
        "unit": "index",
        "category": "growth",
        "note": "Cyclical activity backdrop.",
    },
    {
        "id": "DTWEXBGS",
        "label": "Broad USD index",
        "unit": "index",
        "category": "fx",
        "note": "Dollar strength headwind/tailwind for multinationals.",
    },
    {
        "id": "VIXCLS",
        "label": "VIX",
        "unit": "index",
        "category": "risk",
        "note": "Equity volatility regime.",
    },
)


def macro_snapshot() -> dict[str, Any]:
    """Return a compact source-stamped macro dashboard."""
    out: dict[str, Any] = {
        "source": "fred",
        "as_of": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "series": {},
        "errors": [],
    }
    results: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(_series_snapshot, spec): spec for spec in SERIES}
        for future in concurrent.futures.as_completed(futures):
            spec = futures[future]
            try:
                results[spec["id"]] = future.result()
            except ProviderError as exc:
                errors[spec["id"]] = f"{spec['id']}: {exc}"
    for spec in SERIES:
        if spec["id"] in results:
            out["series"][spec["id"]] = results[spec["id"]]
        if spec["id"] in errors:
            out["errors"].append(errors[spec["id"]])
    return out


def snapshot_for(ids: "list[str] | tuple[str, ...]") -> dict[str, Any]:
    """Source-stamped snapshot for an arbitrary set of series ids, fanned out
    concurrently. Ids outside :data:`SERIES` fall back to a bare spec (label ==
    id). Shaped exactly like :func:`macro_snapshot` so callers (e.g. the regime
    strip) can pick their own compact panel without paying for the full set."""
    specs = [
        next((s for s in SERIES if s["id"] == i),
             {"id": i, "label": i, "category": "", "unit": "", "note": ""})
        for i in ids
    ]
    out: dict[str, Any] = {
        "source": "fred",
        "as_of": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "series": {},
        "errors": [],
    }
    results: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    if specs:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(6, len(specs)))) as pool:
            futures = {pool.submit(_series_snapshot, spec): spec for spec in specs}
            for future in concurrent.futures.as_completed(futures):
                spec = futures[future]
                try:
                    results[spec["id"]] = future.result()
                except ProviderError as exc:
                    errors[spec["id"]] = f"{spec['id']}: {exc}"
    for spec in specs:
        if spec["id"] in results:
            out["series"][spec["id"]] = results[spec["id"]]
        if spec["id"] in errors:
            out["errors"].append(errors[spec["id"]])
    return out


def series_snapshot(series_id: str) -> dict[str, Any]:
    """Snapshot for a single FRED series, shaped like :func:`macro_snapshot` so a
    caller that needs just one number (e.g. the options overlay's ``DGS10``
    risk-free rate) fetches one CSV instead of paying for the whole nine-series
    fan-out. Falls back to a bare spec for ids outside :data:`SERIES`."""
    spec = next(
        (s for s in SERIES if s["id"] == series_id),
        {"id": series_id, "label": series_id, "category": "", "unit": "", "note": ""},
    )
    out: dict[str, Any] = {
        "source": "fred",
        "as_of": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "series": {},
        "errors": [],
    }
    try:
        out["series"][series_id] = _series_snapshot(spec)
    except ProviderError as exc:
        out["errors"].append(f"{series_id}: {exc}")
    return out


def _series_snapshot(spec: dict[str, str]) -> dict[str, Any]:
    rows = _fetch_rows(spec["id"])
    if not rows:
        raise ProviderError("no numeric observations")
    latest_date, latest_value = rows[-1]
    node: dict[str, Any] = {
        "id": spec["id"],
        "label": spec["label"],
        "category": spec["category"],
        "unit": spec["unit"],
        "value": round(latest_value, 6),
        "display": _display(latest_value, spec["unit"]),
        "as_of": latest_date,
        "source": "fred",
        "url": f"https://fred.stlouisfed.org/series/{spec['id']}",
        "note": spec["note"],
    }

    prior = _prior_year_value(rows, latest_date)
    if prior is not None and prior != 0:
        change = latest_value - prior
        node["change_1y"] = round(change, 6)
        if spec["unit"] in {"%", "pp"}:
            node["change_1y_display"] = f"{change:+.2f} pp"
        else:
            pct = (latest_value / prior - 1.0) * 100.0
            node["change_1y_pct"] = round(pct, 6)
            node["change_1y_display"] = f"{pct:+.1f}% YoY"
    return node


def _fetch_rows(series_id: str) -> list[tuple[str, float]]:
    start = (dt.date.today() - dt.timedelta(days=800)).isoformat()
    query = urllib.parse.urlencode({"id": series_id, "cosd": start})
    raw = http_get(
        f"{_BASE}?{query}",
        headers={"Accept": "text/csv"},
        timeout=5,
        retries=0,
    )
    text = raw.decode("utf-8-sig", "replace")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or series_id not in reader.fieldnames:
        raise ProviderError(f"unexpected FRED CSV header for {series_id}")

    rows: list[tuple[str, float]] = []
    for row in reader:
        date_s = (row.get("observation_date") or "").strip()
        value_s = (row.get(series_id) or "").strip()
        if not date_s or value_s in {"", "."}:
            continue
        try:
            rows.append((date_s, float(value_s)))
        except ValueError:
            continue
    return rows


def _prior_year_value(rows: list[tuple[str, float]], latest_date: str) -> float | None:
    try:
        cutoff = dt.date.fromisoformat(latest_date) - dt.timedelta(days=365)
    except ValueError:
        return None
    candidates = [(d, v) for d, v in rows
                  if (pd := _date_or_none(d)) is not None and pd <= cutoff]
    return candidates[-1][1] if candidates else None


def _date_or_none(value: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def _display(value: float, unit: str) -> str:
    if unit in {"%", "pp"}:
        return f"{value:.2f}{unit}"
    return f"{value:.2f}"
