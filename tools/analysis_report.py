#!/usr/bin/env python3
"""Parsing the model's free-text output back into structured data.

Pure, side-effect-free extractors that read a backend's report and pull out the
machine-usable bits: the valuation-anchored price-level ladder, a tolerant JSON
object extractor for the JSON-shaped prompts, and the token/cache usage
normalizer. Nothing here runs a backend or touches the filesystem -- it is the
"what came back" half of ticker_analysis, kept separate from the "how to run
it" half so the parsing is trivially testable in isolation.

Extracted from ticker_analysis.py; the runner re-imports these so callers
reaching ticker_analysis.parse_price_levels / _extract_json_object are
unchanged.
"""

from __future__ import annotations

import json
import re
from typing import Any

import price_levels

_NONE_TOKENS = ("none", "n/a", "not ", "no level", "no trigger", "tbd", "null")


def _extract_level(report: str, label: str) -> float | None:
    """Pull a single price after *label* (a regex like ``buy\\s+below``) out of a
    free-text report. Tolerant of ``$``/currency symbols, thousands separators,
    and trailing parentheticals; returns None for an explicit "none"/"n/a" or
    when no positive number is present."""
    m = re.search(rf"{label}\b\s*[:=]?\s*(.+)", report, re.IGNORECASE)
    if not m:
        return None
    tail = m.group(1)
    num = re.search(r"[0-9][0-9,]*(?:\.[0-9]+)?", tail)
    head = (tail[: num.start()] if num else tail).lower()
    if any(tok in head for tok in _NONE_TOKENS):
        return None
    if not num:
        return None
    try:
        val = float(num.group(0).replace(",", ""))
    except ValueError:
        return None
    return val if val > 0 else None


def _extract_levels_block(report: str) -> dict | None:
    """Pull the fenced JSON ladder out of the '## Price levels' section. Bounded
    to that section (before the trailing DATA payload) so it can't accidentally
    grab the data snapshot's own ```json block. None if absent or unparseable."""
    start = re.search(r"##\s*Price\s+levels", report or "", re.IGNORECASE)
    if not start:
        return None
    region = report[start.end():]
    data_at = re.search(r"\n\s*DATA\b", region)
    if data_at:
        region = region[: data_at.start()]
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", region, re.DOTALL)
    if not fence:
        return None
    try:
        data = json.loads(fence.group(1))
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def parse_price_levels(report: str, currency: str = "") -> dict[str, Any]:
    """Extract the analysis's suggested valuation-anchored ladder from the
    '## Price levels' section. Prefers the fenced JSON block (fair_value +
    buy/trim ladders); falls back to the legacy two-line 'Buy below / Trim above'
    format (read as single-tranche ladders) so older reports still parse. Always
    returns the canonical normalized shape. Currency is the instrument's trading
    currency, carried through so levels and price comparisons stay in one unit."""
    block = _extract_levels_block(report or "")
    if block is not None:
        return price_levels.normalize_suggested(
            fair_value=block.get("fair_value"),
            buy_ladder=block.get("buy_ladder"),
            trim_ladder=block.get("trim_ladder"),
            currency=currency,
        )
    return price_levels.normalize_suggested(
        buy_below=_extract_level(report or "", r"buy\s+below"),
        trim_above=_extract_level(report or "", r"trim\s+above"),
        currency=currency,
    )


def _extract_json_object(text: str) -> dict | None:
    """Pull a JSON object out of an LLM response. Tolerates ```json fences and
    leading/trailing prose by trying a fenced block first, then the outermost
    balanced {...}. Returns None if nothing parses to a dict."""
    if not text:
        return None
    s = text.strip()
    candidates: list[str] = []
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", s, re.DOTALL)
    if fenced:
        candidates.append(fenced.group(1))
    start = s.find("{")
    if start != -1:
        depth = 0
        for i in range(start, len(s)):
            ch = s[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(s[start:i + 1])
                    break
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _norm_usage(u: dict | None) -> dict[str, int]:
    """Keep just the token counters worth showing (incl. prompt-cache read/write)."""
    if not isinstance(u, dict):
        return {}
    keys = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    return {k: int(u[k]) for k in keys if isinstance(u.get(k), (int, float))}
