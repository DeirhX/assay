#!/usr/bin/env python3
"""Decision-support overlay for rebalance-plan rows: research context + price gate.

Extracted from serve.py. Given a rebalance plan, this enriches each held target
row, in place, with:

* a compact ``research`` object read from the per-ticker dossier (data quality,
  decision, 3m momentum, thesis action/summary) plus a ``research_conflict`` flag
  when the band's suggested action and the human thesis point opposite ways, and
* a ``price_gate`` derived from any locked price level: a level that isn't
  favorable yet downgrades the action to ``"wait"``; a partially-live ladder
  scales the suggested delta down to the live fraction.

Nothing here feeds the trade math -- it is decision support only, and every step
is best-effort so a missing/malformed dossier or level never breaks the planner.
Public names are underscore-free; serve.py imports attach_research_overlay
aliased to its existing private name.
"""

from __future__ import annotations

import hygiene
import price_levels
from config import RESEARCH_DIR
from store import load
from symbols import resolve_symbol


# How a thesis verdict leans. Used only to flag when the deterministic band action
# and the human thesis disagree -- never to size a trade. Thesis actions are free
# text from the dossier form, so the match is loose and lowercased.
_THESIS_ADD_LIKE = {"add", "accumulate", "buy", "build", "increase", "overweight"}
_THESIS_TRIM_LIKE = {"trim", "sell", "reduce", "exit", "avoid", "underweight", "do_not_add"}


def research_conflict(row_action: str | None, thesis_action: str | None) -> bool:
    """True when the band's suggested action and the thesis verdict point opposite
    ways: trimming a name the thesis wants more of, or buying one it wants less
    of. Anything else (no thesis, agreement, a neutral hold/wait) is not a
    conflict."""
    ta = (thesis_action or "").lower().strip()
    if not ta:
        return False
    if row_action == "trim" and ta in _THESIS_ADD_LIKE:
        return True
    if row_action == "buy" and ta in _THESIS_TRIM_LIKE:
        return True
    return False


def research_overlay(provider_sym: str) -> dict | None:
    """Compact, independent research context for one rebalance row, read from the
    per-ticker dossier. Returns None when there's no dossier so the row reads as
    'no signal'. Nothing here feeds the trade math -- it is decision support
    only. ``research_score`` is deliberately omitted (it is segment-only, not on
    per-ticker files)."""
    rec = load(RESEARCH_DIR / f"{provider_sym}.json")
    if not isinstance(rec, dict):
        return None
    thesis = rec.get("thesis") if isinstance(rec.get("thesis"), dict) else {}
    momentum = rec.get("momentum") if isinstance(rec.get("momentum"), dict) else {}
    return {
        "as_of": rec.get("as_of"),
        "data_quality": hygiene.worst_severity(rec.get("cross_checks") or []),
        "decision": rec.get("decision"),
        "momentum_3m_pct": momentum.get("chg_3m_pct"),
        "thesis_action": (thesis.get("action") or "").strip() or None,
        "thesis_summary": (thesis.get("summary") or "").strip() or None,
        "thesis_as_of": thesis.get("as_of"),
    }


def mark_price_map(holdings: dict | None) -> dict[str, dict]:
    """symbol -> {price, currency} from the holdings snapshot's marks. The same
    marks the plan was sized against, so the price gate compares apples to
    apples (instrument currency)."""
    out: dict[str, dict] = {}
    for p in (holdings or {}).get("positions") or []:
        sym = str(p.get("symbol") or "").strip().upper()
        price = p.get("mark_price")
        if sym and isinstance(price, (int, float)) and price:
            out[sym] = {"price": float(price), "currency": (p.get("currency") or "").upper()}
    return out


def gate_current_price(name: str, provider_sym: str, price_map: dict[str, dict]) -> float | None:
    """Current instrument price for the gate: the holdings mark first (what the
    plan was sized on), else the dossier's last spot. None when neither knows."""
    held = price_map.get((name or "").upper())
    if held and isinstance(held.get("price"), (int, float)):
        return float(held["price"])
    rec = load(RESEARCH_DIR / f"{provider_sym}.json")
    if isinstance(rec, dict):
        pv = rec.get("price")
        if isinstance(pv, dict) and isinstance(pv.get("value"), (int, float)):
            return float(pv["value"])
    return None


def apply_price_gate(row: dict, provider_sym: str, price_map: dict[str, dict]) -> None:
    """Attach a ``price_gate`` to a target row from its locked level (if any) and
    the current price, and GRADE the suggested move by how much of the ladder the
    price currently unlocks:

    * fraction 0  -> nothing triggered yet: downgrade the action to ``"wait"``.
    * 0 < f < 1   -> some tranches live: keep the action but scale the band's
      suggested delta down to the live fraction (``full_*`` keeps the original).
    * fraction 1  -> fully unlocked: act on the whole band-implied delta.

    The weight band still sets the *target* delta; the ladder decides how much of
    it to act on now, and at what price. No level -> nothing changes."""
    level = price_levels.get(provider_sym)
    if not level:
        return
    current = gate_current_price(row.get("name") or "", provider_sym, price_map)
    gate = price_levels.evaluate(level, current)
    if not gate:
        return
    action = row.get("action")
    side = "buy" if action == "buy" else ("trim" if action == "trim" else None)
    total = gate["buy_total"] if side == "buy" else (gate["trim_total"] if side == "trim" else 0)
    if side and gate["price_known"] and total:
        fraction = gate["buy_fraction"] if side == "buy" else gate["trim_fraction"]
        gate["applied_fraction"] = fraction
        if fraction <= 0:
            gate["blocked_action"] = side
            row["action"] = "wait"
        elif fraction < 1.0:
            gate["partial"] = True
            for key in ("suggest_delta_pct", "suggest_delta_czk"):
                val = row.get(key)
                if isinstance(val, (int, float)):
                    gate["full_" + key] = val
                    row[key] = round(val * fraction, 2) if key.endswith("pct") else round(val * fraction)
    row["price_gate"] = gate


def attach_research_overlay(plan: dict, holdings: dict | None = None) -> None:
    """Enrich each held target row of a rebalance plan, in place, with a compact
    ``research`` object + a ``research_conflict`` flag, and a ``price_gate`` from
    any locked price level (downgrading the action to ``"wait"`` when the price
    isn't favorable yet). Best-effort: a missing/malformed dossier or level is
    skipped silently so the planner always renders."""
    price_map = mark_price_map(holdings)
    for row in plan.get("rows") or []:
        if row.get("kind") != "target" or not row.get("held"):
            continue
        try:
            provider_sym = resolve_symbol(row.get("name") or "")
        except Exception:  # noqa: BLE001 - a bad symbol shouldn't break the plan
            continue
        try:
            overlay = research_overlay(provider_sym)
        except Exception:  # noqa: BLE001 - the overlay is optional; never break the plan
            overlay = None
        if overlay:
            row["research"] = overlay
            row["research_conflict"] = research_conflict(row.get("action"), overlay["thesis_action"])
        try:
            apply_price_gate(row, provider_sym, price_map)
        except Exception:  # noqa: BLE001 - gating is decision support, never fatal
            pass
