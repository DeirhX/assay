#!/usr/bin/env python3
"""Guided "Direction -> Rebalance" strategy run: the serve-side leg runners.

Extracted from serve.py. The durable state machine lives in orchestrate.py; the
runners here do the per-leg work on daemon threads, exactly like the deep-
research job runners. A run pauses at a gate by simply landing in an awaiting_*
state -- no thread is left blocked on a human. The synthesis leg reuses the deep-
research job wholesale (login walls, clarify, auto-save) by starting it via
browser_jobs and polling its sub-job to completion.

Pure orchestration over the segment / browser / target-model services -- no HTTP.
Public names are underscore-free; serve.py imports the three gate entry points
(start, approve_segment, approve_proposal) aliased to its existing private names.
"""

from __future__ import annotations

import datetime as dt
import threading
import time

import jobs
import orchestrate
import research_pull
import review_deep_research
import target_construct
import target_staging
import ticker_analysis
from apierror import Conflict
from browser_jobs import start_deep_research
from config import DEEP_DIR, HOLDINGS_JSON, SEGMENT_DEF_DIR
from jobs import new_job, update_job
from research_pull import PULL_LOCK
from segments_service import (
    draft_segment, merge_draft_members, segment_prompt, validate_definition,
)
from store import load, slugify, write_json
from target_model import preview_plan_for_proposal

# Direction verbs that mean "remove this exposure" -> construct may propose
# outright drops (remove_target) for avoid-rated held, unpinned names.
_DROP_VERBS = ("drop", "remove", "exit", "sell out of", "cut", "eliminate")


def _is_drop_direction(direction: str) -> bool:
    text = (direction or "").strip().lower()
    return any(text.startswith(v) for v in _DROP_VERBS)


def strategy_progress(run_id: str, job_id: str | None):
    def progress(msg: str) -> None:
        if job_id:
            update_job(job_id, message=msg)
        orchestrate.update_run(run_id, message=msg)
    return progress


def run_strategy_draft(run_id: str) -> None:
    run = orchestrate.load_run(run_id)
    if not run:
        return
    direction = run["direction"]
    try:
        baseline = draft_segment(direction)
        definition = baseline["definition"]
        members = list(definition.get("members") or [])
        warnings = list(baseline.get("warnings") or [])
        backend_label = None
        if any(ticker_analysis.available_backends().values()):
            orchestrate.update_run(run_id, message="researching candidate tickers…")
            llm = ticker_analysis.draft_segment_members(direction)
            if llm.get("ok"):
                members = merge_draft_members(members, llm.get("members") or [])
                backend_label = llm.get("backend_label")
                if llm.get("title"):
                    definition["title"] = llm["title"]
                if llm.get("comment"):
                    definition["comment"] = llm["comment"]
            else:
                warnings.append(
                    "LLM draft failed (" + (llm.get("error") or "unknown")
                    + "); showing keyword matches only — edit the members before approving."
                )
        definition["members"] = members
        definition["sleeves"] = sorted({m["sleeve"] for m in members}) or ["other"]
        definition["status"] = "draft"
        orchestrate.set_state(
            run_id, orchestrate.AWAITING_SEGMENT,
            segment=baseline["slug"],
            message=f"Review the drafted segment ({len(members)} names), then approve.",
            draft={
                "slug": baseline["slug"],
                "definition": definition,
                "llm_prompt": baseline["llm_prompt"],
                "warnings": warnings,
                "backend_label": backend_label,
            },
        )
    except Exception as exc:  # noqa: BLE001
        orchestrate.set_state(run_id, orchestrate.ERROR,
                              error=f"{type(exc).__name__}: {exc}", message="drafting failed")


def start_strategy(direction: str) -> dict:
    run = orchestrate.new_run(direction)
    threading.Thread(target=run_strategy_draft, args=(run["run_id"],), daemon=True).start()
    return orchestrate.public(run)


def start_basket_plan(members, *, title: str | None = None) -> dict:
    """Draft a plan from a hand-picked basket.

    Build an APPROVED ad-hoc segment from the given ``[{symbol, sleeve}]`` members
    and launch synthesis straight away. The draft + segment-approval gates are
    skipped on purpose: the membership IS the user's explicit pick, so there is
    nothing to draft or approve. From there it runs exactly like any guided
    strategy run — Deep Research over the basket-as-segment (whose prompt also
    surfaces complementary names worth adding), deterministic pull, review,
    construct, and a pause at the proposal gate for review + staging. Raises
    ValueError on an empty basket."""
    members = [m for m in (members or []) if isinstance(m, dict) and m.get("symbol")]
    if not members:
        raise ValueError("basket is empty — star some tickers first")
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    ttl = (title or f"Basket plan {today}").strip()
    raw = {
        "title": ttl,
        "kind": "research",
        "status": "draft",  # approve_strategy_segment promotes + validates members
        "overlap_allowed": True,
        "comment": "Ad-hoc plan drafted from the cross-surface basket.",
        "members": members,
        "origin": {"type": "basket", "created": today},
    }
    definition = validate_definition(raw)
    slug = slugify(ttl)
    run = orchestrate.new_run(f"basket plan ({len(members)} pick{'s' if len(members) != 1 else ''})")
    run_id = run["run_id"]
    orchestrate.set_state(
        run_id, orchestrate.AWAITING_SEGMENT, segment=slug,
        message="drafted from your basket — starting synthesis…",
        draft={"slug": slug, "definition": definition, "warnings": [], "backend_label": None})
    # Reuse the segment-approval gate verbatim: it re-validates, writes the def,
    # transitions to SYNTHESIS_RUNNING, and spawns the synthesis worker.
    return approve_strategy_segment(run_id, definition)


def approve_strategy_segment(run_id: str, definition_raw: dict | None) -> dict:
    run = orchestrate.load_run(run_id)
    if not run:
        raise ValueError(f"unknown strategy run {run_id}")
    if run.get("state") not in (orchestrate.AWAITING_SEGMENT, orchestrate.NEEDS_LOGIN):
        raise Conflict(f"run {run_id} is not awaiting segment approval")
    raw = dict(definition_raw or (run.get("draft") or {}).get("definition") or {})
    raw["status"] = "approved"  # approving requires members; validate enforces it
    definition = validate_definition(raw)
    slug = run.get("segment") or slugify(definition.get("title") or "segment")
    write_json(SEGMENT_DEF_DIR / f"{slug}.json", definition)
    orchestrate.set_state(run_id, orchestrate.SYNTHESIS_RUNNING, segment=slug,
                          message="starting synthesis…", error=None)
    threading.Thread(target=run_strategy_synthesis, args=(run_id,), daemon=True).start()
    return orchestrate.public_or_raise(run_id)


def run_strategy_synthesis(run_id: str) -> None:
    run = orchestrate.load_run(run_id)
    if not run:
        return
    seg = run["segment"]
    if not seg:
        # The segment gate sets this before synthesis; absent means a corrupt or
        # out-of-order run -- fail loudly rather than feed None down the pipeline.
        orchestrate.set_state(run_id, orchestrate.ERROR,
                              error="run has no approved segment", message="synthesis failed")
        return
    job = new_job("strategy", segment=seg, run_id=run_id)
    orchestrate.update_run(run_id, job_id=job["id"])
    # This is the one card the user should see for the whole run; flip it to
    # running so it isn't stuck at "queued" while its child deep-research job
    # does the work (the Task Center folds that child into this entry).
    update_job(job["id"], state="running")
    progress = strategy_progress(run_id, job["id"])

    def fail(msg: str) -> None:
        orchestrate.set_state(run_id, orchestrate.ERROR, error=msg, message="synthesis failed")
        update_job(job["id"], state="error", error=msg)

    try:
        progress("building the Deep Research prompt…")
        prompt_info = segment_prompt(seg)
        date = prompt_info["date"]
        orchestrate.update_run(run_id, date=date)
        stem = f"{seg}-{date}"

        if (DEEP_DIR / f"{stem}.md").exists():
            progress("reusing the existing Deep Research report (no quota spent)…")
        else:
            try:
                sub = start_deep_research({
                    "segment": seg, "date": date,
                    "prompt": prompt_info["prompt"], "window_mode": "offscreen",
                })
            except RuntimeError as exc:
                return fail(str(exc))
            sub_id = sub["id"]
            # Tag the child so the Task Center can fold it into this strategy
            # card instead of listing it as a second, identical-looking task.
            update_job(sub_id, parent_run_id=run_id)
            # Last URL we propagated to the parent. Tracked locally so we never
            # mutate the shared registry job dict outside jobs._LOCK (update_job
            # is the only writer); avoids a read-modify-write race on the record.
            last_source_url = job.get("source_url")
            while True:
                time.sleep(3)
                pub = jobs.get_public(sub_id)
                if not pub:
                    return fail("Deep Research job vanished")
                if pub.get("message"):
                    progress(pub["message"])
                # Carry the live Perplexity URL up to the parent so the single
                # strategy card keeps the "view live run" link the child had.
                if pub.get("source_url") and pub.get("source_url") != last_source_url:
                    update_job(job["id"], source_url=pub["source_url"])
                    last_source_url = pub["source_url"]
                state = pub.get("state")
                if state == "done":
                    break
                if state == "needs_login":
                    orchestrate.set_state(
                        run_id, orchestrate.NEEDS_LOGIN,
                        message="Perplexity login required. Set it up, then resume the run.")
                    update_job(job["id"], state="done", message="paused for login")
                    return
                if state in ("error", "cancelled"):
                    return fail(pub.get("error") or f"Deep Research {state}")

        progress("pulling deterministic segment data…")
        try:
            with PULL_LOCK:
                research_pull.pull_segment(seg)
        except Exception as exc:  # noqa: BLE001 - deterministic data is best-effort
            progress(f"deterministic pull skipped: {exc}")

        progress("running the review gate…")
        review = review_deep_research.review(seg, date, write=True)
        progress("synthesizing target bands…")
        holdings = load(HOLDINGS_JSON)
        proposal = target_construct.construct(
            seg, date, review, progress=progress,
            holdings=holdings if isinstance(holdings, dict) else None,
            drop_mode=_is_drop_direction(run.get("direction") or ""))
        progress("computing the rebalance preview…")
        preview = preview_plan_for_proposal(proposal)

        orchestrate.set_state(
            run_id, orchestrate.AWAITING_PROPOSAL,
            proposal=proposal, preview=preview,
            review={
                "findings": review.get("findings"),
                "blocked_symbols": review.get("blocked_symbols"),
                "source_summary": review.get("source_summary"),
                "review_path": review.get("review_path"),
            },
            message=f"Review {len(proposal.get('changes') or [])} proposed target change(s), then approve.")
        update_job(job["id"], state="done", message="synthesis complete")
    except SystemExit as exc:
        fail(str(exc) or "missing report for this segment + date")
    except Exception as exc:  # noqa: BLE001
        fail(f"{type(exc).__name__}: {exc}")


def approve_strategy_proposal(run_id: str, changes, *, allow_blocked: bool = False) -> dict:
    """Approving no longer writes the live model -- it STAGES the run's changes
    into the shared working draft so multiple runs compose into one portfolio the
    user reviews and commits once. Edits made at the gate are persisted back to
    the proposal file for the record, then staged."""
    run = orchestrate.load_run(run_id)
    if not run:
        raise ValueError(f"unknown strategy run {run_id}")
    if run.get("state") != orchestrate.AWAITING_PROPOSAL:
        raise Conflict(f"run {run_id} is not awaiting proposal approval")
    seg, date = run.get("segment"), run.get("date")
    if not seg or not date:
        raise Conflict(f"run {run_id} has no segment/date to stage")
    if changes is not None:
        ppath = DEEP_DIR / f"{seg}-{date}.target-proposal.json"
        proposal = load(ppath) or (run.get("proposal") or {})
        proposal["changes"] = changes
        write_json(ppath, proposal)
    try:
        staged = target_staging.stage_proposal(
            seg, date, changes=changes, run_id=run_id, source="strategy",
            allow_blocked=allow_blocked)
    except Exception as exc:  # noqa: BLE001
        orchestrate.set_state(run_id, orchestrate.ERROR,
                              error=f"{type(exc).__name__}: {exc}", message="staging failed")
        raise
    diff = target_staging.diff_staged_vs_live()
    n = len(staged.get("applied") or [])
    orchestrate.set_state(
        run_id, orchestrate.STAGED, staged=staged, preview=diff,
        message=(f"Staged {n} change(s) into the working draft. "
                 f"Review the draft ({diff['counts']['total']} pending) and commit."))
    return orchestrate.public_or_raise(run_id)
