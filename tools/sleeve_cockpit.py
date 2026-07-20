#!/usr/bin/env python3
"""Allocation-segment (sleeve) cockpit payload.

Read-only view of one sleeve: aggregate band vs held weight, per-member
targets/caps/plan deltas from ``rebalance.plan``, plus advisory OC ranking
from dossiers / provenance convictions. Research *topics* are a separate
lens; this page is the partition that owns capital.
"""

from __future__ import annotations

from typing import Any

import opportunity_cost
import portfolio
import rebalance
import segment_home
import target_staging
from config import HOLDINGS_JSON, RESEARCH_DIR
from store import load as _load


def _metric_val(node: Any) -> float | None:
    if isinstance(node, (int, float)):
        return float(node)
    if isinstance(node, dict) and isinstance(node.get("value"), (int, float)):
        return float(node["value"])
    return None


def _dossier_row(sym: str, model: dict[str, Any]) -> dict[str, Any]:
    """Build an OC-input row from the cached dossier + provenance."""
    prov = (model.get("provenance") or {}).get(sym) or {}
    rec = _load(RESEARCH_DIR / f"{sym}.json") or {}
    metrics = rec.get("metrics") or {}
    worst = "INFO"
    for check in rec.get("cross_checks") or []:
        sev = str((check or {}).get("severity") or "").upper()
        if sev == "ERROR":
            worst = "ERROR"
            break
        if sev == "WARN" and worst != "ERROR":
            worst = "WARN"
    try:
        decision = portfolio.decision_label(portfolio.portfolio_context(sym))
    except Exception:  # noqa: BLE001
        decision = None
    return {
        "symbol": sym,
        "conviction": prov.get("conviction") if isinstance(prov, dict) else None,
        "ps": _metric_val(metrics.get("ps")),
        "pe_fwd": _metric_val(metrics.get("pe_fwd")),
        "data_quality": worst,
        "decision": decision,
    }


def index(*, model: dict[str, Any] | None = None,
          holdings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Light list of allocation sleeves for Composition / pickers."""
    model = model if model is not None else target_staging.active_model()
    holdings = holdings if holdings is not None else (_load(HOLDINGS_JSON) or {})
    weights = rebalance.current_weights(holdings) if holdings else {}
    sleeves = []
    for name, sl in sorted((model.get("sleeves") or {}).items()):
        if not isinstance(sl, dict):
            continue
        members = [str(m).upper() for m in (sl.get("members") or []) if m]
        cur = sum(weights.get(m, 0.0) for m in members)
        low, high = sl.get("low"), sl.get("high")
        status = None
        target_pct = None
        if (isinstance(low, (int, float)) and isinstance(high, (int, float))
                and rebalance._band_ok(low, high)):
            status = rebalance._status(cur, float(low), float(high))
            target_pct = round((float(low) + float(high)) / 2, 2)
        sleeves.append({
            "name": name,
            "member_count": len(members),
            "members": members,
            "current_pct": round(cur, 2),
            "low": low,
            "high": high,
            "target_pct": target_pct,
            "status": status,
            "rule": sl.get("rule") or "accumulate",
            "note": sl.get("note") or "",
        })
    draft = target_staging.load_staged(create=False)
    return {
        "as_of": model.get("as_of"),
        "staged": {"has_draft": isinstance(draft, dict)},
        "sleeves": sleeves,
    }


def detail(name: str, *, model: dict[str, Any] | None = None,
           holdings: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Full cockpit for one allocation sleeve, or None if unknown."""
    name = str(name or "").strip()
    if not name:
        return None
    model = model if model is not None else target_staging.active_model()
    holdings = holdings if holdings is not None else (_load(HOLDINGS_JSON) or {})
    sl = (model.get("sleeves") or {}).get(name)
    if not isinstance(sl, dict):
        return None

    plan = rebalance.plan(model, holdings) if holdings else {"rows": []}
    plan_row = next(
        (r for r in (plan.get("rows") or [])
         if r.get("kind") == "sleeve" and r.get("name") == name),
        None,
    )

    members_plan = {
        str(m.get("symbol") or "").upper(): m
        for m in ((plan_row or {}).get("members") or [])
        if m.get("symbol")
    }
    syms = [str(m).upper() for m in (sl.get("members") or []) if m]
    weights = rebalance.current_weights(holdings) if holdings else {}
    caps = sl.get("member_caps") or {}

    research_rows = []
    for sym in syms:
        row = _dossier_row(sym, model)
        mp = members_plan.get(sym) or {}
        if not row.get("conviction") and mp.get("conviction"):
            row["conviction"] = mp["conviction"]
        research_rows.append(row)

    ranked = opportunity_cost.annotate_segment_members(
        research_rows, home_by_symbol=segment_home.home_map(model))
    by_sym = {str(r.get("symbol") or "").upper(): r for r in ranked}

    members_out = []
    for sym in syms:
        mp = dict(members_plan.get(sym) or {
            "symbol": sym,
            "current_pct": round(weights.get(sym, 0.0), 2),
            "target_pct": None,
            "cap": caps.get(sym),
            "suggest_delta_pct": 0.0,
            "member_action": None,
            "conviction": None,
        })
        oc = by_sym.get(sym) or {}
        for key in ("prospect", "edge", "oc_score", "oc_rank", "data_quality",
                    "decision", "ps", "pe_fwd", "home_segment"):
            if key in oc and oc[key] is not None:
                mp[key] = oc[key]
        if oc.get("conviction") and not mp.get("conviction"):
            mp["conviction"] = oc["conviction"]
        members_out.append(mp)

    def _sort_key(m: dict[str, Any]) -> tuple:
        rank = m.get("oc_rank")
        if rank is None:
            return (1, m.get("order") or 999, m.get("symbol") or "")
        return (0, int(rank), m.get("symbol") or "")

    members_out.sort(key=_sort_key)

    cur = sum(weights.get(m, 0.0) for m in syms)
    low, high = sl.get("low"), sl.get("high")
    mid = None
    status = None
    if isinstance(low, (int, float)) and isinstance(high, (int, float)):
        low_f, high_f = float(low), float(high)
        if rebalance._band_ok(low_f, high_f):
            mid = (low_f + high_f) / 2
            status = rebalance._status(cur, low_f, high_f)

    sleeve_out = {
        "name": name,
        "low": low,
        "high": high,
        "mid": round(mid, 2) if mid is not None else None,
        "rule": sl.get("rule") or "accumulate",
        "note": sl.get("note") or "",
        "member_caps": dict(sl.get("member_caps") or {}),
        "current_pct": round(cur, 2),
        "status": status,
    }
    if plan_row:
        for key in ("action", "suggest_delta_pct", "suggest_delta_czk",
                    "current_czk", "status"):
            if key in plan_row:
                sleeve_out[key] = plan_row[key]

    # Persist advisory ranks so rebalance can optionally prefer them later.
    opportunity_cost.store_sleeve_ranks(name, members_out)

    draft = target_staging.load_staged(create=False)
    return {
        "name": name,
        "as_of": model.get("as_of"),
        "staged": {"has_draft": isinstance(draft, dict)},
        "sleeve": sleeve_out,
        "members": members_out,
    }
