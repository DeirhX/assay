#!/usr/bin/env python3
"""Live trading service (Client Portal Web API) -- GATED.

Extracted from serve.py so the HTTP handlers stay thin wrappers over this and
the security-sensitive logic lives in one separately-testable module. Unlike
the read-only Flex path, these helpers can place REAL orders, so every entry
point checks ibkr_trade.trading_enabled(); placement additionally requires a
matching preview token, an explicit confirm flag, and (for live, non-paper
accounts) IBKR_ALLOW_LIVE. Order details are always derived server-side from the
token-bound basket so a tampered client payload cannot place something the human
never previewed.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

import ibkr_trade
import order_peg
import overview
import price_levels
import rebalance
import whatif
from apierror import BadGateway as _BadGateway, Forbidden as _Forbidden
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
_preview_issued: dict[str, float] = {}

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
                "fx_to_base": float(p.get("fx_rate_to_base") or 1.0),
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
        fx = p.get("fx_rate_to_base")
        if ccy and isinstance(fx, (int, float)) and fx:
            out[ccy] = float(fx)
    return out


def _resolve_trade_account(requested: str | None) -> str:
    """Pick the account to trade. An explicit request must be visible to the
    session. Otherwise prefer IBKR_TRADE_ACCOUNT_ID, then a paper (DU) account,
    then the first one -- paper-first by construction."""
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


def _prepare_trade_orders(account_id: str, basket: list[dict]) -> tuple[list[dict], list[str]]:
    """Translate a token-bound CZK basket into CPAPI order dicts, server-side.
    Resolves conids, prices held names from the snapshot and unheld names from a
    live CPAPI snapshot, then defers the shares math + skips to
    ibkr_trade.build_orders. Returns (orders, warnings)."""
    price_map = _trade_price_map()
    fx_map = _fx_by_currency()
    conids: dict[str, int] = {}
    for t in basket:
        cid = ibkr_trade.resolve_conid(t["symbol"])
        if cid is not None:
            conids[t["symbol"]] = cid

    missing = [s for s in conids if s not in price_map]
    snap = ibkr_trade.market_snapshot([conids[s] for s in missing]) if missing else {}
    warnings: list[str] = []

    def price_lookup(sym: str) -> dict | None:
        if sym in price_map:
            return price_map[sym]
        cid = conids.get(sym)
        row = snap.get(cid) if cid is not None else None
        px = _parse_snapshot_price(row.get("31")) if isinstance(row, dict) else None
        if px is None:
            return None
        # Currency for an unheld name isn't reliably in the snapshot; assume USD
        # (the dominant case) and surface it so the human can sanity-check size.
        warnings.append(f"{sym}: not held — sized from a live price assuming USD FX")
        return {"price": px, "fx_to_base": fx_map.get("USD", 1.0)}

    orders, skip_warnings = ibkr_trade.build_orders(
        basket,
        price_lookup=price_lookup,
        conid_lookup=lambda s: conids.get(s),
        account_id=account_id,
        coid_prefix="assay-" + _basket_token(account_id, basket),
        limit_lookup=_locked_limit,
    )
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
            for a in ibkr_trade.accounts():
                aid = str(a.get("accountId") or a.get("id") or "")
                if aid:
                    out.append({"id": aid, "kind": ibkr_trade.account_kind(aid)})
            status["accounts"] = out
            if out:
                try:
                    status["default_account"] = _resolve_trade_account(None)
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
    orders, warnings = _prepare_trade_orders(account_id, basket)

    ibkr_preview = {}
    if orders:
        try:
            ibkr_preview = ibkr_trade.preview_orders(account_id, orders)
        except ibkr_trade.CPAPIError as exc:
            raise _BadGateway(str(exc)) from exc

    local = None
    holdings = _load(HOLDINGS_JSON)
    # Preview against the working draft when one exists, so the what-if's band
    # status matches the planner the basket was built in (both now reflect the
    # draft). With no draft this is the live model, exactly as before.
    import target_staging
    model = target_staging.active_model()
    if holdings and model:
        try:
            local = whatif.simulate(holdings, model, basket)
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
    _preview_issued[token] = now
    for t in [t for t, at in _preview_issued.items() if now - at > PREVIEW_TTL_S]:
        _preview_issued.pop(t, None)

    return {
        "account": account_id,
        "kind": ibkr_trade.account_kind(account_id),
        "is_paper": ibkr_trade.is_paper_account(account_id),
        "live_allowed": ibkr_trade.live_allowed(),
        "token": token,
        "preview_ttl_s": PREVIEW_TTL_S,
        "trades": basket,
        "orders": orders,
        "warnings": warnings,
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
    accounts) live placement is unlocked. Orders are re-derived server-side from
    the token-bound basket, never trusted from the client."""
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
    if issued is None or time.time() - issued > PREVIEW_TTL_S:
        raise ValueError("preview expired — prices and sizes may be stale; "
                         "re-preview the basket before placing")
    orders, warnings = _prepare_trade_orders(account_id, basket)
    if not orders:
        raise ValueError("no orders could be sized from this basket — see preview warnings")
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


def _trade_orders() -> dict:
    if not ibkr_trade.trading_enabled():
        raise _Forbidden("trading is disabled")
    try:
        # Fold in the active pegs so the UI can badge which working orders are
        # being kept at the top of book (and offer a Stop) in a single call.
        return {"orders": ibkr_trade.live_orders(), "pegs": order_peg.active_pegs()}
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
