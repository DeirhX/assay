"""SEC EDGAR provider -- the free, authoritative cross-check for US filers.

Pulls structured XBRL facts straight from company filings via
``data.sec.gov/api/xbrl/companyfacts``. This is primary-source data: it exists to
catch the internally-impossible figures (the ``$349B``/``$161B`` class of bug)
that an unofficial scrape can hand you with a straight face.

Limitations, stated honestly:
* US filers only. Foreign private issuers (ADRs like ARM/TSM/ASML, or KRX names)
  often have thin or absent us-gaap XBRL, so the cross-check simply won't fire
  for them -- and the app says so rather than pretending it verified anything.
* TTM is best-effort: we sum the four most recent clean quarters when available,
  else fall back to the latest fiscal year.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from .common import ProviderError, get_json, usd_b, metric

_CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "cache"
_TICKER_MAP = _CACHE_DIR / "sec_ticker_cik.json"
_TICKER_MAP_TTL = 7 * 86400  # refresh weekly

# Contact UA per SEC fair-access policy. Override with SEC_USER_AGENT.
_UA = os.environ.get(
    "SEC_USER_AGENT", "finance-rebalancing research (contact: set SEC_USER_AGENT)"
)
_HEADERS = {"User-Agent": _UA}

REVENUE_CONCEPTS = [
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
]
NET_INCOME_CONCEPTS = ["NetIncomeLoss", "ProfitLoss"]


def _load_ticker_map() -> dict[str, str]:
    """ticker (upper) -> zero-padded 10-digit CIK, cached weekly on disk."""
    fresh = (
        _TICKER_MAP.exists()
        and (time.time() - _TICKER_MAP.stat().st_mtime) < _TICKER_MAP_TTL
    )
    if fresh:
        try:
            return json.loads(_TICKER_MAP.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    data = get_json("https://www.sec.gov/files/company_tickers.json", headers=_HEADERS)
    mapping = {
        str(row["ticker"]).upper(): f"{int(row['cik_str']):010d}"
        for row in data.values()
    }
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _TICKER_MAP.write_text(json.dumps(mapping), encoding="utf-8")
    return mapping


def cik_for(symbol: str) -> str | None:
    return _load_ticker_map().get(symbol.upper())


def _company_facts(cik: str) -> dict[str, Any]:
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    return get_json(url, headers=_HEADERS)


def _usd_units(facts: dict[str, Any], concepts: list[str]) -> list[dict[str, Any]]:
    """Union of USD facts across candidate concepts, deduped by (start, end).

    Companies migrate XBRL tags over time (NVDA's revenue lives under
    ``Revenues`` now, but the old ``RevenueFromContract...`` tag still returns
    stale pre-2022 numbers). Taking the first non-empty concept would grab the
    dead tag; unioning and later sorting by period end keeps the live data.
    """
    gaap = facts.get("facts", {}).get("us-gaap", {})
    by_period: dict[tuple[str, str], dict[str, Any]] = {}
    for concept in concepts:
        node = gaap.get(concept)
        if not node:
            continue
        for entry in node.get("units", {}).get("USD", []):
            start, end = entry.get("start"), entry.get("end")
            if not start or not end:
                continue
            key = (start, end)
            prev = by_period.get(key)
            if prev is None or entry.get("filed", "") >= prev.get("filed", ""):
                by_period[key] = entry
    return list(by_period.values())


def _latest_shares(facts: dict[str, Any]) -> float | None:
    dei = facts.get("facts", {}).get("dei", {})
    for concept in ("EntityCommonStockSharesOutstanding", "CommonStockSharesOutstanding"):
        node = dei.get(concept) or facts.get("facts", {}).get("us-gaap", {}).get(concept)
        if not node:
            continue
        units = node.get("units", {}).get("shares")
        if units:
            latest = max(units, key=lambda e: e.get("end", ""))
            return float(latest["val"])
    return None


def _ttm_and_fy(units: list[dict[str, Any]]) -> tuple[float | None, float | None, str | None]:
    """Return (ttm_value, latest_fy_value, fy_period) from period-style facts."""
    if not units:
        return None, None, None

    def days(entry: dict[str, Any]) -> int:
        try:
            from datetime import date
            s = date.fromisoformat(entry["start"])
            e = date.fromisoformat(entry["end"])
            return (e - s).days
        except (KeyError, ValueError):
            return -1

    annuals = [u for u in units if u.get("form") == "10-K" and 350 <= days(u) <= 380]
    latest_fy = max(annuals, key=lambda e: e["end"]) if annuals else None

    quarters = [u for u in units if 80 <= days(u) <= 100]
    by_end: dict[str, dict[str, Any]] = {}
    for q in sorted(quarters, key=lambda e: e.get("filed", "")):
        by_end[q["end"]] = q
    ordered = sorted(by_end.values(), key=lambda e: e["end"], reverse=True)
    ttm = sum(q["val"] for q in ordered[:4]) if len(ordered) >= 4 else None

    fy_val = latest_fy["val"] if latest_fy else None
    fy_period = latest_fy["fy"] if latest_fy else None

    # Sanity: a real TTM should be in the same ballpark as the latest full year
    # (a fast grower can exceed it, a recent slump can dip below). If it's wildly
    # off, the quarterly tags are inconsistent -- drop TTM rather than lie.
    if ttm is not None and fy_val:
        if not (0.4 * abs(fy_val) <= abs(ttm) <= 3.0 * abs(fy_val)):
            ttm = None

    return ttm, fy_val, str(fy_period) if fy_period else None


def fundamentals(symbol: str) -> dict[str, Any] | None:
    """Independent anchors for a US filer, or None if not covered by EDGAR."""
    cik = cik_for(symbol)
    if not cik:
        return None
    try:
        facts = _company_facts(cik)
    except ProviderError:
        return None

    shares = _latest_shares(facts)
    rev_ttm, rev_fy, rev_fy_period = _ttm_and_fy(_usd_units(facts, REVENUE_CONCEPTS))
    ni_ttm, ni_fy, _ = _ttm_and_fy(_usd_units(facts, NET_INCOME_CONCEPTS))
    src = "sec_edgar"

    note_fy = f"FY{rev_fy_period}" if rev_fy_period else None
    return {
        "cik": cik,
        "entity": facts.get("entityName"),
        "shares_out_b": metric((shares / 1e9) if shares else None, src,
                               note="latest dei filing"),
        "revenue_ttm_usd_b": metric(usd_b(rev_ttm), src, note="approx TTM from 10-Qs"),
        "revenue_fy_usd_b": metric(usd_b(rev_fy), src, note=note_fy),
        "net_income_ttm_usd_b": metric(usd_b(ni_ttm), src, note="approx TTM from 10-Qs"),
        "net_income_fy_usd_b": metric(usd_b(ni_fy), src, note=note_fy),
    }
