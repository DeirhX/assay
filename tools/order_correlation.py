"""Durable links between execution intent, client ids, and IBKR order ids.

Intent, the local queue, and broker truth deliberately remain separate stores.
This module is the join table: one placed queue leg may represent several
execution-plan items, while each placement attempt has one cOID and (once IBKR
accepts it) one broker order id.
"""
from __future__ import annotations

import datetime as dt
import threading
from pathlib import Path
from typing import Any

import config
import store
from portfolio import clean_symbol

CORRELATION_FILE = config.DATA_DIR / "cache" / "order-correlation.json"
SCHEMA_VERSION = 1
RECENT_LIMIT = 100
_LOCK = threading.RLock()
_TERMINAL = {"filled", "cancelled", "canceled", "expired", "rejected", "inactive", "apicancelled"}
_FAILED = {"cancelled", "canceled", "expired", "rejected", "inactive", "apicancelled"}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _empty() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "records": [], "updated_at": None}


def load_state(path: Path = CORRELATION_FILE) -> dict[str, Any]:
    raw = store.load(path, {})
    if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
        return _empty()
    raw.setdefault("records", [])
    raw.setdefault("updated_at", None)
    return raw


def _broker_id(row: dict[str, Any]) -> str:
    return str(row.get("orderId") or row.get("order_id") or "").strip()


def _status(row: dict[str, Any]) -> str:
    return str(row.get("status") or row.get("order_status") or "").strip().lower()


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _leg_id(row: dict[str, Any]) -> str:
    return str(row.get("leg_id") or f"stock:{clean_symbol(row.get('symbol'))}")


def _item_ids(leg: dict[str, Any] | None) -> list[str]:
    return sorted({
        str(provenance.get("execution_item_id") or "")
        for provenance in (leg or {}).get("provenance") or []
        if isinstance(provenance, dict) and provenance.get("execution_item_id")
    })


def _kind(status: str, filled: float, total: float) -> str | None:
    if status == "filled":
        return "filled"
    if status in {"cancelled", "canceled", "expired", "apicancelled"}:
        return "cancelled"
    if status in {"rejected", "inactive"}:
        return "rejected"
    if filled > 0 and (not total or filled < total):
        return "partial"
    return None


def _write(state: dict[str, Any], path: Path, now: str) -> None:
    state["schema_version"] = SCHEMA_VERSION
    state["updated_at"] = now
    state["records"] = sorted(
        state.get("records") or [],
        key=lambda record: str(record.get("updated_at") or record.get("placed_at") or ""),
        reverse=True,
    )[:RECENT_LIMIT]
    store.write_json(path, state)


def record_placements(
    account_id: str,
    basket: list[dict[str, Any]],
    acknowledgements: list[dict[str, Any]],
    *,
    path: Path = CORRELATION_FILE,
    now: str | None = None,
) -> list[dict[str, Any]]:
    """Persist accepted IBKR acknowledgements enriched by ``place_orders``."""
    stamp = now or _now()
    basket_by_leg = {_leg_id(leg): leg for leg in basket}
    with _LOCK:
        state = load_state(path)
        records = state.get("records") or []
        changed: list[dict[str, Any]] = []
        for acknowledgement in acknowledgements:
            sent = acknowledgement.get("assay_order")
            if not isinstance(sent, dict):
                continue
            coid = str(sent.get("cOID") or "").strip()
            broker_id = _broker_id(acknowledgement)
            if not coid or not broker_id:
                continue
            leg_id = _leg_id(sent)
            key = f"{account_id}:{broker_id}"
            record = next(
                (
                    candidate for candidate in records
                    if candidate.get("key") == key
                    or (
                        candidate.get("account_id") == account_id
                        and candidate.get("cOID") == coid
                    )
                ),
                None,
            )
            if record is None:
                record = {"key": key, "placed_at": stamp}
                records.append(record)
            status = _status(acknowledgement) or "submitted"
            record.update({
                "key": key,
                "account_id": account_id,
                "leg_id": leg_id,
                "execution_item_ids": _item_ids(basket_by_leg.get(leg_id)),
                "instrument_type": sent.get("instrument_type") or "stock",
                "symbol": clean_symbol(sent.get("symbol")),
                "conid": sent.get("conid"),
                "side": str(sent.get("side") or "").upper(),
                "quantity": _number(sent.get("quantity")),
                "cOID": coid,
                "broker_order_id": broker_id,
                "broker_status": status,
                "filled_qty": _number(acknowledgement.get("filledQuantity")),
                "total_qty": _number(sent.get("quantity")),
                "terminal": status in _TERMINAL,
                "last_event_kind": _kind(status, 0, _number(sent.get("quantity"))),
                "updated_at": stamp,
            })
            changed.append(dict(record))
        if changed:
            state["records"] = records
            _write(state, path, stamp)
        return changed


def sync_orders(
    orders: list[dict[str, Any]],
    *,
    path: Path = CORRELATION_FILE,
    now: str | None = None,
) -> dict[str, Any]:
    """Merge live broker state and identify failed intent safe to reopen."""
    stamp = now or _now()
    by_id = {_broker_id(order): order for order in orders if _broker_id(order)}
    by_coid = {
        str(order.get("cOID") or "").strip(): order
        for order in orders
        if str(order.get("cOID") or "").strip()
    }
    with _LOCK:
        state = load_state(path)
        records = state.get("records") or []
        changed = False
        for record in records:
            order = by_id.get(str(record.get("broker_order_id") or "")) or by_coid.get(
                str(record.get("cOID") or ""),
            )
            if not order:
                continue
            status = _status(order) or str(record.get("broker_status") or "")
            filled = _number(order.get("filledQuantity"))
            total = _number(order.get("totalSize") or order.get("quantity") or record.get("total_qty"))
            broker_id = _broker_id(order) or str(record.get("broker_order_id") or "")
            updates = {
                "broker_order_id": broker_id,
                "broker_status": status,
                "filled_qty": filled,
                "total_qty": total,
                "terminal": status in _TERMINAL,
                "last_event_kind": _kind(status, filled, total),
            }
            if any(record.get(key) != value for key, value in updates.items()):
                record.update(updates)
                record["last_event_at"] = stamp
                record["updated_at"] = stamp
                changed = True
        if changed:
            state["records"] = records
            _write(state, path, stamp)

        active_item_ids = {
            str(item_id)
            for record in records
            if not record.get("terminal")
            for item_id in record.get("execution_item_ids") or []
        }
        reopen = sorted({
            str(item_id)
            for record in records
            if record.get("terminal") and str(record.get("broker_status") or "") in _FAILED
            for item_id in record.get("execution_item_ids") or []
            if str(item_id) not in active_item_ids
        })
        return {
            "changed": changed,
            "reopen_item_ids": reopen,
            "state": state,
            "summary": summary(state),
        }


def _recent(record: dict[str, Any], now: dt.datetime) -> bool:
    raw = record.get("last_event_at")
    if not raw:
        return False
    try:
        stamp = dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    return now - stamp <= dt.timedelta(hours=24)


def summary(
    state: dict[str, Any] | None = None,
    *,
    path: Path = CORRELATION_FILE,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    state = load_state(path) if state is None else state
    records = state.get("records") or []
    current = now or dt.datetime.now(dt.timezone.utc)
    return {
        "active": sum(1 for record in records if not record.get("terminal")),
        "partial": sum(
            1 for record in records
            if not record.get("terminal") and record.get("last_event_kind") == "partial"
        ),
        "recent_filled": sum(
            1 for record in records
            if record.get("last_event_kind") == "filled" and _recent(record, current)
        ),
        "recent_failed": sum(
            1 for record in records
            if record.get("last_event_kind") in {"cancelled", "rejected"}
            and _recent(record, current)
        ),
        "updated_at": state.get("updated_at"),
    }


def public_state(*, path: Path = CORRELATION_FILE) -> dict[str, Any]:
    state = load_state(path)
    return {
        "schema_version": SCHEMA_VERSION,
        "records": state.get("records") or [],
        "summary": summary(state),
        "updated_at": state.get("updated_at"),
    }
