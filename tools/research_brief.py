#!/usr/bin/env python3
"""Compact, per-name research briefs for the conviction step.

The strategy rebalancer's conviction LLM historically saw only the segment-level
Deep Research essay. The per-name research the pipeline already generated -- the
deterministic dossier numbers plus the per-ticker narrative reports (the cheap
CLI analyst note and the expensive Perplexity per-ticker Deep Research) -- never
reached it. This module turns that found research into a small, prompt-ready
brief per symbol so the conviction read is grounded in our own work, not just a
keyword scan of one essay.

Design constraints (deliberate):
* Deterministic facts come from the review row we already built -- no re-pull,
  and the bulky dossier fields (price_history, momentum, profile) are excluded
  by construction because the row never carried them.
* The narrative reports are bulky prose. We compress each ONCE via the cheap
  summarizer backend (a Haiku-class model) and cache the result keyed by the
  source content hash, so a re-run is free and an edited report re-summarizes.
* The summary is best-effort: a disabled summarizer, no backend, or a failed
  call all fall back to a deterministic lead+conclusion excerpt -- a reproducible
  floor that never blocks a construct run.
* A hard total character budget bounds the block so a big segment can't blow up
  the conviction prompt; names are filled most-material-first.
* The hand-authored ``thesis`` is intentionally NOT used here: it is usually
  empty, and the whole point is to lean on found research instead.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable

import ticker_analysis
from store import load as _load, write_json as _write_json
from config import DEEP_DIR, RESEARCH_DIR

ANALYSIS_DIR = RESEARCH_DIR / "analysis"
BRIEFS_DIR = RESEARCH_DIR / "briefs"

DEFAULT_BUDGET_CHARS = 6000
_PER_NAME_NARRATIVE_CHARS = 1400  # cap one name's summary/excerpt
_EXCERPT_HEAD = 1100
_EXCERPT_TAIL = 600
_SUMMARY_WORD_CAP = 120
_SUMMARY_INPUT_CAP = 14000  # bound the text we hand the summarizer

_DATE_RE = r"\d{4}-\d{2}-\d{2}"


# --------------------------------------------------------------------------- #
# Narrative lookup
# --------------------------------------------------------------------------- #
def _newest_match(directory: Path, pattern: re.Pattern[str]) -> Path | None:
    if not directory.exists():
        return None
    matches = [p for p in directory.iterdir() if p.is_file() and pattern.match(p.name)]
    if not matches:
        return None
    # Filenames embed an ISO date, so a lexical sort is chronological.
    return sorted(matches, key=lambda p: p.name, reverse=True)[0]


def latest_narrative(symbol: str) -> dict[str, Any] | None:
    """Newest per-ticker narrative for ``symbol``: the Perplexity per-ticker Deep
    Research if present (richest), else the cheap CLI analyst note.

    Handles the casing split on disk: deep run stems are slugified lowercase
    (``ticker-nvda-<date>.md``) while analysis stems keep the upper symbol
    (``NVDA-<date>.md``). The ``.md`` suffix also matches sidecars like
    ``*.review.md``, so we pin the exact ``<stem>-<ISO date>.md`` shape."""
    sym = (symbol or "").strip().upper()
    if not sym:
        return None
    esc = re.escape(sym)
    deep = _newest_match(DEEP_DIR, re.compile(rf"^ticker-{esc}-{_DATE_RE}\.md$", re.IGNORECASE))
    if deep:
        return {"kind": "deep", "path": deep, "text": deep.read_text(encoding="utf-8")}
    ana = _newest_match(ANALYSIS_DIR, re.compile(rf"^{esc}-{_DATE_RE}\.md$", re.IGNORECASE))
    if ana:
        return {"kind": "analysis", "path": ana, "text": ana.read_text(encoding="utf-8")}
    return None


# --------------------------------------------------------------------------- #
# Deterministic facts (from the review row -- never re-pulls)
# --------------------------------------------------------------------------- #
def _fmt_band(band: Any) -> str:
    if not isinstance(band, (list, tuple)) or len(band) != 2:
        return ""
    lo, hi = band
    if not isinstance(lo, (int, float)) or not isinstance(hi, (int, float)):
        return ""
    return f"[{lo:g}-{hi:g}]"


def _worst_check_message(checks: Any) -> str:
    """Message of the single worst (ERROR > WARN) cross-check, if any flagged."""
    if not isinstance(checks, list):
        return ""
    rank = {"ERROR": 0, "WARN": 1}
    flagged = [c for c in checks if isinstance(c, dict) and c.get("severity") in rank]
    if not flagged:
        return ""
    worst = min(flagged, key=lambda c: rank.get(c.get("severity"), 9))
    return str(worst.get("message") or "").strip()[:160]


def deterministic_facts(row: dict[str, Any]) -> str:
    """A compact, single-line digest of the numbers we already cross-checked for
    this name. Built from a review row (see ``review_deep_research.review``)."""
    bits: list[str] = []
    held = row.get("held_pct_nav")
    bits.append(f"held {held:.2f}% NAV" if isinstance(held, (int, float)) else "not held")
    rule = row.get("target_rule")
    if rule:
        bits.append(f"target {rule} {_fmt_band(row.get('target_band'))}".strip())
    val_parts = [f"{label} {row.get(key)}"
                 for label, key in (("mcap", "market_cap"), ("P/E", "pe_ttm"), ("P/S", "ps"))
                 if row.get(key) and str(row.get(key)) != "n/a"]
    if val_parts:
        bits.append(", ".join(val_parts))
    dq = row.get("data_quality")
    if dq and dq not in ("OK", ""):
        bits.append(f"data {dq}")
    action = row.get("report_action")
    if action and action not in ("not mentioned", ""):
        bits.append(f"segment report leans {action}")
    worst = _worst_check_message(row.get("cross_checks"))
    if worst:
        bits.append(f"flag: {worst}")
    return "; ".join(b for b in bits if b)


# --------------------------------------------------------------------------- #
# Narrative summarization (cheap LLM, cached) with deterministic fallback
# --------------------------------------------------------------------------- #
def _summarizer_cfg(cfg: dict[str, Any]) -> dict[str, Any] | None:
    """A cfg variant pinned to the cheap summarizer model, or None when the
    summarizer is disabled. Tools stay off (``allow_web`` False) -- a summary is
    a pure reduction of text we already have, never a fresh web pull."""
    summ = (cfg.get("summarizer") or {}) if isinstance(cfg, dict) else {}
    if not summ.get("enabled", True):
        return None
    variant = json.loads(json.dumps(cfg))  # deep copy; cfg is JSON-shaped
    variant["allow_web"] = False
    model = str(summ.get("model") or "").strip()
    if model:
        for provider in variant.get("providers") or []:
            provider["model"] = model
    return variant


def _summarizer_model(cfg: dict[str, Any]) -> str:
    summ = (cfg.get("summarizer") or {}) if isinstance(cfg, dict) else {}
    return str(summ.get("model") or "").strip()


def _cache_path(symbol: str) -> Path:
    return BRIEFS_DIR / f"{symbol.upper()}.brief.json"


def _content_key(text: str, model: str) -> str:
    h = hashlib.sha256()
    h.update(model.encode("utf-8"))
    h.update(b"\x00")
    h.update((text or "").encode("utf-8"))
    return h.hexdigest()


def _excerpt(text: str) -> str:
    """Deterministic lead + conclusion of a report -- the reproducible floor when
    the LLM summarizer is unavailable. Research reports tend to put the verdict at
    the end, so we keep both ends rather than a naive head truncation."""
    body = (text or "").strip()
    if len(body) <= _EXCERPT_HEAD + _EXCERPT_TAIL:
        return body[:_PER_NAME_NARRATIVE_CHARS]
    head = body[:_EXCERPT_HEAD].rsplit(" ", 1)[0]
    tail = body[-_EXCERPT_TAIL:].split(" ", 1)[-1]
    return f"{head}\n[...]\n{tail}"


def _clean_summary(text: str) -> str:
    s = (text or "").strip()
    s = re.sub(r"^```[a-zA-Z0-9]*\s*", "", s)
    s = re.sub(r"\s*```$", "", s).strip()
    return s[:_PER_NAME_NARRATIVE_CHARS]


def _summary_prompt(symbol: str, kind: str, text: str) -> str:
    body = text.strip()
    if len(body) > _SUMMARY_INPUT_CAP:
        body = body[:_SUMMARY_INPUT_CAP] + "\n…[truncated]"
    return f"""Distill the following {kind} research note on {symbol} into at most {_SUMMARY_WORD_CAP} words for a portfolio sizing decision. Capture only: the note's stance on OWNING {symbol} long-term (bullish / neutral / bearish), the 1-2 strongest supporting reasons, and the key risk or what would break the thesis. Plain prose, no preamble, no headers, no bullet characters. If the note is inconclusive, say so plainly.

NOTE:
{body}
"""


def summarize_narrative(symbol: str, text: str, cfg: dict[str, Any], *,
                        kind: str = "research",
                        progress: Callable[[str], None] | None = None,
                        cancel: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Compact summary of a narrative report. Cheap-model LLM where available,
    cached by content hash; deterministic excerpt as the reproducible fallback.

    Returns ``{"summary": str, "method": "cache"|"llm"|"excerpt"}``. Only genuine
    LLM summaries are cached -- an excerpt is cheap to recompute and we want to
    retry the model once a backend appears or the report changes."""
    text = text or ""
    summ_cfg = _summarizer_cfg(cfg)
    model = _summarizer_model(cfg) if summ_cfg else ""
    key = _content_key(text, model)

    cached = _load(_cache_path(symbol))
    if isinstance(cached, dict) and cached.get("key") == key and cached.get("summary"):
        return {"summary": str(cached["summary"]), "method": "cache"}

    if not summ_cfg or not any(ticker_analysis.available_backends().values()):
        return {"summary": _excerpt(text), "method": "excerpt"}

    if progress:
        progress(f"summarizing {symbol} research…")
    res = ticker_analysis._run_with_fallback(
        _summary_prompt(symbol, kind, text), summ_cfg, progress, cancel,
        label="research-summary")
    if not res.get("ok"):
        return {"summary": _excerpt(text), "method": "excerpt"}
    summary = _clean_summary(res.get("report") or "")
    if not summary:
        return {"summary": _excerpt(text), "method": "excerpt"}
    _write_json(_cache_path(symbol), {
        "symbol": symbol.upper(),
        "key": key,
        "model": model,
        "summarized_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "summary": summary,
    })
    return {"summary": summary, "method": "llm"}


# --------------------------------------------------------------------------- #
# Block assembly
# --------------------------------------------------------------------------- #
def build_research_block(rows: list[dict[str, Any]], cfg: dict[str, Any] | None = None, *,
                         use_llm: bool = True,
                         budget_chars: int | None = None,
                         progress: Callable[[str], None] | None = None,
                         cancel: Callable[[], bool] | None = None) -> str:
    """Assemble per-name briefs (facts + narrative summary) into one prompt block,
    most-material-first under a hard char budget. Returns ``""`` when there is
    nothing useful to add, so the conviction prompt stays identical to the legacy
    path for an empty segment."""
    cfg = cfg or ticker_analysis.load_config()
    if budget_chars is None:
        summ = cfg.get("summarizer") or {}
        budget_chars = int(summ.get("max_research_chars") or DEFAULT_BUDGET_CHARS)

    entries: list[dict[str, Any]] = []
    for row in rows or []:
        sym = str(row.get("symbol") or "").upper()
        if not sym:
            continue
        narrative = latest_narrative(sym)
        summary = ""
        kind = ""
        if narrative:
            kind = narrative["kind"]
            if use_llm:
                summary = summarize_narrative(sym, narrative["text"], cfg, kind=kind,
                                              progress=progress, cancel=cancel)["summary"]
            else:
                summary = _excerpt(narrative["text"])
        held = row.get("held_pct_nav")
        held = float(held) if isinstance(held, (int, float)) else 0.0
        entries.append({
            "sym": sym,
            "facts": deterministic_facts(row),
            "summary": summary,
            "kind": kind,
            # Material-first: names with a narrative lead, then by held weight.
            "order": (0 if narrative else 1, -held, sym),
        })

    entries.sort(key=lambda e: e["order"])

    lines: list[str] = []
    used = 0
    for e in entries:
        block = [f"- {e['sym']}: {e['facts']}" if e["facts"] else f"- {e['sym']}: (no local data)"]
        if e["summary"]:
            block.append(f"    research ({e['kind']}): {e['summary']}")
        chunk = "\n".join(block)
        if lines and used + len(chunk) > budget_chars:
            break  # budget spent on the most material names; stop cleanly
        lines.append(chunk)
        used += len(chunk) + 1

    if not lines:
        return ""
    header = ("PER-NAME RESEARCH (numbers we pulled + distilled prior research on "
              "each name; weigh alongside the segment report, and prefer name-specific "
              "evidence where they disagree):")
    return header + "\n" + "\n".join(lines)
