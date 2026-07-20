#!/usr/bin/env python3
"""Allocation-segment home assignment.

Allocation segments are the model's ``sleeves`` — a partition of the invested
book. Research *topics* (``data/segments/*.json``) may overlap freely; each
governed symbol has at most one ``home_segment`` (a sleeve name).

Resolution order for a symbol's home:

1. Sleeve membership in ``model.sleeves`` (authoritative — already partitioned
   by ``rebalance.check_model``)
2. ``provenance[sym].home_segment`` (durable assignment for standalone targets)
3. ``targets[sym].sleeve`` when it names an existing allocation sleeve

``provenance.segment`` remains research *lineage* (which topic last staged the
band) and must NOT be confused with home.
"""

from __future__ import annotations

from typing import Any


def sleeve_names(model: dict[str, Any]) -> set[str]:
    return {str(n) for n in (model.get("sleeves") or {}) if str(n).strip()}


def membership_map(model: dict[str, Any]) -> dict[str, str]:
    """symbol -> sleeve name for every allocation-sleeve member."""
    out: dict[str, str] = {}
    for name, sl in (model.get("sleeves") or {}).items():
        if not isinstance(sl, dict):
            continue
        for m in sl.get("members") or []:
            sym = str(m or "").strip().upper()
            if sym and sym not in out:
                out[sym] = str(name)
    return out


def resolve_home(model: dict[str, Any], sym: str) -> str | None:
    """Return the allocation segment that owns *sym*, or None if unassigned."""
    key = str(sym or "").strip().upper()
    if not key:
        return None
    members = membership_map(model)
    if key in members:
        return members[key]
    prov = (model.get("provenance") or {}).get(key) or {}
    if isinstance(prov, dict):
        home = str(prov.get("home_segment") or "").strip()
        if home:
            return home
    band = (model.get("targets") or {}).get(key) or {}
    tag = str(band.get("sleeve") or "").strip()
    if tag and tag in sleeve_names(model):
        return tag
    return None


def home_map(model: dict[str, Any]) -> dict[str, str]:
    """Every governed symbol with a resolvable home -> segment name."""
    managed = set(model.get("targets") or {}) | set(membership_map(model))
    out: dict[str, str] = {}
    for sym in managed:
        home = resolve_home(model, sym)
        if home:
            out[sym] = home
    return out


def ensure_homes(model: dict[str, Any]) -> dict[str, str]:
    """Backfill ``provenance[sym].home_segment`` from sleeve membership and
    compatible target tags. Mutates *model* in place. Returns the resulting
    home map (including membership-derived homes)."""
    prov = model.setdefault("provenance", {})
    known = sleeve_names(model)
    members = membership_map(model)
    for sym, sleeve in members.items():
        rec = prov.setdefault(sym, {"source": "legacy-plan"})
        if isinstance(rec, dict) and not rec.get("home_segment"):
            rec["home_segment"] = sleeve
    for sym, band in (model.get("targets") or {}).items():
        if sym in members:
            continue
        rec = prov.setdefault(sym, {"source": "legacy-plan"})
        if not isinstance(rec, dict) or rec.get("home_segment"):
            continue
        tag = str((band or {}).get("sleeve") or "").strip()
        if tag and tag in known:
            rec["home_segment"] = tag
    return home_map(model)


def validate_homes(model: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Return ``(severity, area, message)`` triples for home-partition issues.

    * ERROR — provenance home disagrees with sleeve membership, or names an
      unknown sleeve.
    * WARN — managed symbol with no home (orphan / unassigned).
    """
    findings: list[tuple[str, str, str]] = []
    known = sleeve_names(model)
    members = membership_map(model)
    managed = set(model.get("targets") or {}) | set(members)
    prov = model.get("provenance") or {}

    for sym in sorted(managed):
        area = f"home:{sym}"
        member_home = members.get(sym)
        rec = prov.get(sym) if isinstance(prov.get(sym), dict) else {}
        declared = str((rec or {}).get("home_segment") or "").strip() or None
        tag = str(((model.get("targets") or {}).get(sym) or {}).get("sleeve") or "").strip()

        if declared and known and declared not in known:
            # Allow declared homes that aren't sleeves yet only when the model
            # has *no* sleeves — otherwise it's a typo / stale label.
            if known:
                findings.append((
                    "ERROR", area,
                    f"home_segment '{declared}' is not an allocation sleeve "
                    f"(known: {', '.join(sorted(known))}).",
                ))
        if member_home and declared and declared != member_home:
            findings.append((
                "ERROR", area,
                f"home_segment '{declared}' disagrees with sleeve membership "
                f"'{member_home}'.",
            ))
        home = member_home or declared or (tag if tag in known else None)
        if not home:
            findings.append((
                "WARN", area,
                "no home_segment — assign to an allocation sleeve (or leave "
                "explicitly unassigned until migrated).",
            ))
    return findings


def conflict_reason(model: dict[str, Any], sym: str, proposed_home: str | None,
                    *, allow_rehome: bool = False) -> str | None:
    """If staging *sym* into *proposed_home* would break the partition, return
    a skip reason; otherwise None."""
    if not proposed_home or allow_rehome:
        return None
    key = str(sym or "").strip().upper()
    current = resolve_home(model, key)
    if current and current != proposed_home:
        return (
            f"home_segment conflict: already assigned to '{current}' "
            f"(proposed '{proposed_home}'; pass allow_rehome to move)"
        )
    return None
