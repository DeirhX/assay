#!/usr/bin/env python3
"""Target-model mutation and rebalance preview.

Extracted from serve.py. Applies LLM/deterministic target-proposal change
records onto the committed target model (with a reversible backup) and computes
the rebalance plan a proposal *would* produce without writing anything -- the
shared engine behind the Gate-2 preview, the final recommendation, and the
direct apply endpoint. Disk paths come from config; site regeneration is reused
from holdings_sync so an apply keeps the static plan in lockstep.
"""

from __future__ import annotations

import copy
import datetime as dt

import rebalance
import tax_lots
from config import DATA_DIR, DEEP_DIR, HOLDINGS_JSON, REPO_ROOT, TARGET_MODEL_JSON
from holdings_sync import regenerate_site
from store import (
    load as _load, safe_symbol as _safe_symbol,
    slugify as _slugify, write_json as _write_json,
)

# Backups of the target model before a mutating apply -- target-model-only, so
# it lives here rather than on the shared config leaf.
TARGET_MODEL_BACKUP_DIR = DATA_DIR / "backups"

# The only band keys an apply is allowed to write; anything else in a proposed
# target (conviction, sleeve hints, ...) is stripped so the model schema can't
# silently widen.
_TARGET_WRITE_KEYS = ("low", "high", "rule", "note", "structural")


def _clean_target(raw: dict) -> dict:
    return {k: raw[k] for k in _TARGET_WRITE_KEYS if k in raw}


def _apply_changes_to_model(model: dict, changes: list, *, blocked: set) -> tuple[list, list]:
    """Apply proposal change records onto `model` IN PLACE. Pure with respect to
    disk so it is shared by the live apply (which then writes) and the Gate-2
    preview (which works on a throwaway copy). Returns (applied, skipped).

    Supported actions: add_target (new band), modify_target (merge onto the
    existing band, preserving keys the proposal didn't touch), and a guarded
    sleeve upsert. Anything else is recorded as skipped rather than silently
    dropped, so an unexpected action is visible instead of a no-op."""
    targets = model.setdefault("targets", {})
    sleeves = model.setdefault("sleeves", {})
    applied: list = []
    skipped: list = []
    for change in changes or []:
        action = change.get("action")
        if action in ("add_target", "modify_target"):
            try:
                sym = _safe_symbol(change.get("symbol", ""))
            except ValueError:
                skipped.append({"symbol": change.get("symbol"), "reason": "invalid symbol"})
                continue
            # Never derive a band from a ticker whose deterministic data failed an
            # ERROR-level check (override only on an explicit allow_blocked).
            if sym in blocked:
                skipped.append({"symbol": sym, "reason": "blocked: ERROR-level deterministic data; resolve before applying"})
                continue
            pt = _clean_target(dict(change.get("proposed_target") or {}))
            if not pt:
                skipped.append({"symbol": sym, "reason": "missing proposed_target"})
                continue
            if action == "add_target":
                if sym in targets:
                    skipped.append({"symbol": sym, "reason": "target already exists"})
                    continue
                targets[sym] = pt
            else:  # modify_target merges so structural bands / unrelated keys survive
                cur = dict(targets.get(sym) or {})
                cur.update(pt)
                targets[sym] = cur
            applied.append(sym)
        elif action in ("add_sleeve", "modify_sleeve", "set_sleeve"):
            name = str(change.get("sleeve") or change.get("name") or "").strip()
            proposed = change.get("proposed_sleeve")
            if not name or not isinstance(proposed, dict):
                skipped.append({"symbol": name or "(sleeve)", "reason": "missing sleeve name or definition"})
                continue
            cur = dict(sleeves.get(name) or {})
            cur.update(proposed)
            sleeves[name] = cur
            applied.append(f"[{name}]")
        else:
            skipped.append({"symbol": change.get("symbol"), "reason": f"unsupported action: {action}"})
    return applied, skipped


def _backup_target_model() -> str | None:
    """Snapshot the current target model before mutating it, so an apply is
    reversible. Returns the backup's repo-relative path, or None if there was
    nothing to back up."""
    model = _load(TARGET_MODEL_JSON)
    if not model:
        return None
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = TARGET_MODEL_BACKUP_DIR / f"target-model-{ts}.json"
    _write_json(backup, model)
    return str(backup.relative_to(REPO_ROOT))


def apply_target_proposal(segment: str, date: str, confirm: bool, *, allow_blocked: bool = False) -> dict:
    if not confirm:
        raise ValueError("confirm=true is required")
    segment = _slugify(segment)
    proposal_path = DEEP_DIR / f"{segment}-{date}.target-proposal.json"
    proposal = _load(proposal_path)
    if not proposal:
        raise ValueError(f"proposal not found: {proposal_path.relative_to(REPO_ROOT)}")
    model = _load(TARGET_MODEL_JSON)
    if not model:
        raise ValueError("target model not found")
    blocked = set(proposal.get("blocked_symbols", [])) if not allow_blocked else set()
    backup = _backup_target_model()
    applied, skipped = _apply_changes_to_model(model, proposal.get("changes", []), blocked=blocked)
    proposal["status"] = "applied" if applied else "reviewed"
    proposal["applied_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    proposal["applied_symbols"] = applied
    proposal["skipped"] = skipped
    _write_json(TARGET_MODEL_JSON, model)
    _write_json(proposal_path, proposal)
    # Keep the static plan in lockstep with the model the apply just changed.
    site = regenerate_site()
    return {"applied": applied, "skipped": skipped, "proposal": proposal, "backup": backup, "site": site}


def preview_plan_for_proposal(proposal: dict, *, allow_blocked: bool = False) -> dict:
    """Compute the rebalance plan that WOULD result from a proposal, against a
    throwaway copy of the model -- nothing is written. Powers the Gate-2 preview
    (with the proposal's changes) and the final recommendation (empty changes,
    i.e. the already-committed model)."""
    model = _load(TARGET_MODEL_JSON)
    holdings = _load(HOLDINGS_JSON)
    if not model or not holdings:
        return {"available": False, "reason": "need both a target model and a holdings snapshot to preview a rebalance"}
    draft = copy.deepcopy(model)
    blocked = set(proposal.get("blocked_symbols", [])) if not allow_blocked else set()
    applied, skipped = _apply_changes_to_model(draft, proposal.get("changes", []), blocked=blocked)
    try:
        plan = tax_lots.enrich_plan(rebalance.plan(draft, holdings), holdings)
    except Exception as exc:  # noqa: BLE001 - a bad band shouldn't kill the gate
        return {"available": False, "reason": f"could not compute plan: {exc}"}
    return {"available": True, "applied": applied, "skipped": skipped, "plan": plan}
