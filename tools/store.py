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
import re
from pathlib import Path


def load(path: Path, default=None):
    """Forgiving JSON read: missing or corrupt file returns ``default``.

    Most callers treat "no data" and "bad data" the same way (fall back to a
    live pull / empty default), so a corrupt file is not worth a stack trace.
    Pass ``default`` (e.g. ``{}`` or ``[]``) when the caller wants a concrete
    empty shape instead of ``None``.
    """
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def write_json(path: Path, payload, *, sort_keys: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=sort_keys) + "\n", encoding="utf-8")


def write_text(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")


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
