#!/usr/bin/env python3
"""Durable activity feed: tickers visited + background tasks performed.

The Task Center's job registry is in-memory and vanishes on restart, and the
"viewed tickers" list has only ever lived in the browser's localStorage. Neither
survives a server bounce or crosses devices. This module is the one server-side,
append-only record that does: a JSONL log under ``data/cache/`` (gitignored), one
event per line, read back by the Activity view.

Design notes:
- Append-only JSONL keeps writes O(1) and crash-safe (a torn final line is just
  skipped on read). We trim the file back to ``MAX_EVENTS`` only once it grows
  past a slack threshold, so the common path never rewrites the whole file.
- View events are debounced in-process: refreshing or re-opening the same ticker
  within ``VIEW_DEBOUNCE_S`` doesn't spam the log. The debounce is best-effort
  (in-memory) -- it only guards against floods, not correctness.
- Task events are logged once, on the terminal transition, by ``jobs.update_job``.

Stdlib only; imports just ``config`` so it stays a safe leaf.
"""

from __future__ import annotations

import datetime as dt
import json
import threading
from typing import Any

from config import ACTIVITY_LOG
from timeutil import now_iso

# Keep the tail bounded. We only rewrite (trim) when the line count exceeds
# MAX_EVENTS * (1 + SLACK), so appends stay cheap between trims.
MAX_EVENTS = 2000
_TRIM_SLACK = 0.5
VIEW_DEBOUNCE_S = 60.0

_LOCK = threading.Lock()
# In-process guard against consecutive duplicate view events (symbol -> epoch s).
_last_view: dict[str, float] = {}


def _append(event: dict[str, Any]) -> None:
    """Append one event as a JSONL line, trimming the file if it's grown too big.

    Never raises: the activity log is a convenience feed, so a disk hiccup must
    not break the request that triggered it."""
    try:
        ACTIVITY_LOG.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, ensure_ascii=False)
        with _LOCK:
            with ACTIVITY_LOG.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            _maybe_trim_locked()
    except Exception:  # noqa: BLE001 -- feed is best-effort
        pass


def _maybe_trim_locked() -> None:
    """Called under ``_LOCK``. Rewrite the file to the newest ``MAX_EVENTS`` lines
    once it drifts past the slack threshold, so the log can't grow without bound."""
    try:
        limit = int(MAX_EVENTS * (1 + _TRIM_SLACK))
        lines = ACTIVITY_LOG.read_text(encoding="utf-8").splitlines()
        if len(lines) <= limit:
            return
        keep = lines[-MAX_EVENTS:]
        ACTIVITY_LOG.write_text("\n".join(keep) + "\n", encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def record_view(symbol: str, name: str | None = None) -> bool:
    """Log that the user opened ``symbol``'s dossier. Debounced: a repeat view of
    the same symbol within ``VIEW_DEBOUNCE_S`` is dropped. Returns True if written."""
    sym = (symbol or "").strip().upper()
    if not sym:
        return False
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    with _LOCK:
        last = _last_view.get(sym)
        if last is not None and (now - last) < VIEW_DEBOUNCE_S:
            _last_view[sym] = now
            return False
        _last_view[sym] = now
    _append({"ts": now_iso(), "type": "view", "symbol": sym, "name": (name or "").strip()})
    return True


def record_task(job: dict[str, Any]) -> None:
    """Log a finished background task. Called on the terminal state transition;
    stores only the small routing identifiers the Activity view needs to deep-link
    back to the result (no giant report/result bodies)."""
    if not job:
        return
    artifact = job.get("artifact") if isinstance(job.get("artifact"), dict) else {}
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    event = {
        "ts": now_iso(),
        "type": "task",
        "id": job.get("id"),
        "kind": job.get("kind"),
        "state": job.get("state"),
        "symbol": job.get("symbol"),
        "segment": job.get("segment"),
        "run_id": job.get("run_id"),
        "stem": job.get("stem"),
        "artifact_stem": (artifact or {}).get("stem"),
        "slug": (result or {}).get("slug"),
        "error": job.get("error"),
        "message": job.get("message"),
    }
    _append(event)


def recent(limit: int = 400) -> list[dict[str, Any]]:
    """The newest ``limit`` events, newest-first. Skips torn/blank lines so a
    partially written final record never breaks the feed."""
    limit = max(1, int(limit))
    try:
        raw = ACTIVITY_LOG.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in reversed(raw):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except (ValueError, TypeError):
            continue
        if len(out) >= limit:
            break
    return out
