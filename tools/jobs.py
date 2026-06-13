#!/usr/bin/env python3
"""In-memory job registry for the research server.

The server runs background work on daemon threads -- Perplexity Deep Research
browser runs, login windows, URL imports, and CLI ticker analyses -- and the UI
learns about progress by polling. This module owns the shared, thread-safe job
state so the HTTP layer and the job runners agree on a single source of truth.

Jobs are in-memory and vanish on restart; the durable record is the artifacts
written to disk.

Concurrency: a single ``_ACTIVE`` flag guards the Perplexity browser, which uses
one persistent Chrome profile that cannot be opened twice and spends scarce Pro
quota. Take it with ``claim_active`` before a browser run and drop it with
``release_active`` after. CLI analyses deliberately do NOT take it -- they are
independent subprocesses and may run alongside a browser run.
"""

from __future__ import annotations

import datetime as dt
import threading
import uuid
from typing import Callable

_JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()
_ACTIVE = {"running": False}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def new_job(kind: str, **fields) -> dict:
    job = {
        "id": uuid.uuid4().hex[:8],
        "kind": kind,
        "state": "queued",
        "message": "",
        "created_at": _now(),
        **fields,
    }
    with _LOCK:
        _JOBS[job["id"]] = job
    return job


def update_job(job_id: str, **fields) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return
        job.update(fields)
        job["updated_at"] = _now()


def public(job: dict) -> dict:
    """The UI-safe view of a job (no giant report body)."""
    return {
        "id": job["id"],
        "kind": job["kind"],
        "state": job["state"],
        "message": job.get("message", ""),
        "segment": job.get("segment"),
        "date": job.get("date"),
        # The live Perplexity run URL, surfaced mid-run so the UI can link to the
        # ongoing analysis (and so a clarification stall is one click away).
        "source_url": job.get("source_url"),
        "result": job.get("result"),
        "artifact": job.get("artifact"),
        "error": job.get("error"),
        "cancelled": bool(job.get("cancelled")),
        "updated_at": job.get("updated_at"),
    }


def cancel_job(job_id: str) -> bool:
    """Flag a job for cooperative cancellation. The runner is responsible for
    noticing (via ``is_cancelled``) and tearing down its subprocess; here we just
    set the intent so concurrency checks stop counting it as live. Returns False
    if the job is unknown or already finished."""
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job or job.get("state") not in ("queued", "running"):
            return False
        job["cancelled"] = True
        job["message"] = "cancelling…"
        job["updated_at"] = _now()
        return True


def is_cancelled(job_id: str) -> bool:
    with _LOCK:
        job = _JOBS.get(job_id)
        return bool(job and job.get("cancelled"))


def get_public(job_id: str) -> dict | None:
    """Thread-safe public snapshot of one job, or None if unknown."""
    with _LOCK:
        job = _JOBS.get(job_id)
        return public(job) if job else None


def find(predicate: Callable[[dict], bool]) -> bool:
    """True if any job satisfies predicate (evaluated under the lock)."""
    with _LOCK:
        return any(predicate(j) for j in _JOBS.values())


def claim_active() -> bool:
    """Take the single browser/login slot; False if it's already held."""
    with _LOCK:
        if _ACTIVE["running"]:
            return False
        _ACTIVE["running"] = True
        return True


def release_active() -> None:
    with _LOCK:
        _ACTIVE["running"] = False


def any_active() -> bool:
    """True if the browser slot is held or any job is queued/running."""
    with _LOCK:
        if _ACTIVE["running"]:
            return True
        return any(j.get("state") in ("queued", "running") for j in _JOBS.values())
