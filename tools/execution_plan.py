"""Durable human execution intent between generated advice and the order queue."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import threading
import uuid
from pathlib import Path
from typing import Any

from config import DATA_DIR
from portfolio import clean_symbol
from store import load, write_json
from value_coercion import coerce_optional_limit_price

EXECUTION_PLAN_JSON = DATA_DIR / "cache" / "execution-plan.json"
SCHEMA_VERSION = 1
_LOCK = threading.RLock()
_EDITABLE = {
    "status",
    "direction",
    "delta_czk",
    "delta_pct",
    "desired_weight_pct",
    "route_policy",
    "route_selection",
    "limit_price",
    "defer_until",
    "dismiss_reason",
}
_STATUSES = {
    "suggested",
    "selected",
    "deferred",
    "dismissed",
    "queued",
    "submitted",
    "superseded",
}
_ROUTES = {
    "auto_put",
    "buy_shares",
    "sell_shares",
    "cash_secured_put",
    "covered_call",
}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def plan_fingerprint(plan: dict[str, Any]) -> str:
    rows: list[dict[str, Any]] = []
    for row in plan.get("rows") or []:
        if not isinstance(row, dict):
            continue
        rows.append({
            "kind": row.get("kind"),
            "name": row.get("name"),
            "action": row.get("action"),
            "suggest_delta_pct": row.get("suggest_delta_pct"),
            "members": [
                {
                    "symbol": member.get("symbol"),
                    "suggest_delta_pct": member.get("suggest_delta_pct"),
                }
                for member in row.get("members") or []
                if isinstance(member, dict)
            ],
        })
    payload = {
        "as_of": plan.get("as_of"),
        "snapshot": plan.get("snapshot"),
        "rows": rows,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _item(
    fingerprint: str,
    symbol: str,
    delta_pct: float,
    delta_czk: float,
    current_pct: float,
    *,
    origin_key: str,
) -> dict[str, Any]:
    sym = clean_symbol(symbol)
    direction = "increase" if delta_czk > 0 else "reduce"
    stable = hashlib.sha256(
        f"{fingerprint}:{origin_key}:{sym}".encode("utf-8"),
    ).hexdigest()[:12]
    return {
        "id": f"rebalance:{stable}",
        "symbol": sym,
        "source": "rebalance",
        "origin_key": origin_key,
        "plan_fingerprint": fingerprint,
        "direction": direction,
        "delta_czk": round(delta_czk, 2),
        "delta_pct": round(delta_pct, 6),
        "desired_weight_pct": round(max(0.0, current_pct + delta_pct), 6),
        "route_policy": "auto_put" if direction == "increase" else "sell_shares",
        "route_selection": None,
        "limit_price": None,
        "status": "suggested",
        "defer_until": None,
        "dismiss_reason": None,
        "queued_leg_id": None,
        "created_at": _now(),
        "updated_at": _now(),
    }


def recommendations(plan: dict[str, Any]) -> list[dict[str, Any]]:
    fingerprint = plan_fingerprint(plan)
    base = float(plan.get("invested") or plan.get("nav") or 0)
    out: list[dict[str, Any]] = []
    for row in plan.get("rows") or []:
        if not isinstance(row, dict):
            continue
        if row.get("kind") == "target" and row.get("interactive"):
            delta_pct = float(row.get("suggest_delta_pct") or 0)
            delta_czk = float(row.get("suggest_delta_czk") or (delta_pct / 100 * base))
            if abs(delta_czk) >= 1:
                out.append(_item(
                    fingerprint,
                    str(row.get("name") or ""),
                    delta_pct,
                    delta_czk,
                    float(row.get("current_pct") or 0),
                    origin_key=str(row.get("key") or row.get("name") or ""),
                ))
        elif row.get("kind") == "sleeve":
            for member in row.get("members") or []:
                if not isinstance(member, dict):
                    continue
                delta_pct = float(member.get("suggest_delta_pct") or 0)
                delta_czk = delta_pct / 100 * base
                if abs(delta_czk) < 1:
                    continue
                symbol = str(member.get("symbol") or "")
                out.append(_item(
                    fingerprint,
                    symbol,
                    delta_pct,
                    delta_czk,
                    float(member.get("current_pct") or 0),
                    origin_key=f"{row.get('name')}:{symbol}",
                ))
    return out


def _empty() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "version": 0,
        "plan_binding": None,
        "items": [],
        "updated_at": None,
    }


def load_plan(path: Path = EXECUTION_PLAN_JSON) -> dict[str, Any]:
    raw = load(path, {})
    if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
        return _empty()
    raw.setdefault("items", [])
    raw.setdefault("version", 0)
    return raw


def _binding(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "fingerprint": plan_fingerprint(plan),
        "as_of": plan.get("as_of"),
        "snapshot": plan.get("snapshot"),
    }


def _write(state: dict[str, Any], path: Path) -> dict[str, Any]:
    state["schema_version"] = SCHEMA_VERSION
    state["version"] = int(state.get("version") or 0) + 1
    state["updated_at"] = _now()
    write_json(path, state)
    return state


def state_for_plan(
    plan: dict[str, Any],
    *,
    path: Path = EXECUTION_PLAN_JSON,
) -> dict[str, Any]:
    """Return workbook state; initialize it, but never overwrite a newer choice on GET."""
    with _LOCK:
        state = load_plan(path)
        binding = _binding(plan)
        if not state.get("plan_binding"):
            state["plan_binding"] = binding
            state["items"] = recommendations(plan)
            _write(state, path)
        state["stale"] = (
            state.get("plan_binding", {}).get("fingerprint") != binding["fingerprint"]
        )
        state["pending_binding"] = binding if state["stale"] else None
        state["pending_count"] = len(recommendations(plan)) if state["stale"] else 0
        return state


def replace_rebalance(
    plan: dict[str, Any],
    *,
    path: Path = EXECUTION_PLAN_JSON,
) -> dict[str, Any]:
    """Replace unqueued rebalance advice while preserving queue/manual/Exit intent."""
    with _LOCK:
        state = load_plan(path)
        keep = [
            item for item in state.get("items") or []
            if (
                item.get("source") != "rebalance"
                or item.get("status") in {"queued", "submitted"}
            )
        ]
        queued_by_symbol: dict[str, float] = {}
        for item in keep:
            if item.get("source") == "rebalance" and item.get("status") == "queued":
                symbol = clean_symbol(item.get("symbol"))
                queued_by_symbol[symbol] = (
                    queued_by_symbol.get(symbol, 0)
                    + float(item.get("delta_czk") or 0)
                )
        generated = recommendations(plan)
        base = float(plan.get("invested") or plan.get("nav") or 0)
        residual: list[dict[str, Any]] = []
        for item in generated:
            pending = queued_by_symbol.get(clean_symbol(item.get("symbol")), 0)
            delta = round(float(item.get("delta_czk") or 0) - pending, 2)
            if abs(delta) < 1:
                continue
            item["delta_czk"] = delta
            if base:
                item["delta_pct"] = round(delta / base * 100, 6)
            item["direction"] = "increase" if delta > 0 else "reduce"
            item["route_policy"] = "auto_put" if delta > 0 else "sell_shares"
            residual.append(item)
        state["items"] = keep + residual
        state["plan_binding"] = _binding(plan)
        _write(state, path)
        return state_for_plan(plan, path=path)


def patch_item(
    item_id: str,
    changes: dict[str, Any],
    *,
    expected_version: int | None = None,
    path: Path = EXECUTION_PLAN_JSON,
) -> dict[str, Any]:
    with _LOCK:
        state = load_plan(path)
        if expected_version is not None and int(state.get("version") or 0) != expected_version:
            raise ValueError("execution plan changed; reload before editing")
        target = next(
            (item for item in state.get("items") or [] if item.get("id") == item_id),
            None,
        )
        if target is None:
            raise ValueError("execution plan item not found")
        for key, value in changes.items():
            if key not in _EDITABLE:
                continue
            if key == "status" and value not in _STATUSES:
                raise ValueError("invalid execution plan status")
            if key == "direction" and value not in {"increase", "reduce"}:
                raise ValueError("invalid execution direction")
            if key == "route_policy" and value not in _ROUTES:
                raise ValueError("invalid execution route policy")
            if key in {"delta_czk", "delta_pct", "desired_weight_pct", "limit_price"}:
                if value is None and key == "limit_price":
                    target[key] = None
                    continue
                if key == "limit_price":
                    value = coerce_optional_limit_price(
                        value,
                        numeric_error=f"{key} must be numeric",
                        positive_error="limit_price must be positive",
                    )
                else:
                    try:
                        value = float(value)
                    except (TypeError, ValueError):
                        raise ValueError(f"{key} must be numeric") from None
            target[key] = value
        target["updated_at"] = _now()
        return _write(state, path)


def add_manual(
    payload: dict[str, Any],
    *,
    path: Path = EXECUTION_PLAN_JSON,
) -> tuple[dict[str, Any], dict[str, Any]]:
    sym = clean_symbol(payload.get("symbol"))
    delta_raw = payload.get("delta_czk")
    try:
        delta = float(delta_raw) if isinstance(delta_raw, (int, float, str)) else 0
    except ValueError:
        delta = 0
    if not sym or abs(delta) < 1:
        raise ValueError("manual execution item needs symbol and non-zero delta_czk")
    route = str(payload.get("route_policy") or (
        "auto_put" if delta > 0 else "sell_shares"
    ))
    if route not in _ROUTES:
        raise ValueError("invalid execution route policy")
    limit_price = coerce_optional_limit_price(
        payload.get("limit_price"),
        positive_error="limit_price must be positive",
    )
    now = _now()
    item = {
        "id": f"manual:{uuid.uuid4().hex[:12]}",
        "symbol": sym,
        "source": str(payload.get("source") or "ticker"),
        "origin_key": str(payload.get("origin_key") or sym),
        "plan_fingerprint": None,
        "direction": "increase" if delta > 0 else "reduce",
        "delta_czk": round(delta, 2),
        "delta_pct": payload.get("delta_pct"),
        "desired_weight_pct": payload.get("desired_weight_pct"),
        "route_policy": route,
        "route_selection": payload.get("route_selection"),
        "limit_price": limit_price,
        "status": str(payload.get("status") or "selected"),
        "defer_until": None,
        "dismiss_reason": None,
        "queued_leg_id": None,
        "created_at": now,
        "updated_at": now,
    }
    with _LOCK:
        state = load_plan(path)
        state.setdefault("items", []).append(item)
        return _write(state, path), item


def mark_queued(
    item_ids: list[str],
    basket: list[dict[str, Any]],
    *,
    path: Path = EXECUTION_PLAN_JSON,
) -> dict[str, Any]:
    ids = set(item_ids)
    if not ids:
        return load_plan(path)
    by_item: dict[str, str] = {}
    for leg in basket:
        leg_id = str(leg.get("leg_id") or "")
        for provenance in leg.get("provenance") or []:
            item_id = str(provenance.get("execution_item_id") or "")
            if item_id:
                by_item[item_id] = leg_id
    with _LOCK:
        state = load_plan(path)
        for item in state.get("items") or []:
            if item.get("id") in ids:
                item["status"] = "queued"
                item["queued_leg_id"] = by_item.get(str(item.get("id")))
                item["updated_at"] = _now()
        return _write(state, path)


def mark_submitted(*, path: Path = EXECUTION_PLAN_JSON) -> dict[str, Any]:
    with _LOCK:
        state = load_plan(path)
        changed = False
        for item in state.get("items") or []:
            if item.get("status") == "queued":
                item["status"] = "submitted"
                item["updated_at"] = _now()
                changed = True
        return _write(state, path) if changed else state


def reconcile_queue(
    basket: list[dict[str, Any]],
    *,
    path: Path = EXECUTION_PLAN_JSON,
) -> dict[str, Any]:
    """Return deleted/cleared queue items to selected intent."""
    present = {
        str(provenance.get("execution_item_id") or "")
        for leg in basket
        for provenance in leg.get("provenance") or []
        if isinstance(provenance, dict)
    }
    with _LOCK:
        state = load_plan(path)
        changed = False
        for item in state.get("items") or []:
            if item.get("status") != "queued":
                continue
            if str(item.get("id") or "") not in present:
                item["status"] = "selected"
                item["queued_leg_id"] = None
                item["updated_at"] = _now()
                changed = True
        return _write(state, path) if changed else state


def queue_selected(
    holdings: dict[str, Any],
    *,
    path: Path = EXECUTION_PLAN_JSON,
) -> dict[str, Any]:
    """Consolidate selected intent by symbol and append executable routes."""
    import rebalance_routes

    state = load_plan(path)
    selected = [
        item for item in state.get("items") or []
        if item.get("status") == "selected"
    ]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in selected:
        grouped.setdefault(clean_symbol(item.get("symbol")), []).append(item)
    trades: list[dict[str, Any]] = []
    selections: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    queued_ids: list[str] = []
    for symbol, items in grouped.items():
        delta = round(sum(float(item.get("delta_czk") or 0) for item in items), 2)
        if abs(delta) < 1:
            deferred.append({"symbol": symbol, "reason": "selected actions net to zero"})
            continue
        ids = [str(item.get("id")) for item in items]
        direct = "buy_shares" if delta > 0 else "sell_shares"
        option_kind = "cash_secured_put" if delta > 0 else "covered_call"
        explicit_direct = next(
            (
                item for item in reversed(items)
                if item.get("route_policy") == direct
            ),
            None,
        )
        explicit_option = next(
            (
                item for item in reversed(items)
                if isinstance(item.get("route_selection"), dict)
                and item["route_selection"].get("route") == option_kind
            ),
            None,
        )
        choice: dict[str, Any] = {"symbol": symbol, "route": direct}
        if explicit_option is not None and explicit_direct is None:
            choice.update(explicit_option["route_selection"])
        elif delta > 0 and explicit_direct is None:
            route = rebalance_routes.build_route(holdings, symbol, delta)
            rung = next(
                (
                    row for row in route.get("ladder") or []
                    if row.get("stageable") and row.get("conid")
                ),
                None,
            )
            if not route.get("option", {}).get("eligible") or rung is None:
                reason = " · ".join(route.get("option", {}).get("reasons") or [])
                deferred.append({
                    "symbol": symbol,
                    "reason": reason or "no executable cash-secured put",
                })
                for item in items:
                    item["status"] = "deferred"
                    item["updated_at"] = _now()
                continue
            choice = {
                "symbol": symbol,
                "route": option_kind,
                "conid": rung["conid"],
                "expiry": rung["expiry"],
                "strike": rung["strike"],
                "contracts": route["option"]["contracts"],
                "limit_price": rung.get("limit_price"),
            }
        explicit_limit: float | None = None
        for item in reversed(items):
            raw_limit = item.get("limit_price")
            if isinstance(raw_limit, (int, float)):
                explicit_limit = float(raw_limit)
                break
        if explicit_limit is not None:
            choice["limit_price"] = explicit_limit
        choice["execution_item_ids"] = ids
        trades.append({"symbol": symbol, "delta_czk": delta})
        selections.append(choice)
        queued_ids.extend(ids)
    if not trades:
        if deferred:
            _write(state, path)
        return {"state": state, "basket": [], "deferred": deferred}
    result = rebalance_routes.stage_routes(
        holdings,
        trades,
        selections,
        mode="append",
        source="execution_plan",
    )
    if deferred:
        _write(state, path)
    updated = mark_queued(queued_ids, result.get("basket") or [], path=path)
    return {**result, "state": updated, "deferred": deferred}
