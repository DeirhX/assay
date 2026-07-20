#!/usr/bin/env python3
"""Fold standalone top-level targets into allocation sleeves (the partition).

Grouping precedence for each standalone symbol:

1. ``provenance.home_segment`` when it names a sleeve (or a creatable tag)
2. Normalized ``targets[sym].sleeve`` via ``sleeve_aliases``
3. ETF ticker heuristic (``SMH`` / ``SOXX`` / ``XSD`` / … → ``semis-etf``)
4. Fallback sleeve ``other`` (honest migration debt)

The result is a list of staging changes: upsert sleeves (members + summed
bands + member_caps), then ``remove_target`` for each migrated name while
keeping provenance (with ``home_segment`` set). Nothing writes live — callers
must ``stage_migration`` → review → commit.
"""

from __future__ import annotations

from typing import Any

import rebalance
import segment_home
import sleeve_aliases
import target_staging
from config import TARGET_MODEL_JSON
from store import load as _load

# Display tickers that are sector/theme ETFs — land in the ETF sleeve when
# untagged rather than polluting ``other``.
_ETF_TICKERS = frozenset({
    "SMH", "SOXX", "XSD", "SOXL", "SOXS", "USD", "SEMI", "CHPS", "SHIPS",
})
DEFAULT_BUCKET = "other"
HALF_WIDTH_FLOOR = 0.5


def _members_of(model: dict[str, Any]) -> set[str]:
    return set(segment_home.membership_map(model))


def _resolve_dest(sym: str, band: dict[str, Any], prov: dict[str, Any],
                  *, known_sleeves: set[str], aliases: dict[str, str]) -> tuple[str, str]:
    """Return ``(sleeve_name, reason)``."""
    home = str((prov.get(sym) or {}).get("home_segment") or "").strip()
    if home:
        return home, "home_segment"
    tag = str((band or {}).get("sleeve") or "").strip()
    if tag:
        canon, known = sleeve_aliases.normalize(tag, aliases=aliases)
        if canon:
            # ``other`` is a deliberate bucket even if not yet a live sleeve.
            if known or canon == DEFAULT_BUCKET or canon in known_sleeves:
                return canon, f"tag:{tag}"
            return canon, f"tag:{tag} (new)"
    if sym.upper() in _ETF_TICKERS:
        return "semis-etf", "etf-heuristic"
    return DEFAULT_BUCKET, "fallback:other"


def _sum_band(parts: list[tuple[float, float]]) -> tuple[float, float]:
    low = round(sum(p[0] for p in parts), 1)
    high = round(sum(p[1] for p in parts), 1)
    if high < low:
        high = low
    # Keep a minimum width so the sleeve isn't a knife-edge.
    if high - low < HALF_WIDTH_FLOOR:
        mid = (low + high) / 2.0
        low = max(0.0, round(mid - HALF_WIDTH_FLOOR / 2, 1))
        high = min(100.0, round(mid + HALF_WIDTH_FLOOR / 2, 1))
    return low, high


def _pick_rule(rules: list[str]) -> str:
    if not rules:
        return "accumulate"
    if len(set(rules)) == 1:
        return rules[0]
    # Mixed: prefer the most conservative no-buy rule if present.
    for r in ("avoid", "reduce", "trim_only", "do_not_add", "wait", "hold", "accumulate"):
        if r in rules:
            return r
    return "hold"


def plan_migration(model: dict[str, Any] | None = None) -> dict[str, Any]:
    """Pure plan: which standalones fold into which sleeves. No disk writes."""
    model = model if model is not None else (_load(TARGET_MODEL_JSON) or {})
    targets = model.get("targets") or {}
    sleeves = model.get("sleeves") or {}
    prov = model.get("provenance") or {}
    known = set(sleeves)
    aliases = sleeve_aliases.load_aliases()
    already = _members_of(model)

    assignments: list[dict[str, Any]] = []
    by_sleeve: dict[str, list[str]] = {}
    for sym, band in sorted(targets.items()):
        if sym in already:
            continue
        if not isinstance(band, dict) or not rebalance._band_ok(band.get("low"), band.get("high")):
            assignments.append({
                "symbol": sym, "sleeve": None, "reason": "invalid-band", "skipped": True,
            })
            continue
        dest, reason = _resolve_dest(
            sym, band, prov, known_sleeves=known, aliases=aliases)
        assignments.append({
            "symbol": sym,
            "sleeve": dest,
            "reason": reason,
            "low": band.get("low"),
            "high": band.get("high"),
            "rule": band.get("rule"),
            "skipped": False,
        })
        by_sleeve.setdefault(dest, []).append(sym)

    sleeve_plans: list[dict[str, Any]] = []
    for name, syms in sorted(by_sleeve.items()):
        existing = dict(sleeves.get(name) or {})
        old_members = [str(m).upper() for m in (existing.get("members") or []) if m]
        new_members = sorted(set(old_members) | set(syms))
        band_parts: list[tuple[float, float]] = []
        rules: list[str] = []
        caps = dict(existing.get("member_caps") or {})
        if rebalance._band_ok(existing.get("low"), existing.get("high")) and old_members:
            # Existing sleeve band already covers old_members — keep it as the
            # baseline and *add* the newcomers' bands on top.
            band_parts.append((float(existing["low"]), float(existing["high"])))
            rules.append(str(existing.get("rule") or "accumulate"))
        for sym in syms:
            b = targets[sym]
            band_parts.append((float(b["low"]), float(b["high"])))
            rules.append(str(b.get("rule") or "hold"))
            caps[sym] = float(b["high"])
        low, high = _sum_band(band_parts)
        sleeve_plans.append({
            "name": name,
            "exists": name in sleeves,
            "members_before": old_members,
            "members_after": new_members,
            "adding": sorted(syms),
            "low": low,
            "high": high,
            "rule": _pick_rule(rules),
            "member_caps": caps,
        })

    return {
        "assignments": assignments,
        "sleeves": sleeve_plans,
        "counts": {
            "standalone": len([a for a in assignments if not a.get("skipped")]),
            "skipped": len([a for a in assignments if a.get("skipped")]),
            "sleeves_touched": len(sleeve_plans),
        },
    }


def build_changes(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn a ``plan_migration`` result into staging change records."""
    changes: list[dict[str, Any]] = []
    for sp in plan.get("sleeves") or []:
        proposed = {
            "low": sp["low"],
            "high": sp["high"],
            "rule": sp["rule"],
            "members": list(sp["members_after"]),
            "member_caps": dict(sp.get("member_caps") or {}),
            "note": "Migrated standalone targets into allocation sleeve.",
        }
        action = "modify_sleeve" if sp.get("exists") else "add_sleeve"
        changes.append({
            "action": action,
            "sleeve": sp["name"],
            "proposed_sleeve": proposed,
            "home_segment": sp["name"],
        })
    for a in plan.get("assignments") or []:
        if a.get("skipped") or not a.get("sleeve"):
            continue
        changes.append({
            "action": "remove_target",
            "symbol": a["symbol"],
            "preserve_provenance": True,
            "home_segment": a["sleeve"],
        })
    return changes


def stage_migration(*, model: dict[str, Any] | None = None) -> dict[str, Any]:
    """Plan + stage into the working draft. Review under Pending model changes."""
    plan = plan_migration(model)
    if not plan["counts"]["standalone"]:
        return {"staged": False, "plan": plan, "reason": "no standalone targets to migrate"}
    changes = build_changes(plan)
    result = target_staging.stage_changes(
        changes, source="sleeve-migrate", allow_rehome=True)
    result["plan"] = plan
    result["staged"] = True
    return result


def _resolve_change_sleeve(change: dict[str, Any], *, known_sleeves: set[str],
                           aliases: dict[str, str]) -> str | None:
    """Allocation sleeve implied by a construct/proposal change, or None."""
    if change.get("sleeve_unknown"):
        return None
    explicit = str(change.get("home_segment") or "").strip()
    if explicit:
        canon, _known = sleeve_aliases.normalize(explicit, aliases=aliases)
        tag = canon or explicit
        return tag if tag in known_sleeves else None
    pt = change.get("proposed_target") or {}
    raw = ""
    if isinstance(pt, dict):
        raw = str(pt.get("sleeve") or "").strip()
    if not raw:
        raw = str(change.get("sleeve") or "").strip()
    if not raw:
        return None
    canon, _known = sleeve_aliases.normalize(raw, aliases=aliases)
    tag = canon or raw
    return tag if tag in known_sleeves else None


def fold_target_changes_into_sleeves(
    changes: list[dict[str, Any]] | None,
    model: dict[str, Any],
    *,
    aliases: dict[str, str] | None = None,
    allow_rehome: bool = False,
) -> list[dict[str, Any]]:
    """Rewrite ``add_target`` / ``modify_target`` with a known allocation sleeve
    into sleeve roster mutations (+ ``remove_target`` when a standalone exists).

    Unknown / missing sleeve tags stay as standalone target changes (honest debt
    until an alias or sleeve exists). Already-members of a *different* sleeve are
    left alone so ``stage_changes`` can surface the home conflict.
    """
    if not changes:
        return []
    aliases = aliases if aliases is not None else sleeve_aliases.load_aliases()
    sleeves = model.get("sleeves") or {}
    targets = model.get("targets") or {}
    known = set(sleeves)
    members = segment_home.membership_map(model)

    passthrough: list[dict[str, Any]] = []
    # sleeve -> list of (sym, proposed_target dict)
    pending: dict[str, list[tuple[str, dict[str, Any]]]] = {}

    for ch in changes:
        act = ch.get("action")
        if act not in ("add_target", "modify_target"):
            passthrough.append(ch)
            continue
        sym = str(ch.get("symbol") or "").strip().upper()
        if not sym:
            passthrough.append(ch)
            continue
        dest = _resolve_change_sleeve(ch, known_sleeves=known, aliases=aliases)
        if not dest:
            passthrough.append(ch)
            continue
        member_home = members.get(sym)
        if member_home and member_home != dest:
            # Never auto-rehome an existing sleeve member via fold — leave the
            # target change so stage_changes can skip (or apply with allow_rehome
            # as a standalone, which is still wrong) rather than dual-list.
            passthrough.append(ch)
            continue
        raw_pt = ch.get("proposed_target")
        pt: dict[str, Any] = dict(raw_pt) if isinstance(raw_pt, dict) else {}
        if member_home == dest:
            # Already partitioned here — optionally refresh member_caps only.
            if rebalance._band_ok(pt.get("low"), pt.get("high")):
                pending.setdefault(dest, []).append((sym, pt))
            continue
        # Provenance-only home on a standalone may still conflict.
        reason = segment_home.conflict_reason(
            model, sym, dest, allow_rehome=allow_rehome)
        if reason:
            passthrough.append(ch)
            continue
        if not rebalance._band_ok(pt.get("low"), pt.get("high")):
            passthrough.append(ch)
            continue
        pending.setdefault(dest, []).append((sym, pt))

    if not pending:
        return passthrough

    folded: list[dict[str, Any]] = []
    remove_syms: list[tuple[str, str]] = []
    for name, items in sorted(pending.items()):
        existing = dict(sleeves.get(name) or {})
        old_members = [str(m).upper() for m in (existing.get("members") or []) if m]
        adding = [sym for sym, _ in items if sym not in old_members]
        new_members = sorted(set(old_members) | {sym for sym, _ in items})
        caps = dict(existing.get("member_caps") or {})
        band_parts: list[tuple[float, float]] = []
        rules: list[str] = []
        if rebalance._band_ok(existing.get("low"), existing.get("high")) and old_members:
            band_parts.append((float(existing["low"]), float(existing["high"])))
            rules.append(str(existing.get("rule") or "accumulate"))
        for sym, pt in items:
            caps[sym] = float(pt["high"])
            if sym in adding:
                band_parts.append((float(pt["low"]), float(pt["high"])))
                rules.append(str(pt.get("rule") or "hold"))
            if sym in targets:
                remove_syms.append((sym, name))
        if band_parts:
            low, high = _sum_band(band_parts)
        elif rebalance._band_ok(existing.get("low"), existing.get("high")):
            low, high = float(existing["low"]), float(existing["high"])
        else:
            # Cap-only refresh with no usable band — skip sleeve mutation.
            continue
        proposed = {
            "low": low,
            "high": high,
            "rule": _pick_rule(rules) if rules else str(existing.get("rule") or "accumulate"),
            "members": new_members,
            "member_caps": caps,
        }
        if existing.get("note"):
            proposed["note"] = existing["note"]
        folded.append({
            "action": "modify_sleeve" if name in sleeves else "add_sleeve",
            "sleeve": name,
            "proposed_sleeve": proposed,
            "home_segment": name,
        })

    for sym, home in remove_syms:
        folded.append({
            "action": "remove_target",
            "symbol": sym,
            "preserve_provenance": True,
            "home_segment": home,
        })
    return passthrough + folded
