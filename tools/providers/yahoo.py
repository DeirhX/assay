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
import threading
import urllib.parse
import urllib.request
from typing import Any

from .common import ProviderError, get_json, http_get, metric, usd_b

_SESSION: tuple[urllib.request.OpenerDirector, str] | None = None
# Guards lazy session creation so concurrent callers (the parallel risk/exit
# fetches) share one crumb handshake instead of each racing to build their own.
_SESSION_LOCK = threading.Lock()


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
        with _SESSION_LOCK:
            if _SESSION is None:   # double-checked: only the first thread pays the handshake
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


def search(query: str, *, limit: int = 8) -> list[dict[str, Any]]:
    """Symbol / company-name search for a partial string.

    Hits Yahoo's public ``/v1/finance/search`` (no crumb needed) and returns
    equity-ish matches so the UI can suggest real tickers when someone types a
    name or a not-quite-right symbol. Best-effort: a hiccup falls back to the
    authenticated opener, and the caller treats failures as "no matches".
    """
    q = (query or "").strip()
    if not q:
        return []
    url = (
        "https://query1.finance.yahoo.com/v1/finance/search?"
        + urllib.parse.urlencode({
            "q": q,
            "quotesCount": max(1, min(limit * 3, 25)),
            "newsCount": 0,
            "listsCount": 0,
            "enableFuzzyQuery": "false",
        })
    )
    try:
        data = get_json(url)
    except ProviderError:
        opener, _crumb = _session()
        data = get_json(url, opener=opener)

    allowed = {"EQUITY", "ETF"}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in data.get("quotes") or []:
        if not isinstance(item, dict):
            continue
        sym = str(item.get("symbol") or "").strip().upper()
        qtype = str(item.get("quoteType") or "").upper()
        if not sym or sym in seen or qtype not in allowed:
            continue
        seen.add(sym)
        out.append({
            "symbol": sym,
            "name": (item.get("longname") or item.get("shortname") or "").strip(),
            "exchange": str(item.get("exchDisp") or item.get("exchange") or "").strip(),
            "type": str(item.get("typeDisp") or qtype.title()).strip(),
        })
        if len(out) >= limit:
            break
    return out


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


def price_history_from_chart(result: dict[str, Any], *, rng: str, interval: str) -> dict[str, Any] | None:
    """Compact daily close series for UI charts.

    Keep this as structured data, not a downloaded vendor chart image. The UI can
    redraw it, downsample it, or annotate it later without reverse-engineering a
    PNG like absolute goblins.
    """
    meta = result.get("meta", {})
    ts = result.get("timestamp") or []
    quote = (result.get("indicators", {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    # Daily traded volume rides alongside the closes so downstream liquidity math
    # (ADV -> child-slice caps in the exit planner) doesn't need a second fetch.
    volumes = quote.get("volume") or []
    # Intraday intervals (5m/30m/1h...) keep the time-of-day so the UI can label
    # and span a sub-day window; daily+ intervals stay date-only as before.
    intraday = interval[-1:] in ("m", "h")
    points: list[dict[str, Any]] = []
    for i, (timestamp, close) in enumerate(zip(ts, closes)):
        if timestamp is None or close is None:
            continue
        try:
            moment = dt.datetime.fromtimestamp(timestamp, dt.timezone.utc)
        except (OSError, OverflowError, TypeError, ValueError):
            continue
        stamp = moment.isoformat() if intraday else moment.date().isoformat()
        point: dict[str, Any] = {"date": stamp, "close": round(float(close), 4)}
        vol = volumes[i] if i < len(volumes) else None
        if isinstance(vol, (int, float)):
            point["volume"] = int(vol)
        points.append(point)
    if not points:
        return None
    return {
        "source": "yahoo",
        "range": rng,
        "interval": interval,
        "currency": meta.get("currency"),
        "points": points,
    }


def momentum(symbol: str) -> dict[str, Any]:
    """Current price, 52-week range, and trailing returns from the daily series."""
    rng = "1y"
    interval = "1d"
    result = chart(symbol, rng=rng, interval=interval)
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
        "price_history": price_history_from_chart(result, rng=rng, interval=interval),
    }


def latest_price(symbol: str) -> dict[str, Any]:
    """Just the latest price for a symbol, cheaply -- the background quote sweep's
    workhorse. Prefers the meta ``regularMarketPrice`` (intraday), falls back to
    the last daily close. Returns ``{price, currency, at}`` (``at`` is ISO-8601
    UTC of the market time when Yahoo reports it, else now). Raises ProviderError
    when there is no usable price so the caller can cache a negative result."""
    result = chart(symbol, rng="5d", interval="1d")
    meta = result.get("meta", {})
    price = _raw(meta.get("regularMarketPrice"))
    if price is None:
        quote = (result.get("indicators", {}).get("quote") or [{}])[0]
        closes = [c for c in (quote.get("close") or []) if c is not None]
        price = float(closes[-1]) if closes else None
    if price is None or not price:
        raise ProviderError(f"Yahoo chart has no usable price for {symbol}")
    market_ts = meta.get("regularMarketTime")
    when = (dt.datetime.fromtimestamp(market_ts, dt.timezone.utc)
            if isinstance(market_ts, (int, float)) and market_ts
            else dt.datetime.now(dt.timezone.utc))
    return {
        "price": round(float(price), 4),
        "currency": meta.get("currency"),
        "at": when.isoformat(timespec="seconds"),
    }


def _round(x: float | None) -> float | None:
    return None if x is None else round(x, 2)


def _profile(ap: dict[str, Any]) -> dict[str, Any] | None:
    """Business overview from Yahoo's assetProfile module: what the company
    actually does, plus the sector/industry/HQ/headcount tags. Returns None if
    Yahoo gave us nothing usable so the caller can skip the section entirely."""
    if not ap:
        return None
    employees = ap.get("fullTimeEmployees")
    if isinstance(employees, dict):  # occasionally wrapped like the numbers
        employees = employees.get("raw")
    out = {
        "summary": (ap.get("longBusinessSummary") or "").strip() or None,
        "sector": ap.get("sector") or None,
        "industry": ap.get("industry") or None,
        "country": ap.get("country") or None,
        "website": ap.get("website") or None,
        "employees": int(employees) if isinstance(employees, (int, float)) else None,
    }
    return out if any(out.values()) else None


def asset_profile(symbol: str) -> dict[str, Any] | None:
    """Just the business/sector tags (assetProfile module) -- much lighter than
    ``fundamentals`` when all we want is the sector for a traded symbol. Returns
    None when Yahoo has nothing usable (common for foreign/derivative codes)."""
    opener, crumb = _session()
    url = (
        f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/"
        f"{urllib.parse.quote(symbol)}?modules=assetProfile"
        f"&crumb={urllib.parse.quote(crumb, safe='')}"
    )
    try:
        data = get_json(url, opener=opener)
    except ProviderError:
        reset_session()
        opener, crumb = _session()
        url = (
            f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/"
            f"{urllib.parse.quote(symbol)}?modules=assetProfile"
            f"&crumb={urllib.parse.quote(crumb, safe='')}"
        )
        data = get_json(url, opener=opener)
    results = data.get("quoteSummary", {}).get("result") or []
    if not results:
        return None
    return _profile(results[0].get("assetProfile", {}))


def fundamentals(symbol: str) -> dict[str, Any]:
    """Market cap, multiples, shares, margins, revenue from quoteSummary."""
    opener, crumb = _session()
    modules = "assetProfile,price,summaryDetail,defaultKeyStatistics,financialData"
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
    ap = res.get("assetProfile", {})

    rev = _raw(fd.get("totalRevenue"))
    gross_margin = _raw(fd.get("grossMargins"))
    rev_growth = _raw(fd.get("revenueGrowth"))
    src = "yahoo"
    return {
        "name": price.get("longName") or price.get("shortName"),
        "currency": price.get("currency"),
        "quote_type": price.get("quoteType"),
        "profile": _profile(ap),
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


def _opt_contract(node: dict[str, Any]) -> dict[str, Any] | None:
    """Flatten one Yahoo option contract to the fields the overlay math needs.

    Yahoo already unwraps these to bare numbers on ``/v7/finance/options`` (unlike
    quoteSummary), so no ``raw`` peeling -- but bid/ask can be absent on thin
    strikes, in which case ``lastPrice`` is the only usable mark.
    """
    strike = node.get("strike")
    if not isinstance(strike, (int, float)):
        return None
    bid = node.get("bid")
    ask = node.get("ask")
    last = node.get("lastPrice")
    def _num(x: Any) -> float | None:
        return float(x) if isinstance(x, (int, float)) else None
    return {
        "contract": node.get("contractSymbol"),
        "strike": float(strike),
        "bid": _num(bid),
        "ask": _num(ask),
        "last": _num(last),
        "implied_vol": _num(node.get("impliedVolatility")),
        "open_interest": node.get("openInterest") if isinstance(node.get("openInterest"), int) else None,
        "volume": node.get("volume") if isinstance(node.get("volume"), int) else None,
        "in_the_money": bool(node.get("inTheMoney")) if node.get("inTheMoney") is not None else None,
    }


def option_chain(symbol: str, *, max_expiries: int = 4) -> dict[str, Any] | None:
    """Option chain for ``symbol`` across the nearest ``max_expiries`` expiries.

    Hits ``/v7/finance/options`` (crumb-authenticated, same session as
    ``fundamentals``). First call returns the near expiry plus the full
    ``expirationDates`` list; we then fetch each subsequent expiry by its epoch.
    Returns ``{"symbol","currency","underlying_price","expiries":[{expiry,
    calls,puts}]}`` or ``None`` when the name has no listed options (common for
    non-US tickers) so the caller can fall back to the Black-Scholes estimate.
    """
    opener, crumb = _session()

    def _fetch(date_epoch: int | None) -> dict[str, Any] | None:
        url = (
            f"https://query2.finance.yahoo.com/v7/finance/options/"
            f"{urllib.parse.quote(symbol)}?crumb={urllib.parse.quote(crumb, safe='')}"
        )
        if date_epoch is not None:
            url += f"&date={date_epoch}"
        try:
            data = get_json(url, opener=opener)
        except ProviderError:
            return None
        results = (data.get("optionChain", {}) or {}).get("result") or []
        return results[0] if results else None

    root = _fetch(None)
    if root is None:  # crumb may be stale; rebuild once and retry the near expiry
        reset_session()
        opener, crumb = _session()
        root = _fetch(None)
    if root is None:
        return None

    exp_dates = [d for d in (root.get("expirationDates") or []) if isinstance(d, int)]
    quote = root.get("quote") or {}
    underlying = _raw(quote.get("regularMarketPrice")) or quote.get("regularMarketPrice")
    currency = quote.get("currency")

    expiries: list[dict[str, Any]] = []

    def _add(res: dict[str, Any] | None) -> None:
        if not res:
            return
        options = (res.get("options") or [{}])[0]
        expiry_epoch = options.get("expirationDate")
        if not isinstance(expiry_epoch, int):
            return
        calls = [c for c in (_opt_contract(n) for n in (options.get("calls") or [])) if c]
        puts = [p for p in (_opt_contract(n) for n in (options.get("puts") or [])) if p]
        if not calls and not puts:
            return
        expiries.append({
            "expiry": dt.datetime.fromtimestamp(expiry_epoch, dt.timezone.utc).date().isoformat(),
            "expiry_epoch": expiry_epoch,
            "calls": sorted(calls, key=lambda c: c["strike"]),
            "puts": sorted(puts, key=lambda p: p["strike"]),
        })

    _add(root)
    fetched_epochs = {e for e in exp_dates[:1]}
    for epoch in exp_dates:
        if len(expiries) >= max_expiries:
            break
        if epoch in fetched_epochs:
            continue
        fetched_epochs.add(epoch)
        _add(_fetch(epoch))

    if not expiries:
        return None
    expiries.sort(key=lambda e: e["expiry_epoch"])
    return {
        "source": "yahoo",
        "symbol": symbol.upper(),
        "currency": currency,
        "underlying_price": round(float(underlying), 4) if isinstance(underlying, (int, float)) else None,
        "expiries": expiries[:max_expiries],
    }
