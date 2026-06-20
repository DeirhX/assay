#!/usr/bin/env python3
"""Construct a full target-model delta from a reviewed Deep Research run.

The review gate (``review_deep_research``) only ever proposed ``add_target`` for
*held* names missing a band -- it never sized the *new* names the research
surfaced, so "a direction in -> a rebalance recommendation out" never actually
happened. This module fills that gap: it turns a reviewed run into concrete
target bands/rules for every name in the segment, so the deterministic
``rebalance.plan`` has something real to react to.

Two stages, deliberately separated so the risky part is pure and testable:

1. ``infer_convictions`` -- a best-effort read of how the report feels about each
   name (high / medium / low / avoid). Uses the LLM when a backend is
   configured, and falls back to the report-action keywords the review gate
   already extracted, so it works fully offline.
2. ``normalize_targets`` -- a *pure, deterministic* sizing pass that converts
   convictions into ``{low, high, rule}`` bands that sum sanely: it respects a
   segment budget, per-name and sleeve ``member_caps``, leaves cash headroom,
   and never proposes a band for a blocked symbol. This is mandatory, not
   optional: an LLM left to free-size a portfolio will happily emit an
   infeasible >100% book.

``construct`` glues them together and writes the enriched proposal to the same
``<segment>-<date>.target-proposal.json`` the apply step consumes.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any, Callable

import research_brief
import sleeve_aliases
import ticker_analysis
from store import load as _load, write_json as _write_json, safe_symbol as _safe_symbol
# Canonical layout from config; re-exported as module attrs so tests can override.
from config import DEEP_DIR, TARGET_MODEL_JSON

# Pin stances that mean "we intend to own this" -- a run that rates such a name
# `avoid` is contradicting standing human judgement and must flag, not silently
# trim it away.
_OWN_STANCES = {"accumulate", "hold", "wait", "do_not_add"}

# Conviction -> sizing weight. Avoid carries no budget; it only ever trims.
_POINTS = {"high": 3.0, "medium": 2.0, "low": 1.0}
# Conviction -> target rule for a name we want to own.
_RULE = {"high": "accumulate", "medium": "hold", "low": "wait"}
# Report-action (from the review gate) -> conviction, the offline fallback.
_ACTION_CONVICTION = {
    "add": "high",
    "hold": "medium",
    "wait": "low",
    "mentioned": "low",
    "trim": "avoid",
    "sell": "avoid",
    "not mentioned": None,  # no opinion -> no target proposed
}

DEFAULT_BUDGET_PCT = 15.0   # used when the segment isn't held at all yet
MIN_BUDGET_PCT = 5.0
MAX_BUDGET_PCT = 40.0       # one theme should never swallow the whole book
DEFAULT_PER_NAME_CAP = 12.0
MIN_BAND_WIDTH = 1.0        # percentage points
_REPORT_CHAR_LIMIT = 14000  # keep the LLM prompt bounded on long reports

# Only these keys belong in a target-model entry; everything else the engine
# carries (conviction, sleeve, rationale) is metadata for the review UI and must
# be stripped before the band is written into target-model.json.
TARGET_KEYS = ("low", "high", "rule", "note", "structural")


def _round1(value: float) -> float:
    return round(float(value), 1)


def _band_from_weight(weight: float) -> tuple[float, float]:
    """A no-trade band around a target weight, with a sane minimum width."""
    if weight < 1.0:
        return 0.0, 1.0
    low = _round1(max(0.0, weight * 0.85))
    high = _round1(weight * 1.15)
    if high - low < MIN_BAND_WIDTH:
        mid = (high + low) / 2.0
        low = _round1(max(0.0, mid - MIN_BAND_WIDTH / 2.0))
        high = _round1(low + MIN_BAND_WIDTH)
    return low, high


def _pins(model: dict[str, Any]) -> dict[str, dict]:
    """Conviction pins (soft-locks) carried on the model's provenance layer."""
    prov = model.get("provenance") or {}
    return {k: v for k, v in prov.items()
            if isinstance(v, dict) and v.get("source") == "user-pin"}


def _member_caps(model: dict[str, Any]) -> dict[str, float]:
    """Per-symbol caps declared on sleeves in the existing model."""
    caps: dict[str, float] = {}
    for sleeve in (model.get("sleeves") or {}).values():
        if not isinstance(sleeve, dict):
            continue
        for sym, cap in (sleeve.get("member_caps") or {}).items():
            try:
                caps[_safe_symbol(sym)] = float(cap)
            except (ValueError, TypeError):
                continue
    return caps


def heuristic_convictions(rows: list[dict[str, Any]]) -> dict[str, str]:
    """Offline conviction read from the review gate's keyword report-action."""
    out: dict[str, str] = {}
    for row in rows:
        sym = str(row.get("symbol") or "").upper()
        if not sym:
            continue
        conviction = _ACTION_CONVICTION.get(str(row.get("report_action") or ""), "low")
        if conviction:
            out[sym] = conviction
    return out


def _build_llm_prompt(report_text: str, symbols: list[str], research_block: str = "") -> str:
    body = report_text.strip()
    if len(body) > _REPORT_CHAR_LIMIT:
        body = body[:_REPORT_CHAR_LIMIT] + "\n…[truncated]"
    names = ", ".join(symbols)
    research_section = f"\n\n{research_block}" if research_block else ""
    return f"""You are sizing a research segment into portfolio convictions. Below is a segment Deep Research report, followed (when available) by per-name research we pulled ourselves. For each ticker in this list, judge how strongly the evidence supports OWNING it as a long-term position:

{names}{research_section}

Return ONLY a JSON object mapping each ticker to an object with:
- "conviction": one of "high", "medium", "low", "avoid" ("avoid" = the evidence argues against owning / says trim or sell)
- "rationale": one short sentence grounded in the report and any per-name research

Do not invent tickers outside the list. Do not add prose outside the JSON.

REPORT:
{body}
"""


def llm_convictions(report_text: str, symbols: list[str], *,
                    cfg: dict | None = None, research_block: str = "",
                    progress: Callable[[str], None] | None = None,
                    cancel: Callable[[], bool] | None = None) -> dict[str, dict[str, str]]:
    """Ask the configured backend for per-name conviction. Returns {} on any
    failure (no backend, bad JSON, cancel) so callers fall back to heuristics."""
    if not symbols:
        return {}
    cfg = cfg or ticker_analysis.load_config()
    if not any(ticker_analysis.available_backends().values()):
        return {}
    prompt = _build_llm_prompt(report_text, symbols, research_block)
    res = ticker_analysis._run_with_fallback(prompt, cfg, progress, cancel, label="target-construct")
    if not res.get("ok"):
        return {}
    parsed = ticker_analysis._extract_json_object(res.get("report") or "")
    if not isinstance(parsed, dict):
        return {}
    valid = set(symbols)
    out: dict[str, dict[str, str]] = {}
    for sym, node in parsed.items():
        key = str(sym).upper().strip()
        if key not in valid:
            continue
        if isinstance(node, str):
            node = {"conviction": node}
        if not isinstance(node, dict):
            continue
        conviction = str(node.get("conviction") or "").lower().strip()
        if conviction not in ("high", "medium", "low", "avoid"):
            continue
        out[key] = {
            "conviction": conviction,
            "rationale": str(node.get("rationale") or "").strip(),
        }
    return out


def infer_convictions(rows: list[dict[str, Any]], report_text: str, *,
                      use_llm: bool = True, cfg: dict | None = None,
                      progress: Callable[[str], None] | None = None,
                      cancel: Callable[[], bool] | None = None) -> dict[str, dict[str, str]]:
    """Conviction + rationale per symbol. LLM read where available, with the
    deterministic keyword read as the floor so every name gets a verdict."""
    symbols = [str(r.get("symbol") or "").upper() for r in rows if r.get("symbol")]
    floor = heuristic_convictions(rows)
    result: dict[str, dict[str, str]] = {
        sym: {"conviction": conv, "rationale": "", "source": "heuristic"}
        for sym, conv in floor.items()
    }
    if use_llm:
        cfg = cfg or ticker_analysis.load_config()
        # Ground the conviction read in the research we already generated -- our
        # pulled numbers plus distilled per-ticker reports -- not just the essay.
        research_block = research_brief.build_research_block(
            rows, cfg, use_llm=True, progress=progress, cancel=cancel)
        llm = llm_convictions(report_text, symbols, cfg=cfg, research_block=research_block,
                              progress=progress, cancel=cancel)
        for sym, node in llm.items():
            result[sym] = {**node, "source": "llm"}
    return result


def normalize_targets(convictions: dict[str, dict[str, str]], rows: list[dict[str, Any]],
                      model: dict[str, Any], *, blocked: set[str] | None = None,
                      segment_budget_pct: float | None = None,
                      per_name_cap: float = DEFAULT_PER_NAME_CAP,
                      holdings: dict[str, Any] | None = None,
                      drop_mode: bool = False) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Pure sizing pass: convictions -> a list of target-model change records.

    Guarantees: never proposes a band for a blocked symbol; never exceeds the
    per-name cap (or a tighter sleeve member_cap); the sum of proposed buy
    midpoints never exceeds the segment budget; avoid-rated *held* names get a
    trim band, avoid-rated unheld names are skipped entirely.

    Pin-aware: a pinned name's ``floor_pct``/``ceiling_pct`` clamps its band (the
    pin is an anchor, not just the transient current band); a pinned name is
    never auto-dropped; and when a run rates a name-we-intend-to-own as ``avoid``
    the change is still emitted but tagged ``challenges_pin`` so the review gate
    can surface the conflict instead of silently trimming standing judgement.

    Whole-book aware: ``holdings`` (when given) feeds a book-level reconciliation
    in the meta -- the sum of the resulting midpoints against the rest of the
    book -- so an over-allocating run is visible. ``drop_mode`` turns an
    avoid-rated held (unpinned) name into a ``remove_target`` instead of a trim
    band, for explicit "drop this sector" directions.
    """
    blocked = blocked or set()
    existing_targets = model.get("targets") or {}
    caps = _member_caps(model)
    pins = _pins(model)
    aliases = sleeve_aliases.load_aliases()
    by_symbol = {str(r.get("symbol") or "").upper(): r for r in rows}

    held: dict[str, float] = {}
    for sym, row in by_symbol.items():
        hp = row.get("held_pct_nav")
        if isinstance(hp, (int, float)):
            held[sym] = float(hp)

    # Names we'd actually buy/hold drive the budget split; avoid-rated names do not.
    buy = {sym: node["conviction"] for sym, node in convictions.items()
           if node["conviction"] in _POINTS and sym not in blocked}

    if segment_budget_pct is None:
        held_in_segment = sum(held.get(sym, 0.0) for sym in convictions)
        segment_budget_pct = held_in_segment if held_in_segment > 0 else DEFAULT_BUDGET_PCT
    budget = max(MIN_BUDGET_PCT, min(MAX_BUDGET_PCT, float(segment_budget_pct)))

    total_points = sum(_POINTS[c] for c in buy.values())
    changes: list[dict[str, Any]] = []
    sized_total = 0.0

    def cap_for(sym: str) -> float:
        return min(per_name_cap, caps.get(sym, per_name_cap))

    def clamp_to_pin(sym: str, low: float, high: float) -> tuple[float, float]:
        pin = pins.get(sym)
        if not pin:
            return low, high
        floor = pin.get("floor_pct")
        ceil = pin.get("ceiling_pct")
        if isinstance(floor, (int, float)):
            low = max(low, float(floor))
        if isinstance(ceil, (int, float)):
            high = min(high, float(ceil)) if high else float(ceil)
        if high - low < MIN_BAND_WIDTH:
            high = _round1(low + MIN_BAND_WIDTH)
        return _round1(low), _round1(high)

    for sym, conviction in sorted(buy.items()):
        weight = (budget * _POINTS[conviction] / total_points) if total_points else 0.0
        weight = min(weight, cap_for(sym))
        low, high = _band_from_weight(weight)
        low, high = clamp_to_pin(sym, low, high)
        sized_total += (low + high) / 2.0
        changes.append(_change(sym, by_symbol.get(sym), convictions.get(sym), existing_targets,
                               low=low, high=high, rule=_RULE[conviction], aliases=aliases))

    # Avoid-rated held names: propose a trim band (or a drop). When the model
    # already has a sized target, trim toward THAT existing band rather than
    # inventing a tighter one. A PINNED name is never auto-dropped and its trim
    # is flagged as challenging the pin; a `drop_mode` run removes unpinned held
    # avoids outright.
    for sym, node in sorted(convictions.items()):
        if node["conviction"] != "avoid" or sym in blocked:
            continue
        cur = held.get(sym, 0.0)
        if cur <= 0:
            continue  # nothing to trim and we won't buy it -> no target needed
        pin = pins.get(sym)
        challenges = bool(pin and str(pin.get("stance") or "") in _OWN_STANCES)
        if drop_mode and not pin:
            changes.append(_change(sym, by_symbol.get(sym), node, existing_targets,
                                   low=0.0, high=0.0, rule="avoid", aliases=aliases,
                                   action="remove_target"))
            continue
        existing = existing_targets.get(sym)
        if isinstance(existing, dict) and isinstance(existing.get("high"), (int, float)):
            low = _round1(max(0.0, float(existing.get("low") or 0.0)))
            high = _round1(float(existing["high"]))
        else:
            low = 0.0
            high = _round1(max(MIN_BAND_WIDTH, cur * 0.5))
        if pin:  # never trim a pinned name below its own floor
            low, high = clamp_to_pin(sym, low, high)
        changes.append(_change(sym, by_symbol.get(sym), node, existing_targets,
                               low=low, high=high, rule="trim_only", aliases=aliases,
                               challenges_pin=challenges))

    meta = {
        "segment_budget_pct": _round1(budget),
        "sized_midpoint_total_pct": _round1(sized_total),
        "buy_count": len(buy),
        "trim_count": sum(1 for c in changes if c["proposed_target"].get("rule") == "trim_only"),
        "per_name_cap": per_name_cap,
        "pinned_count": sum(1 for c in changes if c.get("symbol") in pins),
        "challenges_pins": sorted(c["symbol"] for c in changes if c.get("challenges_pin")),
    }
    if isinstance(holdings, dict):
        meta["book_reconciliation"] = _book_reconciliation(model, changes)
    return changes, meta


def _book_reconciliation(model: dict[str, Any], changes: list[dict[str, Any]]) -> dict[str, Any]:
    """Project the whole-book midpoint total if these changes were applied, so an
    over-allocating run is visible at synthesis time (not just at the staging
    gate). Pure: builds a throwaway copy of the target map."""
    targets = dict(model.get("targets") or {})
    for ch in changes:
        sym = ch.get("symbol")
        if ch.get("action") == "remove_target":
            targets.pop(sym, None)
        elif isinstance(ch.get("proposed_target"), dict):
            targets[sym] = ch["proposed_target"]

    def mid(b: dict) -> float:
        lo, hi = b.get("low"), b.get("high")
        return (float(lo) + float(hi)) / 2.0 if isinstance(lo, (int, float)) and isinstance(hi, (int, float)) else 0.0

    sleeves = model.get("sleeves") or {}
    cash = float(model.get("cash_target_pct") or 0.0)
    targeted = sum(mid(t) for t in targets.values()) + sum(mid(s) for s in sleeves.values())
    return {
        "targeted_mid_pct": _round1(targeted),
        "cash_target_pct": cash,
        "available_pct": _round1(100.0 - targeted - cash),
        "over_allocated": (100.0 - targeted - cash) < -0.01,
    }


def _change(sym: str, row: dict[str, Any] | None, conviction: dict[str, str] | None,
            existing_targets: dict[str, Any], *, low: float, high: float, rule: str,
            aliases: dict[str, str] | None = None, action: str | None = None,
            challenges_pin: bool = False) -> dict[str, Any]:
    current = existing_targets.get(sym)
    raw_sleeve = (row or {}).get("sleeve") or ""
    sleeve, sleeve_known = sleeve_aliases.normalize(raw_sleeve, aliases=aliases)
    note_bits = []
    if conviction and conviction.get("rationale"):
        note_bits.append(conviction["rationale"])
    note_bits.append("Synthesized from segment review; verify before applying.")
    # A normalized sleeve tag rides ON the proposed band so the apply persists it
    # (sleeve is a write key now); an empty/unknown one is omitted, not guessed.
    proposed: dict[str, Any] = {"low": low, "high": high, "rule": rule, "note": " ".join(note_bits)}
    if sleeve:
        proposed["sleeve"] = sleeve
    change: dict[str, Any] = {
        "symbol": sym,
        "action": action or ("modify_target" if current else "add_target"),
        "status": "draft",
        "conviction": (conviction or {}).get("conviction"),
        "conviction_source": (conviction or {}).get("source"),
        "sleeve": sleeve,
        "sleeve_unknown": bool(raw_sleeve) and not sleeve_known,
        "current_target": current,
        "proposed_target": proposed,
        "rationale": (conviction or {}).get("rationale") or "",
    }
    if challenges_pin:
        change["challenges_pin"] = True
    return change


def proposal_path(segment: str, date: str) -> Path:
    return DEEP_DIR / f"{segment}-{date}.target-proposal.json"


def construct(segment: str, date: str, review: dict[str, Any], *,
              use_llm: bool = True, write: bool = True, model: dict[str, Any] | None = None,
              segment_budget_pct: float | None = None, cfg: dict | None = None,
              holdings: dict[str, Any] | None = None, drop_mode: bool = False,
              progress: Callable[[str], None] | None = None,
              cancel: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Turn a review() result into an enriched target-model proposal and (by
    default) persist it where the apply step reads it."""
    segment = re.sub(r"[^a-z0-9-]", "", str(segment).lower())
    rows = review.get("rows") or []
    blocked = set(review.get("blocked_symbols") or [])
    report_text = review.get("report") or ""
    if model is None:
        model = _load(TARGET_MODEL_JSON) or {}

    if progress:
        progress("reading conviction from the report…")
    convictions = infer_convictions(rows, report_text, use_llm=use_llm, cfg=cfg,
                                    progress=progress, cancel=cancel)
    if progress:
        progress("sizing target bands…")
    changes, meta = normalize_targets(convictions, rows, model, blocked=blocked,
                                      segment_budget_pct=segment_budget_pct,
                                      holdings=holdings, drop_mode=drop_mode)

    # Surface conflicts the sizing pass detected as review findings, in the same
    # WARN channel as the existing rule/report mismatches, so the gate shows them.
    findings = list(review.get("findings") or [])
    for ch in changes:
        if ch.get("challenges_pin"):
            findings.append({
                "level": "WARN", "symbol": ch["symbol"],
                "message": f"{ch['symbol']}: pinned (standing intent) but this run rates it avoid — review before accepting.",
            })
        if ch.get("sleeve_unknown"):
            findings.append({
                "level": "FYI", "symbol": ch["symbol"],
                "message": f"{ch['symbol']}: sleeve '{ch.get('sleeve')}' is not in sleeve-aliases.json (kept as-is).",
            })
    book = meta.get("book_reconciliation")
    if isinstance(book, dict) and book.get("over_allocated"):
        findings.append({
            "level": "WARN", "symbol": None,
            "message": (f"book over-allocated: midpoints would total {book.get('targeted_mid_pct')}% "
                        f"+ {book.get('cash_target_pct')}% cash; trim funding sources before committing."),
        })

    proposal = {
        "schema_version": 2,
        "segment": segment,
        "date": date,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "status": "draft",
        "message": ("Synthesized target-model delta. Review every band before "
                    "approving — this is a proposal, not an allocation change."),
        "changes": changes,
        "construct_meta": meta,
        "warnings": review.get("warnings") or [],
        "findings": findings,
        "blocked_symbols": sorted(blocked),
        "source_summary": review.get("source_summary"),
    }
    if write:
        _write_json(proposal_path(segment, date), proposal)
    return proposal


def main() -> int:  # pragma: no cover - thin CLI around review + construct
    import argparse
    import review_deep_research

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--segment", required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--no-llm", action="store_true", help="skip the LLM conviction pass")
    args = parser.parse_args()
    review = review_deep_research.review(args.segment, args.date, write=False)
    proposal = construct(args.segment, args.date, review, use_llm=not args.no_llm)
    print(json.dumps(proposal["construct_meta"], indent=2))
    print(f"{len(proposal['changes'])} change(s); proposal at {proposal_path(args.segment, args.date)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
