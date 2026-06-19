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

import datetime as dt
import uuid
from pathlib import Path
from typing import Any

from store import load as _load, write_json as _write_json
from config import REPO_ROOT

# Overridable so tests can point the manifest store at a temp dir, exactly like
# review_deep_research swaps its module-level paths.
DATA_DIR = REPO_ROOT / "data"
STRATEGY_DIR = DATA_DIR / "research" / "strategy"

# ---- states ---------------------------------------------------------------
DRAFT_RUNNING = "draft_running"
AWAITING_SEGMENT = "awaiting_segment_approval"
SYNTHESIS_RUNNING = "synthesis_running"
AWAITING_PROPOSAL = "awaiting_proposal_approval"
APPLYING = "applying"
DONE = "done"
ERROR = "error"
NEEDS_LOGIN = "needs_login"

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
    AWAITING_PROPOSAL: {APPLYING, SYNTHESIS_RUNNING},
    APPLYING: {DONE, ERROR},
    # An errored run can be retried from the top or re-synthesized.
    ERROR: {DRAFT_RUNNING, SYNTHESIS_RUNNING},
    DONE: set(),
}

# States in which the run is parked waiting for a human (no thread is running).
GATE_STATES = {AWAITING_SEGMENT, AWAITING_PROPOSAL, NEEDS_LOGIN}
# Terminal-ish states the UI should stop polling on.
RESTING_STATES = GATE_STATES | {DONE, ERROR}
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


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def manifest_path(run_id: str) -> Path:
    return STRATEGY_DIR / f"{run_id}.json"


def can_transition(current: str | None, to_state: str) -> bool:
    return to_state in _TRANSITIONS.get(current, set())


def new_run(direction: str) -> dict[str, Any]:
    """Create a fresh run manifest in DRAFT_RUNNING and persist it."""
    direction = (direction or "").strip()
    if not direction:
        raise ValueError("direction is required")
    run_id = uuid.uuid4().hex[:8]
    manifest: dict[str, Any] = {
        "run_id": run_id,
        "direction": direction,
        "state": None,
        "created_at": _now(),
        "updated_at": _now(),
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
        "error": None,
    }
    _transition(manifest, DRAFT_RUNNING)
    save_run(manifest)
    return manifest


def load_run(run_id: str) -> dict[str, Any] | None:
    return _load(manifest_path(run_id))


def save_run(manifest: dict[str, Any]) -> dict[str, Any]:
    manifest["updated_at"] = _now()
    _write_json(manifest_path(manifest["run_id"]), manifest)
    return manifest


def _transition(manifest: dict[str, Any], to_state: str) -> None:
    current = manifest.get("state")
    if not can_transition(current, to_state):
        raise ValueError(f"illegal strategy transition: {current} -> {to_state}")
    manifest["state"] = to_state
    # Stamp the live process onto any run we hand to a worker, so a later restart
    # can recognize the (now-dead) worker's run as orphaned.
    if to_state in RUNNING_STATES:
        manifest["boot"] = _BOOT_TOKEN


def set_state(run_id: str, to_state: str, **fields: Any) -> dict[str, Any]:
    """Load, guard the transition, merge fields, and persist. The single way the
    server's runners move a run forward."""
    manifest = load_run(run_id)
    if not manifest:
        raise ValueError(f"unknown strategy run {run_id}")
    _transition(manifest, to_state)
    manifest.update(fields)
    return save_run(manifest)


def update_run(run_id: str, **fields: Any) -> dict[str, Any] | None:
    """Merge fields WITHOUT a state change (e.g. progress messages). Returns None
    for an unknown run so a late progress callback can't crash a runner."""
    manifest = load_run(run_id)
    if not manifest:
        return None
    manifest.update(fields)
    return save_run(manifest)


def is_orphaned(manifest: dict[str, Any] | None) -> bool:
    """True if *manifest* claims a worker is running but that worker belongs to a
    dead server process. A thread cannot survive a restart, so a run in a
    RUNNING_STATE whose boot token != the live process's is provably stalled."""
    if not manifest or manifest.get("state") not in RUNNING_STATES:
        return False
    if _BOOT_TOKEN is None:
        return False  # liveness unknown (no server context) -- don't reap
    return manifest.get("boot") != _BOOT_TOKEN


def reap_if_orphaned(manifest: dict[str, Any] | None) -> dict[str, Any] | None:
    """Durably fail an orphaned run so the UI stops spinning forever and offers a
    retry. Returns the healed manifest (or the original if nothing to do). An
    errored run is retryable per _TRANSITIONS (ERROR -> DRAFT_RUNNING/SYNTHESIS)."""
    if not is_orphaned(manifest):
        return manifest
    return set_state(
        manifest["run_id"], ERROR,
        error="The run was interrupted by a server restart; its background worker "
              "did not survive. Start a new run.",
        message="interrupted by a server restart",
    )


def list_runs(limit: int = 25) -> list[dict[str, Any]]:
    """Recent runs (newest first), trimmed to list-card fields."""
    out: list[dict[str, Any]] = []
    if not STRATEGY_DIR.exists():
        return out
    for path in sorted(STRATEGY_DIR.glob("*.json"), reverse=True):
        m = _load(path) or {}
        if not m.get("run_id"):
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


def public(manifest: dict[str, Any], *, job: dict | None = None) -> dict[str, Any]:
    """UI-facing view: the full manifest plus a live message borrowed from the
    in-leg job (if one is running), so a single GET drives the whole UI."""
    view = dict(manifest)
    if job and job.get("message") and manifest.get("state") not in RESTING_STATES:
        view["message"] = job["message"]
    view["job"] = job
    view["resting"] = manifest.get("state") in RESTING_STATES
    view["at_gate"] = manifest.get("state") in GATE_STATES
    return view
