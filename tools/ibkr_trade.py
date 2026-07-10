#!/usr/bin/env python3
"""Live order execution via the IBKR Client Portal Web API (CPAPI).

Unlike the read-only Flex reader (``ibkr_portfolio.py``), this module CAN place
trades, so it is gated hard everywhere it is wired in (see ``serve.py``).

Architecture: CPAPI is REST/JSON served by a local **Client Portal Gateway**
(a Java program you run yourself, default ``https://127.0.0.1:5000/v1/api``). We
talk to it with stdlib ``urllib`` exactly like the rest of this app talks to
Yahoo/SEC/Flex -- zero third-party dependencies, no async rewrite. The gateway
holds the authenticated session; you log in through its browser page once per
session and clear two-factor auth there. This is a *supervised* path: a human is
present, previews every basket, and confirms every order.

Why CPAPI and not the TWS socket API: CPAPI's request/response model maps onto
this project's synchronous ``http.server`` design and stays stdlib-only. The TWS
API would force a third-party client (``ib_async``) and an async event model.

Safety model (enforced by the caller, restated here so it is not forgotten):
  * Trading is OFF unless ``IBKR_TRADING_ENABLED`` is set.
  * Live accounts are locked unless ``IBKR_ALLOW_LIVE`` is set; paper (``DU...``)
    works without it.
  * A basket must be previewed before it can be placed (preview-token match).
  * Orders are never placed without an explicit human confirmation flag.

Standard library only.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from config import TOOLS_SECRETS, config_value as _config_value

SECRETS_FILE = TOOLS_SECRETS
USER_AGENT = "assay-ibkr-trade/1.0 (+stdlib)"
# Use the IPv4 literal deliberately. On Windows, Python resolves ``localhost`` to
# ::1 first; the gateway listens on IPv4, so every new urllib connection waits
# roughly two seconds for IPv6 to fail before falling back to 127.0.0.1.
DEFAULT_GATEWAY_BASE = "https://127.0.0.1:5000/v1/api"
# Short timeout for the frequent session-status pings (auth_status / tickle). A
# healthy gateway answers these in well under a second; capping them keeps a
# wedged gateway from stalling the Trade view's polling and the exit overlay's
# readiness probe for the full 30s default.
_SESSION_TIMEOUT = 8.0


# --------------------------------------------------------------------------- #
# Config / safety flags
# --------------------------------------------------------------------------- #
# _config_value is now config.config_value (env -> tools/secrets.env -> default),
# re-exported under the old name so trade_service and this module keep working.


def _truthy(val: str) -> bool:
    return val.lower() in ("1", "true", "yes", "on")


def trading_enabled() -> bool:
    """Master switch. Placement is refused unless this is explicitly set."""
    return _truthy(_config_value("IBKR_TRADING_ENABLED", "0"))


def live_allowed() -> bool:
    """Whether placing against a LIVE (non-paper) account is unlocked. Default
    off so paper must be validated first."""
    return _truthy(_config_value("IBKR_ALLOW_LIVE", "0"))


def gateway_base() -> str:
    """Base URL of the local Client Portal Gateway, without a trailing slash."""
    return _config_value("IBKR_GATEWAY_BASE", DEFAULT_GATEWAY_BASE).rstrip("/")


def is_paper_account(account_id: str) -> bool:
    """IBKR paper accounts are prefixed ``DU`` (demo user); live individual
    accounts are ``U`` + digits. Used to keep live placement behind a second
    flag and to drive the prominent paper/live banner."""
    return str(account_id or "").upper().startswith("DU")


def account_kind(account_id: str) -> str:
    return "paper" if is_paper_account(account_id) else "live"


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class CPAPIError(RuntimeError):
    """A gateway request failed. ``status`` is the HTTP code (0 for transport
    errors such as the gateway not running)."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


def _ssl_context() -> ssl.SSLContext:
    """The Client Portal Gateway serves a self-signed cert on localhost. We
    deliberately do not verify it: the endpoint is loopback-only, so there is no
    meaningful MITM surface, and there is no CA for a self-signed localhost cert.
    This context is used ONLY for the gateway, never for public hosts."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _http(method: str, url: str, body: dict | None = None, *, timeout: float = 30.0) -> Any:
    """Single funnel for every gateway call. Returns parsed JSON (or {} for an
    empty body). Raises CPAPIError with a useful message on HTTP/transport
    failure. Isolated so tests can monkeypatch one function."""
    data = None
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:  # noqa: S310 (loopback gateway)
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        raise CPAPIError(f"gateway HTTP {exc.code}: {detail or exc.reason}", status=exc.code) from exc
    except urllib.error.URLError as exc:
        raise CPAPIError(
            f"cannot reach the Client Portal Gateway at {gateway_base()} "
            f"({exc.reason}). Is it running and logged in?",
        ) from exc
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CPAPIError(f"gateway returned non-JSON: {exc}") from exc


def _request(method: str, endpoint: str, body: dict | None = None,
             *, timeout: float = 30.0) -> Any:
    """Call a CPAPI endpoint by its path (e.g. ``/iserver/auth/status``). The
    session-status pings pass a short ``timeout`` so a wedged gateway (socket
    accepted, no answer) degrades to 'not connected' in seconds instead of
    stalling the Trade view's polling and the exit overlay's readiness probe."""
    return _http(method, f"{gateway_base()}{endpoint}", body, timeout=timeout)


# --------------------------------------------------------------------------- #
# Session
# --------------------------------------------------------------------------- #
def auth_status() -> dict:
    """Raw brokerage-session status from the gateway: authenticated, connected,
    competing (another session stole the slot). Empty dict if the call fails so
    callers can render 'not connected' instead of erroring."""
    try:
        res = _request("POST", "/iserver/auth/status", timeout=_SESSION_TIMEOUT)
    except CPAPIError:
        try:
            res = _request("GET", "/iserver/auth/status", timeout=_SESSION_TIMEOUT)
        except CPAPIError:
            return {}
    return res if isinstance(res, dict) else {}


def reauthenticate() -> dict:
    """Re-initialize the brokerage session (after the gateway's periodic
    restart) without a fresh browser login, when the gateway still holds the
    SSO cookie. Does NOT clear 2FA -- that is the browser login's job."""
    return _request("POST", "/iserver/auth/ssodh/init", {"publish": True, "compete": True})


def tickle() -> dict:
    """Keepalive ping. The brokerage session times out after a few idle minutes;
    a periodic tickle keeps it warm during an active rebalancing sitting."""
    return _request("GET", "/tickle", timeout=_SESSION_TIMEOUT)


def logout() -> dict:
    return _request("POST", "/logout")


# --------------------------------------------------------------------------- #
# Accounts
# --------------------------------------------------------------------------- #
def accounts() -> list[dict]:
    """Accounts visible to this session. CPAPI requires hitting this once before
    portfolio endpoints work, so it doubles as that initializer."""
    res = _request("GET", "/portfolio/accounts")
    return res if isinstance(res, list) else []


def positions(account_id: str, page: int = 0) -> list[dict]:
    res = _request("GET", f"/portfolio/{urllib.parse.quote(account_id)}/positions/{page}")
    return res if isinstance(res, list) else []


# --------------------------------------------------------------------------- #
# Instruments (conid resolution)
# --------------------------------------------------------------------------- #
_conid_cache: dict[str, int] = {}


def resolve_conid(symbol: str, *, use_cache: bool = True) -> int | None:
    """Resolve a ticker to its IBKR contract id (conid) -- required to price or
    order anything. Picks the first US stock (STK) match. Cached in-process
    because conids are stable. Returns None when nothing resolves."""
    sym = str(symbol or "").strip().upper()
    if not sym:
        return None
    if use_cache and sym in _conid_cache:
        return _conid_cache[sym]
    res = _request("GET", "/iserver/secdef/search?" + urllib.parse.urlencode({"symbol": sym}))
    rows = res if isinstance(res, list) else []
    conid = _pick_stock_conid(rows, sym)
    if conid is not None:
        _conid_cache[sym] = conid
    return conid


def _pick_stock_conid(rows: list[dict], symbol: str) -> int | None:
    """From secdef/search rows, choose the equity contract. Prefer an exact
    ticker match whose sections include STK; fall back to the first row with a
    conid."""
    best = None
    for row in rows:
        conid = row.get("conid")
        if conid is None or conid == "":
            continue
        sections = row.get("sections") or []
        is_stk = any((s.get("secType") or s.get("secturetype")) == "STK" for s in sections)
        if str(row.get("symbol") or "").upper() == symbol and is_stk:
            return int(conid)
        if best is None:
            best = int(conid)
    return best


def market_snapshot(conids: list[int], fields: tuple[str, ...] = ("31", "84", "86")) -> dict[int, dict]:
    """Last/bid/ask snapshot per conid (field 31 = last price). Best-effort:
    CPAPI sometimes needs two calls to warm up the feed, but for a one-shot
    share-sizing estimate one call is enough; missing prices are simply absent."""
    if not conids:
        return {}
    params = {"conids": ",".join(str(c) for c in conids), "fields": ",".join(fields)}
    res = _request("GET", "/iserver/marketdata/snapshot?" + urllib.parse.urlencode(params))
    out: dict[int, dict] = {}
    for row in (res if isinstance(res, list) else []):
        cid = row.get("conid")
        if cid is not None:
            out[int(cid)] = row
    return out


# --------------------------------------------------------------------------- #
# Option chains (reference data + quotes)
# --------------------------------------------------------------------------- #
# Read-only: pulling a chain needs an authenticated gateway session but NOT
# IBKR_TRADING_ENABLED -- fetching strikes/quotes is not trading. Two tiers of
# data with different entitlement needs:
#   * reference (months/strikes/conids/expiry) -- always available to a logged-in
#     session, no market-data subscription required.
#   * quote (bid/ask/last/IV) -- needs an options market-data (OPRA) subscription;
#     absent without one, so the caller (options_overlay) estimates the premium.
_MD_LAST, _MD_BID, _MD_ASK, _MD_IV = "31", "84", "86", "7283"
# Option-specific fields: 87 = day volume (may arrive as "1.2K"), 7308 = delta,
# 7638 = option open interest. Delta/OI need an options market-data (OPRA)
# subscription; absent without one, in which case the overlay models them.
_MD_VOLUME, _MD_DELTA, _MD_OI = "87", "7308", "7638"
_OPTION_MD_FIELDS = (_MD_LAST, _MD_BID, _MD_ASK, _MD_IV, _MD_VOLUME, _MD_DELTA, _MD_OI)
_COUNT_MULT = {"K": 1e3, "M": 1e6, "B": 1e9}
_MONTH_TOKENS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
                 "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


def _snap_num(value: Any) -> float | None:
    """A float out of a CPAPI market-data field. These arrive as strings that may
    carry a non-numeric price prefix (``C123.4`` = prior close when the market is
    shut) or a percent suffix (``25.3%`` for implied vol, which we return as the
    decimal 0.253). None for an empty/foreign field so a missing quote falls
    through to the estimate."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    pct = s.endswith("%")
    match = re.search(r"-?\d+(?:\.\d+)?", s)
    if not match:
        return None
    num = float(match.group())
    return num / 100.0 if pct else num


def _snap_count(value: Any) -> int | None:
    """A whole count (volume / open interest) out of a CPAPI field, honoring a
    ``K``/``M``/``B`` multiplier suffix (``"1.2K"`` -> ``1200``). None on empty."""
    num = _snap_num(value)
    if num is None:
        return None
    if isinstance(value, str):
        s = value.strip().upper()
        for suf, mult in _COUNT_MULT.items():
            if s.endswith(suf):
                num *= mult
                break
    return int(round(num))


def _fmt_strike(strike: float) -> str:
    """Render a strike the way CPAPI's ``strikes`` list does: ``130`` not
    ``130.0``, but ``130.5`` kept."""
    f = float(strike)
    return str(int(f)) if f.is_integer() else str(f)


def _parse_maturity(raw: Any) -> str | None:
    """CPAPI ``maturityDate`` (``YYYYMMDD``) -> ISO ``YYYY-MM-DD``."""
    try:
        return dt.datetime.strptime(str(raw), "%Y%m%d").date().isoformat()
    except (TypeError, ValueError):
        return None


def _month_key(token: str) -> tuple[int, int] | None:
    """``AUG26`` -> ``(2026, 8)`` for chronological ordering; None if unparseable."""
    t = str(token or "").strip().upper()
    if len(t) < 5:
        return None
    mon, yy = t[:3], t[3:]
    if mon not in _MONTH_TOKENS or not yy.isdigit():
        return None
    year = 2000 + int(yy) if len(yy) == 2 else int(yy)
    return (year, _MONTH_TOKENS.index(mon) + 1)


def _months_by_date(months: list[str], as_of: dt.date | None = None) -> list[str]:
    """Expiry-month tokens from this calendar month onward, soonest first. Past
    months are dropped (a chain never needs an expired series)."""
    today = as_of or dt.datetime.now(dt.timezone.utc).date()
    cur = (today.year, today.month)
    keyed: list[tuple[tuple[int, int], str]] = []
    for m in months:
        k = _month_key(m)
        if k is not None and k >= cur:
            keyed.append((k, m))
    keyed.sort(key=lambda t: t[0])
    return [m for _, m in keyed]


def option_months(symbol: str) -> tuple[int | None, list[str]]:
    """``(underlying_conid, ["AUG26", ...])`` for a symbol's listed options, from
    the same ``secdef/search`` the equity resolver uses. Empty month list when the
    name has no OPT section (common for non-US names)."""
    sym = str(symbol or "").strip().upper()
    if not sym:
        return None, []
    res = _request("GET", "/iserver/secdef/search?" + urllib.parse.urlencode({"symbol": sym}))
    rows = res if isinstance(res, list) else []
    conid = _pick_stock_conid(rows, sym)
    months: list[str] = []
    for row in rows:
        for sec in (row.get("sections") or []):
            if (sec.get("secType") or sec.get("secturetype")) == "OPT":
                raw = str(sec.get("months") or "")
                months = [m.strip().upper() for m in raw.split(";") if m.strip()]
                break
        if months:
            break
    return conid, months


def option_strikes(conid: int, month: str, *, exchange: str = "SMART") -> dict[str, list[float]]:
    """``{"call": [strike, ...], "put": [strike, ...]}`` for one expiry month, from
    ``/iserver/secdef/strikes``. Reference data -- no market-data subscription
    needed. Empty lists on a miss."""
    params = {"conid": str(int(conid)), "sectype": "OPT",
              "month": month.upper(), "exchange": exchange}
    res = _request("GET", "/iserver/secdef/strikes?" + urllib.parse.urlencode(params))
    if not isinstance(res, dict):
        return {"call": [], "put": []}

    def _nums(key: str) -> list[float]:
        return [float(x) for x in (res.get(key) or []) if isinstance(x, (int, float))]

    return {"call": _nums("call"), "put": _nums("put")}


def option_info(conid: int, month: str, strike: float, right: str,
                *, exchange: str = "SMART") -> dict[str, Any] | None:
    """The specific option contract as ``{"conid", "expiry", "strike", "right"}``
    from ``/iserver/secdef/info``, or None if that strike/right doesn't list.
    ``right`` is ``"C"`` or ``"P"``."""
    r = right.upper()[:1]
    params = {"conid": str(int(conid)), "sectype": "OPT", "month": month.upper(),
              "strike": _fmt_strike(strike), "right": r, "exchange": exchange}
    res = _request("GET", "/iserver/secdef/info?" + urllib.parse.urlencode(params))
    rows = res if isinstance(res, list) else ([res] if isinstance(res, dict) else [])
    for row in rows:
        ocid = row.get("conid")
        if ocid is None or ocid == "":
            continue
        return {"conid": int(ocid), "expiry": _parse_maturity(row.get("maturityDate")),
                "strike": float(strike), "right": r}
    return None


def _window_strikes(strikes: list[float], spot: float | None,
                    window_pct: float, per_side: int) -> list[float]:
    """The listed strikes worth resolving: up to ``per_side`` each side of spot,
    within +/-``window_pct``. Without a spot, just a bounded slice off the low end
    (the overlay still finds its nearest-OTM among them)."""
    vals = sorted({float(s) for s in strikes if isinstance(s, (int, float))})
    if not vals:
        return []
    if spot is None or spot <= 0:
        return vals[:per_side * 2]
    lo, hi = spot * (1.0 - window_pct), spot * (1.0 + window_pct)
    below = [s for s in vals if lo <= s <= spot][-per_side:]
    above = [s for s in vals if spot < s <= hi][:per_side]
    return below + above


def _resolve_side(conid: int, month: str, strikes: list[float],
                  right: str) -> tuple[list[dict[str, Any]], str | None]:
    """Resolve each windowed strike to its option contract (conid + expiry).
    Returns the contracts and the expiry ISO date recovered from the first hit."""
    contracts: list[dict[str, Any]] = []
    expiry: str | None = None
    for k in strikes:
        info = option_info(conid, month, k, right)
        if not info:
            continue
        expiry = expiry or info.get("expiry")
        contracts.append(info)
    return contracts, expiry


def _quote_contract(info: dict[str, Any], quotes: dict[int, dict]) -> dict[str, Any]:
    """A chain contract row (Yahoo-shaped) for one resolved option, attaching its
    snapshot quote when present."""
    row = quotes.get(int(info["conid"])) or {}
    return {
        "conid": info["conid"],
        "strike": info["strike"],
        "bid": _snap_num(row.get(_MD_BID)),
        "ask": _snap_num(row.get(_MD_ASK)),
        "last": _snap_num(row.get(_MD_LAST)),
        "implied_vol": _snap_num(row.get(_MD_IV)),
        "delta": _snap_num(row.get(_MD_DELTA)),
        "volume": _snap_count(row.get(_MD_VOLUME)),
        "open_interest": _snap_count(row.get(_MD_OI)),
    }


def _build_expiry(conid: int, month: str, spot: float | None,
                  window_pct: float, per_side: int) -> dict[str, Any] | None:
    """One expiry's ``{expiry, calls, puts}`` block: window the strikes, resolve
    their conids, then one snapshot for the lot. None when nothing resolves."""
    strikes = option_strikes(conid, month)
    call_k = _window_strikes(strikes.get("call") or [], spot, window_pct, per_side)
    put_k = _window_strikes(strikes.get("put") or [], spot, window_pct, per_side)
    if not call_k and not put_k:
        return None
    call_contracts, exp_c = _resolve_side(conid, month, call_k, "C")
    put_contracts, exp_p = _resolve_side(conid, month, put_k, "P")
    expiry = exp_c or exp_p
    if expiry is None:
        return None
    all_conids = [c["conid"] for c in call_contracts + put_contracts]
    quotes = market_snapshot(all_conids, fields=_OPTION_MD_FIELDS) if all_conids else {}
    calls = [_quote_contract(c, quotes) for c in call_contracts]
    puts = [_quote_contract(c, quotes) for c in put_contracts]
    return {
        "expiry": expiry,
        "calls": sorted(calls, key=lambda c: c["strike"]),
        "puts": sorted(puts, key=lambda p: p["strike"]),
    }


def option_chain(symbol: str, *, max_expiries: int = 4, strike_window_pct: float = 0.25,
                 strikes_per_side: int = 6, as_of: dt.date | None = None) -> dict[str, Any] | None:
    """Yahoo-shaped option chain sourced from IBKR CPAPI, or None when the name
    lists no options / the gateway can't resolve it.

    Deliberately targeted, not the full ladder: the nearest ``max_expiries``
    expiries, and only strikes within ``strike_window_pct`` of spot
    (``strikes_per_side`` each way), so the request fan-out stays bounded -- enough
    for the exit overlay's nearest-OTM pick. Reference data (months/strikes/conids)
    needs only a logged-in session; the bid/ask/last/IV quote needs an options
    market-data subscription and is simply absent without one (the overlay then
    estimates the premium off Black-Scholes)."""
    sym = str(symbol or "").strip().upper()
    if not sym:
        return None
    conid, months = option_months(sym)
    if conid is None or not months:
        return None

    # Spot centers the strike window. Underlying last, or bid/ask midpoint.
    snap = market_snapshot([conid], fields=(_MD_LAST, _MD_BID, _MD_ASK))
    urow = snap.get(conid) or {}
    spot = _snap_num(urow.get(_MD_LAST))
    if spot is None:
        bid, ask = _snap_num(urow.get(_MD_BID)), _snap_num(urow.get(_MD_ASK))
        spot = (bid + ask) / 2.0 if bid and ask else None

    expiries: list[dict[str, Any]] = []
    for month in _months_by_date(months, as_of)[:max_expiries]:
        block = _build_expiry(conid, month, spot, strike_window_pct, strikes_per_side)
        if block:
            expiries.append(block)
    if not expiries:
        return None
    expiries.sort(key=lambda e: e["expiry"])
    return {
        "source": "ibkr",
        "symbol": sym,
        # secdef doesn't reliably carry currency here; the overlay uses the
        # position's own currency, so None is fine.
        "currency": None,
        "underlying_price": round(spot, 4) if isinstance(spot, (int, float)) else None,
        "expiries": expiries,
    }


# --------------------------------------------------------------------------- #
# Basket -> orders (the genuinely fiddly currency/shares translation)
# --------------------------------------------------------------------------- #
def shares_for(delta_czk: float, price_in_ccy: float, fx_to_base: float) -> int:
    """Convert a base-currency (CZK) trade delta into a whole-share quantity.

    The staged basket is sized in CZK (see whatif.py); CPAPI orders are in
    shares of the instrument, priced in the instrument's own currency. So:

        price_in_base = price_in_ccy * fx_to_base        (e.g. USD price * USD->CZK)
        shares        = round(delta_czk / price_in_base)

    Sign is preserved (negative = sell). Returns 0 when it cannot size (no/zero
    price), which the caller treats as 'skip with a warning' rather than guess."""
    try:
        price_in_base = float(price_in_ccy) * float(fx_to_base or 1.0)
    except (TypeError, ValueError):
        return 0
    if not price_in_base:
        return 0
    return int(round(float(delta_czk) / price_in_base))


def tick_for_price(rules: Any, price: float, *, default: float = 0.01) -> float:
    """Minimum price increment ("tick") applicable at ``price`` from a CPAPI
    contract-rules payload.

    IBKR quotes increments in price bands: ``incrementRules`` is a list of
    ``{lowerEdge, increment}`` and the tick that applies is the band with the
    greatest ``lowerEdge`` <= ``price``. Falls back to a flat ``increment`` field,
    then to ``default``. Pure and defensive: strings are coerced, malformed rows
    skipped, and the result is never <= 0 (a zero tick would let the peg race)."""
    node = rules if isinstance(rules, dict) else {}
    if isinstance(node.get("rules"), dict):   # accept the whole info-and-rules dict
        node = node["rules"]
    best: float | None = None
    bands = node.get("incrementRules")
    if isinstance(bands, list):
        applicable: list[tuple[float, float]] = []
        for b in bands:
            if not isinstance(b, dict):
                continue
            edge_raw = b.get("lowerEdge")
            inc_raw = b.get("increment")
            if edge_raw is None or inc_raw is None:
                continue
            try:
                edge = float(edge_raw)
                inc = float(inc_raw)
            except (TypeError, ValueError):
                continue
            if inc > 0 and edge <= float(price):
                applicable.append((edge, inc))
        if applicable:
            best = max(applicable, key=lambda e: e[0])[1]
    if best is None:
        inc_raw = node.get("increment")
        try:
            inc = float(inc_raw) if inc_raw is not None else 0.0
            best = inc if inc > 0 else None
        except (TypeError, ValueError):
            best = None
    return best if best and best > 0 else default


def contract_rules(conid: int, *, is_buy: bool = True) -> dict:
    """Order rules for a contract (price-increment bands, order types, ...), via
    ``/iserver/contract/{conid}/info-and-rules``. Best-effort: returns the
    ``rules`` sub-dict when present, else the raw response, else ``{}`` so the
    peg falls back to a default tick instead of erroring."""
    try:
        res = _request(
            "GET",
            f"/iserver/contract/{int(conid)}/info-and-rules?"
            + urllib.parse.urlencode({"isBuy": str(bool(is_buy)).lower()}),
        )
    except CPAPIError:
        return {}
    if not isinstance(res, dict):
        return {}
    rules = res.get("rules")
    return rules if isinstance(rules, dict) else res


def build_orders(
    basket: list[dict],
    *,
    price_lookup: Callable[[str], dict | None],
    conid_lookup: Callable[[str], int | None],
    account_id: str,
    order_type: str = "MKT",
    tif: str = "DAY",
    coid_prefix: str = "assay",
    limit_lookup: Callable[[str, str], float | None] | None = None,
) -> tuple[list[dict], list[str]]:
    """Translate a CZK basket ([{symbol, delta_czk}]) into CPAPI order dicts.

    ``price_lookup(symbol)`` returns {price, fx_to_base} (instrument-currency
    price + that currency's rate to base), or None when unknown. ``conid_lookup``
    resolves the contract id. Returns (orders, warnings); symbols that cannot be
    priced or resolved are skipped and explained in warnings -- never guessed.

    ``limit_lookup(symbol, side)`` is the optional price-trigger hook: when it
    returns a positive price for an order's side (a locked buy-below for a BUY, a
    trim-above for a SELL), that order becomes a LIMIT at that price with
    time-in-force GTC (a limit may not fill the same day). The limit price is in
    the instrument's own currency, matching ``price_lookup`` -- never converted."""
    orders: list[dict] = []
    warnings: list[str] = []
    for trade in basket:
        sym = str(trade.get("symbol") or "").strip().upper()
        delta_raw = trade.get("delta_czk")
        try:
            delta = float(delta_raw) if delta_raw is not None else None
        except (TypeError, ValueError):
            delta = None
        if delta is None:
            warnings.append(f"{sym or '?'}: non-numeric delta_czk, skipped")
            continue
        if not sym or abs(delta) < 1.0:
            continue
        conid = conid_lookup(sym)
        if conid is None:
            warnings.append(f"{sym}: no IBKR contract found, skipped")
            continue
        px = price_lookup(sym)
        if not px or not px.get("price"):
            warnings.append(f"{sym}: no price available to size shares, skipped")
            continue
        qty = shares_for(delta, px["price"], px.get("fx_to_base", 1.0))
        if qty == 0:
            warnings.append(f"{sym}: rounds to 0 shares at the current price, skipped")
            continue
        side = "BUY" if qty > 0 else "SELL"
        order = {
            "symbol": sym,  # for display/audit only; stripped before sending to CPAPI
            "conid": int(conid),
            "orderType": order_type,
            "side": side,
            "quantity": abs(qty),
            "tif": tif,
            "cOID": f"{coid_prefix}-{sym}-{abs(qty)}",
        }
        limit = limit_lookup(sym, side) if limit_lookup else None
        if isinstance(limit, (int, float)) and limit > 0:
            order["orderType"] = "LMT"
            order["price"] = round(float(limit), 4)
            order["tif"] = "GTC"
        orders.append(order)
    return orders, warnings


# CPAPI rejects unknown order keys, so strip our display-only fields (symbol)
# before sending. cOID (client order id, for idempotency) is a supported field.
_CPAPI_ORDER_KEYS = ("conid", "orderType", "side", "quantity", "tif", "price", "cOID")


def _cpapi_order(order: dict) -> dict:
    return {k: order[k] for k in _CPAPI_ORDER_KEYS if k in order}


# --------------------------------------------------------------------------- #
# Preview / place / status / cancel
# --------------------------------------------------------------------------- #
# CPAPI reads a multi-order `orders` array as a bracket/OCA group: every order
# after the first is a *child* that must carry a `parentId` (or `isSingleGroup`
# for OCA). A basket of INDEPENDENT trades has neither, so posting >1 at once
# fails with "parentId parameter is required and not set for child order(s)".
# Both preview and place therefore go one order per request -- the single-order
# array that has always worked -- and recombine the per-order results here.

_MONEY_RE = re.compile(r"-?[\d,]+(?:\.\d+)?")


def _money(text: Any) -> float:
    """Leading number from a CPAPI money string ("23,000 USD", "1.1 USD",
    "1.10 - 1.20 USD"), 0.0 for anything unparseable. A range yields its first
    (lower) figure -- a conservative floor for a summed commission."""
    if isinstance(text, (int, float)):
        return float(text)
    m = _MONEY_RE.search(str(text or ""))
    if not m:
        return 0.0
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return 0.0


def _currency(text: Any) -> str:
    """Trailing 3-letter currency code from a money string ("23,000 USD" -> USD)."""
    tail = str(text or "").strip().split()
    last = tail[-1] if tail else ""
    return last if len(last) == 3 and last.isalpha() else ""


def _money_field(total: float, ccy: str) -> str:
    n = round(total, 2)
    n = int(n) if n == int(n) else n
    return f"{n} {ccy}".strip()


def _whatif_impact(resp: Any) -> dict | None:
    """The margin/commission object from a whatif response (a dict, or the first
    qualifying dict in a list); ``None`` when it carries no impact/error fields."""
    for row in (resp if isinstance(resp, list) else [resp]):
        if isinstance(row, dict) and any(
                k in row for k in ("amount", "initial", "maintenance", "equity", "error")):
            return row
    return None


def _combine_margin(impacts: list[dict], key: str) -> dict | None:
    """Recombine a margin block (initial/maintenance/equity) across per-order
    previews: ``current`` is the shared pre-basket figure, so the basket
    ``change`` is the sum of per-order changes and ``after = current + change``."""
    blocks = [i[key] for i in impacts if isinstance(i.get(key), dict)]
    if not blocks:
        return None
    ccy = next((c for b in blocks for c in [_currency(b.get("current") or b.get("after"))] if c), "")
    current = _money(blocks[0].get("current"))
    change = sum(_money(b.get("change")) for b in blocks)
    return {"current": _money_field(current, ccy),
            "change": _money_field(change, ccy),
            "after": _money_field(current + change, ccy)}


def _aggregate_whatif(impacts: list[dict]) -> dict:
    """Fold per-order whatif previews into one basket-level impact in the shape
    the UI reads: summed order value + commission, recombined margins, joined
    warnings. A single order is returned untouched (exact, as CPAPI gave it)."""
    if len(impacts) == 1:
        return impacts[0]
    amt_ccy = next((c for i in impacts
                    for c in [_currency((i.get("amount") or {}).get("amount"))] if c), "")
    total_amount = sum(_money((i.get("amount") or {}).get("amount")) for i in impacts)
    total_comm = sum(_money((i.get("amount") or {}).get("commission")) for i in impacts)
    warns: list[str] = []
    for i in impacts:
        w = i.get("warn")
        if w and str(w) not in warns:
            warns.append(str(w))
    out: dict[str, Any] = {
        "amount": {"amount": _money_field(total_amount, amt_ccy),
                   "commission": _money_field(total_comm, amt_ccy)},
        "basket_orders": len(impacts),
    }
    for key in ("initial", "maintenance", "equity"):
        block = _combine_margin(impacts, key)
        if block:
            out[key] = block
    if warns:
        out["warn"] = " | ".join(warns)
    return out


# Each whatif is its own gateway round-trip and the CPAPI gateway is often
# sluggish (~2-3s/call), so a big rebalance basket previewed serially crawled --
# 17 orders spent ~45s on "Previewing…". They're independent read-only POSTs, so
# fan them out on a small pool; the wall-clock collapses to ~ceil(N/workers)
# round-trips. Kept modest so we don't hammer the single local gateway session.
_PREVIEW_MAX_WORKERS = 6


def _is_kid_error(raw: str) -> bool:
    """A PRIIPs/KID ineligibility rejection (US-domiciled ETFs and other packaged
    products can't be *sold* to EU retail without an approved KID)."""
    low = raw.lower()
    return "kid" in low or "customer ineligible" in low or "priip" in low


def _explain_whatif_error(raw: str) -> str:
    """Turn a raw CPAPI whatif rejection into something a human can act on.

    The common one for EU retail accounts is the PRIIPs/KID block: IBKR answers
    the whatif with a 500 and a wall of legal text. Collapse it to the actionable
    gist -- crucially noting the options route (the KID rule blocks the broker
    *selling* the product to you, i.e. a direct buy; you can still acquire the
    shares via assignment/exercise). Anything else passes through unchanged."""
    if _is_kid_error(raw):
        return ("IBKR won't let EU retail buy this product's shares directly \u2014 "
                "it lacks an approved KID (PRIIPs rule). You can still get the "
                "exposure via options (sell a put / buy a call and take assignment "
                "or exercise into shares), use a UCITS-domiciled equivalent, or "
                "drop it. Selling or closing an existing position is unaffected.")
    return raw


def preview_orders(account_id: str, orders: list[dict]) -> dict:
    """Margin/commission impact of a basket WITHOUT placing it (CPAPI whatif).
    The same discipline as the local what-if simulator, but from IBKR itself.

    Each order is previewed on its own (a basket can't share one array -- see the
    module note above); the per-order impacts are recombined. Both a per-order
    ``error`` in the body AND a hard gateway rejection (e.g. the PRIIPs/KID block
    on US ETFs, which comes back as a 500) are surfaced -- attributed to the
    symbol and explained -- instead of being silently dropped or aborting the
    whole basket on the first bad order. The previews run in parallel because a
    serial fan-out over a sluggish gateway made a full basket take ~45s."""
    if not orders:
        return {}
    endpoint = f"/iserver/account/{urllib.parse.quote(account_id)}/orders/whatif"

    def _preview_one(order: dict) -> tuple[dict, dict | None, str | None]:
        """(order, impact, raw_error) -- a hard CPAPIError is caught here so one
        bad order can't cancel the whole fan-out; the caller aggregates. The
        error is returned raw so the caller can both classify it (KID?) and
        humanize it."""
        try:
            impact = _whatif_impact(_request("POST", endpoint, {"orders": [_cpapi_order(order)]}))
        except CPAPIError as exc:
            return order, None, str(exc)
        body_err = impact.get("error") if impact else None
        return order, impact, (str(body_err) if body_err else None)

    workers = min(_PREVIEW_MAX_WORKERS, len(orders))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(_preview_one, orders))  # map preserves input order

    impacts: list[dict] = []
    errors: list[str] = []
    kid_symbols: list[str] = []
    for order, impact, raw_err in results:
        if raw_err:
            sym = order.get("symbol")
            errors.append(f"{sym}: {_explain_whatif_error(raw_err)}" if sym else _explain_whatif_error(raw_err))
            if sym and _is_kid_error(raw_err):
                kid_symbols.append(sym)
        elif impact is not None:
            impacts.append(impact)
    if errors:
        exc = CPAPIError("whatif preview rejected: " + "; ".join(errors))
        # Let the service layer learn which names are direct-buy-blocked so it can
        # convert them to options-only next time (kept off ibkr_trade's plate --
        # this module doesn't own the persistent registry).
        exc.kid_symbols = kid_symbols  # type: ignore[attr-defined]
        raise exc
    return _aggregate_whatif(impacts) if impacts else {}


def place_orders(account_id: str, orders: list[dict], *,
                 max_replies: int = 8, auto_confirm: bool = True) -> list[dict]:
    """Place a basket and clear IBKR's confirmation questions.

    Each order is submitted on its own request (a basket can't share one array --
    see the module note above). CPAPI answers an order POST with EITHER order
    acknowledgements OR a list of {id, message[...]} confirmation prompts (margin
    warnings, price caps, etc.) that must each be affirmed via /iserver/reply/{id}.
    We loop, replying ``confirmed: true`` until we get order objects back or hit a
    reply cap. ``auto_confirm=False`` returns the first prompt batch unanswered so
    a caller can surface the exact warnings to the human before committing."""
    endpoint = f"/iserver/account/{urllib.parse.quote(account_id)}/orders"
    placed: list[dict] = []
    for order in orders:
        resp = _request("POST", endpoint, {"orders": [_cpapi_order(order)]})
        for _ in range(max_replies):
            prompts = _reply_prompts(resp)
            if not prompts:
                break
            if not auto_confirm:
                return resp if isinstance(resp, list) else [resp]
            reply_id = prompts[0]
            resp = _request("POST", f"/iserver/reply/{urllib.parse.quote(str(reply_id))}",
                            {"confirmed": True})
        placed.extend(resp if isinstance(resp, list) else [resp])
    return placed


def _reply_prompts(resp: Any) -> list[str]:
    """Extract pending confirmation ids from an order/reply response. A prompt
    row has an ``id`` and a ``message`` list; an accepted order has order_id /
    order_status instead."""
    out: list[str] = []
    for row in (resp if isinstance(resp, list) else [resp]):
        if isinstance(row, dict) and row.get("id") and row.get("message"):
            out.append(str(row["id"]))
    return out


# Signals in a CPAPI error body that the orders subsystem is only half-up right
# after a gateway (re)start / fresh login / idle timeout -- both recover by
# priming the session and retrying once, rather than surfacing a 502:
#   * "query /accounts": a 500 when the iserver session was never initialized
#     with a /iserver/accounts call (distinct from the /portfolio/accounts that
#     accounts() hits).
#   * "no bridge": a 400 when auth/status is healthy but the orders subsystem's
#     backend connection ("bridge") isn't up yet. The gateway establishes it on
#     demand, so a prime (which pings /iserver/accounts) + retry clears it.
_ACCOUNTS_UNPRIMED = "query /accounts"
_NO_BRIDGE = "no bridge"


def _orders_session_recoverable(exc: CPAPIError) -> bool:
    """True if an orders-endpoint failure is a known half-up-session state that
    a prime + single retry should clear, rather than a real error to surface."""
    msg = str(exc)
    return _ACCOUNTS_UNPRIMED in msg or _NO_BRIDGE in msg


def prime_iserver_session() -> None:
    """Initialize the iserver brokerage session so /iserver/account/* endpoints
    answer instead of ``500 {"error":"Please query /accounts first"}``.

    Two distinct post-(re)start states produce that same orders error:
      * the brokerage session is up but was never queried -- a plain
        ``GET /iserver/accounts`` primes it; but
      * only the SSO/web session is up (``/portfolio/*`` works, yet
        ``/iserver/*`` answers ``401 not authenticated``) -- the brokerage
        session must first be brought up with ``POST /iserver/auth/ssodh/init``
        (reauthenticate), which reuses the existing SSO cookie and needs no
        browser 2FA. Only then does querying accounts stick.

    Best-effort throughout: any failure is left for the caller's retry to
    surface as the real error."""
    try:
        if not auth_status().get("authenticated"):
            reauthenticate()
        _request("GET", "/iserver/accounts")
    except CPAPIError:
        pass


def live_orders() -> list[dict]:
    def _fetch() -> Any:
        return _request("GET", "/iserver/account/orders")

    try:
        res = _fetch()
    except CPAPIError as exc:
        # Half-up session right after a gateway (re)start / idle: either it was
        # never primed with /iserver/accounts ("query /accounts"), or the orders
        # bridge isn't connected yet ("no bridge"). Prime and retry once --
        # self-healing, so it recovers without a manual reconnect. Anything else
        # is a real error and propagates as a 502.
        if not _orders_session_recoverable(exc):
            raise
        prime_iserver_session()
        res = _fetch()
    if isinstance(res, dict):
        return res.get("orders") or []
    return res if isinstance(res, list) else []


def cancel_order(account_id: str, order_id: str) -> dict:
    return _request("DELETE",
                    f"/iserver/account/{urllib.parse.quote(account_id)}/order/{urllib.parse.quote(str(order_id))}")


def modify_order(account_id: str, order_id: str, changes: dict, *,
                 max_replies: int = 8, auto_confirm: bool = True) -> list[dict]:
    """Modify a live order in place -- the peg's reprice primitive.

    CPAPI reuses the placement protocol for modifications: a POST to the order's
    own path answers with either order acknowledgements or the same
    ``{id, message[...]}`` confirmation prompts, which we clear by replying
    ``confirmed: true`` (exactly like ``place_orders``). ``changes`` is a partial
    order dict; only the CPAPI-recognized keys are sent (price, orderType, ...).
    Modifying keeps the order id, so it does not needlessly forfeit queue time."""
    body = _cpapi_order(changes)
    resp = _request(
        "POST",
        f"/iserver/account/{urllib.parse.quote(account_id)}/order/{urllib.parse.quote(str(order_id))}",
        body,
    )
    for _ in range(max_replies):
        prompts = _reply_prompts(resp)
        if not prompts:
            break
        if not auto_confirm:
            return resp if isinstance(resp, list) else [resp]
        resp = _request("POST", f"/iserver/reply/{urllib.parse.quote(str(prompts[0]))}",
                        {"confirmed": True})
    return resp if isinstance(resp, list) else [resp]
