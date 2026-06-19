#!/usr/bin/env python3
"""Peer percentile statistics for the ticker dossier.

Extracted from serve.py: pure ranking math over the research cache, with no
HTTP or job machinery. Given a symbol, works out where its metrics sit within
the peer universe of each segment it belongs to (a rank-percentile per segment
plus a cross-segment aggregate), honestly flagging thin comparison samples.
"""

from __future__ import annotations

from config import DATA_DIR, RESEARCH_DIR
from store import load as _load, safe_symbol as _safe_symbol

SEGMENT_DEF_DIR = DATA_DIR / "segments"

# Metrics compared against segment peers, mirroring the dossier's tile order.
_PEER_METRIC_KEYS = (
    "market_cap_usd_b", "pe_ttm", "pe_fwd", "ps",
    "revenue_ttm_usd_b", "net_income_ttm_usd_b",
    "gross_margin_pct", "rev_growth_yoy_pct", "shares_out_b",
)

# A rank-percentile over a handful of peers is noise: with n comparable peers the
# resolution is only 1/(n-1), so n=3 can produce just {0, 50, 100}. Most segments
# have many members but only a few have been pulled into the research cache, so we
# flag a comparison as reliable only once enough peers actually have data. Below
# this the dossier shows an honest "k of m peers" rank instead of a hard pctile.
MIN_PEER_SAMPLE = 5


def _metric_value(rec: dict, key: str):
    """Pull a finite float for *key* out of a research record's metrics, or None.
    Metric nodes are usually {value, source, ...} but tolerate a bare number."""
    try:
        node = (rec.get("metrics") or {}).get(key)
        v = node.get("value") if isinstance(node, dict) else node
        v = float(v)
    except (TypeError, ValueError, AttributeError):
        return None
    return v if v == v and v not in (float("inf"), float("-inf")) else None


def _segments_for_symbol(sym: str):
    """(slug, title, [member symbols]) for every segment that lists *sym*."""
    out = []
    for path in sorted(SEGMENT_DEF_DIR.glob("*.json")):
        definition = _load(path) or {}
        members = [str(m.get("symbol") or "").upper()
                   for m in (definition.get("members") or []) if m.get("symbol")]
        if sym.upper() in members:
            out.append((path.stem, definition.get("title", path.stem.title()), members))
    return out


def _peer_stats(symbol: str) -> dict:
    """Where *symbol*'s metrics sit within the peer universe of each segment it
    belongs to: a rank-percentile (0=lowest, 1=highest) per segment, plus an
    aggregate (mean percentile) across segments. Drives the dossier position bars.
    """
    sym = _safe_symbol(symbol)
    segs = _segments_for_symbol(sym)
    result = {"symbol": sym, "segments": [s[0] for s in segs], "metrics": {}}
    if not segs:
        return result

    cache: dict = {}

    def member_metrics(msym: str) -> dict:
        if msym not in cache:
            cache[msym] = _load(RESEARCH_DIR / f"{msym}.json") or {}
        return cache[msym]

    subject = member_metrics(sym)
    for key in _PEER_METRIC_KEYS:
        subj_val = _metric_value(subject, key)
        if subj_val is None:
            continue
        per_segment, pcts = [], []
        for slug, title, members in segs:
            values = [v for v in (_metric_value(member_metrics(m), key) for m in members) if v is not None]
            if len(values) < 2:
                continue  # nothing to compare against
            srt = sorted(values)
            n = len(srt)
            below = sum(1 for v in srt if v < subj_val)
            equal = sum(1 for v in srt if v == subj_val)
            pct = max(0.0, min(1.0, (below + max(equal - 1, 0) / 2.0) / (n - 1)))
            median = srt[n // 2] if n % 2 else (srt[n // 2 - 1] + srt[n // 2]) / 2.0
            per_segment.append({
                "segment": slug, "title": title, "pct": round(pct, 4),
                # n = peers WITH data (the comparison sample); members_total = the
                # segment's full roster. reliable gates the "percentile" framing.
                "n": n, "members_total": len(members), "reliable": n >= MIN_PEER_SAMPLE,
                "min": srt[0], "max": srt[-1], "median": median,
            })
            pcts.append(pct)
        if not per_segment:
            continue
        best_n = max(s["n"] for s in per_segment)
        result["metrics"][key] = {
            "value": subj_val,
            # Surfaced so the frontend can tell a meaningful percentile from a
            # two-or-three-peer coin flip and label it honestly.
            "n": best_n,
            "members_total": max(s["members_total"] for s in per_segment),
            "reliable": best_n >= MIN_PEER_SAMPLE,
            "aggregate": {"pct": round(sum(pcts) / len(pcts), 4), "n_segments": len(per_segment)},
            "per_segment": per_segment,
        }
    return result
