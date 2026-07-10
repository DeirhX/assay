#!/usr/bin/env python3
"""Live trading service (Client Portal Web API) -- GATED.

Extracted from serve.py so the HTTP handlers stay thin wrappers over this and
the security-sensitive logic lives in one separately-testable module. Unlike
the read-only Flex path, these helpers can place REAL orders, so every entry
point checks ibkr_trade.trading_enabled(); placement additionally requires a
matching preview token, an explicit confirm flag, and (for live, non-paper
accounts) IBKR_ALLOW_LIVE. Order details are retained server-side with the
token-bound preview so a tampered client payload cannot place something the
human never previewed.
"""

from __future__ import annotations

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
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
from portfolio import HOLDINGS_JSON, normalize_basket, provider_symbol_for
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

# A holdings snapshot older than this shouldn't silently size real orders —
# the CZK->shares math uses its marks. Warning only; the human decides.
STALE_SNAPSHOT_DAYS = 7


def _normalize_basket(trades: Any) -> list[dict]:
    """Canonical, de-duplicated [{symbol, delta_czk}] used for both hashing and
    sizing, sorted so the preview token is stable. Shares
    ``portfolio.normalize_basket`` with the what-if path so the two can never
    disagree on what a basket is. Raises ValueError on malformed input."""
    netted = normalize_basket(trades)
    return [{"symbol": s, "delta_czk": round(d, 2)} for s, d in sorted(netted.items())]


def load_basket() -> list[dict]:
    """The last basket staged from the planner, or [] when nothing is staged. A
    corrupt/foreign file reads as empty rather than raising."""
    raw = _load(STAGED_BASKET_JSON)
    trades = raw.get("trades") if isinstance(raw, dict) else None
    if not isinstance(trades, list):
        return []
    try:
        return _normalize_basket(trades)
    except ValueError:
        return []


def save_basket(trades: Any) -> list[dict]:
    """Persist a normalized staged basket so the planner and the trade desk share
    one durable source of truth. An empty/cleared basket removes the file."""
    basket = _normalize_basket(trades) if trades else []
    if basket:
        _write_json(STAGED_BASKET_JSON, {"trades": basket})
    else:
        try:
            STAGED_BASKET_JSON.unlink()
        except OSError:
            pass
    return basket


def _basket_token(account_id: str, basket: list[dict]) -> str:
    """Stable short hash binding a preview to the exact basket + account. The
    place endpoint requires the caller to echo it, so an unreviewed or mutated
    basket (or a switched account) is rejected."""
    payload = json.dumps({"account": account_id, "trades": basket}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _position_fx_to_base(position: dict) -> float:
    """Read the explicit FX rate, or derive it from the position's paired local
    and base market values. Current Flex snapshots can omit fx_rate_to_base even
    though both values are present; defaulting a USD position to 1 would inflate
    share sizing by roughly USD/CZK."""
    explicit = _number(position.get("fx_rate_to_base"))
    if explicit > 0:
        return explicit
    local_value = _number(position.get("market_value"))
    base_value = _number(position.get("base_market_value"))
    if local_value:
        derived = base_value / local_value
        if derived > 0:
            return derived
    return 1.0


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
                "fx_to_base": _position_fx_to_base(p),
                "currency": (p.get("currency") or "").upper(),
            }
    return out


def _fx_by_currency() -> dict[str, float]:
    """currency -> rate-to-base, harvested from held positions. Used to convert a
    live price for a not-yet-held name; absent currencies fall back to 1.0."""
    holdings = _load(HOLDINGS_JSON) or {}
    out: dict[str, float] = {}
    for p in holdings.get("positions") or []:
        ccy = (p.get("currency") or "").upper()
        fx = _position_fx_to_base(p)
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
    """Translate a token-bound CZK basket into CPAPI order dicts, server-side.
    Resolves conids, prices held names from the snapshot and unheld names from a
    live CPAPI snapshot, then defers the shares math + skips to
    ibkr_trade.build_orders. Returns (orders, warnings)."""
    price_map = _trade_price_map()
    fx_map = _fx_by_currency()
    # resolve_conid is a gateway secdef/search round-trip per symbol (cached in
    # process, but cold on the first preview after a restart). Serially, a big
    # basket over a sluggish gateway spent ~40s here alone -- the bulk of the
    # "Previewing…" hang -- so resolve them in parallel. Writes into the shared
    # conid cache are idempotent and GIL-safe.
    symbols = [t["symbol"] for t in basket]
    workers = min(_PREPARE_MAX_WORKERS, len(symbols) or 1)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        resolved = list(pool.map(lambda s: (s, ibkr_trade.resolve_conid(s)), symbols))
    conids: dict[str, int] = {s: cid for s, cid in resolved if cid is not None}

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
        basket,
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
    # Convert direct buys of KID-blocked names (US-domiciled ETFs) to options-only:
    # drop the order so we don't emit a guaranteed-reject. The Preview surfaces the
    # excluded names separately (see _trade_preview.options_only). The same filter
    # runs on the place path since orders are re-derived here from the token-bound
    # basket, so a blocked buy can never slip through to placement.
    orders = _drop_blocked_buys(orders, kid_block.blocked_symbols())
    return orders, warnings + skip_warnings


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
    """Per-target-name band context so the preview can show each order's effect on
    its band: {SYMBOL: {low, high, before_pct, after_pct, status_after}}. Uses the
    same rebalance.plan the what-if runs on, so the before/after weights can never
    disagree with the rest of the app. Only single-ticker target rows are included
    (a sleeve or untargeted name has no per-symbol band to move within). Empty when
    there's no model/holdings/after-plan to reconcile."""
    if not model or not holdings or not after_plan:
        return {}
    before = {r["name"]: r for r in rebalance.plan(model, holdings).get("rows", [])
              if r.get("kind") == "target"}
    out: dict[str, dict] = {}
    for r in after_plan.get("rows", []):
        if r.get("kind") != "target":
            continue
        name = r.get("name")
        br = before.get(name)
        out[str(name)] = {
            "low": r.get("low"),
            "high": r.get("high"),
            "before_pct": br.get("current_pct") if br else None,
            "after_pct": r.get("current_pct"),
            "status_after": r.get("status"),
        }
    return out


def _number(raw: Any) -> float:
    try:
        return float(raw or 0)
    except (TypeError, ValueError):
        return 0.0


def _normalized_working_orders(raw_orders: list[dict], symbols: set[str]) -> list[dict]:
    """Stable, serializable subset of non-terminal IBKR orders relevant to a
    preview. Remaining quantity is the only quantity that still affects future
    exposure; original/filled quantities are retained for explanation."""
    out: list[dict] = []
    for raw in raw_orders:
        if not isinstance(raw, dict) or _order_terminal(raw):
            continue
        sym = str(raw.get("ticker") or raw.get("symbol") or "").strip().upper()
        if not sym or sym not in symbols:
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
        o["symbol"], o["side"], o["order_id"], o["remaining_qty"],
    ))


def _working_fingerprint(working: list[dict]) -> str:
    """Hash only fields whose change alters reconciliation or user intent."""
    rows = [{
        "id": o.get("order_id"),
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
        for t in basket
    }
    working_by_symbol: dict[str, list[dict]] = {}
    for row in working:
        working_by_symbol.setdefault(str(row["symbol"]), []).append(row)

    residual: list[dict] = []
    contexts: list[dict] = []
    effective: list[dict] = []
    for order in proposed:
        sym = str(order.get("symbol") or "").strip().upper()
        side = str(order.get("side") or "").strip().upper()
        proposed_qty = _number(order.get("quantity"))
        rows = working_by_symbol.get(sym, [])
        same = [r for r in rows if r.get("side") == side]
        opposite = [r for r in rows if r.get("side") and r.get("side") != side]
        same_qty = sum(_number(r.get("remaining_qty")) for r in same)
        original_delta = basket_by_symbol.get(sym, 0.0)
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
                next_step = (
                    f"Existing {side} covers {same_qty:g} shares; confirm only "
                    f"the {residual_qty:g}-share remainder."
                )

        if residual_qty > 0:
            adjusted = dict(order)
            adjusted["quantity"] = int(residual_qty) if residual_qty.is_integer() else residual_qty
            coid = str(adjusted.get("cOID") or "")
            if coid:
                adjusted["cOID"] = f"{coid.rsplit('-', 1)[0]}-{adjusted['quantity']}"
            residual.append(adjusted)

        residual_signed_qty = residual_qty * (1 if side == "BUY" else -1)
        effective_delta = (working_signed_qty + residual_signed_qty) * unit_base
        if abs(effective_delta) >= 1:
            effective.append({"symbol": sym, "delta_czk": round(effective_delta, 2)})
        contexts.append({
            "symbol": sym,
            "side": side,
            "classification": classification,
            "proposed_qty": proposed_qty,
            "working_same_qty": same_qty,
            "working_qty": sum(_number(r.get("remaining_qty")) for r in rows),
            "residual_qty": residual_qty,
            "proposed_delta_czk": original_delta,
            "working_delta_czk": round(working_signed_qty * unit_base, 2),
            "residual_delta_czk": round(residual_signed_qty * unit_base, 2),
            "effective_delta_czk": round(effective_delta, 2),
            "working": rows,
            "next_step": next_step,
            "placeable": residual_qty > 0,
        })
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
    if not ibkr_trade.trading_enabled():
        raise _Forbidden("trading is disabled — set IBKR_TRADING_ENABLED to use the trade desk")
    reconnect_error: str | None = None
    try:
        ibkr_trade.reauthenticate()
    except Exception as exc:  # noqa: BLE001 - a reconnect attempt must never 500
        reconnect_error = str(exc)
    status = _trade_status()
    status["reconnect_error"] = reconnect_error
    return status


def _trade_tickle() -> dict:
    """Keepalive for an open Trade view. The brokerage session idles out after a
    few minutes; a periodic tickle holds it warm during an active sitting. The
    tickle response carries the current session booleans, so we return them (no
    second call) and the UI can flip its banner the moment the link drops. Never
    raises: a failed tickle reads as not-authenticated."""
    if not ibkr_trade.trading_enabled():
        return {"trading_enabled": False, "authenticated": False,
                "connected": False, "competing": False}
    auth: dict = {}
    try:
        res = ibkr_trade.tickle()
        iserver = res.get("iserver") if isinstance(res, dict) else None
        if isinstance(iserver, dict) and isinstance(iserver.get("authStatus"), dict):
            auth = iserver["authStatus"]
    except Exception:  # noqa: BLE001
        auth = {}
    return {
        "trading_enabled": True,
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
    basket = _normalize_basket(body.get("trades"))
    if not basket:
        raise ValueError("nothing staged to preview")
    account_id = _resolve_trade_account(body.get("account"))
    proposed_orders, warnings = _prepare_trade_orders(account_id, basket)

    relevant_symbols = {
        str(o.get("symbol") or "").strip().upper() for o in proposed_orders
        if o.get("symbol")
    }
    working_available = True
    working_error: str | None = None
    try:
        raw_working = ibkr_trade.live_orders()
        working_orders = _normalized_working_orders(raw_working, relevant_symbols)
    except ibkr_trade.CPAPIError as exc:
        # A preview can still show sizing/risk, but must not arm placement when
        # we could not rule out a double trade.
        working_available = False
        working_error = str(exc)
        working_orders = []
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
        "working_symbols": sorted(relevant_symbols),
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
    try:
        fresh_working = _normalized_working_orders(ibkr_trade.live_orders(), symbols)
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
    try:
        placed = ibkr_trade.place_orders(account_id, orders)
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
    if not ibkr_trade.trading_enabled():
        raise _Forbidden("trading is disabled")
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
    if not ibkr_trade.trading_enabled():
        raise _Forbidden("trading is disabled")
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
