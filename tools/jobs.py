#!/usr/bin/env python3
"""In-memory job registry for the research server.

The server runs background work on daemon threads -- Perplexity Deep Research
browser runs, login windows, URL imports, and CLI ticker analyses -- and the UI
learns about progress by polling. This module owns the shared, thread-safe job
state so the HTTP layer and the job runners agree on a single source of truth.

Jobs are in-memory and vanish on restart; the durable record is the artifacts
written to disk.

Concurrency: browser runs (Perplexity Deep Research / login / import) used to be
serialized one-at-a-time because they shared one un-clonable Chrome profile. They
are now bounded by a counting limit instead, so several can run in parallel --
each run gets its own throwaway clone of the logged-in profile (see
``pplx_deep_research.clone_base_profile``), which is what lets multiple Chromiums
coexist. Take a slot with ``claim_active`` before a browser run and drop it with
``release_active`` after. The ceiling defaults to ``PPLX_MAX_CONCURRENT`` (3) and
is overridable at runtime via ``configure_max_slots``. CLI analyses deliberately
do NOT take a slot -- they are independent subprocesses and run alongside.
"""

from __future__ import annotations

import datetime as dt
import os
import threading
import uuid
from enum import StrEnum
from typing import Any, Callable, NotRequired, TypedDict, cast


class JobState(StrEnum):
    """Lifecycle states a background job moves through. A ``StrEnum`` so members
    compare equal to (and JSON-serialize as) their plain string value -- existing
    code that reads ``job["state"] == "done"`` or round-trips the value through
    JSON keeps working unchanged."""
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"
    NEEDS_LOGIN = "needs_login"


# Live states: a job is still doing (or about to do) work and counts against the
# "is one already in flight?" guards.
_LIVE_STATES = (JobState.QUEUED, JobState.RUNNING)


class Job(TypedDict):
    """The in-memory job record. The four head fields are always set by
    ``new_job``; the rest are optional identifiers/results stashed by the various
    runners (a deep-research run carries ``stem``/``source_url``, a ticker
    analysis carries ``symbol``, etc.). ``new_job(**fields)`` still accepts
    arbitrary keys -- this documents the shapes the registry and Task Center
    actually read."""
    id: str
    kind: str
    state: str
    message: str
    created_at: str
    updated_at: NotRequired[str]
    segment: NotRequired[str | None]
    date: NotRequired[str | None]
    symbol: NotRequired[str | None]
    stem: NotRequired[str | None]
    run_id: NotRequired[str | None]
    parent_run_id: NotRequired[str | None]
    source_url: NotRequired[str | None]
    result: NotRequired[dict | None]
    artifact: NotRequired[dict | None]
    error: NotRequired[str | None]
    cancelled: NotRequired[bool]


def _env_max_slots() -> int:
    try:
        return max(1, int(os.environ.get("PPLX_MAX_CONCURRENT", "3") or "3"))
    except (TypeError, ValueError):
        return 3


_JOBS: dict[str, Job] = {}
_LOCK = threading.Lock()
# Number of browser slots currently held, and the ceiling on concurrent runs.
_ACTIVE = {"held": 0, "max": _env_max_slots()}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def new_job(kind: str, **fields: Any) -> Job:
    job = cast(Job, {
        "id": uuid.uuid4().hex[:8],
        "kind": kind,
        "state": JobState.QUEUED,
        "message": "",
        "created_at": _now(),
        **fields,
    })
    with _LOCK:
        _JOBS[job["id"]] = job
    return job


def update_job(job_id: str, **fields: Any) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return
        job.update(cast(Job, fields))
        job["updated_at"] = _now()


def public(job: Job) -> dict[str, Any]:
    """The UI-safe view of a job (no giant report body).

    Besides progress fields, this exposes the small set of identifiers the Task
    Center needs to route a finished task back to its result view -- ``symbol``
    (ticker analysis/Q&A), ``stem`` (deep-research report Q&A), ``run_id``
    (guided strategy run) -- alongside ``segment``/``artifact``/``result`` which
    already carry stems and slugs for the other kinds.
    """
    return {
        "id": job["id"],
        "kind": job["kind"],
        "state": job["state"],
        "message": job.get("message", ""),
        "segment": job.get("segment"),
        "date": job.get("date"),
        "symbol": job.get("symbol"),
        "stem": job.get("stem"),
        "run_id": job.get("run_id"),
        # Set on a child job (e.g. a deep-research run spawned by a guided
        # strategy run) so the Task Center can fold it into its parent's card
        # instead of showing two entries for one logical run.
        "parent_run_id": job.get("parent_run_id"),
        # The live Perplexity run URL, surfaced mid-run so the UI can link to the
        # ongoing analysis (and so a clarification stall is one click away).
        "source_url": job.get("source_url"),
        "result": job.get("result"),
        "artifact": job.get("artifact"),
        "error": job.get("error"),
        "cancelled": bool(job.get("cancelled")),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
    }


def spawn(kind: str, target: Callable[..., None], *args: Any, **fields: Any) -> dict[str, Any]:
    """Register a job of ``kind`` and start ``target(job_id, *args)`` on a daemon
    thread, returning the job's public view. Centralizes the new_job + Thread +
    public tail every ``start_*`` runner repeated: keyword args seed the job
    record (``segment=``, ``symbol=``, ...); positional args are forwarded to the
    worker after the new job id."""
    job = new_job(kind, **fields)
    threading.Thread(target=target, args=(job["id"], *args), daemon=True).start()
    return public(job)


def cancel_job(job_id: str) -> bool:
    """Flag a job for cooperative cancellation. The runner is responsible for
    noticing (via ``is_cancelled``) and tearing down its subprocess; here we just
    set the intent so concurrency checks stop counting it as live. Returns False
    if the job is unknown or already finished."""
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job or job.get("state") not in _LIVE_STATES:
            return False
        job["cancelled"] = True
        job["message"] = "cancelling…"
        job["updated_at"] = _now()
        return True


def is_cancelled(job_id: str) -> bool:
    with _LOCK:
        job = _JOBS.get(job_id)
        return bool(job and job.get("cancelled"))


def get_public(job_id: str) -> dict[str, Any] | None:
    """Thread-safe public snapshot of one job, or None if unknown."""
    with _LOCK:
        job = _JOBS.get(job_id)
        return public(job) if job else None


def list_public() -> list[dict[str, Any]]:
    """Public snapshots of every known job, newest first.

    Backs the central Task Center. The registry is in-memory, so this is the
    full set of jobs since the server last started -- it does not survive a
    restart, by design (the durable record is the artifacts on disk)."""
    with _LOCK:
        jobs = [public(j) for j in _JOBS.values()]
    jobs.sort(key=lambda j: j.get("created_at") or "", reverse=True)
    return jobs


def find(predicate: Callable[[Job], bool]) -> bool:
    """True if any job satisfies predicate (evaluated under the lock)."""
    with _LOCK:
        return any(predicate(j) for j in _JOBS.values())


def running(kind: str, **match: Any) -> bool:
    """True if a job of ``kind`` is still live (queued/running and not flagged
    for cancellation), optionally matching extra public fields like
    ``symbol=`` or ``stem=``.

    This is the one place the "is there already an X in flight?" guard lives, so
    every caller agrees that a cancelled-but-not-yet-reaped job no longer counts
    as live (its slot is being released) and can't wedge new work."""
    def predicate(j: Job) -> bool:
        if j.get("kind") != kind or j.get("state") not in _LIVE_STATES:
            return False
        if j.get("cancelled"):
            return False
        return all(j.get(k) == v for k, v in match.items())

    return find(predicate)


def claim_active() -> bool:
    """Take one of the bounded browser slots; False if all are in use."""
    with _LOCK:
        if _ACTIVE["held"] >= _ACTIVE["max"]:
            return False
        _ACTIVE["held"] += 1
        return True


def release_active() -> None:
    with _LOCK:
        if _ACTIVE["held"] > 0:
            _ACTIVE["held"] -= 1


def active_count() -> int:
    """How many browser slots are currently held."""
    with _LOCK:
        return _ACTIVE["held"]


def max_slots() -> int:
    """The ceiling on concurrent browser runs."""
    with _LOCK:
        return _ACTIVE["max"]


def slots_busy_msg() -> str:
    """The uniform 'all browser slots are busy' Conflict message. Lives here next
    to the slot counter so the server and every browser-backed service share one
    wording instead of each re-deriving it from max_slots()."""
    return (f"all {max_slots()} Perplexity browser slots are busy "
            "— wait for a run to finish, or raise PPLX_MAX_CONCURRENT")


def configure_max_slots(n: int) -> int:
    """Set the concurrent-browser ceiling (>=1). Returns the value applied.

    Lowering it does not evict slots already held; it just stops new claims
    until enough finish. Used by the server at boot and by tests."""
    with _LOCK:
        _ACTIVE["max"] = max(1, int(n))
        return _ACTIVE["max"]


def any_active() -> bool:
    """True if any browser slot is held or any job is queued/running."""
    with _LOCK:
        if _ACTIVE["held"] > 0:
            return True
        return any(j.get("state") in _LIVE_STATES for j in _JOBS.values())
