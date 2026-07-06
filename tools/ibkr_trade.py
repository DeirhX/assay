#!/usr/bin/env python3
"""Live order execution via the IBKR Client Portal Web API (CPAPI).

Unlike the read-only Flex reader (``ibkr_portfolio.py``), this module CAN place
trades, so it is gated hard everywhere it is wired in (see ``serve.py``).

Architecture: CPAPI is REST/JSON served by a local **Client Portal Gateway**
(a Java program you run yourself, default ``https://localhost:5000/v1/api``). We
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

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from config import TOOLS_SECRETS, config_value as _config_value

SECRETS_FILE = TOOLS_SECRETS
USER_AGENT = "assay-ibkr-trade/1.0 (+stdlib)"
DEFAULT_GATEWAY_BASE = "https://localhost:5000/v1/api"


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


def _request(method: str, endpoint: str, body: dict | None = None) -> Any:
    """Call a CPAPI endpoint by its path (e.g. ``/iserver/auth/status``)."""
    return _http(method, f"{gateway_base()}{endpoint}", body)


# --------------------------------------------------------------------------- #
# Session
# --------------------------------------------------------------------------- #
def auth_status() -> dict:
    """Raw brokerage-session status from the gateway: authenticated, connected,
    competing (another session stole the slot). Empty dict if the call fails so
    callers can render 'not connected' instead of erroring."""
    try:
        res = _request("POST", "/iserver/auth/status")
    except CPAPIError:
        try:
            res = _request("GET", "/iserver/auth/status")
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
    return _request("GET", "/tickle")


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
        if conid in (None, ""):
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
            try:
                edge = float(b.get("lowerEdge"))
                inc = float(b.get("increment"))
            except (TypeError, ValueError):
                continue
            if inc > 0 and edge <= float(price):
                applicable.append((edge, inc))
        if applicable:
            best = max(applicable, key=lambda e: e[0])[1]
    if best is None:
        try:
            inc = float(node.get("increment"))
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
        try:
            delta = float(trade.get("delta_czk"))
        except (TypeError, ValueError):
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
def preview_orders(account_id: str, orders: list[dict]) -> dict:
    """Margin/commission impact of a basket WITHOUT placing it (CPAPI whatif).
    The same discipline as the local what-if simulator, but from IBKR itself."""
    return _request("POST", f"/iserver/account/{urllib.parse.quote(account_id)}/orders/whatif",
                    {"orders": [_cpapi_order(o) for o in orders]})


def place_orders(account_id: str, orders: list[dict], *,
                 max_replies: int = 8, auto_confirm: bool = True) -> list[dict]:
    """Place a basket and clear IBKR's confirmation questions.

    CPAPI answers an order POST with EITHER order acknowledgements OR a list of
    {id, message[...]} confirmation prompts (margin warnings, price caps, etc.)
    that must each be affirmed via /iserver/reply/{id}. We loop, replying
    ``confirmed: true`` until we get order objects back or hit a reply cap.
    ``auto_confirm=False`` returns the first prompt batch unanswered so a caller
    can surface the exact warnings to the human."""
    resp = _request("POST", f"/iserver/account/{urllib.parse.quote(account_id)}/orders",
                    {"orders": [_cpapi_order(o) for o in orders]})
    for _ in range(max_replies):
        prompts = _reply_prompts(resp)
        if not prompts:
            break
        if not auto_confirm:
            return resp if isinstance(resp, list) else [resp]
        reply_id = prompts[0]
        resp = _request("POST", f"/iserver/reply/{urllib.parse.quote(str(reply_id))}",
                        {"confirmed": True})
    return resp if isinstance(resp, list) else [resp]


def _reply_prompts(resp: Any) -> list[str]:
    """Extract pending confirmation ids from an order/reply response. A prompt
    row has an ``id`` and a ``message`` list; an accepted order has order_id /
    order_status instead."""
    out: list[str] = []
    for row in (resp if isinstance(resp, list) else [resp]):
        if isinstance(row, dict) and row.get("id") and row.get("message"):
            out.append(str(row["id"]))
    return out


def live_orders() -> list[dict]:
    res = _request("GET", "/iserver/account/orders")
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
