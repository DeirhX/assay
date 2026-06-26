#!/usr/bin/env python3
"""Deep Research run artifacts: list, enrich, save, and delete.

Extracted from serve.py. A "run" is a Deep Research answer saved under
data/research/deep/ as a `<segment>-<date>` stem plus sidecars (.sources.json,
.review.md, .target-proposal.json, .qa.json). This module manages those files:
discovering runs for the Analyses list, decorating each with human-facing
metadata, guarding saves against accidentally-stored JSON blobs, and deleting a
run with all of its sidecars. Pure disk work -- no HTTP or job machinery.
"""

from __future__ import annotations

import datetime as dt
import json
import re

import report_tickers
from config import DEEP_DIR, REPO_ROOT, SEGMENT_DEF_DIR
from store import (
    load as _load, slugify as _slugify,
    write_json as _write_json, write_text as _write_text,
)


def deep_runs() -> list[dict]:
    runs = {}
    for path in sorted(DEEP_DIR.glob("*")):
        if not path.is_file():
            continue
        name = path.name
        suffix = None
        stem = path.stem
        if name.endswith(".sources.json"):
            suffix = "sources"
            stem = name[:-len(".sources.json")]
        elif name.endswith(".target-proposal.json"):
            suffix = "proposal"
            stem = name[:-len(".target-proposal.json")]
        elif name.endswith(".review.md"):
            suffix = "review"
            stem = name[:-len(".review.md")]
        elif name.endswith(".md"):
            suffix = "report"
            stem = name[:-len(".md")]
        else:
            continue
        rec = runs.setdefault(stem, {"stem": stem, "files": {}})
        rec["files"][suffix] = str(path.relative_to(REPO_ROOT))
    for rec in runs.values():
        _enrich_deep_run(rec)
    return sorted(runs.values(), key=lambda r: r["stem"], reverse=True)


def _enrich_deep_run(rec: dict) -> None:
    """Attach human-facing metadata so a run can stand on its own in a list:
    a real title (from the segment definition), the run date, how many sources
    backed it, and whether a review / applicable proposal exists."""
    stem = rec["stem"]
    m = re.match(r"^(.*)-(\d{4}-\d{2}-\d{2})$", stem)
    segment = m.group(1) if m else stem
    date = m.group(2) if m else ""
    seg_def = _load(SEGMENT_DEF_DIR / f"{segment}.json") or {}
    # A single-name Deep Research run is namespaced `ticker-<sym>` and has no
    # segment definition; synthesise a human title and surface the symbol so the
    # ticker deep-dive can claim its own runs.
    is_ticker = not seg_def and segment.startswith("ticker-")
    symbol = segment[len("ticker-"):].upper() if is_ticker else (seg_def.get("symbol") or "")
    if seg_def.get("title"):
        title = seg_def["title"]
    elif is_ticker:
        title = f"{symbol} \u2014 deep research"
    else:
        title = segment.replace("-", " ").title()
    sources = _load(DEEP_DIR / f"{stem}.sources.json") or {}
    proposal = _load(DEEP_DIR / f"{stem}.target-proposal.json") or {}
    rec.update({
        "segment": segment,
        "date": date,
        "title": title,
        "symbol": symbol,
        "kind": "ticker" if is_ticker else "segment",
        "source_count": len(sources.get("citations") or []),
        "source_url": sources.get("source_url") or "",
        "generated_at": sources.get("extracted_at") or "",
        "has_review": "review" in rec["files"],
        "has_proposal": "proposal" in rec["files"],
        "change_count": len(proposal.get("changes") or []),
        "blocked_symbols": proposal.get("blocked_symbols") or [],
    })


def discovered_for(stem: str) -> list[dict]:
    """Names a segment report discusses *beyond* the segment's own member list —
    candidates you might want to pull into the optimizer pool. Reads the saved
    report and the segment definition; returns ``[]`` when the report is missing
    or it's a single-ticker run (nothing to "discover" for a one-name dive).

    Computed only on the single-run fetch (where the report is already read), so
    the Analyses list stays cheap."""
    report_path = DEEP_DIR / f"{stem}.md"
    if not report_path.exists():
        return []
    m = re.match(r"^(.*)-(\d{4}-\d{2}-\d{2})$", stem)
    segment = m.group(1) if m else stem
    if segment.startswith("ticker-"):
        return []
    seg_def = _load(SEGMENT_DEF_DIR / f"{segment}.json") or {}
    members = {
        str(mem.get("symbol", "")).upper()
        for mem in seg_def.get("members", [])
        if mem.get("symbol")
    }
    try:
        text = report_path.read_text(encoding="utf-8")
    except OSError:
        return []
    cands = report_tickers.discovered_candidates(text, exclude=members)
    for c in cands:
        c["segment"] = segment
        c["run"] = stem
    return cands


def _looks_like_json_doc(text: str) -> bool:
    """True if `text` is really a JSON object/array rather than a narrative.

    A Deep Research report is prose/markdown; it must never be a bare JSON
    document. A bad scrape or paste once captured a segment-universe JSON blob
    and we silently stored it as the `.md`, so the Analyses view rendered raw
    JSON. We gate on a leading '{'/'[' and an actual successful parse, so genuine
    markdown (which neither starts with a brace nor parses as JSON) is untouched.
    A single surrounding ```json fence is stripped before the check.
    """
    s = text.strip()
    fenced = re.match(r"^```[a-zA-Z0-9]*\s*\n(.*)\n```$", s, re.DOTALL)
    if fenced:
        s = fenced.group(1).strip()
    if not s or s[0] not in "{[":
        return False
    try:
        return isinstance(json.loads(s), (dict, list))
    except (ValueError, TypeError):
        return False


def save_deep_artifact(body: dict) -> dict:
    segment = _slugify(str(body.get("segment") or ""))
    date = str(body.get("date") or dt.datetime.now(dt.timezone.utc).date().isoformat())
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise ValueError("date must be YYYY-MM-DD")
    stem = f"{segment}-{date}"
    report = str(body.get("report") or "").strip()
    if not report:
        raise ValueError("report text is required")
    if _looks_like_json_doc(report):
        raise ValueError(
            "report looks like a JSON document, not a Deep Research narrative -- "
            "the scrape or paste captured structured data instead of the report "
            "body. Re-scrape the Perplexity answer (the prose/markdown), not a "
            "JSON blob, before saving."
        )
    citations = body.get("citations") or []
    if isinstance(citations, str):
        citations = json.loads(citations) if citations.strip() else []
    sources = {
        "schema_version": 1,
        "segment": segment,
        "source_url": body.get("source_url") or "",
        "mode": "perplexity_in_app_deep_research",
        "extracted_from": body.get("extracted_from") or "Perplexity Links tab",
        "extracted_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "citations": citations,
    }
    _write_text(DEEP_DIR / f"{stem}.md", report + "\n")
    _write_json(DEEP_DIR / f"{stem}.sources.json", sources)
    return {"stem": stem, "report": f"data/research/deep/{stem}.md", "sources": f"data/research/deep/{stem}.sources.json"}


# Every artifact a single Deep Research run can leave behind, keyed off its stem
# (segment-date). Deleting a run must clear all of them -- report, sidecars, and
# the follow-up Q&A archive -- so no orphaned half of a run lingers in the list.
_DEEP_RUN_SUFFIXES = (".md", ".sources.json", ".review.md", ".target-proposal.json", ".qa.json")


def delete_deep_run(stem: str) -> dict:
    """Remove a saved Deep Research run and all of its sidecar artifacts. Raises
    ValueError if the stem is empty or nothing on disk matches it."""
    if not (stem or "").strip():
        raise ValueError("stem is required")
    stem = _slugify(stem)
    removed = []
    for suffix in _DEEP_RUN_SUFFIXES:
        path = DEEP_DIR / f"{stem}{suffix}"
        if path.exists():
            path.unlink()
            removed.append(path.name)
    if not removed:
        raise ValueError(f"unknown run {stem}")
    return {"stem": stem, "removed": removed, "runs": deep_runs()}
