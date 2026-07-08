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

import os
from pathlib import Path

# tools/config.py -> tools/ -> repo root.
TOOLS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOLS_DIR.parent
DATA_DIR = REPO_ROOT / "data"
RESEARCH_DIR = DATA_DIR / "research"
WEB_DIR = REPO_ROOT / "web"
# Repo-root static assets the server will serve directly (the mini-site:
# privacy.html, site.css, privacy.js ...). Shared by the static-file router and
# the dev live-reload watcher, which fingerprints the same set.
ROOT_STATIC_SUFFIXES = {".html", ".css", ".js"}

# The curated, committed portfolio snapshot. Shared by the HTTP server and the
# holdings-sync service, so it lives on the leaf to avoid an import cycle.
HOLDINGS_JSON = DATA_DIR / "current-holdings.json"

# Display-symbol -> provider-symbol alias map. Read by portfolio's resolver and
# written by the symbols service; centralized here so it has one definition.
SYMBOL_ALIASES_JSON = DATA_DIR / "symbol-aliases.json"

# Deep-research artifacts and the committed target model. Shared by the server
# and the target-model service, so they live here for a single source of truth.
DEEP_DIR = RESEARCH_DIR / "deep"
TARGET_MODEL_JSON = DATA_DIR / "target-model.json"

# On-demand single-ticker CLI analyses (dated markdown + provenance sidecars).
# Shared by the server and the analysis-jobs service.
ANALYSIS_DIR = RESEARCH_DIR / "analysis"

# Cached Perplexity login flag (gitignored). Shared by the server's setup status
# and the browser-jobs service that refreshes it on every run.
AUTH_STATE_FILE = DATA_DIR / "cache" / "pplx-auth.json"

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


def config_value(key: str, default: str = "", *, secrets: Path | None = None) -> str:
    """Resolve a config flag: live env first, then the gitignored secrets file
    (``tools/secrets.env`` by default), then ``default`` -- always stripped.

    The one resolver shared by the IBKR integration and the background
    scheduler, so env-vs-file precedence is defined in exactly one place instead
    of each module re-rolling it.
    """
    val = os.environ.get(key)
    if val is None:
        val = read_env_file(secrets or TOOLS_SECRETS).get(key)
    return (val if val is not None else default).strip()


def flag_enabled(key: str, default: str = "0", *, secrets: Path | None = None) -> bool:
    """Truthy reading of a ``config_value`` flag (``1/true/yes/on``)."""
    return config_value(key, default, secrets=secrets).lower() in ("1", "true", "yes", "on")


def set_secret(key: str, value: str | None, *, secrets: Path | None = None) -> dict[str, str]:
    """Upsert one ``KEY=VALUE`` in a gitignored secrets file, preserving the
    others. An empty/None value removes the key. Rewrites the file from the
    parsed dict (same lossy shape ``save_ibkr_secrets`` already uses -- comments
    and quoting aren't round-tripped). Returns the resulting key/value map."""
    path = secrets or TOOLS_SECRETS
    existing = read_env_file(path)
    if value in (None, ""):
        existing.pop(key, None)
    else:
        existing[key] = str(value)
    lines = [
        "# Gitignored local config -- never commit.",
        "# Written by the Settings tab; also holds IBKR Flex/CPAPI + ASSAY_* flags.",
    ]
    lines += [f"{k}={v}" for k, v in existing.items() if v != ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return existing
