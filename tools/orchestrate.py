#!/usr/bin/env python3
"""Durable state machine for the guided "Direction -> Rebalance" strategy run.

The research workflow (draft a segment, run Deep Research, pull deterministic
data, review, construct a target-model delta, apply, recommend) used to be a
chain of buttons clicked in the browser. This module owns the *durable* spine of
a single guided run so the chain can advance on its own and survive a refresh or
a server restart.

Design split (deliberate, to keep this module a testable leaf):

* This module owns ONLY the run manifest (a JSON file per run) and the legal
  state transitions. It imports just ``store`` (disk IO) -- never ``serve`` --
  so it has no web/server/browser dependencies and is trivial to unit-test.
* ``serve.py`` owns the per-leg work (drafting, Deep Research, review,
  construction, apply). Its background threads drive a run forward by calling
  the small helpers here (``set_state`` / ``update_run``). This mirrors the
  existing job-runner architecture, where ``jobs.py`` holds state and
  ``serve.py`` holds the runners.

A run pauses at a gate by simply *stopping* at an ``awaiting_*`` state -- no
thread is left blocked waiting on a human. The frontend renders the gate and an
approve call starts the next async leg.
"""

from __future__ import annotations

import threading
import uuid
from enum import StrEnum
from pathlib import Path
from typing import Any, TypedDict, cast

from store import load as _load, write_json as _write_json
from timeutil import now_iso
from config import DATA_DIR

# Overridable so tests can point the manifest store at a temp dir, exactly like
# review_deep_research swaps its module-level paths.
STRATEGY_DIR = DATA_DIR / "research" / "strategy"

# Serializes the load->mutate->save cycle in set_state/update_run so two worker
# threads (e.g. a leg runner and a poller reaping an orphan) can't clobber each
# other's field merges. The per-write fsync lock in store only protects the file
# write itself, not the read-modify-write around it.
_MANIFEST_LOCK = threading.RLock()


# ---- states ---------------------------------------------------------------
class StrategyState(StrEnum):
    """The states a guided run moves through. A ``StrEnum`` so each member
    compares equal to (and JSON-serializes as) its plain string value -- a
    manifest reloaded from disk comes back as a bare ``str`` and still matches
    these members in transitions/sets."""
    DRAFT_RUNNING = "draft_running"
    AWAITING_SEGMENT = "awaiting_segment_approval"
    SYNTHESIS_RUNNING = "synthesis_running"
    AWAITING_PROPOSAL = "awaiting_proposal_approval"
    APPLYING = "applying"
    STAGED = "staged"
    DONE = "done"
    ERROR = "error"
    NEEDS_LOGIN = "needs_login"


# Back-compat module-level aliases: serve.py / strategy_service.py / tests refer
# to orchestrate.DRAFT_RUNNING etc. These now resolve to StrategyState members.
DRAFT_RUNNING = StrategyState.DRAFT_RUNNING
AWAITING_SEGMENT = StrategyState.AWAITING_SEGMENT
SYNTHESIS_RUNNING = StrategyState.SYNTHESIS_RUNNING
AWAITING_PROPOSAL = StrategyState.AWAITING_PROPOSAL
APPLYING = StrategyState.APPLYING
STAGED = StrategyState.STAGED
DONE = StrategyState.DONE
ERROR = StrategyState.ERROR
NEEDS_LOGIN = StrategyState.NEEDS_LOGIN

# Legal transitions. The keys are the *current* state (None == a brand-new run);
# the values are the states it may move to. Guarding transitions turns an
# orchestration bug (e.g. applying before the proposal gate) into a loud error
# instead of a silently corrupt run.
_TRANSITIONS: dict[str | None, set[str]] = {
    None: {DRAFT_RUNNING},
    DRAFT_RUNNING: {AWAITING_SEGMENT, ERROR},
    AWAITING_SEGMENT: {SYNTHESIS_RUNNING},
    SYNTHESIS_RUNNING: {AWAITING_PROPOSAL, ERROR, NEEDS_LOGIN},
    NEEDS_LOGIN: {SYNTHESIS_RUNNING, ERROR},
    # Approving the proposal now STAGES into the working draft (no live write);
    # APPLYING is kept for back-compat with any legacy direct-apply path.
    AWAITING_PROPOSAL: {APPLYING, STAGED, SYNTHESIS_RUNNING},
    APPLYING: {DONE, ERROR},
    # Staged is a resting endpoint; the user commits the draft globally. A run
    # may be re-synthesized from here if the direction is revisited.
    STAGED: {SYNTHESIS_RUNNING},
    # An errored run can be retried from the top or re-synthesized.
    ERROR: {DRAFT_RUNNING, SYNTHESIS_RUNNING},
    DONE: set(),
}

# States in which the run is parked waiting for a human (no thread is running).
GATE_STATES = {AWAITING_SEGMENT, AWAITING_PROPOSAL, NEEDS_LOGIN}
# Terminal-ish states the UI should stop polling on.
RESTING_STATES = GATE_STATES | {STAGED, DONE, ERROR}
# States in which a background worker thread SHOULD be alive driving the run.
# A thread cannot outlive its process, so if a run is parked in one of these from
# a previous server boot, its worker is dead and the run is orphaned (see
# is_orphaned / reap_if_orphaned).
RUNNING_STATES = {DRAFT_RUNNING, SYNTHESIS_RUNNING, APPLYING}

# Identifies the current server process. serve.py stamps this once at startup;
# every run that enters a RUNNING_STATE records the boot token of the process
# that started its worker. When the token on a running run no longer matches the
# live process, the worker died on a restart -- the run is orphaned. None (the
# default, e.g. in unit tests) disables the check.
_BOOT_TOKEN: str | None = None


def set_boot_token(token: str | None) -> None:
    """Register the live server process token (called once by serve.py at boot)."""
    global _BOOT_TOKEN
    _BOOT_TOKEN = token


def manifest_path(run_id: str) -> Path:
    return STRATEGY_DIR / f"{run_id}.json"


def can_transition(current: str | None, to_state: str) -> bool:
    return to_state in _TRANSITIONS.get(current, set())


class RunManifest(TypedDict):
    """The durable per-run record persisted as ``<run_id>.json``. ``new_run``
    seeds every key; the blob legs (``draft``/``review``/``proposal``/...) start
    as None and are filled in as the run advances. ``set_state``/``update_run``
    still accept arbitrary ``**fields`` -- this documents the canonical shape the
    UI and runners read."""
    run_id: str
    direction: str
    state: str | None
    created_at: str
    updated_at: str
    message: str
    job_id: str | None
    boot: str | None
    segment: str | None
    date: str | None
    draft: Any
    review: Any
    proposal: Any
    preview: Any
    applied: Any
    staged: Any
    error: str | None


def new_run(direction: str) -> RunManifest:
    """Create a fresh run manifest in DRAFT_RUNNING and persist it."""
    direction = (direction or "").strip()
    if not direction:
        raise ValueError("direction is required")
    run_id = uuid.uuid4().hex[:8]
    manifest: RunManifest = {
        "run_id": run_id,
        "direction": direction,
        "state": None,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "message": "drafting a research segment…",
        "job_id": None,
        "boot": None,
        "segment": None,
        "date": None,
        "draft": None,
        "review": None,
        "proposal": None,
        "preview": None,
        "applied": None,
        "staged": None,
        "error": None,
    }
    _transition(manifest, DRAFT_RUNNING)
    save_run(manifest)
    return manifest


def load_run(run_id: str) -> RunManifest | None:
    return cast("RunManifest | None", _load(manifest_path(run_id)))


def save_run(manifest: RunManifest) -> RunManifest:
    manifest["updated_at"] = now_iso()
    _write_json(manifest_path(manifest["run_id"]), manifest)
    return manifest


def _transition(manifest: RunManifest, to_state: str) -> None:
    current = manifest.get("state")
    if not can_transition(current, to_state):
        raise ValueError(f"illegal strategy transition: {current} -> {to_state}")
    manifest["state"] = to_state
    # Stamp the live process onto any run we hand to a worker, so a later restart
    # can recognize the (now-dead) worker's run as orphaned.
    if to_state in RUNNING_STATES:
        manifest["boot"] = _BOOT_TOKEN


def set_state(run_id: str, to_state: str, **fields: Any) -> RunManifest:
    """Load, guard the transition, merge fields, and persist. The single way the
    server's runners move a run forward. The load->save cycle is locked so a
    concurrent ``update_run`` (or a second ``set_state``) can't drop fields."""
    with _MANIFEST_LOCK:
        manifest = load_run(run_id)
        if not manifest:
            raise ValueError(f"unknown strategy run {run_id}")
        _transition(manifest, to_state)
        manifest.update(cast(RunManifest, fields))
        return save_run(manifest)


def update_run(run_id: str, **fields: Any) -> RunManifest | None:
    """Merge fields WITHOUT a state change (e.g. progress messages). Returns None
    for an unknown run so a late progress callback can't crash a runner. Shares
    the manifest lock with set_state so the merge is not lost to a racing write."""
    with _MANIFEST_LOCK:
        manifest = load_run(run_id)
        if not manifest:
            return None
        manifest.update(cast(RunManifest, fields))
        return save_run(manifest)


def is_orphaned(manifest: RunManifest | None) -> bool:
    """True if *manifest* claims a worker is running but that worker belongs to a
    dead server process. A thread cannot survive a restart, so a run in a
    RUNNING_STATE whose boot token != the live process's is provably stalled."""
    if not manifest or manifest.get("state") not in RUNNING_STATES:
        return False
    if _BOOT_TOKEN is None:
        return False  # liveness unknown (no server context) -- don't reap
    return manifest.get("boot") != _BOOT_TOKEN


def reap_if_orphaned(manifest: RunManifest | None) -> RunManifest | None:
    """Durably fail an orphaned run so the UI stops spinning forever and offers a
    retry. Returns the healed manifest (or the original if nothing to do). An
    errored run is retryable per _TRANSITIONS (ERROR -> DRAFT_RUNNING/SYNTHESIS)."""
    if not is_orphaned(manifest):
        return manifest
    assert manifest is not None  # is_orphaned is False for None
    try:
        return set_state(
            manifest["run_id"], ERROR,
            error="The run was interrupted by a server restart; its background worker "
                  "did not survive. Start a new run.",
            message="interrupted by a server restart",
        )
    except ValueError:
        # Another poller won the reap race and already moved it out of a running
        # state; return the now-healed manifest rather than raising.
        return load_run(manifest["run_id"]) or manifest


def list_runs(limit: int = 25) -> list[dict[str, Any]]:
    """Recent runs (newest first), trimmed to list-card fields."""
    out: list[dict[str, Any]] = []
    if not STRATEGY_DIR.exists():
        return out
    for path in sorted(STRATEGY_DIR.glob("*.json"), reverse=True):
        m = cast("RunManifest | None", _load(path))
        if not m or not m.get("run_id"):
            continue
        m = reap_if_orphaned(m) or m  # keep the recents list honest, too
        out.append({
            "run_id": m["run_id"],
            "direction": m.get("direction"),
            "state": m.get("state"),
            "segment": m.get("segment"),
            "date": m.get("date"),
            "created_at": m.get("created_at"),
            "updated_at": m.get("updated_at"),
        })
    out.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return out[:limit]


def public_or_raise(run_id: str, *, job: dict | None = None) -> dict[str, Any]:
    """Load a run and return its UI view; raise if it vanished. Saves callers
    that just transitioned a run from re-deriving the (now provably present)
    manifest and re-handling the None case."""
    manifest = load_run(run_id)
    if not manifest:
        raise ValueError(f"unknown strategy run {run_id}")
    return public(manifest, job=job)


def public(manifest: RunManifest, *, job: dict | None = None) -> dict[str, Any]:
    """UI-facing view: the full manifest plus a live message borrowed from the
    in-leg job (if one is running), so a single GET drives the whole UI."""
    view: dict[str, Any] = dict(manifest)
    if job and job.get("message") and manifest.get("state") not in RESTING_STATES:
        view["message"] = job["message"]
    view["job"] = job
    view["resting"] = manifest.get("state") in RESTING_STATES
    view["at_gate"] = manifest.get("state") in GATE_STATES
    return view
