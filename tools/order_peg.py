#!/usr/bin/env python3
"""Opt-in "keep at top of book" order pegging -- GATED, in-memory.

A resting limit order in a thin market sits idle until someone else beats your
price. This module runs a small per-order loop that re-prices your order to stay
one tick better than the best price on its *own* side -- undercut the best ask
for a SELL, outbid the best bid for a BUY -- so you hold queue priority as the
book moves. It is deliberately conservative:

* **Passive only.** It never crosses the spread / never posts a marketable
  price; the new price is always kept strictly inside the opposite touch.
* **Bounded.** It never moves past a worst-acceptable price (the floor for a
  sell, the ceiling for a buy). Default bound is the order's own original limit.
* **Reactive, not racing.** It moves only when someone has actually beaten us;
  when we are already the best price on our side it holds, so it never chases
  itself down to the bound one tick at a time.

Safety posture mirrors the rest of the trade desk: nothing runs unless
``IBKR_TRADING_ENABLED`` *and* the dedicated ``IBKR_AUTO_PEG`` master flag are
set, and pegging a LIVE (non-paper) account additionally requires
``IBKR_ALLOW_LIVE``. State is in-memory only: a server restart stops every peg
and leaves each order resting at its last price (a safe no-op), by design.

The pricing decision (:func:`desired_price`) is a pure function, unit-tested
without any network; :func:`start_peg`/:func:`_peg_worker` do the IBKR IO.
"""
from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import apierror
import ibkr_trade
import jobs

# Loop cadence + churn guards (all overridable per peg by the caller/UI).
DEFAULT_POLL_S = 4.0            # how often to read the book
DEFAULT_MIN_INTERVAL_S = 3.0    # minimum seconds between two reprices (gateway courtesy)
_EPS = 1e-9


# --------------------------------------------------------------------------- #
# Flags
# --------------------------------------------------------------------------- #
def auto_peg_enabled() -> bool:
    """Dedicated master switch for autonomous re-pricing. Off unless explicitly
    set -- pegging mutates live orders on a timer, so it gets its own flag on top
    of ``IBKR_TRADING_ENABLED`` rather than riding the manual-placement gate."""
    return ibkr_trade._truthy(ibkr_trade._config_value("IBKR_AUTO_PEG", "0"))


# --------------------------------------------------------------------------- #
# Pure pricing decision
# --------------------------------------------------------------------------- #
def _round_to_tick(price: float, tick: float) -> float:
    """Snap ``price`` to the nearest multiple of ``tick`` (grid origin 0, which
    matches ordinary equity increments). Keeps derived prices on IBKR's grid."""
    if tick <= 0:
        return price
    return round(round(price / tick) * tick, 6)


def desired_price(
    side: str,
    mine: float | None,
    best_bid: float | None,
    best_ask: float | None,
    tick: float,
    bound: float,
) -> float | None:
    """The new limit price to move to, or ``None`` to hold.

    ``mine`` is our order's current resting limit; ``bound`` is the
    worst-acceptable price (floor for SELL, ceiling for BUY). Returns a price
    only when we have been beaten on our own side *and* moving is a real
    improvement of at least one tick that stays inside the spread and within the
    bound. Pure: no IO, safe on missing/None quotes."""
    if mine is None or tick <= 0:
        return None
    s = str(side or "").upper()

    if s == "SELL":
        if best_ask is None or best_bid is None:
            return None
        # Only react when a competing ask is below us; otherwise we are (tied
        # for) the best offer and must NOT undercut ourselves.
        if best_ask >= mine - _EPS:
            return None
        target = best_ask - tick                    # undercut the competitor
        target = max(target, best_bid + tick)       # stay non-marketable (inside the spread)
        target = max(target, bound)                 # never below the worst-acceptable floor
        target = _round_to_tick(target, tick)
        # A genuine improvement is a lower ask by >= a tick; clamps can erase it.
        if target <= mine - tick + _EPS and target >= bound - _EPS:
            return target
        return None

    if s == "BUY":
        if best_bid is None or best_ask is None:
            return None
        if best_bid <= mine + _EPS:                 # we are (tied for) the best bid -> hold
            return None
        target = best_bid + tick                    # outbid the competitor
        target = min(target, best_ask - tick)       # stay non-marketable
        target = min(target, bound)                 # never above the worst-acceptable ceiling
        target = _round_to_tick(target, tick)
        if target >= mine + tick - _EPS and target <= bound + _EPS:
            return target
        return None

    return None


# --------------------------------------------------------------------------- #
# Config + registry
# --------------------------------------------------------------------------- #
@dataclass
class PegConfig:
    order_id: str
    account_id: str
    conid: int
    side: str
    bound: float
    tick: float
    symbol: str = ""
    poll_s: float = DEFAULT_POLL_S
    min_interval_s: float = DEFAULT_MIN_INTERVAL_S


# order_id -> live peg record. In-memory on purpose (see module docstring).
_ACTIVE: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()


def _record(cfg: PegConfig, job_id: str) -> dict[str, Any]:
    return {
        "order_id": cfg.order_id,
        "job_id": job_id,
        "cfg": cfg,
        "state": "running",
        "reprices": 0,
        "price": None,
        "message": "armed",
        "last_reprice_ts": 0.0,
    }


def _public(rec: dict[str, Any]) -> dict[str, Any]:
    """UI-safe view of one peg record (no PegConfig object leaked wholesale)."""
    cfg: PegConfig = rec["cfg"]
    return {
        "order_id": rec["order_id"],
        "job_id": rec["job_id"],
        "state": rec["state"],
        "reprices": rec["reprices"],
        "price": rec["price"],
        "message": rec["message"],
        "side": cfg.side,
        "symbol": cfg.symbol,
        "bound": cfg.bound,
        "tick": cfg.tick,
    }


def active_pegs() -> list[dict[str, Any]]:
    """Public snapshots of every registered peg, for the Working orders card."""
    with _LOCK:
        return [_public(rec) for rec in _ACTIVE.values()]


def is_pegging(order_id: str) -> bool:
    with _LOCK:
        return str(order_id) in _ACTIVE


# --------------------------------------------------------------------------- #
# Order resolution helpers
# --------------------------------------------------------------------------- #
_DONE_STATUSES = {"filled", "cancelled", "canceled", "inactive", "rejected", "expired"}


def _num(raw: Any) -> float | None:
    """Parse a CPAPI numeric field: a plain number, or a string that may carry a
    leading flag letter (e.g. ``C``=prior close) or thousands separators."""
    if raw in (None, ""):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).replace(",", "").lstrip("CHchx ").strip()
    try:
        return float(s)
    except ValueError:
        return None


def _order_id_of(o: dict) -> str:
    return str(o.get("orderId") or o.get("order_id") or "")


def find_live_order(order_id: str) -> dict | None:
    """The working order with ``order_id`` from CPAPI, or None if not present."""
    oid = str(order_id)
    for o in ibkr_trade.live_orders():
        if isinstance(o, dict) and _order_id_of(o) == oid:
            return o
    return None


def _order_is_done(o: dict) -> bool:
    status = str(o.get("status") or o.get("order_status") or "").strip().lower()
    if status in _DONE_STATUSES:
        return True
    remaining = _num(o.get("remainingQuantity"))
    return remaining is not None and abs(remaining) < _EPS


def _order_price(o: dict) -> float | None:
    return _num(o.get("price"))


def _order_side(o: dict) -> str:
    return str(o.get("side") or "").upper()


def _order_symbol(o: dict) -> str:
    return str(o.get("ticker") or o.get("symbol") or o.get("conid") or "")


# --------------------------------------------------------------------------- #
# start / stop
# --------------------------------------------------------------------------- #
def start_peg(
    order_id: str,
    account_id: str,
    *,
    worst_price: float | None = None,
    poll_s: float = DEFAULT_POLL_S,
    min_interval_s: float = DEFAULT_MIN_INTERVAL_S,
) -> dict[str, Any]:
    """Arm a top-of-book peg on an existing working order.

    Refuses unless trading and the ``IBKR_AUTO_PEG`` master flag are on (and,
    for a live account, ``IBKR_ALLOW_LIVE``). Resolves the order from CPAPI to
    learn its side/conid/limit; the bound defaults to that original limit unless
    ``worst_price`` overrides it. Fetches the contract tick once. Idempotent-ish:
    pegging an already-pegged order is a Conflict, not a second loop."""
    oid = str(order_id or "").strip()
    if not oid:
        raise apierror.BadRequest("order_id is required")
    if not ibkr_trade.trading_enabled():
        raise apierror.Forbidden("trading is disabled — set IBKR_TRADING_ENABLED")
    if not auto_peg_enabled():
        raise apierror.Forbidden(
            "order pegging is disabled — set IBKR_AUTO_PEG=1 to keep orders at the top of book")
    if not ibkr_trade.is_paper_account(account_id) and not ibkr_trade.live_allowed():
        raise apierror.Forbidden(
            "live account pegging is locked — validate on paper, then set IBKR_ALLOW_LIVE")

    with _LOCK:
        if oid in _ACTIVE:
            raise apierror.Conflict(f"order {oid} is already being pegged")

    try:
        order = find_live_order(oid)
    except ibkr_trade.CPAPIError as exc:
        raise apierror.BadGateway(str(exc)) from exc
    if order is None:
        raise apierror.BadRequest(f"order {oid} is not a live working order")

    side = _order_side(order)
    if side not in ("BUY", "SELL"):
        raise apierror.BadRequest(f"order {oid} has no usable side")
    conid_raw = order.get("conid")
    if conid_raw is None:
        raise apierror.BadRequest(f"order {oid} has no contract id to price against")
    try:
        conid = int(conid_raw)
    except (TypeError, ValueError):
        raise apierror.BadRequest(f"order {oid} has no contract id to price against")
    limit = _order_price(order)
    if limit is None:
        raise apierror.BadRequest(
            f"order {oid} has no limit price to peg (only limit orders can be pegged)")

    bound = float(worst_price) if worst_price is not None else float(limit)

    try:
        rules = ibkr_trade.contract_rules(conid, is_buy=(side == "BUY"))
    except ibkr_trade.CPAPIError:
        rules = {}
    tick = ibkr_trade.tick_for_price(rules, limit)

    cfg = PegConfig(
        order_id=oid,
        account_id=str(account_id),
        conid=conid,
        side=side,
        bound=bound,
        tick=tick,
        symbol=_order_symbol(order),
        poll_s=max(1.0, float(poll_s)),
        min_interval_s=max(0.0, float(min_interval_s)),
    )
    job = jobs.spawn("order_peg", _peg_worker, oid, symbol=cfg.symbol)
    with _LOCK:
        # A racing second start could have registered between our checks; honor it.
        if oid in _ACTIVE:
            jobs.cancel_job(job["id"])
            raise apierror.Conflict(f"order {oid} is already being pegged")
        _ACTIVE[oid] = _record(cfg, job["id"])
        rec = _public(_ACTIVE[oid])
    return rec


def stop_peg(order_id: str) -> dict[str, Any]:
    """Stop re-pricing an order. Does NOT cancel the order -- it just rests at its
    current price. Returns {stopped: bool}."""
    oid = str(order_id or "").strip()
    with _LOCK:
        rec = _ACTIVE.get(oid)
    if not rec:
        return {"stopped": False}
    jobs.cancel_job(rec["job_id"])
    return {"stopped": True}


# --------------------------------------------------------------------------- #
# Worker loop
# --------------------------------------------------------------------------- #
def _update(oid: str, **fields: Any) -> None:
    with _LOCK:
        rec = _ACTIVE.get(oid)
        if rec:
            rec.update(fields)


def _sleep_cancellable(job_id: str, seconds: float) -> bool:
    """Sleep in small slices; return True as soon as the job is cancelled."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if jobs.is_cancelled(job_id):
            return True
        time.sleep(min(0.5, max(0.0, end - time.monotonic())))
    return jobs.is_cancelled(job_id)


def _peg_worker(job_id: str, order_id: str) -> None:
    oid = str(order_id)
    with _LOCK:
        rec = _ACTIVE.get(oid)
        cfg: PegConfig | None = rec["cfg"] if rec else None
    if cfg is None:
        jobs.update_job(job_id, state=jobs.JobState.ERROR, message="peg config missing")
        return
    jobs.update_job(job_id, state=jobs.JobState.RUNNING, message="pegging armed")
    final_state = jobs.JobState.DONE
    final_msg = "peg stopped"
    try:
        while not jobs.is_cancelled(job_id):
            # Session guard: if the gateway lost its login, stop rather than
            # hammer a dead endpoint.
            try:
                if not ibkr_trade.auth_status().get("authenticated"):
                    final_state, final_msg = jobs.JobState.ERROR, "gateway not authenticated — peg stopped"
                    break
            except ibkr_trade.CPAPIError:
                final_state, final_msg = jobs.JobState.ERROR, "gateway unreachable — peg stopped"
                break

            try:
                order = find_live_order(oid)
            except ibkr_trade.CPAPIError as exc:
                _update(oid, message=f"orders read failed: {exc}")
                if _sleep_cancellable(job_id, cfg.poll_s):
                    break
                continue
            if order is None or _order_is_done(order):
                final_msg = "order filled or gone — peg stopped"
                break

            mine = _order_price(order)
            snap = {}
            try:
                snap = ibkr_trade.market_snapshot([cfg.conid], fields=("84", "86"))
            except ibkr_trade.CPAPIError as exc:
                _update(oid, message=f"quote read failed: {exc}")
            row = snap.get(cfg.conid) if isinstance(snap, dict) else None
            best_bid = _num(row.get("84")) if isinstance(row, dict) else None
            best_ask = _num(row.get("86")) if isinstance(row, dict) else None

            target = desired_price(cfg.side, mine, best_bid, best_ask, cfg.tick, cfg.bound)
            with _LOCK:
                r = _ACTIVE.get(oid)
                last_ts = r["last_reprice_ts"] if r else 0.0
                reprices = r["reprices"] if r else 0
            throttled = (time.monotonic() - last_ts) < cfg.min_interval_s

            if target is not None and not throttled:
                changes = {
                    "conid": cfg.conid,
                    "orderType": "LMT",
                    "side": cfg.side,
                    "price": round(float(target), 6),
                    "tif": str(order.get("tif") or order.get("timeInForce") or "GTC"),
                }
                qty = _num(order.get("remainingQuantity")) or _num(order.get("totalSize")) or _num(order.get("quantity"))
                if qty:
                    changes["quantity"] = abs(qty)
                try:
                    ibkr_trade.modify_order(cfg.account_id, oid, changes)
                    reprices += 1
                    _update(oid, reprices=reprices, price=target,
                            last_reprice_ts=time.monotonic(),
                            message=f"{reprices} reprice(s), resting @ {target:g}")
                    jobs.update_job(job_id, message=f"{cfg.symbol}: {reprices} reprice(s) @ {target:g}")
                except ibkr_trade.CPAPIError as exc:
                    _update(oid, message=f"reprice failed: {exc}")
            else:
                _update(oid, price=mine,
                        message=("holding — top of book" if target is None else "holding — throttled"))

            if _sleep_cancellable(job_id, cfg.poll_s):
                break

        if jobs.is_cancelled(job_id):
            final_state, final_msg = jobs.JobState.CANCELLED, "peg stopped"
    except Exception as exc:  # noqa: BLE001 -- never let the loop die silently
        final_state, final_msg = jobs.JobState.ERROR, f"peg error: {exc}"
    finally:
        with _LOCK:
            _ACTIVE.pop(oid, None)
        jobs.update_job(job_id, state=final_state, message=final_msg)
