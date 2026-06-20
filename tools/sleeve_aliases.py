#!/usr/bin/env python3
"""Normalize the ad-hoc sleeve labels segment drafts invent onto the canonical
allocation-sleeve taxonomy used in target-model.json.

A segment Deep Research run tags each member with whatever sleeve name the LLM
felt like that day (``compute``, ``semicap-equipment``, ``custom-silicon-
networking``). The target model uses a stable, prefixed taxonomy
(``semis-compute``, ``semis-equipment``, ``semis-foundry``). Without a mapping,
an applied name either lands sleeve-less or pollutes the model with a one-off
label. This leaf reads ``data/sleeve-aliases.json`` and maps labels through it;
an unknown label is kept (lowercased) and reported so it can be added to the map
deliberately rather than silently.
"""

from __future__ import annotations

from config import DATA_DIR
from store import load as _load

# Optional override file (lives in the private data submodule); the canonical
# defaults below ship in the public repo so the mapping works without it.
SLEEVE_ALIASES_JSON = DATA_DIR / "sleeve-aliases.json"

# Canonical label -> sleeve-tag map. Kept in code (not just the data submodule)
# so CI and a fresh checkout normalize sleeves identically; the data file, when
# present, augments/overrides these.
_DEFAULT_ALIASES: dict[str, str] = {
    "compute": "semis-compute",
    "accelerators": "semis-compute",
    "ip": "semis-compute",
    "foundry": "semis-foundry",
    "equipment": "semis-equipment",
    "semicap-equipment": "semis-equipment",
    "litho": "semis-equipment-litho",
    "lithography": "semis-equipment-litho",
    "analog": "analog",
    "memory": "semis-memory",
    "materials": "semis-materials",
    "eda": "semis-eda",
    "eda-design": "semis-eda",
    "custom-silicon-networking": "semis-networking",
    "networking": "semis-networking",
    "etf": "semis-etf",
    "etfs": "semis-etf",
}


def load_aliases() -> dict[str, str]:
    """label -> canonical sleeve tag: built-in defaults, augmented/overridden by
    the optional ``data/sleeve-aliases.json``. Forgiving: a missing/corrupt file
    just leaves the defaults in place."""
    out = dict(_DEFAULT_ALIASES)
    data = _load(SLEEVE_ALIASES_JSON)
    aliases = (data or {}).get("aliases") if isinstance(data, dict) else None
    if isinstance(aliases, dict):
        for key, val in aliases.items():
            k = str(key or "").strip().lower()
            v = str(val or "").strip()
            if k and v:
                out[k] = v
    return out


def normalize(label: str, *, aliases: dict[str, str] | None = None) -> tuple[str, bool]:
    """Map *label* to its canonical sleeve tag.

    Returns ``(tag, known)``: ``known`` is True when the label was in the alias
    map. An empty/whitespace label returns ``("", True)`` -- nothing to map, not
    a gap. An unmapped non-empty label is returned lowercased with ``known``
    False so the caller can warn.
    """
    raw = str(label or "").strip().lower()
    if not raw:
        return "", True
    aliases = load_aliases() if aliases is None else aliases
    if raw in aliases:
        return aliases[raw], True
    return raw, False
