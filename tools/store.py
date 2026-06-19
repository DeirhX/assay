#!/usr/bin/env python3
"""Tiny disk + identifier helpers shared by the research server.

Extracted from serve.py so the IO primitives (read/write JSON and text) and the
two identifier validators (slug, symbol) live in one small, dependency-free
place that other modules can import without dragging in the whole HTTP server.

All writes create parent directories and use UTF-8 so reports with em-dashes
survive on Windows. ``load`` is forgiving: a missing or corrupt file returns
None rather than raising, because most callers treat "no data" and "bad data"
the same way (fall back to a live pull / empty default).
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import uuid
from pathlib import Path

# Serializes writes across the server's request/job threads. The HTTP server is
# threaded, so two handlers (e.g. proposal-apply + an IBKR sync) can race on the
# same JSON file; a single process-wide lock plus an atomic replace makes every
# write all-or-nothing. Writes are small and rare, so one global lock is fine.
_WRITE_LOCK = threading.Lock()


def load(path: Path, default=None, *, strict: bool = False):
    """Forgiving JSON read: missing or corrupt file returns ``default``.

    A *missing* file is normal and silent. A file that EXISTS but won't parse is
    corruption, not "no data" -- it's reported on stderr (and re-raised when
    ``strict`` is set) so a clobbered target model can't masquerade as an empty
    default. Pass ``default`` (e.g. ``{}`` or ``[]``) for a concrete empty shape.
    """
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        if strict:
            raise
        # Present-but-unreadable: surface it instead of silently masking the loss.
        print(f"store.load: ignoring unreadable {path} "
              f"({type(exc).__name__}: {exc})", file=sys.stderr)
        return default


def write_json(path: Path, payload, *, sort_keys: bool = True) -> None:
    _atomic_write(path, json.dumps(payload, indent=2, sort_keys=sort_keys) + "\n")


def write_text(path: Path, payload: str) -> None:
    _atomic_write(path, payload)


def _atomic_write(path: Path, text: str) -> None:
    """Write the whole file or nothing: stream to a sibling temp file, flush to
    disk, then ``os.replace`` (atomic on the same filesystem). Held under
    ``_WRITE_LOCK`` so concurrent writers can't interleave or leave a truncated
    file. A failed write cleans up its temp so we don't litter ``*.tmp-*``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex[:8]}")
    with _WRITE_LOCK:
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    if not slug or len(slug) > 64:
        raise ValueError("bad segment slug")
    return slug


def safe_symbol(value: str) -> str:
    sym = (value or "").upper().strip()
    if not sym or len(sym) > 16 or not re.match(r"^[A-Z0-9.=\- ]+$", sym):
        raise ValueError(f"bad symbol: {value!r}")
    return sym
