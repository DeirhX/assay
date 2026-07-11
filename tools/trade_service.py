#!/usr/bin/env python3
"""Live trading service (Client Portal Web API) -- GATED.

Extracted from serve.py so the HTTP handlers stay thin wrappers over this and
the security-sensitive logic lives in one separately-testable module. Session
and market-data reads remain available in data-only mode; every order mutation
checks ibkr_trade.trading_enabled(), and placement additionally requires a
matching preview token, an explicit confirm flag, and (for live, non-paper
accounts) IBKR_ALLOW_LIVE. Order details are retained server-side with the
token-bound preview so a tampered client payload cannot place something the
human never previewed.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

import ibkr_trade
import kid_block
import order_peg
import overview
import price_levels
import rebalance
import whatif
from apierror import (
    BadGateway as _BadGateway,
    Conflict as _Conflict,
    Forbidden as _Forbidden,
)
from config import DATA_DIR
from portfolio import (
    HOLDINGS_JSON,
    cash_base,
    clean_symbol,
    normalize_basket,
    option_root,
    parse_occ_symbol,
    position_fx_to_base,
    provider_symbol_for,
)
from store import load as _load, write_json as _write_json

# The planner-staged basket, persisted so the trade desk survives a reload or a
# navigation away and back instead of relying on an in-browser hand-off that a
# refresh silently drops. Gitignored cache, not portfolio truth.
STAGED_BASKET_JSON = DATA_DIR / "cache" / "staged-basket.json"

# A preview binds the exact basket+account via its token, but the *sizes* were
# computed from prices at preview time. After this many seconds the preview is
# stale and place refuses until the caller re-previews. In-memory on purpose: a
# server restart forgets outstanding previews, which fails safe (place then
# demands a fresh preview).
PREVIEW_TTL_S = 600
_preview_issued: dict[str, dict] = {}
_basket_lock = threading.RLock()

# A holdings snapshot older than this shouldn't silently size real orders —
# the CZK->shares math uses its marks. Warning only; the human decides.
STALE_SNAPSHOT_DAYS = 7
OPTION_QUOTE_MAX_AGE_S = ibkr_trade.OPTION_QUOTE_MAX_AGE_SECONDS
OPTION_MULTIPLIER = ibkr_trade.OPTION_MULTIPLIER


def _normalize_basket(trades: Any) -> list[dict]:
    """Canonical stock + short-option legs used for persistence and token hashing.

    Legacy ``{symbol, delta_czk}`` rows remain valid stock legs. Stock rows net by
    symbol; option rows never do. An option ``leg_id`` is its idempotency key:
    restaging the same exact contract replaces the prior row instead of doubling
    the write. Unknown fields are deliberately discarded so the preview hash binds
    only the server-understood order definition.
    """
    if not isinstance(trades, list):
        raise ValueError("trades must be a list")
    stock_rows: list[dict] = []
    option_rows: dict[str, dict] = {}
    for raw in trades:
        if not isinstance(raw, dict):
            raise ValueError("each trade must be an object")
        leg_type = str(raw.get("type") or "stock").strip().lower()
        if leg_type == "stock":
            stock_rows.append(raw)
            continue
        if leg_type not in {"covered_call", "cash_secured_put"}:
            raise ValueError(f"unsupported trade leg type {leg_type!r}")
        option_name = "covered-call" if leg_type == "covered_call" else "cash-secured put"
        right = "C" if leg_type == "covered_call" else "P"
        sym = clean_symbol(raw.get("symbol"))
        if not sym:
            raise ValueError(f"each {option_name} leg needs a symbol")
        try:
            conid_raw = raw.get("conid")
            strike_raw = raw.get("strike")
            contracts_raw = raw.get("contracts")
            if conid_raw is None or strike_raw is None or contracts_raw is None:
                raise TypeError
            conid = int(conid_raw)
            strike = float(strike_raw)
            contracts = int(contracts_raw)
            multiplier = int(raw.get("multiplier") or OPTION_MULTIPLIER)
        except (TypeError, ValueError):
            raise ValueError(
                f"{sym}: {option_name} needs numeric conid, strike, and contracts"
            ) from None
        expiry = str(raw.get("expiry") or "").strip()
        if conid <= 0 or strike <= 0 or contracts <= 0 or multiplier != OPTION_MULTIPLIER or not expiry:
            raise ValueError(f"{sym}: invalid {option_name} contract definition")
        try:
            # Strict ISO parsing also rejects clever garbage before it enters the token.
            import datetime as _dt
            _dt.date.fromisoformat(expiry)
        except ValueError:
            raise ValueError(f"{sym}: {option_name} expiry must be YYYY-MM-DD") from None
        leg_id = str(raw.get("leg_id") or f"{leg_type}:{sym}:{conid}").strip()
        if not leg_id:
            raise ValueError(f"{sym}: {option_name} leg_id is required")
        limit_raw = raw.get("limit_price")
        try:
            limit_price = float(limit_raw) if limit_raw is not None else None
        except (TypeError, ValueError):
            raise ValueError(f"{sym}: {option_name} limit_price must be numeric") from None
        provenance = raw.get("provenance")
        if provenance is None:
            provenance = []
        elif isinstance(provenance, dict):
            provenance = [provenance]
        elif not isinstance(provenance, list) or not all(isinstance(p, dict) for p in provenance):
            raise ValueError(f"{sym}: provenance must be an object or list of objects")
        fx_raw = raw.get("fx_to_base")
        fx_value = float(fx_raw) if isinstance(fx_raw, (int, float)) else None
        option_rows[leg_id] = {
            "type": leg_type,
            "leg_id": leg_id,
            "symbol": sym,
            "route": leg_type,
            "conid": conid,
            "expiry": expiry,
            "strike": round(strike, 6),
            "right": right,
            "contracts": contracts,
            "multiplier": OPTION_MULTIPLIER,
            "limit_price": round(limit_price, 6) if limit_price and limit_price > 0 else None,
            "quote_timestamp": str(raw.get("quote_timestamp") or "") or None,
            "staging_warning": str(raw.get("staging_warning") or "") or None,
            "currency": str(raw.get("currency") or "") or None,
            "fx_to_base": round(fx_value, 8) if fx_value and fx_value > 0 else None,
            "provenance": provenance,
        }

    netted = normalize_basket(stock_rows)
    stock_provenance: dict[str, list[dict]] = {}
    for row in stock_rows:
        sym = clean_symbol(row.get("symbol"))
        prov = row.get("provenance")
        vals = [prov] if isinstance(prov, dict) else prov if isinstance(prov, list) else []
        stock_provenance.setdefault(sym, []).extend(p for p in vals if isinstance(p, dict))
    stocks = [{
        "type": "stock",
        "leg_id": f"stock:{sym}",
        "symbol": sym,
        "delta_czk": round(delta, 2),
        **({"provenance": stock_provenance[sym]} if stock_provenance.get(sym) else {}),
    } for sym, delta in sorted(netted.items()) if abs(delta) >= 0.01]
    return stocks + [option_rows[k] for k in sorted(option_rows)]


def _stock_legs(basket: list[dict]) -> list[dict]:
    """Legacy what-if/build-order shape for the stock subset of a typed basket."""
    return [{"symbol": t["symbol"], "delta_czk": t["delta_czk"]}
            for t in basket if t.get("type") == "stock"]


def _basket_from_raw(raw: Any) -> list[dict]:
    trades = raw.get("trades") if isinstance(raw, dict) else None
    if not isinstance(trades, list):
        return []
    try:
        return _normalize_basket(trades)
    except ValueError:
        return []


def load_basket() -> list[dict]:
    """The last basket staged from the planner, or [] when nothing is staged. A
    corrupt/foreign file reads as empty rather than raising."""
    with _basket_lock:
        return _basket_from_raw(_load(STAGED_BASKET_JSON))


def _basket_revision(basket: list[dict]) -> str:
    """Content identity for the exact normalized order queue."""
    if not basket:
        return ""
    payload = json.dumps(basket, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def basket_state() -> dict:
    """Current queue plus whether that exact revision was projection-reviewed."""
    with _basket_lock:
        raw = _load(STAGED_BASKET_JSON)
        basket = _basket_from_raw(raw)
        revision = _basket_revision(basket)
        reviewed_revision = str(raw.get("reviewed_revision") or "") if isinstance(raw, dict) else ""
        return {
            "trades": basket,
            "revision": revision,
            "reviewed": bool(revision and reviewed_revision == revision),
            "reviewed_at": raw.get("reviewed_at") if isinstance(raw, dict) and reviewed_revision == revision else None,
        }


def save_basket(trades: Any) -> list[dict]:
    """Persist a normalized staged basket so the planner and the trade desk share
    one durable source of truth. A content change invalidates both projection
    review and every outstanding IBKR preview token."""
    with _basket_lock:
        basket = _normalize_basket(trades) if trades else []
        previous = basket_state()
        revision = _basket_revision(basket)
        changed = revision != previous["revision"]
        if changed:
            _preview_issued.clear()
        if basket:
            payload: dict[str, Any] = {"trades": basket, "revision": revision}
            # Saving an identical queue is not a mutation; retain its review.
            if not changed and previous["reviewed"]:
                payload["reviewed_revision"] = revision
                payload["reviewed_at"] = previous["reviewed_at"]
            _write_json(STAGED_BASKET_JSON, payload)
        else:
            try:
                STAGED_BASKET_JSON.unlink()
            except OSError:
                pass
        return basket


def replace_stock_basket(trades: Any) -> list[dict]:
    """Replace planner stock legs while preserving unrelated server-staged options.

    The generic basket endpoint is intentionally stock-only. Re-staging a plain
    rebalance clears prior rebalance option alternatives, while Exit-planner
    options survive. Every option must come through its validating stage endpoint.
    """
    with _basket_lock:
        incoming = _normalize_basket(trades) if trades else []
        if any(leg.get("type") != "stock" for leg in incoming):
            raise ValueError(
                "covered calls must be staged from the Exit plan and cash-secured "
                "puts from Rebalance; "
                "direct basket option legs are rejected"
            )
        existing_options = [
            leg for leg in load_basket()
            if leg.get("type") in {"covered_call", "cash_secured_put"}
            and not any(
                str(prov.get("source") or "") == "rebalance_routes"
                for prov in leg.get("provenance") or []
                if isinstance(prov, dict)
            )
        ]
        return save_basket(existing_options + incoming)


def remove_basket_leg(leg_id: Any) -> list[dict]:
    """Remove one server-known queue leg without accepting a client definition."""
    requested = str(leg_id or "").strip()
    if not requested:
        raise ValueError("remove_leg_id is required")
    with _basket_lock:
        current = load_basket()
        remaining = [
            leg for leg in current if str(leg.get("leg_id") or "") != requested
        ]
        if len(remaining) == len(current):
            raise _Conflict("order queue changed — reload it before removing that leg")
        return save_basket(remaining)


def review_basket(revision: Any) -> dict:
    """Record that the human reviewed the projection for this exact queue."""
    with _basket_lock:
        state = basket_state()
        requested = str(revision or "")
        if not state["trades"]:
            raise ValueError("nothing staged to review")
        if not requested or requested != state["revision"]:
            raise _Conflict("order queue changed since projection — reload Target state and review it again")
        reviewed_at = datetime.now(UTC).isoformat()
        _write_json(STAGED_BASKET_JSON, {
            "trades": state["trades"],
            "revision": state["revision"],
            "reviewed_revision": state["revision"],
            "reviewed_at": reviewed_at,
        })
        return basket_state()


def _reviewed_preview_basket(body: dict) -> list[dict]:
    """Return the staged queue only when this exact revision was reviewed."""
    queue = basket_state()
    submitted = _normalize_basket(body.get("trades"))
    if not submitted:
        raise ValueError("nothing staged to preview")
    if submitted != queue["trades"] or str(body.get("queue_revision") or "") != queue["revision"]:
        raise _Conflict("order queue changed since projection — review the current Target state before previewing")
    if not queue["reviewed"]:
        raise _Conflict("projected portfolio has not been approved — review Target state before previewing")
    return submitted


def _basket_token(account_id: str, basket: list[dict]) -> str:
    """Stable short hash binding a preview to the exact basket + account. The
    place endpoint requires the caller to echo it, so an unreviewed or mutated
    basket (or a switched account) is rejected."""
    payload = json.dumps({"account": account_id, "trades": basket}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _trade_price_map() -> dict[str, dict]:
    """Per-symbol {price, fx_to_base, currency} from the holdings snapshot -- the
    very marks the CZK basket was sized against, so held names size precisely.
    Names you don't hold are absent and get a live price at preview time."""
    holdings = _load(HOLDINGS_JSON) or {}
    out: dict[str, dict] = {}
    for p in holdings.get("positions") or []:
        sym = str(p.get("symbol") or "").strip().upper()
        price = p.get("mark_price")
        if sym and isinstance(price, (int, float)) and price:
            out[sym] = {
                "price": float(price),
                "fx_to_base": position_fx_to_base(p),
                "currency": (p.get("currency") or "").upper(),
            }
    return out


def _position_quantity_map() -> dict[str, float]:
    """Current stock quantity by symbol from the same holdings snapshot used for
    sizing. This is explanation-only context for the preview: it lets the UI
    distinguish an order remainder from the position left after all orders."""
    holdings = _load(HOLDINGS_JSON) or {}
    out: dict[str, float] = {}
    for p in holdings.get("positions") or []:
        if p.get("asset_class") == "OPT":
            continue
        sym = str(p.get("symbol") or "").strip().upper()
        qty = p.get("quantity")
        if sym and isinstance(qty, (int, float)):
            out[sym] = out.get(sym, 0.0) + float(qty)
    return out


def _held_short_call_contracts() -> dict[str, int]:
    """Existing short-call assignment obligations by underlying."""
    holdings = _load(HOLDINGS_JSON) or {}
    out: dict[str, int] = {}
    for p in holdings.get("positions") or []:
        if p.get("asset_class") != "OPT":
            continue
        parsed = parse_occ_symbol(p.get("symbol"))
        qty = _number(p.get("quantity"))
        if not parsed or parsed[0] != "C" or qty >= 0:
            continue
        root = option_root(p.get("symbol"))
        if root:
            out[root] = out.get(root, 0) + int(abs(qty))
    return out


def _held_call_capacity() -> dict[str, dict[str, int]]:
    """Whole-call capacity after contracts already short in the holdings book."""
    shares = _position_quantity_map()
    held_short = _held_short_call_contracts()
    symbols = set(shares) | set(held_short)
    return {
        sym: {
            "current_shares": int(max(0, shares.get(sym, 0))),
            "held_short_calls": held_short.get(sym, 0),
            "capacity_contracts": max(
                0,
                int(max(0, shares.get(sym, 0)) // OPTION_MULTIPLIER) - held_short.get(sym, 0),
            ),
        }
        for sym in symbols
    }


def _live_positions(account_id: str) -> list[dict]:
    rows: list[dict] = []
    for page in range(8):
        chunk = ibkr_trade.positions(account_id, page)
        if not chunk:
            break
        rows.extend(chunk)
        if len(chunk) < 30:
            break
    return rows


def _live_position_call_capacity(
    account_id: str, symbol: str, rows: list[dict] | None = None,
) -> dict[str, int]:
    """Coverage base from CPAPI positions, failing closed when the feed fails."""
    sym = clean_symbol(symbol)
    rows = _live_positions(account_id) if rows is None else rows
    shares = 0
    short_calls = 0
    for row in rows:
        asset = str(row.get("assetClass") or row.get("asset_class") or "").upper()
        desc = str(
            row.get("ticker") or row.get("symbol") or row.get("contractDesc") or ""
        ).strip()
        root = option_root(desc) if asset == "OPT" else clean_symbol(desc.split()[0] if desc else "")
        if root != sym:
            continue
        qty = _number(row.get("position") if row.get("position") is not None else row.get("quantity"))
        if asset == "STK":
            shares += int(qty)
        elif asset == "OPT" and qty < 0:
            right = str(row.get("putOrCall") or row.get("right") or "").upper()
            desc_up = desc.upper()
            # Unknown short option rights fail closed as calls; overstating an
            # obligation is inconvenient, understating it can create a naked call.
            if right in {"C", "CALL"} or " CALL" in desc_up or right not in {"P", "PUT"}:
                short_calls += int(abs(qty))
    return {
        "current_shares": max(0, shares),
        "held_short_calls": short_calls,
        "capacity_contracts": max(0, max(0, shares) // OPTION_MULTIPLIER - short_calls),
    }


def covered_call_capacity(
    symbol: str,
    raw_working: list[dict] | None = None,
    *,
    live_account_id: str | None = None,
) -> dict[str, int]:
    """Shares still available to cover a new call after held and working shorts."""
    sym = clean_symbol(symbol)
    source = (
        {sym: _live_position_call_capacity(live_account_id, sym)}
        if live_account_id else _held_call_capacity()
    )
    base = (source.get(sym) or {
        "current_shares": 0, "held_short_calls": 0, "capacity_contracts": 0,
    }).copy()
    working_short = 0
    for raw in raw_working or []:
        if not isinstance(raw, dict) or _order_terminal(raw) or not _is_working_option(raw):
            continue
        if _working_option_right(raw) == "P":
            continue
        raw_sym = option_root(raw.get("ticker") or raw.get("symbol"))
        if raw_sym != sym or str(raw.get("side") or "").upper() != "SELL":
            continue
        remaining = raw.get("remainingQuantity")
        qty = _number(remaining) if remaining is not None else max(
            0, _number(raw.get("totalSize") or raw.get("quantity")) - _number(raw.get("filledQuantity")),
        )
        working_short += int(qty)
    base["working_short_calls"] = working_short
    base["capacity_contracts"] = max(0, int(base["capacity_contracts"]) - working_short)
    base["available_shares"] = base["capacity_contracts"] * OPTION_MULTIPLIER
    return base


def _held_short_put_collateral(holdings: dict[str, Any] | None = None) -> float:
    """Conservative strike notional already pledged by held short puts."""
    holdings = holdings if holdings is not None else (_load(HOLDINGS_JSON) or {})
    total = 0.0
    for row in holdings.get("positions") or []:
        parsed = parse_occ_symbol(row.get("symbol"))
        if not parsed or parsed[0] != "P":
            continue
        qty = _number(row.get("quantity"))
        if qty >= 0:
            continue
        total += (
            abs(qty) * parsed[1] * OPTION_MULTIPLIER * position_fx_to_base(row)
        )
    return total


def cash_secured_put_capacity(
    holdings: dict[str, Any] | None = None,
) -> dict[str, float]:
    """Snapshot cash remaining after held short-put strike obligations."""
    holdings = holdings if holdings is not None else (_load(HOLDINGS_JSON) or {})
    cash = max(0.0, float(cash_base(holdings) or 0))
    held = _held_short_put_collateral(holdings)
    return {
        "cash_czk": cash,
        "held_short_put_collateral_czk": round(held, 2),
        "available_cash_czk": round(max(0.0, cash - held), 2),
    }


def working_short_put_collateral(raw_orders: list[dict]) -> float:
    """Strike collateral for every non-terminal resting short put in the account."""
    fx_map = _fx_by_currency()
    total = 0.0
    for raw in raw_orders:
        if not isinstance(raw, dict) or _order_terminal(raw):
            continue
        is_put = (
            raw.get("instrument_type") == "cash_secured_put"
            or (_is_working_option(raw) and _working_option_right(raw) == "P")
        )
        if not is_put or str(raw.get("side") or "").upper() != "SELL":
            continue
        parsed = parse_occ_symbol(raw.get("ticker") or raw.get("symbol"))
        strike = _number(raw.get("strike")) or (parsed[1] if parsed else 0.0)
        remaining_raw = raw.get("remainingQuantity")
        remaining = (
            _number(remaining_raw)
            if remaining_raw is not None
            else _number(raw.get("remaining_qty"))
            or max(
                0.0,
                _number(raw.get("totalSize") or raw.get("quantity"))
                - _number(raw.get("filledQuantity")),
            )
        )
        currency = str(raw.get("currency") or "USD").upper()
        total += strike * OPTION_MULTIPLIER * remaining * fx_map.get(currency, 1.0)
    return round(total, 2)


def _is_working_option(raw: dict) -> bool:
    sec = str(raw.get("secType") or raw.get("assetClass") or raw.get("asset_class") or "").upper()
    desc = str(raw.get("orderDesc") or "").upper()
    ticker = raw.get("ticker") or raw.get("symbol")
    return (
        sec == "OPT"
        or " CALL " in f" {desc} "
        or " PUT " in f" {desc} "
        or parse_occ_symbol(ticker) is not None
    )


def _working_option_right(raw: dict) -> str | None:
    right = str(raw.get("putOrCall") or raw.get("right") or "").upper()
    if right in {"C", "CALL"}:
        return "C"
    if right in {"P", "PUT"}:
        return "P"
    parsed = parse_occ_symbol(raw.get("ticker") or raw.get("symbol"))
    if parsed:
        return parsed[0]
    desc = f" {str(raw.get('orderDesc') or '').upper()} "
    if " CALL " in desc:
        return "C"
    if " PUT " in desc:
        return "P"
    return None


def _working_short_calls(working: list[dict], symbol: str) -> int:
    return int(sum(
        _number(row.get("remaining_qty"))
        for row in working
        if row.get("instrument_type") == "covered_call"
        and row.get("symbol") == symbol
        and row.get("side") == "SELL"
    ))


def _working_stock_sell_shares(working: list[dict], symbol: str) -> int:
    return int(sum(
        _number(row.get("remaining_qty"))
        for row in working
        if row.get("instrument_type") == "stock"
        and row.get("symbol") == symbol
        and row.get("side") == "SELL"
    ))


def _fx_by_currency() -> dict[str, float]:
    """currency -> rate-to-base, harvested from held positions. Used to convert a
    live price for a not-yet-held name; absent currencies fall back to 1.0."""
    holdings = _load(HOLDINGS_JSON) or {}
    out: dict[str, float] = {}
    for p in holdings.get("positions") or []:
        ccy = (p.get("currency") or "").upper()
        fx = position_fx_to_base(p)
        if ccy and fx > 0:
            out[ccy] = fx
    return out


def _resolve_trade_account(requested: str | None, accts: list[dict] | None = None) -> str:
    """Pick the account to trade. An explicit request must be visible to the
    session. Otherwise prefer IBKR_TRADE_ACCOUNT_ID, then a paper (DU) account,
    then the first one -- paper-first by construction."""
    # Status already fetched the visible accounts for display; accepting that
    # list avoids a second identical gateway round-trip merely to choose one.
    if accts is None:
        accts = ibkr_trade.accounts()
    ids = [str(a.get("accountId") or a.get("id") or "") for a in accts if isinstance(a, dict)]
    ids = [i for i in ids if i]
    if not ids:
        raise ValueError("no IBKR accounts visible — is the Client Portal Gateway logged in?")
    if requested:
        req = str(requested)
        if req not in ids:
            raise ValueError(f"account {req} is not visible to this session")
        return req
    configured = ibkr_trade._config_value("IBKR_TRADE_ACCOUNT_ID")
    if configured and configured in ids:
        return configured
    paper = [i for i in ids if ibkr_trade.is_paper_account(i)]
    return paper[0] if paper else ids[0]


def _parse_snapshot_price(raw: Any) -> float | None:
    """CPAPI market-data last-price (field 31) is a string that may carry a
    leading letter flag (e.g. 'C'=prior close) or thousands separators. Extract
    the number, or None if there isn't one."""
    if raw in (None, ""):
        return None
    s = str(raw).replace(",", "").lstrip("CHchx ").strip()
    try:
        return float(s)
    except ValueError:
        return None


def _locked_limit(sym: str, side: str) -> float | None:
    """The locked limit price for a basket order's side, looked up SERVER-SIDE
    from the price-level store (never trusted from the client): the active
    (outermost) tranche of the buy ladder for a BUY, of the trim ladder for a
    SELL. With one order per symbol this fills the currently-triggered size at or
    through the market; the size grading lives in the rebalance delta, not here.
    None -> the order stays at market. The level is keyed by provider symbol, so
    resolve the basket symbol the same way the rebalance overlay does."""
    level = price_levels.get(provider_symbol_for(sym))
    return price_levels.limit_price_for(level, side)


# Bound the conid-resolution fan-out so we don't flood the single local gateway
# session; a handful of concurrent secdef lookups is plenty to kill the serial stall.
_PREPARE_MAX_WORKERS = 6


def _drop_blocked_buys(orders: list[dict], blocked: set[str]) -> list[dict]:
    """Strip BUY orders for direct-buy-blocked (KID/PRIIPs) names -- they'd only
    get rejected by the gateway. SELL orders pass through untouched (closing an
    existing position is always allowed). Pure and order-preserving."""
    if not blocked:
        return orders
    return [o for o in orders
            if not (o.get("side") == "BUY" and str(o.get("symbol") or "").upper() in blocked)]


def _prepare_trade_orders(account_id: str, basket: list[dict]) -> tuple[list[dict], list[str]]:
    """Translate canonical stock/short-option legs to server-resolved CPAPI orders."""
    price_map = _trade_price_map()
    position_qty = _position_quantity_map()
    call_symbols = {t["symbol"] for t in basket if t.get("type") == "covered_call"}
    stock_sell_symbols = {
        t["symbol"] for t in basket
        if t.get("type") in (None, "stock") and _number(t.get("delta_czk")) < 0
    }
    snapshot_capacity = _held_call_capacity() if stock_sell_symbols else {}
    call_capacity: dict[str, dict[str, int]] = {
        sym: (snapshot_capacity.get(sym) or {
            "current_shares": 0, "held_short_calls": 0, "capacity_contracts": 0,
        })
        for sym in stock_sell_symbols
    }
    if call_symbols:
        try:
            live_positions = _live_positions(account_id)
        except ibkr_trade.CPAPIError as exc:
            raise ValueError(
                "live positions could not be verified for short-call coverage"
            ) from exc
        call_capacity = {
            **call_capacity,
            **{
                sym: _live_position_call_capacity(account_id, sym, live_positions)
                for sym in call_symbols
            },
        }
    fx_map = _fx_by_currency()
    # resolve_conid is a gateway secdef/search round-trip per symbol (cached in
    # process, but cold on the first preview after a restart). Serially, a big
    # basket over a sluggish gateway spent ~40s here alone -- the bulk of the
    # "Previewing…" hang -- so resolve them in parallel. Writes into the shared
    # conid cache are idempotent and GIL-safe.
    stock_basket = _stock_legs(basket)
    symbols = [t["symbol"] for t in stock_basket]
    workers = min(_PREPARE_MAX_WORKERS, len(symbols) or 1)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        resolved_conids = list(pool.map(lambda s: (s, ibkr_trade.resolve_conid(s)), symbols))
    conids: dict[str, int] = {s: cid for s, cid in resolved_conids if cid is not None}

    missing = [s for s in conids if s not in price_map]
    snap = ibkr_trade.market_snapshot([conids[s] for s in missing]) if missing else {}
    warnings: list[str] = []
    sizing_price: dict[str, dict] = {}

    def price_lookup(sym: str) -> dict | None:
        if sym in price_map:
            sizing_price[sym] = price_map[sym]
            return sizing_price[sym]
        cid = conids.get(sym)
        row = snap.get(cid) if cid is not None else None
        px = _parse_snapshot_price(row.get("31")) if isinstance(row, dict) else None
        if px is None:
            return None
        # Currency for an unheld name isn't reliably in the snapshot; assume USD
        # (the dominant case) and surface it so the human can sanity-check size.
        warnings.append(f"{sym}: not held — sized from a live price assuming USD FX")
        sizing_price[sym] = {"price": px, "fx_to_base": fx_map.get("USD", 1.0)}
        return sizing_price[sym]

    orders, skip_warnings = ibkr_trade.build_orders(
        stock_basket,
        price_lookup=price_lookup,
        conid_lookup=lambda s: conids.get(s),
        account_id=account_id,
        coid_prefix="assay-" + _basket_token(account_id, basket),
        limit_lookup=_locked_limit,
    )
    # Internal estimate metadata is stripped by ibkr_trade._cpapi_order before
    # any gateway call. Reconciliation uses a locked limit when present,
    # otherwise the exact mark/FX pair used above to size the shares.
    for order in orders:
        px = sizing_price.get(str(order.get("symbol") or "")) or {}
        estimate_px = order.get("price") if order.get("orderType") == "LMT" else px.get("price")
        order["_estimate_price"] = _number(estimate_px)
        order["_estimate_fx_to_base"] = _number(px.get("fx_to_base")) or 1.0
        order["_current_position_qty"] = position_qty.get(
            str(order.get("symbol") or "").strip().upper(), 0.0)
        cap = call_capacity.get(str(order.get("symbol") or "").strip().upper())
        if order.get("side") == "SELL" and cap:
            order["_coverage_current_shares"] = cap["current_shares"]
            order["_coverage_held_short_calls"] = cap["held_short_calls"]
    # Convert direct buys of KID-blocked names (US-domiciled ETFs) to options-only:
    # drop the order so we don't emit a guaranteed-reject. The Preview surfaces the
    # excluded names separately (see _trade_preview.options_only). The same filter
    # runs on the place path since orders are re-derived here from the token-bound
    # basket, so a blocked buy can never slip through to placement.
    orders = _drop_blocked_buys(orders, kid_block.blocked_symbols())

    # Covered calls are exact-contract SELL LIMIT orders. Everything executable is
    # resolved again here; staged conid/price are audit context, never authority.
    for leg in [t for t in basket if t.get("type") == "covered_call"]:
        sym = leg["symbol"]
        try:
            resolved_call = ibkr_trade.resolve_executable_call(
                sym,
                leg["expiry"],
                leg["strike"],
                expected_conid=int(leg["conid"]),
                max_quote_age_seconds=OPTION_QUOTE_MAX_AGE_S,
            )
        except ibkr_trade.ExecutableCallError as exc:
            messages = {
                "contract_missing": f"{sym}: exact covered-call contract no longer resolves",
                "contract_changed": f"{sym}: covered-call contract changed — rebuild the Exit plan",
                "quote_invalid": f"{sym}: covered call needs a live, uncrossed IBKR bid/ask quote",
                "quote_stale": f"{sym}: covered-call quote is stale — preview again",
                "limit_invalid": f"{sym}: could not derive a valid option limit price",
            }
            raise ValueError(messages[exc.reason]) from exc
        bid, ask = resolved_call["bid"], resolved_call["ask"]
        limit = resolved_call["limit_price"]
        cap = call_capacity.get(sym, {"current_shares": 0, "held_short_calls": 0, "capacity_contracts": 0})
        if cap["capacity_contracts"] < 1:
            raise ValueError(
                f"{sym}: no covered-call capacity — {cap['current_shares']} shares, "
                f"{cap['held_short_calls']} held short call(s)"
            )
        contracts = int(leg["contracts"])
        order = {
            "instrument_type": "covered_call",
            "leg_id": leg["leg_id"],
            "route": "covered_call",
            "symbol": sym,
            "conid": int(resolved_call["conid"]),
            "orderType": "LMT",
            "side": "SELL",
            "quantity": contracts,
            "tif": "GTC",
            "price": limit,
            "cOID": f"assay-{_basket_token(account_id, basket)}-{int(resolved_call['conid'])}-{contracts}",
            "expiry": resolved_call["expiry"],
            "strike": float(resolved_call["strike"]),
            "right": "C",
            "multiplier": OPTION_MULTIPLIER,
            "contracts": contracts,
            "bid": bid,
            "ask": ask,
            "last": resolved_call.get("last"),
            "quote_timestamp": resolved_call.get("quote_timestamp"),
            "underlying_last": resolved_call.get("underlying_last"),
            "underlying_bid": resolved_call.get("underlying_bid"),
            "underlying_ask": resolved_call.get("underlying_ask"),
            "current_shares": cap["current_shares"],
            "held_short_calls": cap["held_short_calls"],
            "coverage_capacity_contracts": cap["capacity_contracts"],
            "if_assigned_shares": max(0, cap["current_shares"] - contracts * OPTION_MULTIPLIER),
            "premium_credit": round(limit * OPTION_MULTIPLIER * contracts, 2),
            "currency": (price_map.get(sym) or {}).get("currency"),
            "provenance": leg.get("provenance") or [],
            "_estimate_price": limit,
            "_estimate_fx_to_base": 1.0,
            "_current_position_qty": cap["current_shares"],
        }
        orders.append(order)

    # Cash-secured puts are exact-contract SELL LIMIT orders. Their assignment
    # notional plus immediate stock buys must fit snapshot cash conservatively;
    # IBKR preview still performs the authoritative margin check.
    put_legs = [t for t in basket if t.get("type") == "cash_secured_put"]
    put_cash = cash_secured_put_capacity()
    remaining_cash = float(put_cash["available_cash_czk"]) - sum(
        max(0.0, _number(t.get("delta_czk")))
        for t in basket if t.get("type") == "stock"
    )
    total_put_collateral = 0.0
    for leg in put_legs:
        sym = leg["symbol"]
        try:
            resolved_put = ibkr_trade.resolve_executable_put(
                sym,
                leg["expiry"],
                leg["strike"],
                expected_conid=int(leg["conid"]),
                max_quote_age_seconds=OPTION_QUOTE_MAX_AGE_S,
            )
        except ibkr_trade.ExecutablePutError as exc:
            messages = {
                "contract_missing": f"{sym}: exact cash-secured-put contract no longer resolves",
                "contract_changed": f"{sym}: put contract changed — rebuild the Rebalance route",
                "quote_invalid": f"{sym}: cash-secured put needs a live, uncrossed IBKR bid/ask quote",
                "quote_stale": f"{sym}: cash-secured-put quote is stale — preview again",
                "limit_invalid": f"{sym}: could not derive a valid put limit price",
            }
            raise ValueError(messages[exc.reason]) from exc
        contracts = int(leg["contracts"])
        fx = _number(leg.get("fx_to_base")) or _number(
            (price_map.get(sym) or {}).get("fx_to_base")
        ) or fx_map.get("USD", 1.0)
        collateral = float(resolved_put["strike"]) * OPTION_MULTIPLIER * contracts * fx
        total_put_collateral += collateral
        if total_put_collateral > remaining_cash + 0.01:
            raise ValueError(
                f"{sym}: cash-secured puts need {total_put_collateral:,.0f} CZK after stock buys, "
                f"but only {max(0.0, remaining_cash):,.0f} CZK is available"
            )
        limit = resolved_put["limit_price"]
        current_shares = int(position_qty.get(sym, 0.0))
        orders.append({
            "instrument_type": "cash_secured_put",
            "leg_id": leg["leg_id"],
            "route": "cash_secured_put",
            "symbol": sym,
            "conid": int(resolved_put["conid"]),
            "orderType": "LMT",
            "side": "SELL",
            "quantity": contracts,
            "tif": "GTC",
            "price": limit,
            "cOID": (
                f"assay-{_basket_token(account_id, basket)}-"
                f"{int(resolved_put['conid'])}-{contracts}"
            ),
            "expiry": resolved_put["expiry"],
            "strike": float(resolved_put["strike"]),
            "right": "P",
            "multiplier": OPTION_MULTIPLIER,
            "contracts": contracts,
            "bid": resolved_put["bid"],
            "ask": resolved_put["ask"],
            "last": resolved_put.get("last"),
            "quote_timestamp": resolved_put.get("quote_timestamp"),
            "underlying_last": resolved_put.get("underlying_last"),
            "underlying_bid": resolved_put.get("underlying_bid"),
            "underlying_ask": resolved_put.get("underlying_ask"),
            "cash_secured_czk": round(collateral, 2),
            "current_shares": current_shares,
            "if_assigned_shares": current_shares + contracts * OPTION_MULTIPLIER,
            "premium_credit": round(limit * OPTION_MULTIPLIER * contracts, 2),
            "currency": leg.get("currency") or (price_map.get(sym) or {}).get("currency"),
            "provenance": leg.get("provenance") or [],
            "_estimate_price": limit,
            "_estimate_fx_to_base": fx,
            "_current_position_qty": current_shares,
        })
    return orders, warnings + skip_warnings


def _put_cash_requirement(
    orders: list[dict], raw_working: list[dict],
) -> tuple[float, float]:
    """(required, available) cash for immediate buys plus all short-put obligations."""
    available = float(cash_secured_put_capacity()["available_cash_czk"])
    stock_buys = sum(
        _number(order.get("_estimate_price"))
        * _number(order.get("quantity"))
        * (_number(order.get("_estimate_fx_to_base")) or 1.0)
        for order in orders
        if order.get("instrument_type") not in {"covered_call", "cash_secured_put"}
        and order.get("side") == "BUY"
    )
    new_puts = sum(
        _number(order.get("cash_secured_czk"))
        for order in orders if order.get("instrument_type") == "cash_secured_put"
    )
    return stock_buys + new_puts + working_short_put_collateral(raw_working), available


def _trade_status() -> dict:
    """Gateway/session + flag status for the Trade UI. Never raises: an
    unreachable gateway yields a clean 'not connected' shape."""
    status = {
        "trading_enabled": ibkr_trade.trading_enabled(),
        "live_allowed": ibkr_trade.live_allowed(),
        "gateway_base": ibkr_trade.gateway_base(),
        "authenticated": False,
        "connected": False,
        "competing": False,
        "accounts": [],
        "default_account": None,
    }
    try:
        auth = ibkr_trade.auth_status()
    except Exception:  # noqa: BLE001
        auth = {}
    status["authenticated"] = bool(auth.get("authenticated"))
    status["connected"] = bool(auth.get("connected"))
    status["competing"] = bool(auth.get("competing"))
    if status["authenticated"]:
        try:
            out = []
            accts = ibkr_trade.accounts()
            for a in accts:
                aid = str(a.get("accountId") or a.get("id") or "")
                if aid:
                    out.append({"id": aid, "kind": ibkr_trade.account_kind(aid)})
            status["accounts"] = out
            if out:
                try:
                    status["default_account"] = _resolve_trade_account(None, accts)
                except ValueError:
                    pass
        except Exception:  # noqa: BLE001
            pass
    return status


def _order_band_context(model: dict, holdings: dict, after_plan: dict | None) -> dict[str, dict]:
    """Band context for each tradable symbol. Standalone targets use their own
    band; sleeve members point at the aggregate sleeve band and carry scope
    metadata so the UI labels that distinction instead of silently omitting a
    chart. Standalone targets win if a malformed model contains both."""
    if not model or not holdings or not after_plan:
        return {}
    before = {
        (str(r.get("kind")), str(r.get("name"))): r
        for r in rebalance.plan(model, holdings).get("rows", [])
        if r.get("kind") in {"target", "sleeve"}
    }
    out: dict[str, dict] = {}
    rows = after_plan.get("rows", [])
    # Standalone targets first, so they take precedence over accidental overlap.
    for r in rows:
        if r.get("kind") != "target":
            continue
        name = str(r.get("name") or "")
        br = before.get(("target", name))
        out[name] = {
            "low": r.get("low"),
            "high": r.get("high"),
            "before_pct": br.get("current_pct") if br else None,
            "after_pct": r.get("current_pct"),
            "status_after": r.get("status"),
            "scope": "target",
            "scope_name": name,
            "scope_members": [name],
        }
    for r in rows:
        if r.get("kind") != "sleeve":
            continue
        name = str(r.get("name") or "")
        members = [
            str(m.get("symbol") or "").strip().upper()
            for m in r.get("members") or []
            if isinstance(m, dict) and m.get("symbol")
        ]
        br = before.get(("sleeve", name))
        context = {
            "low": r.get("low"),
            "high": r.get("high"),
            "before_pct": br.get("current_pct") if br else None,
            "after_pct": r.get("current_pct"),
            "status_after": r.get("status"),
            "scope": "sleeve",
            "scope_name": name,
            "scope_members": members,
        }
        for symbol in members:
            out.setdefault(symbol, context)
    return out


def _number(raw: Any) -> float:
    try:
        return float(raw or 0)
    except (TypeError, ValueError):
        return 0.0


def _normalized_working_orders(
    raw_orders: list[dict],
    symbols: set[str],
    option_symbols: set[str] | None = None,
    option_conids: set[int] | None = None,
) -> list[dict]:
    """Stable, serializable subset of non-terminal IBKR orders relevant to a
    preview. Remaining quantity is the only quantity that still affects future
    exposure; original/filled quantities are retained for explanation."""
    out: list[dict] = []
    option_symbols = option_symbols or set()
    option_conids = option_conids or set()
    for raw in raw_orders:
        if not isinstance(raw, dict) or _order_terminal(raw):
            continue
        raw_sym = str(raw.get("ticker") or raw.get("symbol") or "").strip().upper()
        try:
            conid_raw = raw.get("conid")
            conid = int(conid_raw) if conid_raw is not None else None
        except (TypeError, ValueError):
            conid = None
        is_option = _is_working_option(raw) or (conid is not None and conid in option_conids)
        option_right = _working_option_right(raw) if is_option else None
        parsed_option = parse_occ_symbol(raw.get("ticker") or raw.get("symbol")) if is_option else None
        sym = option_root(raw_sym) if is_option else clean_symbol(raw_sym)
        if is_option:
            if not sym and conid not in option_conids:
                continue
            if sym not in option_symbols and conid not in option_conids:
                continue
        elif not sym or sym not in symbols:
            continue
        total = _number(raw.get("totalSize") or raw.get("quantity"))
        filled = _number(raw.get("filledQuantity"))
        remaining_raw = raw.get("remainingQuantity")
        remaining = _number(remaining_raw) if remaining_raw is not None else max(0.0, total - filled)
        if remaining <= 0:
            continue
        out.append({
            "order_id": str(raw.get("orderId") or raw.get("order_id") or ""),
            "symbol": sym,
            "instrument_type": (
                "cash_secured_put" if is_option and option_right == "P"
                else "covered_call" if is_option else "stock"
            ),
            "right": option_right,
            "strike": (
                _number(raw.get("strike"))
                or (parsed_option[1] if parsed_option else None)
            ),
            "conid": conid,
            "side": str(raw.get("side") or "").strip().upper(),
            "remaining_qty": remaining,
            "filled_qty": filled,
            "total_qty": total,
            "status": str(raw.get("status") or raw.get("order_status") or ""),
            "order_type": str(raw.get("orderType") or raw.get("order_type") or ""),
            "price": _number(raw.get("price")) or None,
            "tif": str(raw.get("tif") or raw.get("timeInForce") or ""),
        })
    return sorted(out, key=lambda o: (
        o["instrument_type"], o["symbol"], o.get("conid") or 0,
        o["side"], o["order_id"], o["remaining_qty"],
    ))


def _working_fingerprint(working: list[dict]) -> str:
    """Hash only fields whose change alters reconciliation or user intent."""
    rows = [{
        "id": o.get("order_id"),
        "instrument_type": o.get("instrument_type") or "stock",
        "conid": o.get("conid"),
        "right": o.get("right"),
        "strike": o.get("strike"),
        "symbol": o.get("symbol"),
        "side": o.get("side"),
        "remaining": round(_number(o.get("remaining_qty")), 8),
        "status": o.get("status"),
    } for o in working]
    payload = json.dumps(rows, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _reconcile_working_orders(
    proposed: list[dict],
    basket: list[dict],
    working: list[dict],
) -> tuple[list[dict], list[dict], list[dict]]:
    """Return (residual_orders, context, effective_basket).

    Same-side resting quantity reduces a newly proposed order. Any opposite-side
    resting order suppresses the new order entirely: simultaneous opposing orders
    are churn, not a position recommendation, so the human must cancel/modify the
    old intent first. Existing orders are never modified here.
    """
    basket_by_symbol = {
        str(t.get("symbol") or "").upper(): _number(t.get("delta_czk"))
        for t in basket if t.get("type") in (None, "stock")
    }
    basket_by_leg = {str(t.get("leg_id") or ""): t for t in basket}
    working_by_symbol: dict[tuple[str, str], list[dict]] = {}
    for row in working:
        key = (str(row.get("instrument_type") or "stock"), str(row["symbol"]))
        working_by_symbol.setdefault(key, []).append(row)

    residual: list[dict] = []
    contexts: list[dict] = []
    effective: list[dict] = []
    for order in proposed:
        sym = str(order.get("symbol") or "").strip().upper()
        side = str(order.get("side") or "").strip().upper()
        instrument_type = str(order.get("instrument_type") or "stock")
        is_option = instrument_type in {"covered_call", "cash_secured_put"}
        is_call = instrument_type == "covered_call"
        leg_id = str(order.get("leg_id") or f"stock:{sym}")
        proposed_qty = _number(order.get("quantity"))
        symbol_rows = working_by_symbol.get((instrument_type, sym), [])
        rows = ([r for r in symbol_rows if r.get("conid") == order.get("conid")]
                if is_option else symbol_rows)
        same = [r for r in rows if r.get("side") == side]
        opposite = [r for r in rows if r.get("side") and r.get("side") != side]
        same_qty = sum(_number(r.get("remaining_qty")) for r in same)
        original_delta = basket_by_symbol.get(sym, 0.0) if not is_option else 0.0
        unit_base = (
            _number(order.get("_estimate_price"))
            * (_number(order.get("_estimate_fx_to_base")) or 1.0)
        )
        if unit_base <= 0:
            unit_base = abs(original_delta) / proposed_qty if proposed_qty else 0.0
        working_signed_qty = sum(
            _number(r.get("remaining_qty")) * (1 if r.get("side") == "BUY" else -1)
            for r in rows
        )

        if opposite:
            classification = "opposite_side"
            residual_qty = 0.0
            next_step = (
                f"Cancel or modify the existing {opposite[0]['side']} order first, "
                f"then preview {side} again."
            )
        else:
            residual_qty = max(0.0, proposed_qty - same_qty)
            if same_qty <= 0:
                classification = "none"
                next_step = "Review and confirm this new order."
            elif residual_qty <= 0:
                classification = "fully_covered"
                next_step = "No new order needed — monitor the existing working order."
            else:
                classification = "same_side_partial"
                unit = "contract" if is_option else "share"
                next_step = (
                    f"{same_qty:g} {unit}(s) are already working; place "
                    f"{residual_qty:g} more to complete the {proposed_qty:g}-{unit} plan."
                )

        coverage_working = _working_short_calls(symbol_rows, sym) if is_call else 0
        coverage_capacity = int(order.get("coverage_capacity_contracts") or 0) if is_call else 0
        coverage_ok = not is_call or coverage_working + residual_qty <= coverage_capacity
        if is_call and not coverage_ok:
            classification = "coverage_blocked"
            residual_qty = 0.0
            next_step = (
                f"Covered-call capacity is {coverage_capacity:g} contract(s) after held short calls, "
                f"but {coverage_working:g} contract(s) are already working."
            )

        if residual_qty > 0:
            adjusted = dict(order)
            adjusted["quantity"] = int(residual_qty) if residual_qty.is_integer() else residual_qty
            coid = str(adjusted.get("cOID") or "")
            if coid:
                adjusted["cOID"] = f"{coid.rsplit('-', 1)[0]}-{adjusted['quantity']}"
            residual.append(adjusted)

        residual_signed_qty = residual_qty * (1 if side == "BUY" else -1)
        effective_delta = 0.0 if is_option else (working_signed_qty + residual_signed_qty) * unit_base
        current_position_qty = _number(order.get("_current_position_qty"))
        projected_position_qty = (
            current_position_qty if is_option
            else current_position_qty + working_signed_qty + residual_signed_qty
        )
        if not is_option and abs(effective_delta) >= 1:
            effective.append({"symbol": sym, "delta_czk": round(effective_delta, 2)})
        context = {
            "symbol": sym,
            "instrument_type": instrument_type,
            "leg_id": leg_id,
            "conid": order.get("conid"),
            "side": side,
            "classification": classification,
            "proposed_qty": proposed_qty,
            "working_same_qty": same_qty,
            "working_qty": sum(_number(r.get("remaining_qty")) for r in rows),
            "residual_qty": residual_qty,
            "current_position_qty": current_position_qty,
            "projected_position_qty": projected_position_qty,
            "proposed_delta_czk": original_delta,
            "working_delta_czk": round(working_signed_qty * unit_base, 2),
            "residual_delta_czk": round(residual_signed_qty * unit_base, 2),
            "effective_delta_czk": round(effective_delta, 2),
            "working": rows,
            "next_step": next_step,
            "placeable": residual_qty > 0,
        }
        if is_option:
            total_assignment_contracts = int(
                (
                    int(order.get("held_short_calls") or 0) + coverage_working + residual_qty
                    if is_call else same_qty + residual_qty
                )
            )
            current_shares = int(order.get("current_shares") or current_position_qty or 0)
            if_assigned_shares = (
                max(0, current_shares - total_assignment_contracts * OPTION_MULTIPLIER)
                if is_call
                else current_shares + total_assignment_contracts * OPTION_MULTIPLIER
            )
            context.update({
                "expiry": order.get("expiry"),
                "strike": order.get("strike"),
                "right": "C" if is_call else "P",
                "multiplier": OPTION_MULTIPLIER,
                "contracts": proposed_qty,
                "current_shares": current_shares,
                "coverage_capacity_contracts": coverage_capacity,
                "coverage_working_contracts": coverage_working,
                "coverage_ok": coverage_ok,
                "coverage_shares": int(residual_qty * OPTION_MULTIPLIER) if is_call else None,
                "cash_secured_czk": (
                    round(
                        _number(order.get("cash_secured_czk"))
                        * (residual_qty / proposed_qty if proposed_qty else 0),
                        2,
                    )
                    if not is_call else None
                ),
                "if_assigned_shares": if_assigned_shares,
                "premium_credit": round(
                    residual_qty * _number(order.get("price")) * OPTION_MULTIPLIER, 2
                ),
                "currency": order.get("currency"),
                "bid": order.get("bid"),
                "ask": order.get("ask"),
                "last": order.get("last"),
                "quote_timestamp": order.get("quote_timestamp"),
                "provenance": (basket_by_leg.get(leg_id) or {}).get("provenance") or [],
            })
        contexts.append(context)

    # Coverage is per underlying and applies symmetrically: new calls cannot reuse
    # shares, and stock exits cannot sell shares already pledged to held/working
    # calls. If a mixed symbol-level plan is unsafe, block all of its residual legs
    # rather than quietly placing only the share sale.
    coverage_source_by_symbol: dict[str, dict] = {}
    for order in proposed:
        sym = str(order.get("symbol") or "")
        if order.get("instrument_type") == "covered_call":
            coverage_source_by_symbol.setdefault(sym, order)
        elif order.get("side") == "SELL" and order.get("_coverage_current_shares") is not None:
            coverage_source_by_symbol.setdefault(sym, order)
    blocked_symbols: set[str] = set()
    for sym, source in coverage_source_by_symbol.items():
        option_orders = [
            o for o in residual
            if o.get("instrument_type") == "covered_call" and o.get("symbol") == sym
        ]
        option_working_rows = working_by_symbol.get(("covered_call", sym), [])
        stock_working_rows = working_by_symbol.get(("stock", sym), [])
        working_calls = _working_short_calls(option_working_rows, sym)
        future_stock_sells = _working_stock_sell_shares(stock_working_rows, sym) + sum(
            int(_number(o.get("quantity")))
            for o in residual
            if o.get("instrument_type") not in {"covered_call", "cash_secured_put"}
            and o.get("symbol") == sym
            and o.get("side") == "SELL"
        )
        current_shares = int(
            (source.get("current_shares") or 0)
            if source.get("instrument_type") == "covered_call"
            else source.get("_coverage_current_shares") or 0
        )
        held_short_calls = int(
            (source.get("held_short_calls") or 0)
            if source.get("instrument_type") == "covered_call"
            else source.get("_coverage_held_short_calls") or 0
        )
        cap = max(
            0,
            int(max(0, current_shares - future_stock_sells) // OPTION_MULTIPLIER)
            - held_short_calls,
        )
        requested = sum(int(_number(o.get("quantity"))) for o in option_orders)
        if_assigned = max(
            0,
            current_shares - future_stock_sells
            - (held_short_calls + working_calls + requested) * OPTION_MULTIPLIER,
        )
        for context in contexts:
            if context.get("instrument_type") == "covered_call" and context.get("symbol") == sym:
                context["coverage_capacity_contracts"] = cap
                context["future_stock_sell_shares"] = future_stock_sells
                context["if_assigned_shares"] = if_assigned
        required_contracts = held_short_calls + working_calls + requested
        if max(0, current_shares - future_stock_sells) < required_contracts * OPTION_MULTIPLIER:
            blocked_symbols.add(sym)
            for context in contexts:
                if context.get("symbol") != sym:
                    continue
                is_option_context = context.get("instrument_type") == "covered_call"
                context.update({
                    "classification": "coverage_blocked",
                    "residual_qty": 0.0,
                    "residual_delta_czk": 0.0,
                    "coverage_ok": False,
                    "placeable": False,
                    "next_step": (
                        f"{required_contracts} short call contract(s) "
                        f"need {required_contracts * OPTION_MULTIPLIER} "
                        f"shares after planned stock sales, but only "
                        f"{max(0, current_shares - future_stock_sells)} would remain."
                    ),
                })
                if is_option_context:
                    context["premium_credit"] = 0.0
    if blocked_symbols:
        residual = [o for o in residual if o.get("symbol") not in blocked_symbols]
        effective = [t for t in effective if t.get("symbol") not in blocked_symbols]
    return residual, contexts, effective


def _trade_reconnect() -> dict:
    """Best-effort re-establish the brokerage session without a browser round-trip.

    Calls the gateway's ``ssodh/init`` (``ibkr_trade.reauthenticate``), which
    works only while the gateway still holds the SSO cookie from an earlier
    browser login and does NOT clear 2FA -- an SSO that has fully expired still
    needs a real login at the gateway page. The refreshed trade status is
    returned so the UI re-renders its banner in one round-trip; a failure is
    reported in-band as ``reconnect_error`` rather than as a 5xx, because
    'not connected' is exactly the state the user is trying to fix."""
    reconnect_error: str | None = None
    try:
        ibkr_trade.reauthenticate()
    except Exception as exc:  # noqa: BLE001 - a reconnect attempt must never 500
        reconnect_error = str(exc)
    status = _trade_status()
    status["reconnect_error"] = reconnect_error
    return status


def _trade_tickle() -> dict:
    """App-wide read-only keepalive. The brokerage session idles out after a
    few minutes; a periodic tickle holds it warm while Assay is open. The
    tickle response carries the current session booleans, so we return them (no
    second call) and the UI can flip its banner the moment the link drops. Never
    raises: a failed tickle reads as not-authenticated."""
    auth: dict = {}
    try:
        res = ibkr_trade.tickle()
        iserver = res.get("iserver") if isinstance(res, dict) else None
        if isinstance(iserver, dict) and isinstance(iserver.get("authStatus"), dict):
            auth = iserver["authStatus"]
    except Exception:  # noqa: BLE001
        auth = {}
    return {
        "trading_enabled": ibkr_trade.trading_enabled(),
        "authenticated": bool(auth.get("authenticated")),
        "connected": bool(auth.get("connected")),
        "competing": bool(auth.get("competing")),
    }


def _trade_preview(body: dict) -> dict:
    """Resolve + price + size a basket and ask IBKR for its margin/commission
    impact, WITHOUT placing anything. Also returns the local what-if so the two
    can be eyeballed side by side, plus the token the place step must echo."""
    if not ibkr_trade.trading_enabled():
        raise _Forbidden("trading is disabled — set IBKR_TRADING_ENABLED to use the trade desk")
    basket = _reviewed_preview_basket(body)
    account_id = _resolve_trade_account(body.get("account"))
    proposed_orders, warnings = _prepare_trade_orders(account_id, basket)

    relevant_stock_symbols = {
        str(o.get("symbol") or "").strip().upper() for o in proposed_orders
        if o.get("symbol")
        and o.get("instrument_type") not in {"covered_call", "cash_secured_put"}
    }
    relevant_option_symbols = {
        str(o.get("symbol") or "").strip().upper() for o in proposed_orders
        if o.get("symbol")
        and o.get("instrument_type") in {"covered_call", "cash_secured_put"}
    }
    coverage_symbols = relevant_option_symbols | {
        str(o.get("symbol") or "").strip().upper() for o in proposed_orders
        if o.get("symbol")
        and o.get("instrument_type") not in {"covered_call", "cash_secured_put"}
        and o.get("side") == "SELL"
    }
    relevant_option_conids = {
        int(o["conid"]) for o in proposed_orders
        if o.get("instrument_type") in {"covered_call", "cash_secured_put"}
        and o.get("conid") is not None
    }
    working_available = True
    working_error: str | None = None
    try:
        raw_working = ibkr_trade.live_orders()
        working_orders = _normalized_working_orders(
            raw_working,
            relevant_stock_symbols | coverage_symbols,
            coverage_symbols,
            relevant_option_conids,
        )
    except ibkr_trade.CPAPIError as exc:
        # A preview can still show sizing/risk, but must not arm placement when
        # we could not rule out a double trade.
        working_available = False
        working_error = str(exc)
        raw_working = []
        working_orders = []
    if any(
        order.get("instrument_type") == "cash_secured_put"
        for order in proposed_orders
    ) and working_available:
        required_cash, available_cash = _put_cash_requirement(proposed_orders, raw_working)
        if required_cash > available_cash + 0.01:
            raise ValueError(
                f"stock buys plus held, working, and proposed short puts need "
                f"{required_cash:,.0f} CZK, but only {available_cash:,.0f} CZK is available"
            )
    orders, order_context, effective_basket = _reconcile_working_orders(
        proposed_orders, basket, working_orders,
    )
    residual_basket = [
        {"symbol": c["symbol"], "delta_czk": c["residual_delta_czk"]}
        for c in order_context if abs(_number(c.get("residual_delta_czk"))) >= 1
    ]

    ibkr_preview = {}
    if orders:
        try:
            ibkr_preview = ibkr_trade.preview_orders(account_id, orders)
        except ibkr_trade.CPAPIError as exc:
            # Learn any newly-discovered KID/PRIIPs-blocked names so the next
            # preview converts their buys to options-only instead of re-rejecting.
            for sym in getattr(exc, "kid_symbols", None) or []:
                kid_block.mark_blocked(sym, "PRIIPs/KID: no direct buy for EU retail (US-domiciled)")
            raise _BadGateway(str(exc)) from exc

    # Names in the basket the account can't buy directly (US-domiciled / no KID):
    # their buy orders were dropped above; report them so the UI can flag the
    # options-only route rather than silently omitting them.
    blocked = kid_block.blocked_symbols()
    options_only = sorted({str(t["symbol"]).upper() for t in basket
                           if t.get("type") == "stock"
                           if float(t.get("delta_czk") or 0) > 0
                           and str(t.get("symbol") or "").upper() in blocked})

    local = None
    holdings = _load(HOLDINGS_JSON)
    # Preview against the working draft when one exists, so the what-if's band
    # status matches the planner the basket was built in (both now reflect the
    # draft). With no draft this is the live model, exactly as before.
    import target_staging
    model = target_staging.active_model()
    if holdings and model:
        try:
            local = whatif.simulate(holdings, model, effective_basket)
        except ValueError:
            local = None

    # Sizing quality gate: the CZK->shares math leans on the snapshot's marks,
    # so an old snapshot deserves a loud warning before real orders. The age is
    # also returned structured (snapshot_age_days) so the UI can turn it into a
    # soft gate instead of only parsing this prose warning out of a list.
    age = overview.age_days(holdings.get("generated_at")) if isinstance(holdings, dict) else None
    snapshot_stale = age is not None and age > STALE_SNAPSHOT_DAYS
    if snapshot_stale:
        warnings = list(warnings) + [
            f"holdings snapshot is {age} days old — order sizes are computed from "
            f"its marks; resync from IBKR before placing real orders."]

    token = _basket_token(account_id, basket)
    now = time.time()
    # Register the preview and prune expired entries so the map can't grow.
    _preview_issued[token] = {
        "issued_at": now,
        "orders": orders,
        "working_fingerprint": _working_fingerprint(working_orders),
        "working_symbols": sorted(relevant_stock_symbols | coverage_symbols),
        "working_option_symbols": sorted(coverage_symbols),
        "working_option_conids": sorted(relevant_option_conids),
        "working_available": working_available,
    }
    for t in [t for t, rec in _preview_issued.items()
              if now - _number(rec.get("issued_at")) > PREVIEW_TTL_S]:
        _preview_issued.pop(t, None)

    return {
        "account": account_id,
        "kind": ibkr_trade.account_kind(account_id),
        "is_paper": ibkr_trade.is_paper_account(account_id),
        "live_allowed": ibkr_trade.live_allowed(),
        "token": token,
        "preview_ttl_s": PREVIEW_TTL_S,
        "trades": basket,
        "effective_trades": effective_basket,
        "residual_trades": residual_basket,
        "orders": orders,
        "proposed_orders": proposed_orders,
        "order_context": order_context,
        "working_orders_available": working_available,
        "working_orders_error": working_error,
        "warnings": warnings,
        "options_only": options_only,
        "ibkr_preview": ibkr_preview,
        "local_whatif": local,
        "order_bands": _order_band_context(model, holdings, local.get("after") if local else None),
        "snapshot_age_days": age,
        "snapshot_stale": snapshot_stale,
        "stale_after_days": STALE_SNAPSHOT_DAYS,
    }


def _revalidate_covered_call_orders(
    account_id: str, orders: list[dict], working: list[dict],
) -> None:
    """Final place-time contract, quote, and coverage gate for short calls."""
    by_symbol: dict[str, list[dict]] = {}
    for order in orders:
        if order.get("instrument_type") != "covered_call":
            continue
        by_symbol.setdefault(str(order.get("symbol") or "").upper(), []).append(order)
    coverage_symbols = set(by_symbol) | {
        str(order.get("symbol") or "").upper()
        for order in orders
        if order.get("instrument_type") not in {"covered_call", "cash_secured_put"}
        and order.get("side") == "SELL"
    }
    if not coverage_symbols:
        return
    try:
        live_positions = _live_positions(account_id)
    except ibkr_trade.CPAPIError as exc:
        raise _Conflict(
            "live positions could not be rechecked — no covered-call order was placed"
        ) from exc
    capacity = {
        sym: _live_position_call_capacity(account_id, sym, live_positions)
        for sym in coverage_symbols
    }
    for sym in coverage_symbols:
        option_orders = by_symbol.get(sym, [])
        for order in option_orders:
            expiry = str(order.get("expiry") or "")
            strike = _number(order.get("strike"))
            if not expiry or strike <= 0:
                raise _Conflict(f"{sym}: covered-call definition is incomplete — preview again")
            try:
                resolved = ibkr_trade.resolve_executable_call(
                    sym,
                    expiry,
                    strike,
                    expected_conid=int(order.get("conid") or 0),
                    max_quote_age_seconds=OPTION_QUOTE_MAX_AGE_S,
                )
            except ibkr_trade.ExecutableCallError as exc:
                if exc.reason in {"contract_missing", "contract_changed"}:
                    message = f"{sym}: covered-call contract could not be revalidated — preview again"
                elif exc.reason == "quote_invalid":
                    message = f"{sym}: covered-call quote is missing or crossed — no order was placed"
                elif exc.reason == "quote_stale":
                    message = f"{sym}: covered-call quote is stale — no order was placed"
                else:
                    message = f"{sym}: covered-call limit could not be refreshed — no order was placed"
                raise _Conflict(message) from exc
            bid, ask = resolved["bid"], resolved["ask"]
            fresh_limit = resolved["limit_price"]
            old_limit = _number(order.get("price"))
            tick = _number(resolved.get("tick"))
            if tick <= 0:
                tick = ibkr_trade.tick_for_price(resolved.get("rules"), fresh_limit)
            spread = ask - bid
            if old_limit <= 0 or abs(fresh_limit - old_limit) > max(tick, spread) + 1e-9:
                raise _Conflict(
                    f"{sym}: covered-call limit moved from {old_limit:g} to {fresh_limit:g} "
                    "— preview again"
                )
            order.update({
                "price": fresh_limit,
                "bid": resolved.get("bid"),
                "ask": resolved.get("ask"),
                "last": resolved.get("last"),
                "quote_timestamp": resolved.get("quote_timestamp"),
                "premium_credit": round(
                    fresh_limit * OPTION_MULTIPLIER * int(_number(order.get("quantity"))), 2,
                ),
            })
        base = capacity.get(sym) or {}
        future_stock_sells = _working_stock_sell_shares(working, sym) + sum(
            int(_number(order.get("quantity")))
            for order in orders
            if order.get("instrument_type") not in {"covered_call", "cash_secured_put"}
            and str(order.get("symbol") or "").upper() == sym
            and order.get("side") == "SELL"
        )
        working_calls = _working_short_calls(working, sym)
        requested = sum(int(_number(order.get("quantity"))) for order in option_orders)
        required_contracts = int(base.get("held_short_calls") or 0) + working_calls + requested
        if max(0, int(base.get("current_shares") or 0) - future_stock_sells) < (
            required_contracts * OPTION_MULTIPLIER
        ):
            raise _Conflict(
                f"{sym}: short-call coverage changed — "
                f"{required_contracts} "
                f"contract(s) need shares, but only "
                f"{max(0, int(base.get('current_shares') or 0) - future_stock_sells)} "
                "shares would remain after stock sells; preview again"
            )


def _revalidate_cash_secured_put_orders(
    orders: list[dict], working: list[dict], *, raw_working: list[dict] | None = None,
) -> None:
    """Refresh short-put contracts and fail closed if aggregate cash is insufficient."""
    put_orders = [
        order for order in orders
        if order.get("instrument_type") == "cash_secured_put"
    ]
    if not put_orders:
        return
    for order in put_orders:
        sym = str(order.get("symbol") or "").upper()
        try:
            resolved = ibkr_trade.resolve_executable_put(
                sym,
                str(order.get("expiry") or ""),
                _number(order.get("strike")),
                expected_conid=int(order.get("conid") or 0),
                max_quote_age_seconds=OPTION_QUOTE_MAX_AGE_S,
            )
        except ibkr_trade.ExecutablePutError as exc:
            messages = {
                "contract_missing": f"{sym}: cash-secured-put contract no longer resolves",
                "contract_changed": f"{sym}: cash-secured-put contract changed",
                "quote_invalid": f"{sym}: cash-secured-put quote is missing or crossed",
                "quote_stale": f"{sym}: cash-secured-put quote is stale",
                "limit_invalid": f"{sym}: cash-secured-put limit could not be refreshed",
            }
            raise _Conflict(messages.get(exc.reason, f"{sym}: put validation failed") + " — preview again") from exc
        fresh_limit = resolved["limit_price"]
        old_limit = _number(order.get("price"))
        tick = _number(resolved.get("tick")) or ibkr_trade.tick_for_price(
            resolved.get("rules"), fresh_limit,
        )
        spread = float(resolved["ask"]) - float(resolved["bid"])
        if old_limit <= 0 or abs(fresh_limit - old_limit) > max(tick, spread) + 1e-9:
            raise _Conflict(
                f"{sym}: cash-secured-put limit moved from {old_limit:g} "
                f"to {fresh_limit:g} — preview again"
            )
        contracts = int(_number(order.get("quantity")))
        fx = _number(order.get("_estimate_fx_to_base")) or 1.0
        order.update({
            "price": fresh_limit,
            "bid": resolved.get("bid"),
            "ask": resolved.get("ask"),
            "last": resolved.get("last"),
            "quote_timestamp": resolved.get("quote_timestamp"),
            "cash_secured_czk": round(
                float(resolved["strike"]) * OPTION_MULTIPLIER * contracts * fx, 2,
            ),
            "premium_credit": round(fresh_limit * OPTION_MULTIPLIER * contracts, 2),
        })

    capacity = cash_secured_put_capacity()
    available = float(capacity["available_cash_czk"])
    stock_buys = sum(
        _number(order.get("_estimate_price"))
        * _number(order.get("quantity"))
        * (_number(order.get("_estimate_fx_to_base")) or 1.0)
        for order in orders
        if order.get("instrument_type") not in {"covered_call", "cash_secured_put"}
        and order.get("side") == "BUY"
    )
    new_collateral = sum(_number(order.get("cash_secured_czk")) for order in put_orders)
    working_collateral = working_short_put_collateral(
        raw_working if raw_working is not None else working,
    )
    required = stock_buys + new_collateral + working_collateral
    if required > available + 0.01:
        raise _Conflict(
            f"cash coverage changed — stock buys and short puts need {required:,.0f} CZK, "
            f"but only {available:,.0f} CZK is available; preview again"
        )


def _trade_place(body: dict) -> dict:
    """Place a previewed basket. Refuses unless trading is enabled, the caller
    confirmed, the preview token matches the exact basket+account, and (for live
    accounts) live placement is unlocked. Orders come from the token-bound
    server-side preview record, never from the client."""
    if not ibkr_trade.trading_enabled():
        raise _Forbidden("trading is disabled — set IBKR_TRADING_ENABLED to place orders")
    if not body.get("confirm"):
        raise ValueError("placement requires an explicit confirmation")
    basket = _normalize_basket(body.get("trades"))
    if not basket:
        raise ValueError("nothing to place")
    account_id = _resolve_trade_account(body.get("account"))
    expected = _basket_token(account_id, basket)
    if str(body.get("token") or "") != expected:
        raise ValueError("preview token mismatch — re-preview before placing "
                         "(the basket or account changed since the preview)")
    if not ibkr_trade.is_paper_account(account_id) and not ibkr_trade.live_allowed():
        raise _Forbidden("live account placement is locked — validate on paper, "
                         "then set IBKR_ALLOW_LIVE to enable live orders")
    # Freshness (after the authorization gates, which always win): a token is
    # only as good as the prices the preview sized from. Unknown tokens (e.g.
    # after a server restart) read as expired — fail safe.
    issued = _preview_issued.get(expected)
    if issued is None or time.time() - _number(issued.get("issued_at")) > PREVIEW_TTL_S:
        raise ValueError("preview expired — prices and sizes may be stale; "
                         "re-preview the basket before placing")
    if not issued.get("working_available"):
        raise _Conflict(
            "working orders could not be verified during preview — reconnect the "
            "gateway and preview again before placing"
        )
    symbols = set(issued.get("working_symbols") or [])
    option_symbols = set(issued.get("working_option_symbols") or [])
    option_conids = {int(c) for c in issued.get("working_option_conids") or []}
    try:
        fresh_raw_working = ibkr_trade.live_orders()
        fresh_working = _normalized_working_orders(
            fresh_raw_working, symbols, option_symbols, option_conids,
        )
    except ibkr_trade.CPAPIError as exc:
        raise _Conflict(
            "working orders could not be rechecked — no orders were placed; "
            "reconnect the gateway and preview again"
        ) from exc
    if _working_fingerprint(fresh_working) != issued.get("working_fingerprint"):
        raise _Conflict(
            "working orders changed after this preview — no orders were placed; "
            "review the updated orders and preview again"
        )
    # Place exactly the residual set the preview displayed. Re-sizing here could
    # silently restore quantities already covered by resting orders.
    orders = [dict(o) for o in issued.get("orders") or []]
    warnings: list[str] = []
    if not orders:
        raise ValueError("no residual orders remain to place — preview the updated basket")
    _revalidate_covered_call_orders(account_id, orders, fresh_working)
    _revalidate_cash_secured_put_orders(
        orders, fresh_working, raw_working=fresh_raw_working,
    )
    # Calls go first so a partial gateway failure cannot sell the covering
    # shares and then fail before submitting their short call. If a later stock
    # sale fails, an accepted call remains covered by more shares than planned.
    placement_orders = sorted(
        orders,
        key=lambda order: (
            0 if order.get("instrument_type") in {"covered_call", "cash_secured_put"}
            else 1
        ),
    )
    try:
        placed = ibkr_trade.place_orders(account_id, placement_orders)
    except ibkr_trade.CPAPIError as exc:
        raise _BadGateway(str(exc)) from exc
    # Close the loop: the staged basket was just submitted, so stop offering it
    # for re-placement — double-placing the same persisted basket is the worst
    # failure mode of the planner→desk hand-off. Stage a fresh basket any time.
    # The preview token is consumed with it: a re-place needs a re-preview.
    save_basket([])
    _preview_issued.pop(expected, None)
    return {
        "account": account_id,
        "kind": ibkr_trade.account_kind(account_id),
        "is_paper": ibkr_trade.is_paper_account(account_id),
        "orders": orders,
        "warnings": warnings,
        "placed": placed,
        "staged_basket_cleared": True,
    }


# Statuses that mean an order is done and no longer working. IBKR's
# /iserver/account/orders returns recently filled/cancelled orders alongside
# live ones; they can't be pegged or cancelled, so the UI segregates them.
_TERMINAL_STATUSES = frozenset(
    {"filled", "cancelled", "canceled", "expired", "rejected", "apicancelled"}
)


def _order_terminal(o: dict) -> bool:
    st = str(o.get("status") or o.get("order_status") or "").strip().lower()
    return st in _TERMINAL_STATUSES


def _held_avg_cost() -> dict[str, float]:
    """Per-symbol average purchase price (weighted cost basis / share) in the
    instrument's own currency -- the number a SELL order's limit is read against
    to see whether it fills at a gain or a loss.

    Prefers the tax-lot cost basis (exact: per-lot local-currency amounts summed
    over shares); falls back to deriving it from the position row (mark price
    minus per-share unrealized P/L, both local currency) for a name that has no
    lots. The position-level cost_price/cost_basis_money fields are deliberately
    NOT used -- IBKR leaves them null in the Flex position section, so they read
    as a phantom zero cost."""
    holdings = _load(HOLDINGS_JSON) or {}
    cost_sum: dict[str, float] = {}
    qty_sum: dict[str, float] = {}
    for lot in holdings.get("lots") or []:
        sym = str(lot.get("symbol") or "").strip().upper()
        cost = lot.get("cost_basis_money")
        qty = lot.get("quantity")
        if sym and isinstance(cost, (int, float)) and isinstance(qty, (int, float)) and qty:
            cost_sum[sym] = cost_sum.get(sym, 0.0) + float(cost)
            qty_sum[sym] = qty_sum.get(sym, 0.0) + float(qty)
    out: dict[str, float] = {}
    for sym, q in qty_sum.items():
        avg = cost_sum[sym] / q if q else 0.0
        if avg > 0:
            out[sym] = avg
    # Fallback: a name held as a position but with no lots in this snapshot.
    for p in holdings.get("positions") or []:
        sym = str(p.get("symbol") or "").strip().upper()
        if not sym or sym in out:
            continue
        mark, qty, upnl = p.get("mark_price"), p.get("quantity"), p.get("unrealized_pnl")
        if all(isinstance(v, (int, float)) for v in (mark, qty, upnl)) and qty:
            avg = float(mark) - float(upnl) / float(qty)
            if avg > 0:
                out[sym] = avg
    return out


def _attach_avg_cost(orders: list[dict]) -> list[dict]:
    """Fold each working order's average purchase price (from the local holdings
    snapshot, matched by symbol) onto the order so the UI can show a SELL's
    limit-vs-cost gain/loss. A cheap file read -- no gateway round-trip -- so it
    doesn't reintroduce the latency the async quote split just removed."""
    costs = _held_avg_cost()
    if not costs:
        return orders
    for o in orders:
        if _order_terminal(o):
            continue
        sym = str(o.get("ticker") or o.get("symbol") or "").strip().upper()
        cost = costs.get(sym)
        if cost is not None:
            o["avg_cost"] = cost
    return orders


def _trade_quotes(conids: list[int]) -> dict[str, dict]:
    """Live {last,bid,ask} keyed by conid, for the working-orders market cells.

    Fetched by its OWN endpoint rather than folded into ``_trade_orders`` because
    the market-data snapshot is ~as slow as the orders fetch itself (~2s each);
    serving them separately lets the working list paint immediately and the
    quotes stream in a beat later instead of doubling the list's latency.

    Best-effort: a cold/unentitled feed (or a snapshot failure) yields {} so the
    UI simply keeps the 'no quote' state rather than erroring."""
    cids = sorted({int(c) for c in conids if c is not None})
    if not cids:
        return {}
    try:
        snaps = ibkr_trade.market_snapshot(cids)
    except ibkr_trade.CPAPIError:
        return {}
    out: dict[str, dict] = {}
    for cid in cids:
        row = snaps.get(cid)
        if not isinstance(row, dict):
            continue
        q = {
            "last": _parse_snapshot_price(row.get("31")),
            "bid": _parse_snapshot_price(row.get("84")),
            "ask": _parse_snapshot_price(row.get("86")),
        }
        if any(v is not None for v in q.values()):
            out[str(cid)] = q
    return out


def _trade_orders() -> dict:
    try:
        # Fold in the active pegs so the UI can badge which working orders are
        # being kept at the top of book (and offer a Stop) in a single call, plus
        # each order's average purchase price (local holdings snapshot -- cheap)
        # so a SELL can be read against its cost. Quotes are deliberately NOT
        # attached here -- they're a separate, slower snapshot call the client
        # fetches asynchronously (see _trade_quotes).
        orders = _attach_avg_cost(ibkr_trade.live_orders())
        return {"orders": orders, "pegs": order_peg.active_pegs()}
    except ibkr_trade.CPAPIError as exc:
        raise _BadGateway(str(exc)) from exc


def _trade_cancel(body: dict) -> dict:
    if not ibkr_trade.trading_enabled():
        raise _Forbidden("trading is disabled")
    order_id = str(body.get("order_id") or "").strip()
    if not order_id:
        raise ValueError("order_id is required")
    account_id = _resolve_trade_account(body.get("account"))
    try:
        return {"cancelled": ibkr_trade.cancel_order(account_id, order_id)}
    except ibkr_trade.CPAPIError as exc:
        raise _BadGateway(str(exc)) from exc


def _parse_worst_price(raw: Any) -> float | None:
    """Optional worst-acceptable price for a peg: None (use the order's own
    limit), a number, or a numeric string. Anything else is a client error."""
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        raise ValueError("worst_price must be a number")


def _trade_peg_start(body: dict) -> dict:
    """Arm a top-of-book peg on an existing working order. Gating (trading +
    IBKR_AUTO_PEG + live-account rules) lives in order_peg.start_peg; the account
    is resolved server-side exactly like cancel/place so a client can't target
    an account the session can't see."""
    if not ibkr_trade.trading_enabled():
        raise _Forbidden("trading is disabled")
    order_id = str(body.get("order_id") or "").strip()
    if not order_id:
        raise ValueError("order_id is required")
    account_id = _resolve_trade_account(body.get("account"))
    worst_price = _parse_worst_price(body.get("worst_price"))
    kwargs: dict = {}
    if body.get("poll_s") not in (None, ""):
        try:
            kwargs["poll_s"] = float(body["poll_s"])
        except (TypeError, ValueError):
            raise ValueError("poll_s must be a number")
    return {"peg": order_peg.start_peg(order_id, account_id, worst_price=worst_price, **kwargs)}


def _trade_peg_stop(body: dict) -> dict:
    """Stop re-pricing an order (leaves the order resting; does not cancel it)."""
    if not ibkr_trade.trading_enabled():
        raise _Forbidden("trading is disabled")
    order_id = str(body.get("order_id") or "").strip()
    if not order_id:
        raise ValueError("order_id is required")
    return order_peg.stop_peg(order_id)
