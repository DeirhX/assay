#!/usr/bin/env python3
"""Order/fill watcher -- the missing event loop.

The trade desk can place a GTC order that fills on Tuesday while the planner keeps
advising off Monday's book until you happen to press Resync. This closes that: a
read-only poll of the state of orders you already placed, detecting the
transitions a human must know about -- filled, partially filled, cancelled,
rejected -- and reacting:

* a fill kicks the existing holdings-resync job automatically, so the planner and
  Today reflect the post-fill book without a manual step (there is no plan/overview
  cache to bust -- everything reads the snapshot from disk);
* every transition emits a notification through ``notify`` (off by default);
* if the gateway session drops while orders are working -- the case that silently
  stops the peg loop and blinds you to your own ladder -- it alerts once.

STRICTLY read-only with respect to the market: it never places, modifies, sizes,
or cancels an order. It only observes orders the human placed. The transition
logic (:func:`classify` / :func:`diff_orders`) is pure and unit-tested without a
gateway; :func:`poll_once` does the IBKR IO behind injectable seams. Last-seen
order states persist to ``data/cache/order-watch.json`` so a transition is
detected once, across restarts.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import apierror
import config
import holdings_sync
import ibkr_trade
import notify
import store

STATE_FILE = config.DATA_DIR / "cache" / "order-watch.json"

# CPAPI order statuses, normalized to lower-case. Anything not listed is treated
# as "still working" (Submitted / PreSubmitted / PendingSubmit / ...).
FILL_STATES = {"filled"}
CANCEL_STATES = {"cancelled", "canceled", "pendingcancel"}
REJECT_STATES = {"rejected", "inactive"}

RECENT_FILLS_CAP = 50


# --------------------------------------------------------------------------- #
# Pure transition detection
# --------------------------------------------------------------------------- #
def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _order_key(order: dict) -> str:
    return str(order.get("orderId") or order.get("order_id") or "").strip()


def _snapshot(order: dict) -> dict:
    """The bits of an order we persist to compare against next poll."""
    return {
        "status": _norm(order.get("status") or order.get("order_status")),
        "filled": _num(order.get("filledQuantity")),
        "symbol": order.get("ticker") or order.get("symbol"),
        "side": str(order.get("side") or "").upper(),
    }


def classify(prev: dict | None, order: dict) -> dict | None:
    """One noteworthy transition for a single order, or None. Terminal states win
    over a fill-quantity bump; a partial fill is a strictly increased filled
    quantity while the order is not yet fully filled. Re-seeing a state we already
    reported (same terminal status, no new fills) yields nothing."""
    cur = _snapshot(order)
    status = cur["status"]
    p_status = _norm((prev or {}).get("status"))
    p_filled = _num((prev or {}).get("filled"))
    event = {
        "order_id": _order_key(order),
        "symbol": cur["symbol"],
        "side": cur["side"],
        "status": status,
        "filled": cur["filled"],
        "total": _num(order.get("totalSize")),
    }
    if status in FILL_STATES and p_status not in FILL_STATES:
        return {**event, "kind": "filled"}
    if status in CANCEL_STATES and p_status not in CANCEL_STATES:
        return {**event, "kind": "cancelled"}
    if status in REJECT_STATES and p_status not in REJECT_STATES:
        return {**event, "kind": "rejected"}
    if cur["filled"] > p_filled and status not in FILL_STATES:
        return {**event, "kind": "partial"}
    return None


def diff_orders(prev_map: dict, orders: list[dict]) -> tuple[list[dict], dict]:
    """Compare the previous per-order snapshot map against the current order list.
    Returns (events, new_map). Orders without an id are ignored (nothing to key
    on / dedupe against)."""
    events: list[dict] = []
    new_map: dict = {}
    for order in orders or []:
        key = _order_key(order)
        if not key:
            continue
        event = classify(prev_map.get(key), order)
        new_map[key] = _snapshot(order)
        if event:
            events.append(event)
    return events, new_map


# --------------------------------------------------------------------------- #
# Human-readable notification text
# --------------------------------------------------------------------------- #
def _event_message(ev: dict) -> tuple[str, str, tuple[str, ...], str]:
    """(title, body, tags, priority) for a transition."""
    sym = ev.get("symbol") or "?"
    side = ev.get("side") or ""
    kind = ev.get("kind")
    filled = ev.get("filled") or 0
    total = ev.get("total") or 0
    qty = f"{filled:g}" + (f"/{total:g}" if total else "")
    if kind == "filled":
        return (f"Fill: {side} {sym}",
                f"{side} {sym} filled ({qty}). Holdings resync kicked; re-check the planner.",
                ("white_check_mark",), "high")
    if kind == "partial":
        return (f"Partial fill: {side} {sym}",
                f"{side} {sym} partially filled ({qty}).",
                ("hourglass_flowing_sand",), "default")
    if kind == "cancelled":
        return (f"Cancelled: {side} {sym}",
                f"{side} {sym} was cancelled.",
                ("x",), "default")
    if kind == "rejected":
        return (f"Rejected: {side} {sym}",
                f"{side} {sym} was rejected/inactive -- it will not fill.",
                ("warning",), "high")
    return (f"Order update: {sym}", f"{side} {sym}: {ev.get('status')}", (), "default")


# --------------------------------------------------------------------------- #
# The poll (impure; injectable seams for tests)
# --------------------------------------------------------------------------- #
def poll_once(
    *,
    now: dt.datetime | None = None,
    fetch_orders: Callable[[], list[dict]] | None = None,
    fetch_auth: Callable[[], dict] | None = None,
    notifier: Callable[..., list[str]] | None = None,
    resync: Callable[[], dict] | None = None,
    state_path: Path | None = None,
) -> dict:
    """One watch cycle. Reads the gateway session + working orders, diffs against
    last-seen state, reacts to transitions, and persists the new state. Returns a
    small summary dict; never raises (the scheduler stamps its result string)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    fetch_orders = fetch_orders or ibkr_trade.live_orders
    fetch_auth = fetch_auth or ibkr_trade.auth_status
    notifier = notifier or notify.notify
    resync = resync or holdings_sync.start_holdings_sync
    state_path = state_path or STATE_FILE

    state = store.load(state_path, {}) or {}
    prev_map: dict = state.get("orders") or {}

    # A usable session is a precondition for reading orders. If it's gone while we
    # know orders are working, that's the single alert that matters most (a dead
    # session silently stops the peg and hides fills) -- fire it once, not every tick.
    auth = fetch_auth() or {}
    if not auth.get("authenticated"):
        if prev_map and not state.get("session_down_notified"):
            notifier(
                "IBKR session down",
                f"{len(prev_map)} order(s) were working, but the gateway session isn't "
                "authenticated. Re-auth so the peg loop and fill detection keep running.",
                tags=("rotating_light",), priority="urgent",
            )
            state["session_down_notified"] = True
            store.write_json(state_path, state)
        return {"ok": False, "reason": "session not authenticated"}
    state["session_down_notified"] = False

    try:
        orders = fetch_orders()
    except ibkr_trade.CPAPIError as exc:
        store.write_json(state_path, state)
        return {"ok": False, "reason": f"gateway error: {exc}"}

    events, new_map = diff_orders(prev_map, orders)
    fills = [e for e in events if e["kind"] in ("filled", "partial")]

    resynced = False
    if fills:
        try:
            resync()
            resynced = True
        except apierror.Conflict:
            resynced = False  # a resync is already running -- fine, it'll pick up the fill
        except Exception as exc:  # noqa: BLE001 -- a resync failure must not lose the events
            sys.stderr.write(f"[order-watch] resync failed: {exc}\n")

    for ev in events:
        title, body, tags, priority = _event_message(ev)
        notifier(title, body, tags=tags, priority=priority)

    if fills:
        recent = state.get("recent_fills") or []
        for e in fills:
            recent.append({**e, "at": now.isoformat(timespec="seconds")})
        state["recent_fills"] = recent[-RECENT_FILLS_CAP:]

    state["orders"] = new_map
    state["updated_at"] = now.isoformat(timespec="seconds")
    store.write_json(state_path, state)
    return {"ok": True, "events": len(events), "fills": len(fills),
            "resynced": resynced, "orders": len(new_map)}
