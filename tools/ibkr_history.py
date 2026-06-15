#!/usr/bin/env python3
"""Reconstruct the full IBKR trading + portfolio-value history via Flex.

The snapshot reader (``ibkr_portfolio.py``) answers "what do I hold right now".
This module answers "how did I get here": the complete executed-trade ledger and
the day-by-day net-asset-value series, back to account inception.

How it works (and why it has to be a loop):
  The Flex Web Service caps any single request at 365 days, so a multi-year
  history can only be assembled one ≤365-day window at a time. We walk backward
  from the latest *available* day (Flex refuses a window ending today/an unsettled
  day with Code 1003, so we first probe the query for its real ``toDate``), pulling
  windows until two consecutive windows come back empty -- the signature of having
  stepped past the account's opening date -- or until a safety cap. Trades and cash
  transactions are de-duplicated by their IBKR ids; the daily NAV series is
  de-duplicated by report date. Rate limits (Code 1018) are retried with backoff.

Persist once, then top up cheaply:
  ``build_history`` does the full back-to-inception walk. ``extend_history`` takes
  a previously persisted payload and fetches only the days since it was last
  covered (seeding the accumulators with the cached data), so routine refreshes are
  a single Flex request rather than a multi-year re-walk. Dedup makes a few days of
  re-pull overlap free, which is how late Flex restatements get picked up.

Required Flex query sections (names as they appear in the Flex builder UI):
  The query whose id you point this at MUST include the **Trades** section (tick
  Executions) and the **Net Asset Value (NAV) in Base** section -- the latter is
  what emits the ``<EquitySummaryInBase>`` daily-NAV rows in the XML; there is no
  checkbox literally named "EquitySummaryInBase". (CashTransactions is optional
  but recommended; do NOT confuse "Net Asset Value (NAV) in Base", the daily
  series, with "Change in NAV", a single per-period summary.) The snapshot query
  (OpenPositions only) will not work. Create a dedicated Activity Flex query for
  history and put its id in ``IBKR_FLEX_HISTORY_QUERY_ID`` (falls back to
  ``IBKR_FLEX_QUERY_ID``).

Read-only by construction, like the snapshot reader: the Flex service only
returns reports and can never place a trade. Standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone

from ibkr_portfolio import (  # vendored, stdlib-only primitives
    SECRETS_FILE,
    _dec,
    _ibkr_datetime,
    fetch_report,
    load_env_file,
)

# Flex hard limit: a single request spans at most 365 days.
MAX_WINDOW_DAYS = 365
# Safety cap so an unconfigured query (or a service hiccup) can't loop forever.
DEFAULT_MAX_WINDOWS = 20
# Politeness gap between windows. Flex SendRequest is rate-limited (1/sec, 10/min);
# report generation itself adds latency, so a small extra gap keeps us well clear.
DEFAULT_SLEEP = 2.0


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #
def resolve_history_credentials(token: str | None = None,
                                query_id: str | None = None) -> tuple[str, str]:
    """Resolve the Flex token and the *history* query id.

    Order: explicit arg -> environment -> tools/secrets.env. The history query id
    is read from IBKR_FLEX_HISTORY_QUERY_ID, falling back to IBKR_FLEX_QUERY_ID so
    a single combined query still works."""
    file_vals = load_env_file(SECRETS_FILE)

    def pick(*candidates: str | None) -> str:
        for c in candidates:
            if c and not c.strip().startswith("<"):
                return c.strip()
        return ""

    tok = pick(token, os.environ.get("IBKR_FLEX_TOKEN"), file_vals.get("IBKR_FLEX_TOKEN"))
    qid = pick(
        query_id,
        os.environ.get("IBKR_FLEX_HISTORY_QUERY_ID"),
        file_vals.get("IBKR_FLEX_HISTORY_QUERY_ID"),
        os.environ.get("IBKR_FLEX_QUERY_ID"),
        file_vals.get("IBKR_FLEX_QUERY_ID"),
    )
    if not tok:
        raise ValueError(
            f"no Flex token. Paste IBKR_FLEX_TOKEN into {SECRETS_FILE.name}, set "
            "the environment variable, or pass --token."
        )
    if not qid:
        raise ValueError(
            "no Flex query id. Create an Activity Flex query with the Trades and "
            "'Net Asset Value (NAV) in Base' sections, then set "
            f"IBKR_FLEX_HISTORY_QUERY_ID (or IBKR_FLEX_QUERY_ID) in "
            f"{SECRETS_FILE.name}, or pass --query-id."
        )
    return tok, qid


# --------------------------------------------------------------------------- #
# Date helpers
# --------------------------------------------------------------------------- #
def _parse_date(value: str | None) -> date | None:
    """Accept YYYYMMDD or YYYY-MM-DD; return None for blanks/garbage."""
    if not value:
        return None
    v = value.strip().replace("-", "")
    try:
        return datetime.strptime(v, "%Y%m%d").date()
    except ValueError:
        return None


def _iso_date(yyyymmdd: str | None) -> str:
    d = _parse_date(yyyymmdd)
    return d.isoformat() if d else (yyyymmdd or "")


def windows(end: date, *, start: date | None, span_days: int = MAX_WINDOW_DAYS,
            max_windows: int = DEFAULT_MAX_WINDOWS) -> list[tuple[date, date]]:
    """Descending list of (from, to) windows of at most ``span_days`` each.

    With an explicit ``start`` we stop once the window reaches it. Without one we
    emit up to ``max_windows`` windows and rely on the caller's empty-window
    detection to stop early at account inception."""
    out: list[tuple[date, date]] = []
    cur_end = end
    floor = start
    span = timedelta(days=span_days - 1)
    while len(out) < max_windows:
        cur_start = cur_end - span
        if floor and cur_start < floor:
            cur_start = floor
        out.append((cur_start, cur_end))
        if floor and cur_start <= floor:
            break
        cur_end = cur_start - timedelta(days=1)
    return out


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def _trade_datetime(t: ET.Element) -> str:
    """Best-effort ISO timestamp for a trade row across Flex field variants."""
    raw = t.get("dateTime") or ""
    if raw:
        iso = _ibkr_datetime(raw)
        if iso:
            return iso
    td = t.get("tradeDate") or ""
    tt = t.get("tradeTime") or ""
    if td and tt:
        return _ibkr_datetime(f"{td};{tt}")
    return _iso_date(td)


_OPTION_CLASSES = ("OPT", "FOP")


def derive_underlying(asset_class: str, symbol: str,
                      description: str = "", underlying_symbol: str = "") -> str:
    """Best-effort underlying ticker, used as the grouping key.

    Options/FOPs: prefer the Flex ``underlyingSymbol``; failing that, the first
    token of the human description (``"VOW3 21JUN24 110 P"`` -> ``"VOW3"``),
    which is reliable where the raw option ``symbol`` is not (IBKR renders some
    as ``"P VO3  20240621 110 M"`` -- the underlying is NOT the first token).
    Everything else groups under its own symbol."""
    if (asset_class or "").upper() in _OPTION_CLASSES:
        if (underlying_symbol or "").strip():
            return underlying_symbol.strip().upper()
        desc_tok = (description or "").strip().split()
        if desc_tok:
            return desc_tok[0].upper()
        sym_tok = (symbol or "").strip().split()
        if sym_tok:
            return sym_tok[0].upper()
    return (symbol or "").strip() or "?"


def parse_trade(t: ET.Element) -> dict:
    """Normalize one Flex <Trade> execution row.

    IBKR sign conventions: ``proceeds`` is negative for buys / positive for sells,
    ``quantity`` is negative for sells. ``netCash`` already nets commissions. We
    convert to base currency with ``fxRateToBase`` so cross-currency books sum."""
    fx = _dec(t.get("fxRateToBase")) or 1.0
    proceeds = _dec(t.get("proceeds"))
    net_cash = _dec(t.get("netCash"))
    buy_sell = (t.get("buySell") or "").upper()
    qty = _dec(t.get("quantity"))
    side = buy_sell or ("BUY" if qty >= 0 else "SELL")
    asset_class = t.get("assetCategory", "")
    symbol = t.get("symbol", "")
    description = t.get("description", "")
    underlying_symbol = t.get("underlyingSymbol", "")
    return {
        "trade_id": t.get("tradeID", ""),
        "transaction_id": t.get("transactionID", ""),
        "datetime": _trade_datetime(t),
        "date": _iso_date(t.get("tradeDate")),
        "symbol": symbol,
        "asset_class": asset_class,
        "currency": t.get("currency", ""),
        "side": side,
        "quantity": qty,
        "price": _dec(t.get("tradePrice")),
        "proceeds": proceeds,
        "commission": _dec(t.get("ibCommission")),
        "net_cash": net_cash,
        "fx_rate_to_base": fx,
        # Signed cash flow in base currency: negative = cash out (a buy).
        "base_cash_flow": round(net_cash * fx, 2),
        # Signed traded value in base currency (proceeds basis).
        "base_value": round(proceeds * fx, 2),
        "realized_pnl": _dec(t.get("fifoPnlRealized")),
        "open_close": t.get("openCloseIndicator", ""),
        "description": description,
        "listing_exchange": t.get("listingExchange", ""),
        # Derivative metadata + the grouping key (the underlying ticker), so the
        # UI can collapse a swarm of option contracts under one name.
        "underlying_symbol": underlying_symbol,
        "underlying": derive_underlying(asset_class, symbol, description, underlying_symbol),
        "put_call": (t.get("putCall") or "").upper(),
        "strike": _dec(t.get("strike")) or None,
        "expiry": _iso_date(t.get("expiry")) if t.get("expiry") else "",
    }


def _is_execution(t: ET.Element) -> bool:
    """Keep execution-level rows; drop ORDER/CLOSED_LOT aggregates that would
    double-count when a query is configured with multiple levels of detail."""
    level = (t.get("levelOfDetail") or "").upper()
    return level in ("", "EXECUTION")


def parse_nav_row(e: ET.Element) -> dict:
    return {
        "date": _iso_date(e.get("reportDate")),
        "nav": _dec(e.get("total")),
        "cash": _dec(e.get("cash")),
        "stock": _dec(e.get("stock")),
    }


def parse_cash_txn(c: ET.Element) -> dict:
    fx = _dec(c.get("fxRateToBase")) or 1.0
    amount = _dec(c.get("amount"))
    return {
        "transaction_id": c.get("transactionID", ""),
        "datetime": _ibkr_datetime(c.get("dateTime")) or _iso_date(c.get("settleDate")),
        "date": _iso_date(c.get("settleDate") or c.get("reportDate")),
        "type": c.get("type", ""),
        "symbol": c.get("symbol", ""),
        "currency": c.get("currency", ""),
        "amount": amount,
        "base_amount": round(amount * fx, 2),
        "description": c.get("description", ""),
    }


def parse_window(doc: ET.Element) -> dict:
    """Pull trades, NAV rows, and cash transactions out of one Flex response."""
    trades: list[dict] = []
    nav: list[dict] = []
    cash: list[dict] = []
    account = ""
    for stmt in doc.findall("./FlexStatements/FlexStatement"):
        account = account or stmt.get("accountId", "")
        for t in stmt.findall(".//Trades/Trade"):
            if _is_execution(t):
                trades.append(parse_trade(t))
        for e in stmt.findall(".//EquitySummaryInBase/EquitySummaryByReportDateInBase"):
            nav.append(parse_nav_row(e))
        for c in stmt.findall(".//CashTransactions/CashTransaction"):
            cash.append(parse_cash_txn(c))
    return {"account": account, "trades": trades, "nav": nav, "cash": cash}


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #
def _trade_key(t: dict) -> str:
    return t.get("transaction_id") or t.get("trade_id") or f"{t['datetime']}|{t['symbol']}|{t['quantity']}"


def _cash_key(c: dict) -> str:
    return c.get("transaction_id") or f"{c['datetime']}|{c['type']}|{c['amount']}"


class _Unavailable(Exception):
    """Flex Code 1003: the requested window starts before available data
    (account younger than the window), so no statement can be generated."""


def _flex_error_kind(exc: Exception) -> str:
    msg = str(exc).lower()
    if "1018" in msg or "too many requests" in msg:
        return "rate"
    if "1025" in msg or "too many failed attempts" in msg:
        return "locked"
    if "1003" in msg or "not available" in msg:
        return "unavailable"
    return "other"


def _fetch_window(fetch, token, query_id, fd: date | None = None, td: date | None = None,
                  *, verbose, progress, sleep, max_rate_retries: int = 5):
    """Fetch one window, returning the parsed doc. With ``fd``/``td`` omitted the
    query's own configured period is used (handy for probing the latest available
    statement date).

    Survives the Flex responses that otherwise abort a multi-window pull: Code 1018
    (rate limit) is retried with exponential backoff; Code 1003 (range predates
    available data) is raised as ``_Unavailable``; Code 1025 (token locked after
    repeated failures) becomes an actionable error. Anything else propagates."""
    override = {"from_date": fd.strftime("%Y%m%d"), "to_date": td.strftime("%Y%m%d")} if (fd and td) else {}
    wait = max(8.0, sleep)
    last: Exception | None = None
    for _ in range(max_rate_retries):
        try:
            return fetch(token, query_id, verbose=verbose, **override)
        except RuntimeError as exc:
            last = exc
            kind = _flex_error_kind(exc)
            if kind == "rate":
                if progress:
                    progress(f"Flex rate limit hit; backing off {wait:.0f}s…")
                time.sleep(wait)
                wait = min(wait * 2, 60.0)
                continue
            if kind == "unavailable":
                raise _Unavailable() from exc
            if kind == "locked":
                raise RuntimeError(
                    "Flex temporarily locked the token after repeated failed "
                    "requests (Code 1025). Wait ~10 minutes, then retry."
                ) from exc
            raise
    raise RuntimeError(f"Flex rate limit persisted after {max_rate_retries} retries: {last}")


def _anchor_end(fetch, token, query_id, *, fallback: date, verbose, progress, sleep):
    """Flex refuses a statement whose end date is today / an unsettled day (it
    1003s), so anchor every window to the latest date the query itself reports.
    Probe with no date override and read the statement's ``toDate``. Returns
    (end_date, parsed_window_or_None); ``parsed`` is absorbed so that last day's
    data is never lost. Falls back to ``fallback`` if the probe can't be read."""
    if progress:
        progress("checking latest available statement date…")
    try:
        doc = _fetch_window(fetch, token, query_id, verbose=verbose,
                            progress=progress, sleep=sleep)
    except _Unavailable:
        return fallback, None
    stmt = doc.find("./FlexStatements/FlexStatement")
    end = fallback
    if stmt is not None and stmt.get("toDate"):
        parsed_end = _parse_date(stmt.get("toDate"))
        if parsed_end:
            end = parsed_end
    return end, parse_window(doc)


def _largest_available(fetch, token, query_id, td: date, *, max_span: int,
                       verbose, progress, sleep):
    """Binary-search the largest span (in days, ending at ``td``) that Flex will
    serve. Flex 1003s whenever the start predates inception and does NOT clamp, so
    the boundary between served and refused spans is the account's first data day.
    Returns (doc, used_from_date) for that largest window, or (None, None)."""
    lo, hi = 0, max_span  # span 0 (single day) assumed servable; max_span known to 1003
    best: tuple[date, ET.Element] | None = None
    while lo <= hi:
        mid = (lo + hi) // 2
        fd = td - timedelta(days=mid)
        if progress:
            progress(f"locating account start (trying {mid + 1}d)…")
        if sleep:
            time.sleep(sleep)
        try:
            doc = _fetch_window(fetch, token, query_id, fd, td,
                                verbose=verbose, progress=progress, sleep=sleep)
            best = (fd, doc)
            lo = mid + 1  # available -> reach further back
        except _Unavailable:
            hi = mid - 1  # too old -> pull in
    return (best[1], best[0]) if best else (None, None)


def build_history(token: str, query_id: str, *, start: date | None = None,
                  max_windows: int = DEFAULT_MAX_WINDOWS, sleep: float = DEFAULT_SLEEP,
                  today: date | None = None, progress=None, verbose: bool = False,
                  fetch=fetch_report, seed: dict | None = None) -> dict:
    """Pull every window and return the normalized history payload.

    Handles real-world Flex friction: accounts younger than 365 days (the first
    window 1003s, so we binary-search the largest servable window and stop) and
    rate limits (backoff in ``_fetch_window``). ``fetch`` is injectable so tests
    run offline. ``seed`` pre-loads keyed accumulators (see ``_seed_from_payload``)
    so an incremental pull keeps older, no-longer-in-window data; when seeded, the
    empty-tail guard is relaxed (a quiet recent window is normal, not a misconfig).
    Raises ValueError if an *unseeded* statement is genuinely empty -- the
    tell-tale sign the query lacks the Trades / NAV sections."""
    def _p(msg: str) -> None:
        if progress:
            progress(msg)

    trades: dict[str, dict] = dict(seed["trades"]) if seed else {}
    nav: dict[str, dict] = dict(seed["nav"]) if seed else {}
    cash: dict[str, dict] = dict(seed["cash"]) if seed else {}
    account = seed.get("account", "") if seed else ""
    empty_streak = 0
    windows_done = 0

    def _absorb(parsed: dict) -> None:
        nonlocal account
        account = account or parsed["account"]
        for t in parsed["trades"]:
            trades[_trade_key(t)] = t
        for row in parsed["nav"]:
            if row["date"]:
                nav[row["date"]] = row
        for c in parsed["cash"]:
            cash[_cash_key(c)] = c

    # Anchor to the latest date the query actually serves. A live run lets the
    # query tell us (Flex 1003s a window ending today/an unsettled day); tests pin
    # ``today`` and skip the probe for determinism.
    if today is not None:
        end = today
    else:
        end, anchor = _anchor_end(
            fetch, token, query_id,
            fallback=datetime.now(timezone.utc).date(),
            verbose=verbose, progress=_p, sleep=sleep)
        if anchor:
            _absorb(anchor)

    plan = windows(end, start=start, max_windows=max_windows)

    for i, (fd, td) in enumerate(plan):
        _p(f"pulling {fd.isoformat()} → {td.isoformat()} (window {i + 1})…")
        try:
            doc = _fetch_window(fetch, token, query_id, fd, td,
                                verbose=verbose, progress=_p, sleep=sleep)
        except _Unavailable:
            if i == 0:
                # The account is younger than a full window: find the largest
                # window Flex will actually serve (≈ inception → today), then stop
                # -- everything older than that simply does not exist.
                _p("range predates account history; locating inception…")
                doc, _used_fd = _largest_available(
                    fetch, token, query_id, td, max_span=(td - fd).days,
                    verbose=verbose, progress=_p, sleep=sleep)
                if doc is None:
                    raise ValueError(
                        "Flex returned no statement even for a single recent day "
                        "(Code 1003). The query may be a Trade-Confirmation flex, "
                        "the wrong type, or not yet propagated — check it in Client "
                        "Portal."
                    )
                _absorb(parse_window(doc))
                windows_done += 1
            # Either way (salvaged first window, or an older window before
            # inception), there is nothing older to fetch.
            break

        parsed = parse_window(doc)
        _absorb(parsed)
        had_data = bool(parsed["trades"] or parsed["nav"])
        windows_done += 1

        # A served-but-empty most-recent window means the query lacks the required
        # sections (not that the account is empty) -- fail loudly. Skip when seeded:
        # an incremental top-up legitimately finds a quiet recent window.
        if i == 0 and seed is None and not had_data and not parsed["cash"]:
            raise ValueError(
                "the most recent window returned no trades and no NAV. The Flex "
                "query is likely missing the 'Trades' and 'Net Asset Value (NAV) "
                "in Base' sections (the latter produces the daily "
                "EquitySummaryInBase rows) — add both to a dedicated Activity Flex "
                "query and set IBKR_FLEX_HISTORY_QUERY_ID."
            )

        if start is None:
            empty_streak = 0 if had_data else empty_streak + 1
            if empty_streak >= 2:
                break  # walked past account inception

        if i < len(plan) - 1 and sleep:
            time.sleep(sleep)

    return normalize(account, trades, nav, cash, end=end, windows_done=windows_done)


def normalize(account: str, trades: dict[str, dict], nav: dict[str, dict],
              cash: dict[str, dict], *, end: date, windows_done: int) -> dict:
    """Assemble the de-duplicated, time-sorted, charting-ready payload."""
    trade_list = sorted(trades.values(), key=lambda t: (t["datetime"], t["symbol"]))
    nav_list = sorted(nav.values(), key=lambda r: r["date"])
    cash_list = sorted(cash.values(), key=lambda c: c["datetime"])

    by_symbol: dict[str, dict] = {}
    for t in trade_list:
        sym = t["symbol"] or "?"
        rec = by_symbol.setdefault(sym, {
            "symbol": sym, "buys": 0, "sells": 0, "n": 0,
            "net_base_cash_flow": 0.0, "realized_pnl": 0.0,
        })
        rec["n"] += 1
        if t["side"] == "BUY":
            rec["buys"] += 1
        else:
            rec["sells"] += 1
        rec["net_base_cash_flow"] = round(rec["net_base_cash_flow"] + t["base_cash_flow"], 2)
        rec["realized_pnl"] = round(rec["realized_pnl"] + t["realized_pnl"], 2)

    trade_dates = [t["date"] for t in trade_list if t["date"]]
    payload = {
        "account": account,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "to_date": end.isoformat(),
        "from_date": (nav_list[0]["date"] if nav_list else (trade_dates[0] if trade_dates else "")),
        "nav_series": nav_list,
        "trades": trade_list,
        "cash_transactions": cash_list,
        "summary": {
            "n_trades": len(trade_list),
            "n_nav_points": len(nav_list),
            "n_cash_transactions": len(cash_list),
            "first_trade": trade_dates[0] if trade_dates else "",
            "last_trade": trade_dates[-1] if trade_dates else "",
            "windows": windows_done,
            "realized_pnl_total": round(sum(t["realized_pnl"] for t in trade_list), 2),
            "by_symbol": sorted(by_symbol.values(), key=lambda r: -r["n"]),
        },
    }
    return enrich_history_payload(payload)


def enrich_history_payload(payload: dict | None) -> dict | None:
    """Idempotently attach the grouping + currency fields the UI needs.

    Ensures every trade carries ``underlying`` / ``is_option`` /
    ``base_realized_pnl`` and every ``by_symbol`` row carries ``asset_class`` /
    ``underlying`` / ``is_option`` / ``label`` / ``currency`` /
    ``base_realized_pnl``, plus a top-level ``base_currency``. Trades span many
    currencies (price + native realized P&L differ per ticker), so the
    cross-ticker ``realized_pnl_total`` is (re)computed in base currency from
    the per-trade fx rate -- summing raw native P&L across USD/CZK/HKD/... is
    nonsense. Caches written before these fields existed are upgraded on read
    with no re-pull; re-running is a no-op. Mutates in place and returns it."""
    if not isinstance(payload, dict):
        return payload
    trades = payload.get("trades") or []
    sym_meta: dict[str, dict] = {}  # symbol -> {asset_class, underlying, description}
    sym_agg: dict[str, dict] = {}   # symbol -> {currency, base_pnl}
    base_ccy = payload.get("base_currency") or ""
    base_pnl_total = 0.0
    for t in trades:
        ac = t.get("asset_class", "")
        if not t.get("underlying"):
            t["underlying"] = derive_underlying(
                ac, t.get("symbol", ""), t.get("description", ""), t.get("underlying_symbol", ""))
        t["is_option"] = (ac or "").upper() in _OPTION_CLASSES
        fx = float(t.get("fx_rate_to_base") or 1) or 1.0
        if "base_realized_pnl" not in t:
            t["base_realized_pnl"] = round(float(t.get("realized_pnl") or 0) * fx, 2)
        base_pnl_total += t["base_realized_pnl"]
        ccy = t.get("currency", "")
        # The base currency is the one that needs no conversion (fx == 1).
        if not base_ccy and ccy and abs(fx - 1.0) < 1e-9:
            base_ccy = ccy
        sym = t.get("symbol", "")
        sym_meta.setdefault(sym, {
            "asset_class": ac, "underlying": t["underlying"],
            "description": t.get("description", "")})
        agg = sym_agg.setdefault(sym, {"currency": ccy, "base_pnl": 0.0,
                                       "bought": 0.0, "sold": 0.0})
        agg["base_pnl"] += t["base_realized_pnl"]
        # Gross cash out (buys) vs in (sells) in base currency, by cash-flow sign
        # so sold - bought == net_base_cash_flow exactly. Both are >= 0.
        cf = float(t.get("base_cash_flow") or 0)
        if cf < 0:
            agg["bought"] += -cf
        else:
            agg["sold"] += cf
    payload["base_currency"] = base_ccy
    for r in (payload.get("summary") or {}).get("by_symbol") or []:
        meta = sym_meta.get(r.get("symbol", ""), {})
        agg = sym_agg.get(r.get("symbol", ""), {})
        r.setdefault("asset_class", meta.get("asset_class", ""))
        if not r.get("underlying"):
            r["underlying"] = meta.get("underlying") or r.get("symbol", "")
        r["is_option"] = (r.get("asset_class") or "").upper() in _OPTION_CLASSES
        if not r.get("label"):
            desc = meta.get("description", "")
            r["label"] = desc if (r["is_option"] and desc) else r.get("symbol", "")
        if not r.get("currency"):
            r["currency"] = agg.get("currency", "")
        if "base_realized_pnl" not in r:
            r["base_realized_pnl"] = round(float(agg.get("base_pnl", 0.0)), 2)
        if "bought_base" not in r:
            r["bought_base"] = round(float(agg.get("bought", 0.0)), 2)
        if "sold_base" not in r:
            r["sold_base"] = round(float(agg.get("sold", 0.0)), 2)
    if trades:
        payload.setdefault("summary", {})["realized_pnl_total"] = round(base_pnl_total, 2)
    return payload


# --------------------------------------------------------------------------- #
# Incremental update
# --------------------------------------------------------------------------- #
# Re-pull a few days of overlap on every incremental update. Flex can restate a
# trade (corrections, late fills) and settlement dates lag, so the tail of the
# previous pull is not necessarily final. Dedup by IBKR ids makes the overlap free.
DEFAULT_OVERLAP_DAYS = 7


def _seed_from_payload(payload: dict) -> dict:
    """Rebuild the keyed accumulators ``build_history`` uses from a previously
    normalized (and persisted) payload, so an incremental pull can extend it
    instead of re-walking the whole account."""
    trades = {_trade_key(t): t for t in payload.get("trades", [])}
    nav = {r["date"]: r for r in payload.get("nav_series", []) if r.get("date")}
    cash = {_cash_key(c): c for c in payload.get("cash_transactions", [])}
    return {"trades": trades, "nav": nav, "cash": cash,
            "account": payload.get("account", "")}


def _covered_through(payload: dict) -> date | None:
    """The last date the cached payload is known to cover (its ``to_date``, with
    NAV / last-trade dates as fallbacks)."""
    for key in ("to_date",):
        if payload.get(key):
            d = _parse_date(payload[key])
            if d:
                return d
    candidates = [r["date"] for r in payload.get("nav_series", []) if r.get("date")]
    candidates += [t["date"] for t in payload.get("trades", []) if t.get("date")]
    parsed = [d for d in (_parse_date(c) for c in candidates) if d]
    return max(parsed) if parsed else None


def extend_history(existing: dict | None, token: str, query_id: str, *,
                   overlap_days: int = DEFAULT_OVERLAP_DAYS, sleep: float = DEFAULT_SLEEP,
                   today: date | None = None, progress=None, verbose: bool = False,
                   fetch=fetch_report) -> dict:
    """Top up a persisted history with only the days since it was last covered.

    With no ``existing`` payload (first ever run) this is just a full
    ``build_history``. Otherwise it seeds the accumulators with the cached data and
    fetches a short window from ``last_covered - overlap_days`` to the latest
    available day -- typically a single Flex request -- then merges and renormalizes.
    The returned payload carries a ``summary.update`` block describing what changed."""
    def _p(msg: str) -> None:
        if progress:
            progress(msg)

    covered = _covered_through(existing) if existing else None
    if not existing or covered is None:
        _p("no usable cache; pulling full history…")
        return build_history(token, query_id, sleep=sleep, today=today,
                             progress=progress, verbose=verbose, fetch=fetch)

    since = covered - timedelta(days=max(0, overlap_days))
    before = existing.get("summary", {})
    _p(f"updating from {since.isoformat()} (cache covered through {covered.isoformat()})…")
    payload = build_history(
        token, query_id, start=since, sleep=sleep, today=today,
        progress=progress, verbose=verbose, fetch=fetch,
        seed=_seed_from_payload(existing),
    )

    after = payload.get("summary", {})
    payload.setdefault("summary", {})["update"] = {
        "mode": "incremental",
        "covered_from": since.isoformat(),
        "previous_to_date": existing.get("to_date", ""),
        "new_trades": max(0, after.get("n_trades", 0) - before.get("n_trades", 0)),
        "new_nav_points": max(0, after.get("n_nav_points", 0) - before.get("n_nav_points", 0)),
        "new_cash_transactions": max(0, after.get("n_cash_transactions", 0)
                                     - before.get("n_cash_transactions", 0)),
    }
    return payload


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Reconstruct full IBKR trade + NAV history via Flex.")
    ap.add_argument("--token", help="Flex Web Service token (else env/secrets.env).")
    ap.add_argument("--query-id", help="History Flex query id (else IBKR_FLEX_HISTORY_QUERY_ID/QUERY_ID).")
    ap.add_argument("--start", help="Earliest date to pull (YYYY-MM-DD or YYYYMMDD). "
                                    "Default: auto-detect inception by walking back until empty.")
    ap.add_argument("--max-windows", type=int, default=DEFAULT_MAX_WINDOWS,
                    help=f"Safety cap on 365-day windows (default {DEFAULT_MAX_WINDOWS}).")
    ap.add_argument("--sleep", type=float, default=DEFAULT_SLEEP,
                    help=f"Seconds between windows (default {DEFAULT_SLEEP}; Flex is rate-limited).")
    ap.add_argument("--out", help="Write normalized JSON to this file. If it already "
                                  "exists, only new days are fetched and merged in "
                                  "(incremental) unless --full or --start is given.")
    ap.add_argument("--full", action="store_true",
                    help="Force a complete rebuild back to inception, ignoring any "
                         "existing --out cache.")
    ap.add_argument("--json", action="store_true", help="Print normalized JSON to stdout.")
    ap.add_argument("-v", "--verbose", action="store_true", help="Show per-window poll progress.")
    args = ap.parse_args(argv)

    try:
        token, query_id = resolve_history_credentials(args.token, args.query_id)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    progress = (lambda m: print(f"  {m}", file=sys.stderr)) if args.verbose else None

    # Incremental by default: if the cache exists and no explicit full/start was
    # requested, fetch only the days since it was last covered and merge.
    existing = None
    if args.out and not args.full and not args.start:
        from pathlib import Path
        try:
            existing = json.loads(Path(args.out).read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            existing = None

    try:
        if existing:
            payload = extend_history(
                existing, token, query_id,
                sleep=args.sleep, progress=progress, verbose=args.verbose,
            )
        else:
            payload = build_history(
                token, query_id,
                start=_parse_date(args.start),
                max_windows=args.max_windows,
                sleep=args.sleep,
                progress=progress,
                verbose=args.verbose,
            )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — surface a clean message
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    s = payload["summary"]
    print(f"account {payload['account']}: {s['n_trades']} trades, "
          f"{s['n_nav_points']} NAV points, {payload['from_date']} → {payload['to_date']} "
          f"({s['windows']} window(s))", file=sys.stderr)
    upd = s.get("update")
    if upd:
        print(f"  incremental: +{upd['new_trades']} trades, "
              f"+{upd['new_nav_points']} NAV points, "
              f"+{upd['new_cash_transactions']} cash txns since {upd['previous_to_date'] or 'last pull'}",
              file=sys.stderr)

    out = json.dumps(payload, indent=2)
    if args.json:
        print(out)
    if args.out:
        from pathlib import Path
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(out + "\n", encoding="utf-8")
        print(f"(written to {args.out})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
