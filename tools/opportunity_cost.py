#!/usr/bin/env python3
"""Within-segment opportunity-cost ranking (advisory).

Judgement (prospect / conviction) stays separate from the math. Given a list of
peer rows with a prospect score and optional valuation / quality signals, this
module produces a comparable ``oc_score`` and dense ``oc_rank`` (1 = best).

Opportunity cost here means: capital parked in name A instead of the next-best
name (or cash, edge 0) *inside the same segment*. It is not an order signal —
band discipline still owns execution.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from config import DATA_DIR
from store import load as _load, write_json as _write_json

# Sidecar cache — advisory ranks, not part of the staged target-model draft.
OC_RANKS_JSON = DATA_DIR / "cache" / "oc-ranks.json"

# Map qualitative LLM / heuristic convictions onto a 0–100 prospect scale.
_CONVICTION_PROSPECT = {
    "high": 85.0,
    "medium": 65.0,
    "low": 45.0,
    "avoid": 10.0,
}


def prospect_from_row(row: dict[str, Any]) -> float | None:
    """Extract a 0–100 prospect. Prefers explicit ``prospect``, then conviction
    tiers, then ``research_score`` (legacy peer heuristic)."""
    raw = row.get("prospect")
    if isinstance(raw, (int, float)):
        return max(0.0, min(100.0, float(raw)))
    conv = str(row.get("conviction") or "").lower().strip()
    if conv in _CONVICTION_PROSPECT:
        return _CONVICTION_PROSPECT[conv]
    score = row.get("research_score")
    if isinstance(score, (int, float)):
        return max(0.0, min(100.0, float(score)))
    return None


def _valuation_penalty(row: dict[str, Any], peer_ps: list[float],
                       peer_pe: list[float]) -> float:
    """Penalize rich names vs segment peers. Returns 0..35 points to subtract."""
    penalty = 0.0
    ps = row.get("ps")
    if isinstance(ps, (int, float)) and peer_ps:
        med = sorted(peer_ps)[len(peer_ps) // 2]
        if med > 0 and ps > med:
            # Up to 20 pts when 2× the peer median P/S.
            penalty += min(20.0, 20.0 * ((ps / med) - 1.0))
    pe = row.get("pe_fwd")
    if isinstance(pe, (int, float)) and pe > 0 and peer_pe:
        med = sorted(peer_pe)[len(peer_pe) // 2]
        if med > 0 and pe > med:
            penalty += min(15.0, 15.0 * ((pe / med) - 1.0))
    return penalty


def _quality_penalty(row: dict[str, Any]) -> float:
    dq = str(row.get("data_quality") or "").upper()
    if dq == "ERROR":
        return 40.0
    if dq == "WARN":
        return 10.0
    decision = str(row.get("decision") or "").lower()
    if "avoid" in decision:
        return 25.0
    return 0.0


def edge_score(row: dict[str, Any], *, peer_ps: list[float],
               peer_pe: list[float]) -> float | None:
    """Prospect minus valuation/quality drag. None when no prospect signal."""
    prospect = prospect_from_row(row)
    if prospect is None:
        return None
    return prospect - _valuation_penalty(row, peer_ps, peer_pe) - _quality_penalty(row)


def rank_members(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Annotate each row with ``prospect``, ``edge``, ``oc_score``, ``oc_rank``.

    ``oc_score`` = edge − marginal edge of the worst name still in the scored
    set (cash opportunity = 0). Positive means the name beats the sleeve's
    weakest claim on capital; negative means it's the capital sink.
    """
    if not rows:
        return []
    peer_ps = [float(r["ps"]) for r in rows if isinstance(r.get("ps"), (int, float))]
    peer_pe = [float(r["pe_fwd"]) for r in rows
               if isinstance(r.get("pe_fwd"), (int, float)) and float(r["pe_fwd"]) > 0]

    scored: list[tuple[int, float, dict[str, Any]]] = []
    unscored: list[dict[str, Any]] = []
    for i, raw in enumerate(rows):
        row = dict(raw)
        edge = edge_score(row, peer_ps=peer_ps, peer_pe=peer_pe)
        prospect = prospect_from_row(row)
        if prospect is not None:
            row["prospect"] = round(prospect, 1)
        if edge is None:
            row["edge"] = None
            row["oc_score"] = None
            row["oc_rank"] = None
            unscored.append(row)
            continue
        row["edge"] = round(edge, 2)
        scored.append((i, edge, row))

    if not scored:
        return unscored

    edges = [e for _, e, _ in scored]
    marginal = min(edges)  # weakest claim still eating segment attention
    # Cash opportunity cost baseline: 0. Rank relative to max(marginal, 0) so a
    # sleeve of all-negative edges still orders internally without pretending
    # every name beats cash.
    baseline = min(0.0, marginal)

    enriched: list[tuple[float, int, dict[str, Any]]] = []
    for orig_i, edge, row in scored:
        oc = edge - baseline
        row["oc_score"] = round(oc, 2)
        enriched.append((-oc, orig_i, row))  # sort best-first, stable on input

    enriched.sort()
    out: list[dict[str, Any]] = []
    for rank, (_, _, row) in enumerate(enriched, start=1):
        row["oc_rank"] = rank
        out.append(row)
    # Unscored names trail with null rank (discovery fodder, not capital claims).
    out.extend(unscored)
    return out


def annotate_segment_members(members: list[dict[str, Any]],
                             *, home_by_symbol: dict[str, str] | None = None
                             ) -> list[dict[str, Any]]:
    """Rank *members* and optionally stamp ``home_segment`` from the model."""
    ranked = rank_members(members)
    if not home_by_symbol:
        return ranked
    for row in ranked:
        sym = str(row.get("symbol") or "").upper()
        home = home_by_symbol.get(sym)
        if home:
            row["home_segment"] = home
    return ranked


def load_ranks(path: Path | None = None) -> dict[str, Any]:
    """Cached per-sleeve OC ranks. Empty dict shape when missing/corrupt."""
    rec = _load(path or OC_RANKS_JSON) or {}
    if not isinstance(rec, dict):
        return {"as_of": None, "sleeves": {}}
    sleeves = rec.get("sleeves") if isinstance(rec.get("sleeves"), dict) else {}
    return {"as_of": rec.get("as_of"), "sleeves": sleeves}


def store_sleeve_ranks(sleeve: str, members: list[dict[str, Any]],
                       *, path: Path | None = None) -> dict[str, Any]:
    """Persist advisory ranks for one allocation sleeve into the sidecar cache."""
    name = str(sleeve or "").strip()
    if not name:
        return load_ranks(path)
    dest = path or OC_RANKS_JSON
    data = load_ranks(dest)
    by_sym: dict[str, Any] = {}
    for m in members or []:
        sym = str((m or {}).get("symbol") or "").strip().upper()
        if not sym:
            continue
        by_sym[sym] = {
            "oc_rank": m.get("oc_rank"),
            "oc_score": m.get("oc_score"),
            "prospect": m.get("prospect"),
            "edge": m.get("edge"),
        }
    data.setdefault("sleeves", {})[name] = by_sym
    data["as_of"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    dest.parent.mkdir(parents=True, exist_ok=True)
    _write_json(dest, data)
    return data


def ranks_for_sleeve(sleeve: str, *, path: Path | None = None) -> dict[str, dict[str, Any]]:
    """symbol -> {oc_rank, ...} for one sleeve (empty if unknown)."""
    name = str(sleeve or "").strip()
    raw = (load_ranks(path).get("sleeves") or {}).get(name) or {}
    if not isinstance(raw, dict):
        return {}
    return {str(k).upper(): v for k, v in raw.items() if isinstance(v, dict)}
