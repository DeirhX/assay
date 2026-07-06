"""Financial Modeling Prep provider -- optional third opinion (free tier).

Disabled unless ``FMP_API_KEY`` is set in the environment (or secrets.env loaded
by the caller). Free tier is US-only and 250 calls/day, which is plenty for
on-demand single-ticker lookups. Never commit the key.
"""

from __future__ import annotations

import os
import urllib.parse
from typing import Any

from .common import ProviderError, get_json, metric

_BASE = "https://financialmodelingprep.com/api/v3"


def api_key() -> str | None:
    return os.environ.get("FMP_API_KEY") or None


def enabled() -> bool:
    return api_key() is not None


def fundamentals(symbol: str) -> dict[str, Any] | None:
    key = api_key()
    if not key:
        return None
    sym = urllib.parse.quote(symbol)
    src = "fmp"
    try:
        profile = get_json(f"{_BASE}/profile/{sym}?apikey={key}")
        ratios = get_json(f"{_BASE}/ratios-ttm/{sym}?apikey={key}")
    except ProviderError:
        return None
    if not profile:
        return None
    p = profile[0]
    r = ratios[0] if ratios else {}
    mcap = p.get("mktCap")
    return {
        "name": p.get("companyName"),
        "profile": _profile(p),
        "price": metric(p.get("price"), src),
        "market_cap_usd_b": metric((mcap / 1e9) if mcap else None, src),
        "pe_ttm": metric(r.get("peRatioTTM"), src),
        "ps": metric(r.get("priceToSalesRatioTTM"), src),
    }


def _profile(p: dict[str, Any]) -> dict[str, Any] | None:
    """Business overview from FMP's /profile -- the fallback for names Yahoo's
    assetProfile leaves blank. Same shape as the Yahoo profile so research_pull
    can merge them field-by-field. Returns None when nothing usable came back."""
    employees = p.get("fullTimeEmployees")
    try:
        employees = int(employees) if employees is not None and employees != "" else None
    except (TypeError, ValueError):
        employees = None
    out = {
        "summary": (p.get("description") or "").strip() or None,
        "sector": p.get("sector") or None,
        "industry": p.get("industry") or None,
        "country": p.get("country") or None,
        "website": p.get("website") or None,
        "employees": employees,
    }
    return out if any(out.values()) else None
