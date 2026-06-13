#!/usr/bin/env python3
"""Append-only operational error log.

This is for *real* failures we want a durable record of -- an LLM backend
falling over (Cursor's login expiring and the run silently failing over to
Claude, say), or an unhandled server exception. It is deliberately NOT for
expected, user-driven misses: "that ticker has no market data", "you're not
logged into Perplexity yet", a bad symbol search. Those are normal control
flow, not incidents, and logging them would only bury the signal we care about.

Records are JSON lines in ``data/error_log.jsonl``, oldest first on disk. The
file is capped at ``MAX_ENTRIES`` so it can't grow without bound; each append
rewrites the file trimmed to the most recent entries. All writes are guarded by
a process-wide lock because several daemon job threads can log at once.

The log must never become a source of errors itself, so every filesystem op is
best-effort and swallows ``OSError``.
"""

from __future__ import annotations

import datetime as dt
import json
import threading
from pathlib import Path
from typing import Any

LOG_PATH = Path(__file__).resolve().parent.parent / "data" / "error_log.jsonl"
MAX_ENTRIES = 500

_LOCK = threading.Lock()


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def log(category: str, message: str, *, level: str = "error", **context: Any) -> dict:
    """Append one record and return it. ``context`` is stored verbatim (drop the
    ``None``s so the record stays tidy) -- keep it small and JSON-serialisable."""
    rec: dict[str, Any] = {
        "ts": _now(),
        "level": str(level),
        "category": str(category),
        "message": str(message),
    }
    ctx = {k: v for k, v in context.items() if v is not None}
    if ctx:
        rec["context"] = ctx
    line = json.dumps(rec, ensure_ascii=False)
    with _LOCK:
        try:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            lines = (
                LOG_PATH.read_text(encoding="utf-8").splitlines()
                if LOG_PATH.exists()
                else []
            )
            lines.append(line)
            if len(lines) > MAX_ENTRIES:
                lines = lines[-MAX_ENTRIES:]
            LOG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError:
            pass
    return rec


def warn(category: str, message: str, **context: Any) -> dict:
    """A recoverable degradation -- e.g. one backend failed but a fallback served."""
    return log(category, message, level="warning", **context)


def error(category: str, message: str, **context: Any) -> dict:
    """A hard failure -- nothing recovered it."""
    return log(category, message, level="error", **context)


def recent(limit: int = 200) -> list[dict]:
    """The most recent entries, newest first, capped at ``limit``."""
    with _LOCK:
        if not LOG_PATH.exists():
            return []
        try:
            raw = LOG_PATH.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
    out: list[dict] = []
    for line in raw[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            out.append(parsed)
    out.reverse()
    return out


def clear() -> None:
    """Wipe the log (the UI's 'Clear' button)."""
    with _LOCK:
        try:
            if LOG_PATH.exists():
                LOG_PATH.unlink()
        except OSError:
            pass
