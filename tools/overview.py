#!/usr/bin/env python3
"""The "Today" cockpit: one-screen summary of where the portfolio process
stands and what to do next.

The app's workflow is a loop — sync holdings, check drift, plan, stage a draft,
commit, simulate a basket, trade, resync — but the views that own each step are
siblings that never point at each other. This module aggregates their existing
read-side data into lane summaries (portfolio + research) and a single ordered
``next_step`` recommendation so the UI can always show the next door.

Everything here is a pure builder over plain dicts/lists (unit-testable with no
disk or network); ``serve.py`` composes them from the real stores. Status and
advice only: this module never mutates anything and never trades.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import timeutil

# Freshness thresholds (days). Advisory, not enforcement: they decide when the
# cockpit nags, not what the user is allowed to do.
STALE_SNAPSHOT_DAYS = 7     # don't size trades off a holdings pull older than this
STALE_SEGMENT_DAYS = 45     # a peer-universe pull older than this has drifted
AGING_PICK_DAYS = 30        # a basket pick unresearched this long deserves attention
STALE_JOURNAL_DAYS = 60     # an unscored decision this old is ready for an outcome

LIST_LIMIT = 6  # cap every embedded list so the cockpit stays a summary


def _now(now: dt.datetime | None = None) -> dt.datetime:
    return now or dt.datetime.now(dt.timezone.utc)


def age_days(stamp: Any, now: dt.datetime | None = None) -> int | None:
    """Whole days since an ISO timestamp (Z-suffix tolerant); None if unparsable."""
    return timeutil.age_days(stamp, now)


def _parse_dt(stamp: Any) -> dt.datetime | None:
    return timeutil.parse_iso_utc(stamp)


def automation_summary(
    state: dict | None,
    tasks: list[dict] | None,
    *,
    enabled: bool,
    now: dt.datetime | None = None,
) -> dict:
    """Background-scheduler surface for the Today cockpit. Pure over the persisted
    run-state dict (``{task: {last_run, last_result}}``) plus a list of task
    descriptors (``{name, label, enabled, interval_s}``) so this module never
    imports the scheduler. Returns ``{enabled, any_ran, tasks: [...]}`` with a
    computed ``next_eligible`` per task."""
    now = _now(now)
    rows: list[dict] = []
    any_ran = False
    for t in tasks or []:
        rec = (state or {}).get(t.get("name")) or {}
        last_run = rec.get("last_run")
        if last_run:
            any_ran = True
        parsed = _parse_dt(last_run)
        interval_s = t.get("interval_s")
        next_eligible = (
            (parsed + dt.timedelta(seconds=interval_s)).isoformat(timespec="seconds")
            if parsed and interval_s else None
        )
        rows.append({
            "name": t.get("name"),
            "label": t.get("label") or t.get("name"),
            "enabled": bool(t.get("enabled")),
            "last_run": last_run,
            "last_result": rec.get("last_result"),
            "age_days": age_days(last_run, now),
            "next_eligible": next_eligible,
        })
    return {"enabled": bool(enabled), "any_ran": any_ran, "tasks": rows}


# --------------------------------------------------------------------------- #
# Portfolio lane
# --------------------------------------------------------------------------- #
def snapshot_summary(holdings: dict | None, *, now: dt.datetime | None = None) -> dict:
    """Broker-snapshot freshness: the loop's ground truth, so its age leads."""
    positions = holdings.get("positions") if isinstance(holdings, dict) else None
    generated_at = holdings.get("generated_at") if isinstance(holdings, dict) else None
    age = age_days(generated_at, now)
    return {
        "exists": bool(isinstance(positions, list) and positions),
        "generated_at": generated_at,
        "age_days": age,
        "stale": bool(age is not None and age > STALE_SNAPSHOT_DAYS),
        "positions": len(positions) if isinstance(positions, list) else 0,
    }


def plan_summary(plan: dict) -> dict:
    """Counts over an overlay-attached ``rebalance.plan()``: how much of the
    book needs a decision, and how many price gates are waiting vs open."""
    rows = plan.get("rows") or []
    out_of_band = buy = trim = review = 0
    conflicts = gates_waiting = gates_open = 0
    for r in rows:
        if r.get("status") in ("ABOVE", "BELOW"):
            out_of_band += 1
        action = r.get("action")
        if action == "buy":
            buy += 1
        elif action == "trim":
            trim += 1
        elif action == "review":
            review += 1
        if r.get("research_conflict"):
            conflicts += 1
        gate = r.get("price_gate")
        if isinstance(gate, dict):
            if gate.get("blocked_action"):
                gates_waiting += 1
            elif action in ("buy", "trim"):
                # The level is satisfied and the band still wants the move — a
                # trigger you set has fired and is actionable right now.
                gates_open += 1
    return {
        "rows": len(rows),
        "out_of_band": out_of_band,
        "buy": buy,
        "trim": trim,
        "review": review,
        "actionable": buy + trim + review,
        "conflicts": conflicts,
        "gates_waiting": gates_waiting,
        "gates_open": gates_open,
        "untargeted": len(plan.get("untargeted") or []),
        "untargeted_pct": plan.get("untargeted_pct"),
        # First-class cash line (rebalance.cash_block) passed through so the
        # cockpit can flag a breached cash band alongside position drift.
        "cash": plan.get("cash"),
        "snapshot": plan.get("snapshot"),
    }


def staged_basket_summary(trades: list[dict] | None) -> dict:
    """The basket the planner last staged for the trade desk."""
    trades = trades or []
    buys = sum(1 for t in trades if (t.get("delta_czk") or 0) > 0)
    total = sum(abs(t.get("delta_czk") or 0) for t in trades)
    return {
        "count": len(trades),
        "buys": buys,
        "sells": len(trades) - buys,
        "total_abs_czk": round(total),
    }


def journal_summary(entries: list[dict] | None, *, now: dt.datetime | None = None) -> dict:
    """Decisions still waiting for an outcome — the calibration loop's debt."""
    entries = entries or []
    pending = [e for e in entries if not e.get("outcome")]
    ages = [a for e in pending if (a := age_days(e.get("created_at"), now)) is not None]
    oldest = max(ages, default=None)
    return {
        "total": len(entries),
        "pending_outcomes": len(pending),
        "oldest_pending_days": oldest,
        "review_due": sum(1 for a in ages if a > STALE_JOURNAL_DAYS),
    }


# --------------------------------------------------------------------------- #
# Research lane
# --------------------------------------------------------------------------- #
def research_summary(basket_items: list[dict] | None, ticker_index: list[dict] | None,
                     segments: list[dict] | None, segment_records: list[dict] | None,
                     *, now: dt.datetime | None = None) -> dict:
    """The discovery funnel's state: picks that need research, segment caches
    that need a re-pull, and the highest-scoring unresearched segment names."""
    basket_items = basket_items or []
    index = {str(r.get("symbol") or "").upper(): r for r in (ticker_index or [])}

    def researched(sym: str) -> bool:
        return bool(index.get(sym.upper(), {}).get("has_analysis"))

    # Basket triage: picks with no saved analysis, oldest first.
    unresearched: list[dict[str, Any]] = []
    for it in basket_items:
        sym = str(it.get("symbol") or "")
        if not sym or researched(sym):
            continue
        unresearched.append({
            "symbol": sym,
            "tier": it.get("tier") or "want",
            "segment": it.get("segment"),
            "age_days": age_days(it.get("added_at"), now),
        })
    unresearched.sort(key=lambda r: -(r["age_days"] if r["age_days"] is not None else -1))
    aging = sum(1 for r in unresearched
                if r["age_days"] is not None and r["age_days"] > AGING_PICK_DAYS)

    # Segment cache freshness.
    segments = segments or []
    stale_segments = []
    for s in segments:
        if not s.get("cached"):
            continue
        age = age_days(s.get("cached_at"), now)
        if age is not None and age > STALE_SEGMENT_DAYS:
            stale_segments.append({"name": s.get("name"), "title": s.get("title"),
                                   "age_days": age})
    stale_segments.sort(key=lambda r: -(r["age_days"] or 0))

    # Research queue: best research_score across every cached segment pull,
    # excluding names already held, already in the basket, or already analysed —
    # i.e. genuinely new candidates the segment work surfaced and nobody triaged.
    in_basket = {str(it.get("symbol") or "").upper() for it in basket_items}
    best: dict[str, dict] = {}
    for rec in (segment_records or []):
        seg_name = rec.get("segment") if isinstance(rec, dict) else None
        for m in (rec.get("members") or []) if isinstance(rec, dict) else []:
            sym = str(m.get("symbol") or "").upper()
            score = m.get("research_score")
            if not sym or not isinstance(score, (int, float)):
                continue
            if m.get("owned_pct_nav") is not None or sym in in_basket or researched(sym):
                continue
            prev = best.get(sym)
            if prev is None or score > prev["score"]:
                best[sym] = {"symbol": sym, "score": score, "segment": seg_name,
                             "decision": m.get("decision")}
    queue = sorted(best.values(), key=lambda r: -r["score"])

    return {
        "basket": {
            "count": len(basket_items),
            "unresearched_count": len(unresearched),
            "aging_count": aging,
            "unresearched": unresearched[:LIST_LIMIT],
        },
        "segments": {
            "total": len(segments),
            "cached": sum(1 for s in segments if s.get("cached")),
            "stale": stale_segments[:LIST_LIMIT],
            "stale_count": len(stale_segments),
        },
        "queue": queue[:LIST_LIMIT],
    }


# --------------------------------------------------------------------------- #
# The one recommendation
# --------------------------------------------------------------------------- #
def next_step(payload: dict) -> dict:
    """The single most useful next action, by fixed priority: ground truth
    first (snapshot), then decisions already in flight (draft, staged basket),
    then new decisions (plan drift), then research upkeep. Returns
    ``{id, view, label, reason}`` — the UI renders it as the primary CTA."""
    snap = payload.get("snapshot") or {}
    plan = payload.get("plan")
    draft = payload.get("draft") or {}
    basket = payload.get("staged_basket") or {}
    research = payload.get("research") or {}

    if not snap.get("exists"):
        return {"id": "setup", "view": "setup",
                "label": "Set up your data",
                "reason": "No holdings snapshot yet — connect the private data submodule or sync from IBKR."}
    if snap.get("stale"):
        return {"id": "resync", "view": "holdings",
                "label": "Resync holdings from IBKR",
                "reason": f"The broker snapshot is {snap.get('age_days')} days old — everything below is computed from it."}
    if draft.get("pending"):
        n = draft["pending"]
        return {"id": "commit-draft", "view": "working-draft",
                "label": "Review the working draft",
                "reason": f"{n} uncommitted change{'s' if n != 1 else ''} — the planner is previewing the draft, not your live plan."}
    if basket.get("count"):
        n = basket["count"]
        return {"id": "place-basket", "view": "trade",
                "label": "Finish the staged basket",
                "reason": f"{n} simulated trade{'s' if n != 1 else ''} waiting in the Trade desk — place or clear them."}
    if plan and plan.get("gates_open"):
        n = plan["gates_open"]
        return {"id": "gates-open", "view": "rebalance",
                "label": "Act on triggered price levels",
                "reason": f"{n} locked price level{'s have' if n != 1 else ' has'} been met and the band still wants the move."}
    if plan and plan.get("actionable"):
        n = plan["actionable"]
        return {"id": "rebalance", "view": "rebalance",
                "label": "Review suggested trades",
                "reason": f"{n} name{'s' if n != 1 else ''} outside the no-trade band with a suggested action."}
    rb = (research.get("basket") or {})
    if rb.get("unresearched_count"):
        n = rb["unresearched_count"]
        return {"id": "research-picks", "view": "basket",
                "label": "Research your basket picks",
                "reason": f"{n} shortlisted name{'s' if n != 1 else ''} without a saved analysis — research or drop them."}
    if research.get("queue"):
        top = research["queue"][0]
        return {"id": "research-queue", "view": "deepdive",
                "symbol": top.get("symbol"),
                "label": f"Look at {top.get('symbol')}",
                "reason": "Highest-scoring unresearched name from your segment work."}
    return {"id": "all-clear", "view": "rebalance",
            "label": "All caught up",
            "reason": "Snapshot is fresh, nothing is staged, and every targeted name sits inside its band."}
