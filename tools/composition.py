#!/usr/bin/env python3
"""Whole-book allocation-segment composition.

Allocation segments are ``model.sleeves``. This module:

* snapshots current vs target sleeve weights (plus unassigned standalone targets
  and cash),
* asks the LLM for a proposed segment mix (with a deterministic normalize
  fallback),
* turns hand-tuned ratios into ``modify_sleeve`` staging changes.

Ratios suggested by research, fine-tuned by hand — never written live except
through the existing staging commit path.
"""

from __future__ import annotations

from typing import Any, Callable

import rebalance
import segment_home
import target_staging
from config import HOLDINGS_JSON, TARGET_MODEL_JSON
from store import load as _load


def _mid(band: dict[str, Any]) -> float:
    return (float(band["low"]) + float(band["high"])) / 2.0


def _band_pp(target_pct: float, half_width: float = 1.5) -> tuple[float, float]:
    """Center a band on *target_pct* with ±half_width, clamped to [0, 100]."""
    low = max(0.0, round(target_pct - half_width, 1))
    high = min(100.0, round(target_pct + half_width, 1))
    if high < low:
        high = low
    return low, high


def snapshot(model: dict[str, Any] | None = None,
             holdings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Current composition view for the UI / LLM prompt.

    Prefers the working draft when present so the editor reflects staged sleeve
    mids after **Stage composition**, not the pre-stage live model.
    """
    if model is None:
        staged = target_staging.load_staged(create=False)
        model = staged if isinstance(staged, dict) else (_load(TARGET_MODEL_JSON) or {})
    holdings = holdings if holdings is not None else (_load(HOLDINGS_JSON) or {})
    weights = rebalance.current_weights(holdings) if holdings else {}
    sleeves = model.get("sleeves") or {}
    members = segment_home.membership_map(model)
    cash = None
    if holdings:
        try:
            cash = rebalance.cash_block(model, holdings)
        except (TypeError, AttributeError, ValueError):
            cash = None

    segments: list[dict[str, Any]] = []
    for name, sl in sorted(sleeves.items()):
        if not isinstance(sl, dict):
            continue
        mems = [str(m).upper() for m in (sl.get("members") or []) if m]
        cur = sum(weights.get(m, 0.0) for m in mems)
        low, high = sl.get("low"), sl.get("high")
        target_mid = _mid(sl) if rebalance._band_ok(low, high) else None
        segments.append({
            "name": name,
            "members": mems,
            "member_count": len(mems),
            "current_pct": round(cur, 2),
            "low": low,
            "high": high,
            "target_pct": round(target_mid, 2) if target_mid is not None else None,
            "rule": sl.get("rule") or "accumulate",
            "note": sl.get("note") or "",
        })

    # Standalone targets that aren't sleeve members — the migration debt pile.
    unassigned = []
    for sym, band in sorted((model.get("targets") or {}).items()):
        if sym in members:
            continue
        home = segment_home.resolve_home(model, sym)
        unassigned.append({
            "symbol": sym,
            "current_pct": round(weights.get(sym, 0.0), 2),
            "low": band.get("low"),
            "high": band.get("high"),
            "home_segment": home,
            "rule": band.get("rule"),
        })

    return {
        "segments": segments,
        "unassigned": unassigned,
        "cash": cash,
        "cash_target_pct": model.get("cash_target_pct"),
        "homes": segment_home.home_map(model),
    }


def normalize_targets(raw: dict[str, float], *, cash_target: float,
                      max_segment: float = 60.0) -> dict[str, float]:
    """Clamp, drop negatives, and rescale segment targets so
    sum(segments) + cash_target <= 100. Residual shrinks segments pro-rata."""
    cleaned: dict[str, float] = {}
    for name, pct in raw.items():
        try:
            v = float(pct)
        except (TypeError, ValueError):
            continue
        if v < 0:
            continue
        cleaned[str(name)] = min(max_segment, v)
    budget = max(0.0, 100.0 - max(0.0, float(cash_target or 0.0)))
    total = sum(cleaned.values())
    if total <= 0:
        return {k: 0.0 for k in cleaned}
    if total > budget + 1e-6:
        scale = budget / total
        cleaned = {k: round(v * scale, 2) for k, v in cleaned.items()}
    else:
        cleaned = {k: round(v, 2) for k, v in cleaned.items()}
    return cleaned


def _heuristic_proposal(snap: dict[str, Any]) -> dict[str, Any]:
    """No-LLM fallback: keep current sleeve mids; nudge toward current weights
    when a sleeve is wildly off. Honest about being a stub."""
    proposed: dict[str, float] = {}
    rationales: dict[str, str] = {}
    for seg in snap.get("segments") or []:
        name = seg["name"]
        cur = float(seg.get("current_pct") or 0.0)
        mid = seg.get("target_pct")
        if mid is None:
            mid = cur
        # Blend 70% existing target / 30% current — mild mean-reversion, not genius.
        target = 0.7 * float(mid) + 0.3 * cur
        proposed[name] = target
        rationales[name] = "heuristic blend of current target and held weight (no LLM)."
    cash_t = float(snap.get("cash_target_pct") or 0.0)
    proposed = normalize_targets(proposed, cash_target=cash_t)
    return {
        "source": "heuristic",
        "targets": proposed,
        "rationales": rationales,
        "cash_target_pct": cash_t,
        "note": "LLM unavailable — heuristic proposal. Fine-tune by hand.",
    }


def _build_prompt(snap: dict[str, Any], direction: str) -> str:
    lines = [
        "You are proposing portfolio allocation-segment weights (percent of the "
        "invested book). Segments are a PARTITION — every name lives in exactly "
        "one. Return ONLY a JSON object:",
        '{"targets": {"segment-name": <pct>, ...}, '
        '"rationales": {"segment-name": "<one sentence>", ...}, '
        '"cash_target_pct": <pct>}',
        "",
        "Constraints:",
        "- targets are midpoints; keep each segment between 0 and 60",
        "- sum(targets) + cash_target_pct must be <= 100",
        "- prefer small, deliberate shifts from the current targets",
        "- do not invent new segment names",
        "",
        "Current composition:",
    ]
    for seg in snap.get("segments") or []:
        lines.append(
            f"- {seg['name']}: held {seg['current_pct']}%, "
            f"target band [{seg.get('low')}, {seg.get('high')}] "
            f"(mid {seg.get('target_pct')}), members={seg['members']}"
        )
    cash = snap.get("cash") or {}
    lines.append(
        f"- cash: held {cash.get('pct_of_nav', 'n/a')}% of NAV, "
        f"target {snap.get('cash_target_pct')}%"
    )
    unassigned = snap.get("unassigned") or []
    if unassigned:
        bits = ", ".join(
            f"{u['symbol']}@{u['current_pct']}%" for u in unassigned[:12]
        )
        lines.append(f"- unassigned standalone targets (migration debt): {bits}")
    if direction.strip():
        lines.append("")
        lines.append(f"Investor direction: {direction.strip()}")
    return "\n".join(lines)


def propose(direction: str = "", *, use_llm: bool = True,
            model: dict[str, Any] | None = None,
            holdings: dict[str, Any] | None = None,
            progress: Callable[[str], None] | None = None,
            cancel: Callable[[], bool] | None = None) -> dict[str, Any]:
    """LLM (or heuristic) composition proposal. Never writes disk."""
    snap = snapshot(model, holdings)
    names = {s["name"] for s in snap.get("segments") or []}
    if not names:
        return {
            "source": "none",
            "targets": {},
            "rationales": {},
            "cash_target_pct": snap.get("cash_target_pct"),
            "note": "No allocation sleeves in the target model yet.",
            "snapshot": snap,
        }

    if use_llm:
        try:
            import ticker_analysis
            cfg = ticker_analysis.load_config()
            if any(ticker_analysis.available_backends().values()):
                prompt = _build_prompt(snap, direction)
                res = ticker_analysis._run_with_fallback(
                    prompt, cfg, progress, cancel, label="composition")
                if res.get("ok"):
                    parsed = ticker_analysis._extract_json_object(res.get("report") or "")
                    if isinstance(parsed, dict) and isinstance(parsed.get("targets"), dict):
                        raw = {
                            str(k): v for k, v in parsed["targets"].items()
                            if str(k) in names
                        }
                        cash_t = parsed.get("cash_target_pct", snap.get("cash_target_pct") or 0)
                        try:
                            cash_t = float(cash_t)
                        except (TypeError, ValueError):
                            cash_t = float(snap.get("cash_target_pct") or 0)
                        targets = normalize_targets(raw, cash_target=cash_t)
                        rats_raw = parsed.get("rationales")
                        rats: dict = rats_raw if isinstance(rats_raw, dict) else {}
                        return {
                            "source": "llm",
                            "targets": targets,
                            "rationales": {k: str(rats.get(k) or "") for k in targets},
                            "cash_target_pct": cash_t,
                            "note": "LLM proposal — edit freely before staging.",
                            "snapshot": snap,
                        }
        except Exception:  # noqa: BLE001 — fall through to heuristic
            pass

    out = _heuristic_proposal(snap)
    out["snapshot"] = snap
    return out


def stage_ratios(targets: dict[str, float], *,
                 cash_target_pct: float | None = None,
                 half_width: float = 1.5,
                 source: str = "composition") -> dict[str, Any]:
    """Turn edited segment midpoints into sleeve band changes and stage them."""
    live = target_staging.load_live()
    if not live:
        raise ValueError("target model not found")
    sleeves = live.get("sleeves") or {}
    cash_t = float(
        cash_target_pct if cash_target_pct is not None
        else (live.get("cash_target_pct") or 0.0)
    )
    normalized = normalize_targets(
        {str(k): float(v) for k, v in (targets or {}).items() if str(k) in sleeves},
        cash_target=cash_t,
    )
    changes: list[dict[str, Any]] = []
    for name, mid in normalized.items():
        cur = dict(sleeves.get(name) or {})
        low, high = _band_pp(mid, half_width=half_width)
        proposed = {
            "low": low,
            "high": high,
            "rule": cur.get("rule") or "accumulate",
            "members": list(cur.get("members") or []),
        }
        if cur.get("member_caps"):
            proposed["member_caps"] = cur["member_caps"]
        if cur.get("note"):
            proposed["note"] = cur["note"]
        changes.append({
            "action": "modify_sleeve",
            "sleeve": name,
            "proposed_sleeve": proposed,
        })
    if cash_target_pct is not None:
        # Cash is top-level on the model; stage via a tiny manual path — we
        # edit the draft directly after sleeve changes so cash rides along.
        pass

    result = target_staging.stage_changes(changes, source=source)
    if cash_target_pct is not None:
        staged = target_staging.load_staged(create=True)
        assert staged is not None
        staged["cash_target_pct"] = cash_t
        from store import write_json as _write_json
        _write_json(target_staging.STAGED_JSON, staged)
        result["cash_target_pct"] = cash_t
    result["targets"] = normalized
    return result
