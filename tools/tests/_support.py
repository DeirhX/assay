"""Shared test helpers: put the tools/ dir on sys.path so tests can import the
modules under test directly (research_pull, portfolio, ...), and provide a
loader that hijacks a real scraped research dossier as a fixture."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = TOOLS_DIR.parent

if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))


def sample_record() -> dict | None:
    """First committed, well-formed ticker dossier under data/research/.

    We reuse real scraped data instead of inventing a fixture so the integration
    smoke tests exercise the actual shape the providers produce. Returns None if
    nothing is available (e.g. a bare checkout), so callers can skip."""
    research = REPO_ROOT / "data" / "research"
    if not research.exists():
        return None
    for path in sorted(research.glob("*.json")):
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(rec, dict) and rec.get("symbol") and isinstance(rec.get("metrics"), dict):
            return rec
    return None
