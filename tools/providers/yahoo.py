"""Yahoo Finance provider (unofficial, free).

Two endpoints are used:

* ``/v8/finance/chart`` -- no auth, gives the daily price series we turn into
  momentum and 52-week stats.
* ``/v10/finance/quoteSummary`` -- needs a cookie + crumb handshake, gives the
  fundamentals (market cap, P/E, P/S, shares outstanding, margins, revenue).

This is the same data the ``yfinance`` package wraps; we hit the source directly
so the app stays dependency-free on bleeding-edge Pythons. It is a best-effort,
unofficial feed -- which is exactly why ``research_pull`` cross-checks it against
SEC EDGAR instead of trusting it blindly.
"""

from __future__ import annotations

import datetime as dt
import http.cookiejar
import urllib.parse
import urllib.request
from typing import Any

from .common import ProviderError, get_json, http_get, metric, usd_b

_SESSION: tuple[urllib.request.OpenerDirector, str] | None = None


_SEED_URLS = (
    "https://fc.yahoo.com",  # 404s but still sets the session cookie
    "https://finance.yahoo.com/quote/AAPL",
    "https://finance.yahoo.com",
)
_CRUMB_URLS = (
    "https://query1.finance.yahoo.com/v1/test/getcrumb",
    "https://query2.finance.yahoo.com/v1/test/getcrumb",
)


def _attempt_session() -> tuple[urllib.request.OpenerDirector, str] | None:
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    for seed in _SEED_URLS:
        try:
            http_get(seed, opener=opener, headers={"Accept": "text/html"}, retries=0)
        except ProviderError:
            pass  # a cookie may still have been set (notably by fc.yahoo.com)
        if len(jar):
            break
    if not len(jar):
        return None
    for crumb_url in _CRUMB_URLS:
        try:
            crumb = http_get(crumb_url, opener=opener, retries=0).decode(
                "utf-8", "replace"
            ).strip()
        except ProviderError:
            continue
        if crumb and "<" not in crumb and len(crumb) <= 40:
            return opener, crumb
    return None


def _new_session() -> tuple[urllib.request.OpenerDirector, str]:
    """Build a cookie jar, seed it, and fetch a crumb -- retried, since Yahoo's
    anti-bot handshake is moody and 401s at random."""
    import time

    for attempt in range(4):
        session = _attempt_session()
        if session:
            return session
        time.sleep(0.8 * (attempt + 1))
    raise ProviderError("could not obtain a usable Yahoo crumb after retries")


def _session() -> tuple[urllib.request.OpenerDirector, str]:
    global _SESSION
    if _SESSION is None:
        _SESSION = _new_session()
    return _SESSION


def reset_session() -> None:
    global _SESSION
    _SESSION = None


def _raw(node: Any) -> float | None:
    """Yahoo wraps numbers as {'raw': x, 'fmt': '...'}; unwrap defensively."""
    if isinstance(node, dict):
        node = node.get("raw")
    if isinstance(node, (int, float)):
        return float(node)
    return None


def chart(symbol: str, *, rng: str = "1y", interval: str = "1d") -> dict[str, Any]:
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{urllib.parse.quote(symbol)}?range={rng}&interval={interval}"
    )
    data = get_json(url)
    result = (data.get("chart", {}).get("result") or [None])[0]
    if not result:
        raise ProviderError(f"Yahoo chart returned no data for {symbol}")
    return result


def momentum(symbol: str) -> dict[str, Any]:
    """Current price, 52-week range, and trailing returns from the daily series."""
    result = chart(symbol, rng="1y", interval="1d")
    meta = result.get("meta", {})
    ts = result.get("timestamp") or []
    quote = (result.get("indicators", {}).get("quote") or [{}])[0]
    closes = [c for c in (quote.get("close") or []) if c is not None]
    if not closes:
        raise ProviderError(f"Yahoo chart has no closes for {symbol}")

    last = _raw(meta.get("regularMarketPrice")) or closes[-1]
    series = list(zip(ts, closes))

    def ago_return(days: int) -> float | None:
        if not series:
            return None
        cutoff = dt.datetime.now(dt.timezone.utc).timestamp() - days * 86400
        past = [c for (t, c) in series if t and t <= cutoff]
        base = past[-1] if past else (closes[0] if len(closes) > 1 else None)
        if not base:
            return None
        return (last / base - 1.0) * 100.0

    high_52 = _raw(meta.get("fiftyTwoWeekHigh")) or max(closes)
    low_52 = _raw(meta.get("fiftyTwoWeekLow")) or min(closes)
    return {
        "last": round(last, 4),
        "currency": meta.get("currency"),
        "high_52w": round(high_52, 4) if high_52 else None,
        "low_52w": round(low_52, 4) if low_52 else None,
        "pct_below_52w_high": round((last / high_52 - 1.0) * 100.0, 2) if high_52 else None,
        "chg_1m_pct": _round(ago_return(30)),
        "chg_3m_pct": _round(ago_return(91)),
        "chg_6m_pct": _round(ago_return(182)),
        "chg_12m_pct": _round(ago_return(364)),
    }


def _round(x: float | None) -> float | None:
    return None if x is None else round(x, 2)


def fundamentals(symbol: str) -> dict[str, Any]:
    """Market cap, multiples, shares, margins, revenue from quoteSummary."""
    opener, crumb = _session()
    modules = "price,summaryDetail,defaultKeyStatistics,financialData"
    url = (
        f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/"
        f"{urllib.parse.quote(symbol)}?modules={modules}"
        f"&crumb={urllib.parse.quote(crumb, safe='')}"
    )
    try:
        data = get_json(url, opener=opener)
    except ProviderError:
        # Crumb may have expired; rebuild once and retry.
        reset_session()
        opener, crumb = _session()
        url = (
            f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/"
            f"{urllib.parse.quote(symbol)}?modules={modules}"
            f"&crumb={urllib.parse.quote(crumb, safe='')}"
        )
        data = get_json(url, opener=opener)

    results = data.get("quoteSummary", {}).get("result") or []
    if not results:
        raise ProviderError(f"Yahoo quoteSummary empty for {symbol}")
    res = results[0]
    price = res.get("price", {})
    sd = res.get("summaryDetail", {})
    ks = res.get("defaultKeyStatistics", {})
    fd = res.get("financialData", {})

    rev = _raw(fd.get("totalRevenue"))
    gross_margin = _raw(fd.get("grossMargins"))
    rev_growth = _raw(fd.get("revenueGrowth"))
    src = "yahoo"
    return {
        "name": price.get("longName") or price.get("shortName"),
        "currency": price.get("currency"),
        "price": metric(_raw(price.get("regularMarketPrice")), src),
        "market_cap_usd_b": metric(usd_b(_raw(sd.get("marketCap"))), src),
        "shares_out_b": metric(
            (_raw(ks.get("sharesOutstanding")) or 0) / 1e9 or None, src
        ),
        "pe_ttm": metric(_raw(sd.get("trailingPE")), src),
        "pe_fwd": metric(_raw(sd.get("forwardPE")) or _raw(ks.get("forwardPE")), src),
        "ps": metric(_raw(sd.get("priceToSalesTrailing12Months")), src),
        "revenue_ttm_usd_b": metric(usd_b(rev), src),
        "gross_margin_pct": metric(gross_margin * 100 if gross_margin else None, src),
        "rev_growth_yoy_pct": metric(rev_growth * 100 if rev_growth else None, src),
    }
