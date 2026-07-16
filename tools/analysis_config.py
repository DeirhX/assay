#!/usr/bin/env python3
"""Config vocabulary and persistence for the on-demand analysis layer.

The backend policy the analysis stack runs on: which CLI providers exist, the
default fallback order, and the load/save of the user's on-disk override. Kept
separate from the detection ("which backends resolve") and run ("how to drive
them") halves so the schema/validation is trivially testable in isolation and
so ``analysis_backends`` can import it without a cycle back through the runner.

Extracted from ticker_analysis.py; the runner re-imports these so callers
reaching ticker_analysis.load_config / save_config / DEFAULT_CONFIG /
CONFIG_PATH / PROVIDER_LABELS are unchanged.
"""

from __future__ import annotations

import json
from typing import Any

from config import REPO_ROOT

# The canonical provider vocabulary: the only backend ids the stack accepts and
# their human labels. Config validation rejects anything not listed here, and
# both the detection and run layers key off it, so it is the natural root of the
# analysis dependency chain.
PROVIDER_LABELS = {"claude": "Claude CLI", "cursor": "Cursor CLI"}

CONFIG_PATH = REPO_ROOT / "data" / "analysis-config.json"

# Default backend policy. Order == fallback order. A user override file at
# CONFIG_PATH is shallow-merged over this (see load_config).
DEFAULT_CONFIG: dict[str, Any] = {
    "providers": [
        {"id": "claude", "enabled": True, "model": "", "extra_args": []},
        {"id": "cursor", "enabled": True, "model": "", "extra_args": []},
    ],
    "timeout_sec": 300,
    # When true, backends may use their web tools for fresher context. Off by
    # default: keeps runs fast, cheap, and grounded in our deterministic data.
    "allow_web": False,
    # Compresses bulky per-ticker research reports into a short brief for the
    # strategy conviction step (see research_brief). A cheap (Haiku-class) model
    # is pinned via `model` and applied as a --model override on the same CLI
    # backends; an empty model uses each backend's configured model. When the
    # call fails or no backend exists, a deterministic excerpt is used instead.
    "summarizer": {"enabled": True, "model": "haiku", "max_research_chars": 6000},
}


def _normalize_providers(raw: Any, *, strip_model: bool = False) -> list[dict[str, Any]] | None:
    if not isinstance(raw, list):
        return None
    cleaned: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        pid = item.get("id")
        if pid not in PROVIDER_LABELS or pid in seen:
            continue
        seen.add(pid)
        model = str(item.get("model") or "")
        if strip_model:
            model = model.strip()
        cleaned.append({
            "id": pid,
            "enabled": bool(item.get("enabled", True)),
            "model": model,
            "extra_args": [str(a) for a in (item.get("extra_args") or []) if str(a).strip()],
        })
    return cleaned or None


def _normalize_summarizer(raw: Any, base: dict[str, Any]) -> dict[str, Any]:
    """Merge a user-supplied summarizer block over the defaults, ignoring junk.
    Keeps the shape stable so callers can read keys without guarding each one."""
    out = dict(base)
    if isinstance(raw, dict):
        if "enabled" in raw:
            out["enabled"] = bool(raw.get("enabled"))
        if isinstance(raw.get("model"), str):
            out["model"] = raw["model"].strip()
        mc = raw.get("max_research_chars")
        if isinstance(mc, (int, float)) and mc > 0:
            out["max_research_chars"] = int(mc)
    return out


def load_config() -> dict[str, Any]:
    """Defaults merged with the on-disk override (if any). Always returns a
    well-formed config even if the file is missing or partially specified."""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return cfg
    providers = _normalize_providers(raw.get("providers"))
    if providers:
        cfg["providers"] = providers
    if isinstance(raw.get("timeout_sec"), (int, float)) and raw["timeout_sec"] > 0:
        cfg["timeout_sec"] = int(raw["timeout_sec"])
    cfg["allow_web"] = bool(raw.get("allow_web", cfg["allow_web"]))
    cfg["summarizer"] = _normalize_summarizer(raw.get("summarizer"), cfg["summarizer"])
    return cfg


def save_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """Validate and persist a config; returns the normalized, stored version."""
    merged = json.loads(json.dumps(DEFAULT_CONFIG))
    providers = _normalize_providers(cfg.get("providers"), strip_model=True)
    if providers:
        merged["providers"] = providers
    if isinstance(cfg.get("timeout_sec"), (int, float)) and cfg["timeout_sec"] > 0:
        merged["timeout_sec"] = int(cfg["timeout_sec"])
    merged["allow_web"] = bool(cfg.get("allow_web", merged["allow_web"]))
    merged["summarizer"] = _normalize_summarizer(cfg.get("summarizer"), merged["summarizer"])
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    return merged
