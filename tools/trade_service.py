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

import ibkr_trade
import price_levels
import whatif
from apierror import BadGateway as _BadGateway, Forbidden as _Forbidden
from config import DATA_DIR
from portfolio import HOLDINGS_JSON, normalize_basket, provider_symbol_for
from store import load as _load, write_json as _write_json

# The planner-staged basket, persisted so the trade desk survives a reload or a
# navigation away and back instead of relying on an in-browser hand-off that a
# refresh silently drops. Gitignored cache, not portfolio truth.
STAGED_BASKET_JSON = DATA_DIR / "cache" / "staged-basket.json"


def _normalize_basket(trades) -> list[dict]:
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


def save_basket(trades) -> list[dict]:
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


def _resolve_trade_account(requested) -> str:
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


def _parse_snapshot_price(raw) -> float | None:
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

    def price_lookup(sym: str):
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

    return {
        "account": account_id,
        "kind": ibkr_trade.account_kind(account_id),
        "is_paper": ibkr_trade.is_paper_account(account_id),
        "live_allowed": ibkr_trade.live_allowed(),
        "token": _basket_token(account_id, basket),
        "trades": basket,
        "orders": orders,
        "warnings": warnings,
        "ibkr_preview": ibkr_preview,
        "local_whatif": local,
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
    orders, warnings = _prepare_trade_orders(account_id, basket)
    if not orders:
        raise ValueError("no orders could be sized from this basket — see preview warnings")
    try:
        placed = ibkr_trade.place_orders(account_id, orders)
    except ibkr_trade.CPAPIError as exc:
        raise _BadGateway(str(exc)) from exc
    return {
        "account": account_id,
        "kind": ibkr_trade.account_kind(account_id),
        "is_paper": ibkr_trade.is_paper_account(account_id),
        "orders": orders,
        "warnings": warnings,
        "placed": placed,
    }


def _trade_orders() -> dict:
    if not ibkr_trade.trading_enabled():
        raise _Forbidden("trading is disabled")
    try:
        return {"orders": ibkr_trade.live_orders()}
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
