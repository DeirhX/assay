#!/usr/bin/env python3
"""Repo layout + secrets resolution, in one dependency-free place.

Every tool module used to recompute ``REPO_ROOT = Path(__file__).resolve()
.parent.parent`` and re-derive ``data/`` subpaths, and the two gitignored
``secrets.env`` locations were hardcoded in a handful of files. This centralizes
the layout so it lives in exactly one spot -- which also de-risks splitting the
serve.py monolith later, since the pieces all need these paths.

Stdlib only and imports nothing from the project, so it stays a safe leaf that
anything (store, the IBKR readers, the HTTP server) can import without cycles.
"""

from __future__ import annotations

from pathlib import Path

# tools/config.py -> tools/ -> repo root.
TOOLS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOLS_DIR.parent
DATA_DIR = REPO_ROOT / "data"
RESEARCH_DIR = DATA_DIR / "research"
WEB_DIR = REPO_ROOT / "web"

# The curated, committed portfolio snapshot. Shared by the HTTP server and the
# holdings-sync service, so it lives on the leaf to avoid an import cycle.
HOLDINGS_JSON = DATA_DIR / "current-holdings.json"

# Deep-research artifacts and the committed target model. Shared by the server
# and the target-model service, so they live here for a single source of truth.
DEEP_DIR = RESEARCH_DIR / "deep"
TARGET_MODEL_JSON = DATA_DIR / "target-model.json"

# On-demand single-ticker CLI analyses (dated markdown + provenance sidecars).
# Shared by the server and the analysis-jobs service.
ANALYSIS_DIR = RESEARCH_DIR / "analysis"

# Curated segment definitions (the membership lists) and the generated segment
# dossiers. Referenced by the server, the deep-run/peer-stats services, and the
# segment readers, so centralize them.
SEGMENT_DEF_DIR = DATA_DIR / "segments"
SEGMENT_OUT_DIR = RESEARCH_DIR / "segments"

# Two gitignored secrets files, split for historical reasons and kept as-is so
# existing setups don't break: FMP_API_KEY at the repo root, IBKR Flex/CPAPI
# credentials next to the tools.
ROOT_SECRETS = REPO_ROOT / "secrets.env"
TOOLS_SECRETS = TOOLS_DIR / "secrets.env"


def read_env_file(path: Path) -> dict[str, str]:
    """Minimal ``KEY=VALUE`` parser (no python-dotenv dependency).

    The one shared reader for every ``secrets.env``: a missing or unreadable
    file yields an empty dict rather than raising; ``#`` comments and blank
    lines are skipped, and surrounding quotes are stripped from values.
    """
    out: dict[str, str] = {}
    try:
        if not path.exists():
            return out
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        out[key.strip()] = val.strip().strip('"').strip("'")
    return out
