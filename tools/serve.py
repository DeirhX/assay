#!/usr/bin/env python3
"""Local, interactive research server for the rebalancing app.

Serves the single-page UI in ``web/`` and a small JSON API that runs the
on-demand deep-dive puller. Stdlib only -- no Flask, no FastAPI, no pip install,
no wheel roulette on bleeding-edge Python. Just::

    py -3 tools/serve.py
    # then open http://127.0.0.1:8765

Design notes / honest caveats:
* Binds to 127.0.0.1 only. This is a single-user local tool, not a web service.
  It happily runs code (network pulls) on request; do not expose it.
* Pulls are synchronous per request. A single ticker is a few seconds; a full
  segment is ~20 sequential pulls, so expect 30-60s. The server is threaded, so
  the UI stays responsive meanwhile.
* Secrets (FMP key) come from the environment / secrets.env, never from disk in
  this repo.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = REPO_ROOT / "web"
DATA_DIR = REPO_ROOT / "data"
RESEARCH_DIR = DATA_DIR / "research"
DEEP_DIR = RESEARCH_DIR / "deep"
ANALYSIS_DIR = RESEARCH_DIR / "analysis"  # on-demand single-ticker CLI analyses
SEGMENT_DEF_DIR = DATA_DIR / "segments"
SEGMENT_OUT_DIR = RESEARCH_DIR / "segments"
TARGET_MODEL_JSON = DATA_DIR / "target-model.json"
HOLDINGS_JSON = DATA_DIR / "current-holdings.json"
AUTH_STATE_FILE = DATA_DIR / "cache" / "pplx-auth.json"  # gitignored
ROOT_STATIC_SUFFIXES = {".html", ".css", ".js"}

sys.path.insert(0, str(Path(__file__).resolve().parent))
from portfolio import holdings_payload  # noqa: E402
from providers import yahoo  # noqa: E402
import research_pull  # noqa: E402
import review_deep_research  # noqa: E402
import ticker_analysis  # noqa: E402
import jobs  # noqa: E402
# Disk + identifier helpers and the job registry now live in their own modules;
# alias them so the rest of this file's call sites stay unchanged.
from store import (  # noqa: E402
    load as _load, write_json as _write_json, write_text as _write_text,
    slugify as _slugify, safe_symbol as _safe_symbol,
)
from jobs import (  # noqa: E402
    new_job as _new_job, update_job as _update_job, public as _job_public,
    claim_active as _claim_active, release_active as _release_active,
    any_active as _any_active_deep_job,
)

# Selectable chart windows -> (Yahoo range, interval). Longer windows step to a
# coarser interval so we don't ship thousands of daily points for a 10y view.
PRICE_HISTORY_RANGES: dict[str, tuple[str, str]] = {
    "1mo": ("1mo", "1d"),
    "3mo": ("3mo", "1d"),
    "6mo": ("6mo", "1d"),
    "1y": ("1y", "1d"),
    "2y": ("2y", "1wk"),
    "5y": ("5y", "1wk"),
    "max": ("max", "1mo"),
}

_PULL_LOCK = threading.Lock()  # serialize outbound pulls; be polite to sources

# The deep-research / login / analysis job registry lives in jobs.py; the single
# active-browser slot is jobs.claim_active / jobs.release_active.

# Dev live-reload. Off unless started with --reload. _BOOT_TOKEN is recomputed
# each time the process (re)starts, so the browser can tell an API restart apart
# from a no-op poll and reload itself even when no static asset changed.
_RELOAD = False
_BOOT_TOKEN = f"{time.time():.3f}"

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}


def _segment_path(name: str) -> Path:
    return SEGMENT_DEF_DIR / f"{_slugify(name)}.json"


def _validate_segment_definition(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("segment definition must be an object")
    title = str(raw.get("title") or "").strip()
    if not title:
        raise ValueError("segment title is required")
    members = raw.get("members")
    if not isinstance(members, list):
        raise ValueError("members must be a list")

    cleaned_members = []
    sleeves = set(str(s).strip() for s in raw.get("sleeves", []) if str(s).strip())
    for item in members:
        if not isinstance(item, dict):
            raise ValueError("each member must be an object")
        sym = _safe_symbol(str(item.get("symbol") or ""))
        sleeve = str(item.get("sleeve") or "other").strip().lower() or "other"
        sleeves.add(sleeve)
        cleaned = {
            "symbol": sym,
            "sleeve": sleeve,
        }
        for key in ("rationale", "confidence"):
            if item.get(key):
                cleaned[key] = str(item[key]).strip()
        cleaned_members.append(cleaned)

    cleaned = {
        "title": title,
        "kind": raw.get("kind") or "research",
        "status": raw.get("status") or "draft",
        "overlap_allowed": bool(raw.get("overlap_allowed", True)),
        "comment": str(raw.get("comment") or "").strip(),
        "sleeves": sorted(sleeves) or ["other"],
        "members": cleaned_members,
    }
    for key in ("origin", "description", "inclusion_criteria", "exclusion_criteria", "notes"):
        if key in raw:
            cleaned[key] = raw[key]
    return cleaned


_DRAFT_UNIVERSE = [
    ("NVDA", "compute", "Dominant AI accelerator and networking platform.", ["ai", "infrastructure", "semiconductor", "compute", "gpu"]),
    ("AMD", "compute", "AI GPU, EPYC CPU, and accelerator challenger.", ["ai", "infrastructure", "semiconductor", "compute", "gpu"]),
    ("ARM", "ip", "CPU IP and AI/data-center architecture exposure.", ["ai", "infrastructure", "semiconductor", "compute", "ip"]),
    ("TSM", "foundry", "Leading advanced-node foundry.", ["ai", "infrastructure", "semiconductor", "foundry"]),
    ("ASML", "equipment", "Lithography monopoly for advanced semis.", ["ai", "infrastructure", "semiconductor", "equipment"]),
    ("AMAT", "equipment", "Wafer-fab equipment exposure.", ["semiconductor", "equipment"]),
    ("LRCX", "equipment", "Etch/deposition wafer-fab equipment.", ["semiconductor", "equipment"]),
    ("KLAC", "equipment", "Process control and inspection equipment.", ["semiconductor", "equipment"]),
    ("TXN", "analog", "Analog and embedded semiconductor cycle exposure.", ["semiconductor", "analog", "industrial"]),
    ("ADI", "analog", "Analog and mixed-signal semiconductor exposure.", ["semiconductor", "analog", "industrial"]),
    ("MU", "memory", "Memory cycle and HBM/DRAM exposure.", ["semiconductor", "memory", "ai"]),
    ("SOXX", "etf", "Broad semiconductor ETF exposure.", ["semiconductor", "etf"]),
    ("XSD", "etf", "Equal-weight semiconductor ETF exposure.", ["semiconductor", "etf"]),
    ("PYPL", "payments", "Global checkout, wallet, Venmo, and merchant payments.", ["fintech", "payments", "value", "turnaround"]),
    ("SOFI", "digital-bank", "US digital bank, lending, and fintech infrastructure.", ["fintech", "bank", "credit", "growth"]),
    ("NU", "latam-fintech", "Scaled LatAm digital bank.", ["fintech", "bank", "latam", "growth"]),
    ("EEFT", "money-transfer", "ATM, prepaid/epay, and money-transfer network.", ["fintech", "payments", "money", "value", "turnaround"]),
]


def _draft_segment(query: str) -> dict:
    query = (query or "").strip()
    if not query:
        raise ValueError("query is required")
    slug = _slugify(query)
    tokens = set(re.findall(r"[a-z0-9]+", query.lower()))
    members = []
    for symbol, sleeve, rationale, keys in _DRAFT_UNIVERSE:
        score = len(tokens.intersection(keys))
        if score:
            members.append({
                "symbol": symbol,
                "sleeve": sleeve,
                "rationale": rationale,
                "confidence": "high" if score >= 2 else "medium",
            })
    llm_prompt = (
        f"Create a public-equity research segment for '{query}'. Return JSON with "
        "title, comment, sleeves, and members. Each member must include symbol, "
        "sleeve, rationale, and confidence. Exclude private companies unless you "
        "list them only in notes with public proxies."
    )
    return {
        "slug": slug,
        "definition": {
            "title": query.title(),
            "kind": "research",
            "status": "draft",
            "overlap_allowed": True,
            "comment": f"Draft research lens generated from freeform query: {query}",
            "sleeves": sorted({m["sleeve"] for m in members}) or ["other"],
            "members": members,
            "origin": {
                "type": "website_draft",
                "query": query,
            },
        },
        "llm_prompt": llm_prompt,
        "warnings": [] if members else [
            "No local candidates matched. Use the prompt with an LLM, paste/edit members, then validate before approving."
        ],
    }


def _segment_prompt(name: str) -> dict:
    slug = _slugify(name)
    definition = _load(SEGMENT_DEF_DIR / f"{slug}.json")
    if not definition:
        raise ValueError(f"unknown segment {slug}")
    holdings = holdings_payload()
    held = {
        p["symbol"]: p.get("percent_of_nav")
        for p in holdings.get("positions", [])
        if p.get("percent_of_nav") is not None
    }
    symbols = [m["symbol"] for m in definition.get("members", [])]
    held_lines = [
        f"- {sym}: {held[sym]:.2f}% NAV"
        for sym in symbols
        if sym in held
    ]
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    prompt = (
        f"Deep research on the {definition.get('title', slug)} segment as a long-term "
        f"investment, as of {today}.\n"
        f"Known names to start from: {', '.join(symbols)}.\n"
        "Report on the ENTIRE segment, not just these names: explicitly identify and "
        "evaluate other major public companies, leaders, and emerging challengers in "
        "this segment that are not listed above, and flag any listed name that no "
        "longer belongs.\n"
        "Compare business momentum, valuation, competitive positioning, catalysts, "
        "risks, and likely recovery/underperformance over the next 6-24 months.\n"
        "Rank the most attractive long-term opportunities in the segment on their own "
        "merits, independent of what I currently own.\n"
        "Then tie conclusions to portfolio action: keep, trim, sell, add, start, or wait.\n"
        "Include source citations and distinguish facts from opinion.\n"
        "Call out which numeric claims need deterministic verification.\n"
        "On first mention of any public company, append its primary exchange "
        "ticker with a $ prefix, e.g. 'ServiceNow ($NOW)'. Include a comparison "
        "table with a 'Ticker' column covering every company you discuss.\n"
        "Treat the tickers above as individual stocks and the complete scope; "
        "do not ask clarifying questions. If anything is ambiguous, state "
        "assumptions and proceed.\n"
    )
    if held_lines:
        prompt += (
            "\nFor context only (do not bias coverage toward these), I currently own:\n"
            + "\n".join(held_lines) + "\n"
        )
    return {"segment": slug, "date": today, "prompt": prompt}


def _deep_runs() -> list[dict]:
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
    title = seg_def.get("title") or segment.replace("-", " ").title()
    sources = _load(DEEP_DIR / f"{stem}.sources.json") or {}
    proposal = _load(DEEP_DIR / f"{stem}.target-proposal.json") or {}
    rec.update({
        "segment": segment,
        "date": date,
        "title": title,
        "source_count": len(sources.get("citations") or []),
        "source_url": sources.get("source_url") or "",
        "generated_at": sources.get("extracted_at") or "",
        "has_review": "review" in rec["files"],
        "has_proposal": "proposal" in rec["files"],
        "change_count": len(proposal.get("changes") or []),
        "blocked_symbols": proposal.get("blocked_symbols") or [],
    })


def _save_deep_artifact(body: dict) -> dict:
    segment = _slugify(str(body.get("segment") or ""))
    date = str(body.get("date") or dt.datetime.now(dt.timezone.utc).date().isoformat())
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise ValueError("date must be YYYY-MM-DD")
    stem = f"{segment}-{date}"
    report = str(body.get("report") or "").strip()
    if not report:
        raise ValueError("report text is required")
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


# --------------------------------------------------------------------------- #
# On-demand single-ticker analysis (cheap CLI tier: Claude -> Cursor fallback)
# --------------------------------------------------------------------------- #
def _save_analysis_artifact(symbol: str, report: str, meta: dict) -> dict:
    """Persist a CLI analysis as dated markdown + a sidecar of provenance, so it
    survives restarts and can be shown next to the dossier."""
    sym = _safe_symbol(symbol)
    date = dt.datetime.now(dt.timezone.utc).date().isoformat()
    stem = f"{sym}-{date}"
    info = {
        "symbol": sym,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "backend": meta.get("backend"),
        "backend_label": meta.get("backend_label"),
        "model": meta.get("model"),
        "attempts": meta.get("attempts") or [],
    }
    _write_text(ANALYSIS_DIR / f"{stem}.md", report.strip() + "\n")
    _write_json(ANALYSIS_DIR / f"{stem}.meta.json", info)
    return {"stem": stem, "meta": info}


def _latest_analysis(symbol: str) -> dict | None:
    """Most recent saved analysis for a symbol (markdown + provenance), or None."""
    sym = _safe_symbol(symbol)
    md = sorted(ANALYSIS_DIR.glob(f"{sym}-*.md"), reverse=True)
    if not md:
        return None
    path = md[0]
    meta = _load(path.with_name(path.stem + ".meta.json")) or {}
    return {
        "symbol": sym,
        "stem": path.stem,
        "report": path.read_text(encoding="utf-8"),
        "meta": meta,
    }


def _run_analysis_job(job_id: str, symbol: str, refresh: bool) -> None:
    def progress(msg: str) -> None:
        _update_job(job_id, message=msg)

    try:
        sym = _safe_symbol(symbol)
        rec = _load(RESEARCH_DIR / f"{sym}.json")
        if rec is None or refresh:
            progress("pulling fresh deterministic data…")
            with _PULL_LOCK:
                rec = research_pull.pull_ticker(sym)
        _update_job(job_id, state="running", message="analysing…")
        result = ticker_analysis.analyze(rec, progress=progress)
    except Exception as exc:  # noqa: BLE001
        _update_job(job_id, state="error", error=f"{type(exc).__name__}: {exc}")
        return

    if not result.get("ok"):
        _update_job(job_id, state="error",
                    error=result.get("error") or "all analysis backends failed")
        return
    try:
        saved = _save_analysis_artifact(sym, result["report"], result)
    except Exception as exc:  # noqa: BLE001
        _update_job(job_id, state="error", error=f"analysis produced but not saved: {type(exc).__name__}: {exc}")
        return
    _update_job(job_id, state="done", message=f"analysed via {result.get('backend_label')}",
                result={
                    "symbol": sym,
                    "backend": result.get("backend"),
                    "backend_label": result.get("backend_label"),
                    "model": result.get("model"),
                    "report_chars": len(result["report"]),
                },
                artifact=saved)


# At most one analysis per symbol in flight; the CLIs are cheap but not free.
def _analysis_running(symbol: str) -> bool:
    return jobs.find(
        lambda j: j.get("kind") == "ticker_analysis"
        and j.get("symbol") == symbol
        and j.get("state") in ("queued", "running")
    )


def _start_analysis(symbol: str, refresh: bool) -> dict:
    sym = _safe_symbol(symbol)
    if _analysis_running(sym):
        raise RuntimeError(f"an analysis for {sym} is already running")
    # Unlike Perplexity runs, CLI analyses don't touch the shared browser, so we
    # do NOT take _claim_active -- they may run alongside a deep-research job.
    job = _new_job("ticker_analysis", symbol=sym, refresh=bool(refresh))
    threading.Thread(target=_run_analysis_job,
                     args=(job["id"], sym, bool(refresh)), daemon=True).start()
    return _job_public(job)


# --------------------------------------------------------------------------- #
# Deep-dive Q&A: archived, continuable follow-up threads per ticker. Stored next
# to the analyses (gitignored cache) so they survive restarts and can be resumed.
# --------------------------------------------------------------------------- #
def _qa_path(symbol: str) -> Path:
    return ANALYSIS_DIR / f"{_safe_symbol(symbol)}.qa.json"


def _load_qa(symbol: str) -> dict:
    sym = _safe_symbol(symbol)
    data = _load(_qa_path(sym))
    if not isinstance(data, dict):
        return {"symbol": sym, "turns": []}
    data.setdefault("symbol", sym)
    if not isinstance(data.get("turns"), list):
        data["turns"] = []
    return data


def _run_qa_job(job_id: str, symbol: str, question: str) -> None:
    def progress(msg: str) -> None:
        _update_job(job_id, message=msg)

    try:
        sym = _safe_symbol(symbol)
        rec = _load(RESEARCH_DIR / f"{sym}.json")
        if rec is None:
            progress("pulling deterministic data…")
            with _PULL_LOCK:
                rec = research_pull.pull_ticker(sym)
        thread = _load_qa(sym)
        latest = _latest_analysis(sym)
        note = latest.get("report") if latest else None
        _update_job(job_id, state="running", message="thinking…")
        result = ticker_analysis.ask(rec, thread.get("turns") or [], question,
                                     note=note, session=thread.get("session"), progress=progress)
    except Exception as exc:  # noqa: BLE001
        _update_job(job_id, state="error", error=f"{type(exc).__name__}: {exc}")
        return

    if not result.get("ok"):
        _update_job(job_id, state="error", error=result.get("error") or "all Q&A backends failed")
        return

    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    thread.setdefault("created_at", now)
    thread["updated_at"] = now
    # Track the resumable session at thread level. A non-session provider (Cursor)
    # returns no session, so this resets to None -- the next Claude turn then
    # opens a fresh session seeded with the full history, keeping context correct.
    thread["session"] = result.get("session")
    thread["turns"].append({"role": "user", "text": question, "ts": now})
    thread["turns"].append({
        "role": "assistant", "text": result["report"], "ts": now,
        "backend": result.get("backend"), "backend_label": result.get("backend_label"),
        "model": result.get("model"), "usage": result.get("usage") or {},
    })
    try:
        _write_json(_qa_path(sym), thread)
    except Exception as exc:  # noqa: BLE001
        _update_job(job_id, state="error", error=f"answer produced but not saved: {type(exc).__name__}: {exc}")
        return
    _update_job(job_id, state="done", message=f"answered via {result.get('backend_label')}",
                result={"symbol": sym, "turns": len(thread["turns"])})


def _qa_running(symbol: str) -> bool:
    return jobs.find(
        lambda j: j.get("kind") == "ticker_qa"
        and j.get("symbol") == symbol
        and j.get("state") in ("queued", "running")
    )


def _start_qa(symbol: str, question: str) -> dict:
    sym = _safe_symbol(symbol)
    question = (question or "").strip()
    if not question:
        raise ValueError("empty question")
    if _qa_running(sym):
        raise RuntimeError(f"a question for {sym} is already being answered")
    job = _new_job("ticker_qa", symbol=sym)
    threading.Thread(target=_run_qa_job, args=(job["id"], sym, question), daemon=True).start()
    return _job_public(job)


def _apply_target_proposal(segment: str, date: str, confirm: bool, *, allow_blocked: bool = False) -> dict:
    if not confirm:
        raise ValueError("confirm=true is required")
    segment = _slugify(segment)
    proposal_path = DEEP_DIR / f"{segment}-{date}.target-proposal.json"
    proposal = _load(proposal_path)
    if not proposal:
        raise ValueError(f"proposal not found: {proposal_path.relative_to(REPO_ROOT)}")
    model = _load(TARGET_MODEL_JSON)
    if not model:
        raise ValueError("target model not found")
    targets = model.setdefault("targets", {})
    # Never derive a target band from a ticker whose deterministic data failed an
    # ERROR-level check -- the review gate marks those as blocked. Override only on
    # an explicit allow_blocked, after the data has actually been fixed.
    blocked = set(proposal.get("blocked_symbols", [])) if not allow_blocked else set()
    applied = []
    skipped = []
    for change in proposal.get("changes", []):
        sym = _safe_symbol(change.get("symbol", ""))
        if change.get("action") != "add_target":
            skipped.append({"symbol": sym, "reason": "unsupported action"})
            continue
        if sym in blocked:
            skipped.append({"symbol": sym, "reason": "blocked: ERROR-level deterministic data; resolve before applying"})
            continue
        if sym in targets:
            skipped.append({"symbol": sym, "reason": "target already exists"})
            continue
        target = dict(change.get("proposed_target") or {})
        if not target:
            skipped.append({"symbol": sym, "reason": "missing proposed_target"})
            continue
        targets[sym] = target
        applied.append(sym)
    proposal["status"] = "applied" if applied else "reviewed"
    proposal["applied_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    proposal["applied_symbols"] = applied
    proposal["skipped"] = skipped
    _write_json(TARGET_MODEL_JSON, model)
    _write_json(proposal_path, proposal)
    return {"applied": applied, "skipped": skipped, "proposal": proposal}


# Root-level static pages that are the SPA shell or the topbar landing, not
# stand-alone analyses -- excluded from the Analyses tab's "Written reports".
STATIC_REPORT_EXCLUDE = {"index.html", "next-steps.html"}


def _static_reports() -> list[dict]:
    """The hand-authored mini-site report pages (ticker deep-dives + thematic
    reviews) served from the repo root. Discovered from disk so new ones show up
    in the Analyses tab without touching the top menu."""
    reports = []
    for path in sorted(REPO_ROOT.glob("*.html")):
        if path.name in STATIC_REPORT_EXCLUDE:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        m = re.search(r"<title>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
        stem = path.stem
        symbol = stem[:-len("-detail")].upper() if stem.endswith("-detail") else None
        reports.append({
            "href": "/" + path.name,
            "file": path.name,
            "title": (m.group(1).strip() if m else path.name),
            "symbol": symbol,
            "kind": "ticker" if symbol else "thematic",
            "modified": dt.datetime.fromtimestamp(
                path.stat().st_mtime, dt.timezone.utc).isoformat(timespec="seconds"),
        })
    reports.sort(key=lambda r: (r["kind"] != "ticker", r["title"].lower()))
    return reports


_TICKER_SHAPE = re.compile(r"^[A-Z][A-Z0-9.]{0,5}$")


def _known_tickers() -> list[str]:
    """Curated universe of symbols we actually know about: pulled research
    dossiers, held positions, and segment members. The UI uses this to decide
    which bare uppercase tokens in a report are safe to turn into deep-dive
    links -- a small, relevant set beats the full US/EU universe, which collides
    badly with English words (NOW, ON, ALL, IT...)."""
    syms: set[str] = set()

    def add(value) -> None:
        if isinstance(value, dict):
            value = value.get("symbol")
        if not value:
            return
        s = str(value).strip().upper()
        if _TICKER_SHAPE.match(s):
            syms.add(s)

    for path in RESEARCH_DIR.glob("*.json"):
        add(path.stem)
    holdings = _load(HOLDINGS_JSON) or {}
    for pos in (holdings.get("positions") or []):
        add(pos)
    for src_dir in (SEGMENT_DEF_DIR, SEGMENT_OUT_DIR):
        for path in src_dir.glob("*.json"):
            data = _load(path) or {}
            for member in (data.get("members") or data.get("symbols") or []):
                add(member)
    return sorted(syms)


def _verdict_line(report: str) -> str | None:
    """The one-liner under an analysis '## Verdict' heading (stance + confidence
    + justification), stripped of markdown. Used as the recents-list summary."""
    if not report:
        return None
    lines = report.splitlines()
    for i, line in enumerate(lines):
        if re.match(r"^#{1,6}\s+verdict\b", line.strip(), re.I):
            buf: list[str] = []
            for nxt in lines[i + 1:]:
                s = nxt.strip()
                if not s:
                    if buf:
                        break
                    continue
                if re.match(r"^#{1,6}\s", s):
                    break
                buf.append(s)
            text = re.sub(r"\*\*?|`", "", " ".join(buf)).strip()
            return text or None
    return None


def _ticker_index() -> list[dict]:
    """Every ticker we have material on -- a pulled dossier and/or a saved CLI
    analysis -- with timestamps. The UI merges this with the browser's local
    view-history to offer a quick "jump back to a ticker" list. Server-side so a
    fresh browser still sees the tickers of interest, not an empty list."""
    out: dict[str, dict] = {}
    for path in RESEARCH_DIR.glob("*.json"):
        rec = _load(path) or {}
        sym = path.stem.upper()
        out[sym] = {
            "symbol": sym,
            "name": rec.get("name") or sym,
            "as_of": rec.get("as_of"),
            "analyzed_at": None,
            "has_analysis": False,
        }
    for path in ANALYSIS_DIR.glob("*.meta.json"):
        meta = _load(path) or {}
        sym = (meta.get("symbol") or "").upper()
        if not sym:
            continue
        row = out.setdefault(sym, {
            "symbol": sym, "name": sym, "as_of": None,
            "analyzed_at": None, "has_analysis": False,
        })
        ts = meta.get("generated_at")
        if ts and (not row["analyzed_at"] or ts > row["analyzed_at"]):
            row["analyzed_at"] = ts
        row["has_analysis"] = True
    # Attach the verdict one-liner from each analyzed ticker's latest note so the
    # recents list summarizes the call (Accumulate/Hold/Trim/Avoid + why).
    for row in out.values():
        if row.get("has_analysis"):
            latest = _latest_analysis(row["symbol"])
            if latest:
                vl = _verdict_line(latest.get("report") or "")
                if vl:
                    row["verdict"] = vl
    return sorted(out.values(), key=lambda r: r["symbol"])


def _sync_holdings() -> dict:
    """Re-pull the portfolio from the external read-only IBKR Flex reader and
    refresh data/current-holdings.json. The reader lives outside this repo (see
    the ibkr-holdings skill); its folder must be given via IBKR_PORTFOLIO_DIR.
    Read-only: the Flex query cannot trade. Returns the fresh holdings payload."""
    import subprocess

    ibkr_dir = os.environ.get("IBKR_PORTFOLIO_DIR", "").strip()
    if not ibkr_dir:
        raise ValueError("IBKR_PORTFOLIO_DIR is not set; point it at the IBKR Flex reader folder")
    base = Path(ibkr_dir)
    script = base / "ibkr_portfolio.py"
    if not script.exists():
        raise ValueError(f"ibkr_portfolio.py not found in {ibkr_dir}")

    # "py -3" mirrors the documented, known-good launcher on Windows; fall back to
    # this server's interpreter if the launcher is unavailable or off-Windows.
    launcher = ["py", "-3"] if sys.platform == "win32" else [sys.executable]
    tail = [str(script), "--json", "--out", "portfolio.json", "--snapshot-dir", "snapshots"]
    try:
        try:
            proc = subprocess.run(launcher + tail, cwd=str(base), capture_output=True, text=True, timeout=240)
        except FileNotFoundError:
            proc = subprocess.run([sys.executable] + tail, cwd=str(base), capture_output=True, text=True, timeout=240)
    except subprocess.TimeoutExpired:
        raise ValueError("IBKR reader timed out after 240s")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise ValueError("IBKR reader failed: " + (detail[-1] if detail else f"exit {proc.returncode}"))

    fresh = _load(base / "portfolio.json")
    if not isinstance(fresh, dict) or "positions" not in fresh or fresh.get("net_asset_value") is None:
        raise ValueError("IBKR reader produced no usable portfolio.json")

    current = _load(HOLDINGS_JSON) or {}
    _write_json(HOLDINGS_JSON, _merge_holdings_snapshot(current, fresh))
    return holdings_payload()


def _merge_holdings_snapshot(existing: dict, fresh: dict) -> dict:
    """Refresh the curated snapshot from a fresh pull WITHOUT widening its shape.
    Only top-level keys already present in the curated file are updated, and for
    list-of-dict sections each item is restricted to field names already seen in
    the curated file. This guarantees a refresh can never introduce new fields
    (e.g. account identifiers the sanitization deliberately omits). Keys the
    reader doesn't emit (privacy/source markers, sizing_legend) are preserved."""
    if not existing:  # nothing to model the shape on -> trust the pull as-is
        return fresh
    out = dict(existing)
    for key, cur_val in existing.items():
        if key not in fresh:
            continue
        new_val = fresh[key]
        if (isinstance(cur_val, list) and cur_val and isinstance(cur_val[0], dict)
                and isinstance(new_val, list)):
            allowed = set().union(*(set(d) for d in cur_val if isinstance(d, dict)))
            out[key] = [{k: v for k, v in item.items() if k in allowed}
                        for item in new_val if isinstance(item, dict)]
        else:
            out[key] = new_val
    return out


def _is_root_static_file(clean: str) -> bool:
    path = Path(clean)
    return (
        len(path.parts) == 1
        and path.suffix in ROOT_STATIC_SUFFIXES
        and (REPO_ROOT / clean).is_file()
    )


# ---- dev live-reload ------------------------------------------------------
def _assets_version() -> str:
    """Opaque token that changes whenever a served asset changes OR the server
    restarts. The browser reloads when this differs from what it last saw."""
    latest = 0.0
    for p in WEB_DIR.rglob("*"):
        if p.is_file():
            m = p.stat().st_mtime
            if m > latest:
                latest = m
    for p in REPO_ROOT.iterdir():  # root mini-site assets (site.css, *.html)
        if p.is_file() and p.suffix in ROOT_STATIC_SUFFIXES:
            m = p.stat().st_mtime
            if m > latest:
                latest = m
    return f"{latest:.3f}-{_BOOT_TOKEN}"


def _server_sources() -> list[Path]:
    """Python files whose edits warrant restarting the API process."""
    return sorted((REPO_ROOT / "tools").glob("*.py"))


def _reload_watcher() -> None:
    """Child-side watcher. When server code changes, exit with code 3 so the
    supervisor respawns a fresh process. Guards: never restart on code that fails
    to compile (keep serving the last good version), and never interrupt an
    in-flight Deep Research run (defer the exit until it ends)."""
    mtimes = {p: p.stat().st_mtime for p in _server_sources() if p.exists()}
    pending = False
    waited = False
    while True:
        time.sleep(1.0)
        for p in _server_sources():
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            if mtimes.get(p) == m:
                continue
            mtimes[p] = m
            try:
                compile(p.read_text(encoding="utf-8"), str(p), "exec")
            except SyntaxError as exc:
                sys.stderr.write(f"[reload] {p.name}: syntax error, staying on current code ({exc.msg} line {exc.lineno})\n")
                continue
            sys.stderr.write(f"[reload] {p.name} changed\n")
            pending = True
        if not pending:
            continue
        if _any_active_deep_job():
            if not waited:
                sys.stderr.write("[reload] change pending; holding restart until deep-research job(s) finish\n")
                waited = True
            continue
        sys.stderr.write("[reload] restarting to apply changes\n")
        sys.stderr.flush()
        os._exit(3)


def _run_reloader() -> int:
    """Supervisor (parent). Runs the server as a child and respawns it whenever
    the child exits with code 3 (a requested reload). Keeps a stable PID and the
    console, so Ctrl+C and stdout behave normally across reloads -- unlike execv,
    which on Windows detaches into a new, console-less process."""
    import subprocess

    child_env = dict(os.environ, _REBAL_RELOAD_CHILD="1")
    argv = [sys.executable, *sys.argv]
    print("[reload] supervisor watching tools/*.py — edits restart the API in place")
    while True:
        proc = subprocess.Popen(argv, env=child_env)
        try:
            code = proc.wait()
        except KeyboardInterrupt:
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
            return 0
        if code == 3:
            continue  # requested reload -> respawn with the new code
        return code  # clean exit or crash -> stop supervising


def _segments_list():
    out = []
    for path in sorted(SEGMENT_DEF_DIR.glob("*.json")):
        definition = _load(path) or {}
        # "cached" == a deterministic ticker-data pull exists for this segment.
        # Surface its as_of so the UI can show freshness instead of a bare flag.
        cache_path = SEGMENT_OUT_DIR / path.name
        cached_at = None
        if cache_path.exists():
            cached_at = (_load(cache_path) or {}).get("as_of")
        out.append({
            "name": path.stem,
            "title": definition.get("title", path.stem.title()),
            "kind": definition.get("kind", "research"),
            "status": definition.get("status", "approved"),
            "overlap_allowed": definition.get("overlap_allowed", True),
            "count": len(definition.get("members", [])),
            "cached": cache_path.exists(),
            "cached_at": cached_at,
        })
    return out


def _get_auth_state() -> dict:
    st = _load(AUTH_STATE_FILE) or {}
    return {
        "logged_in": bool(st.get("logged_in")),
        "updated_at": st.get("updated_at"),
        "note": st.get("note", ""),
    }


def _set_auth_state(logged_in: bool, note: str = "") -> None:
    _write_json(AUTH_STATE_FILE, {
        "logged_in": bool(logged_in),
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "note": note,
    })


def _verify_login() -> dict:
    """Synchronous, ~8s live probe that refreshes the cached login flag."""
    if not _claim_active():
        raise RuntimeError("a deep research / login job is already running")
    try:
        import pplx_deep_research as worker
        res = worker.check_login()
        if res.get("status") == "error":
            raise RuntimeError(res.get("detail") or "login check failed")
        _set_auth_state(res.get("status") == "logged_in", "active check")
        return _get_auth_state()
    finally:
        _release_active()


def _clarify_answer_for(segment: str) -> str:
    """A concrete reply the worker can submit if Perplexity asks what is in the
    segment, so the run finishes unattended."""
    definition = _load(SEGMENT_DEF_DIR / f"{segment}.json") or {}
    syms = [m.get("symbol") for m in definition.get("members", []) if m.get("symbol")]
    if syms:
        return (
            "My segment is exactly these tickers, treated as individual stocks: "
            + ", ".join(syms)
            + ". Do not ask further clarifying questions; proceed with the full "
            "deep research now."
        )
    return (
        "Use exactly the tickers and scope in my original request. Do not ask "
        "further clarifying questions; proceed now."
    )


def _run_deep_job(job_id: str, segment: str, date: str, prompt: str, window_mode: str) -> None:
    def progress(msg: str) -> None:
        _update_job(job_id, message=msg)

    try:
        import pplx_deep_research as worker
    except Exception as exc:  # noqa: BLE001
        _update_job(job_id, state="error",
                    error=("Playwright not available: "
                           f"{type(exc).__name__}: {exc}. Install with "
                           "`py -3 -m pip install playwright` then "
                           "`py -3 -m playwright install chromium`."))
        _release_active()
        return

    _update_job(job_id, state="running", message="starting browser")
    try:
        res = worker.run_deep_research(
            prompt, window_mode=window_mode,
            clarify_answer=_clarify_answer_for(segment), progress=progress,
        )
    except Exception as exc:  # noqa: BLE001
        _update_job(job_id, state="error", error=f"{type(exc).__name__}: {exc}")
        _release_active()
        return

    status = res.get("status")
    if status == "done":
        try:
            artifact = _save_deep_artifact({
                "segment": segment,
                "date": date,
                "report": res.get("report", ""),
                "citations": res.get("citations", []),
                "source_url": res.get("source_url", ""),
            })
        except Exception as exc:  # noqa: BLE001
            _update_job(job_id, state="error", error=f"saved nothing: {type(exc).__name__}: {exc}")
            _release_active()
            return
        _set_auth_state(True, "deep run")
        _update_job(job_id, state="done", message="report saved",
                    result={
                        "source_url": res.get("source_url"),
                        "citations": res.get("citations", []),
                        "report_chars": len(res.get("report", "")),
                    },
                    artifact=artifact)
    elif status == "needs_login":
        _set_auth_state(False, "run hit login wall")
        _update_job(job_id, state="needs_login",
                    message="Not logged in. Use 'Set up Perplexity login' once, then re-run.")
    elif status == "needs_captcha":
        _update_job(job_id, state="error",
                    error=("A human-verification check (CAPTCHA) appeared and was not "
                           "solved in time. Re-run, and when the browser window pops to "
                           "the front, complete the check to continue."))
    elif status == "computer_trap":
        _update_job(job_id, state="error",
                    error=f"Hit the paid Computer path ({res.get('url')}); aborted to protect credits.")
    elif status == "needs_clarification":
        _update_job(job_id, state="error",
                    error=("Perplexity kept asking clarifying questions. Open "
                           f"{res.get('source_url')} , answer it there, then paste "
                           "the finished report on the Report step."))
    elif status == "timeout":
        url = res.get("source_url") or ""
        detail = "Deep Research timed out before a finished report could be confirmed."
        if url:
            detail += f" If the Perplexity page later finishes, import this URL: {url}"
        _update_job(job_id, state="error", error=detail)
    else:
        _update_job(job_id, state="error",
                    error=res.get("detail") or f"deep research {status}")
    _release_active()


def _run_login_job(job_id: str) -> None:
    def progress(msg: str) -> None:
        _update_job(job_id, message=msg)

    try:
        import pplx_deep_research as worker
    except Exception as exc:  # noqa: BLE001
        _update_job(job_id, state="error",
                    error=f"Playwright not available: {type(exc).__name__}: {exc}")
        _release_active()
        return

    _update_job(job_id, state="running", message="opening login window")
    try:
        res = worker.ensure_login(progress=progress)
    except Exception as exc:  # noqa: BLE001
        _update_job(job_id, state="error", error=f"{type(exc).__name__}: {exc}")
        _release_active()
        return
    if res.get("status") == "logged_in":
        _set_auth_state(True, "login window")
        _update_job(job_id, state="done", message="Perplexity login confirmed")
    else:
        _update_job(job_id, state="error", message="login window timed out",
                    error="login not completed in time")
    _release_active()


def _start_deep_research(body: dict) -> dict:
    segment = _slugify(str(body.get("segment") or ""))
    date = str(body.get("date") or dt.datetime.now(dt.timezone.utc).date().isoformat())
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise ValueError("date must be YYYY-MM-DD")
    prompt = str(body.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("prompt is required")
    window_mode = str(body.get("window_mode") or "offscreen")
    if window_mode not in ("offscreen", "visible", "headless"):
        raise ValueError("window_mode must be offscreen, visible, or headless")
    if not _claim_active():
        raise RuntimeError("a deep research / login job is already running")
    job = _new_job("deep_research", segment=segment, date=date, window_mode=window_mode)
    threading.Thread(target=_run_deep_job,
                     args=(job["id"], segment, date, prompt, window_mode),
                     daemon=True).start()
    return _job_public(job)


def _start_login() -> dict:
    if not _claim_active():
        raise RuntimeError("a deep research / login job is already running")
    job = _new_job("login")
    threading.Thread(target=_run_login_job, args=(job["id"],), daemon=True).start()
    return _job_public(job)


def _run_import_job(job_id: str, segment: str, date: str, url: str) -> None:
    def progress(msg: str) -> None:
        _update_job(job_id, message=msg)

    try:
        import pplx_deep_research as worker
    except Exception as exc:  # noqa: BLE001
        _update_job(job_id, state="error",
                    error=f"Playwright not available: {type(exc).__name__}: {exc}")
        _release_active()
        return

    _update_job(job_id, state="running", message="opening run URL")
    try:
        res = worker.fetch_by_url(url, progress=progress)
    except Exception as exc:  # noqa: BLE001
        _update_job(job_id, state="error", error=f"{type(exc).__name__}: {exc}")
        _release_active()
        return

    status = res.get("status")
    if status == "done":
        try:
            artifact = _save_deep_artifact({
                "segment": segment,
                "date": date,
                "report": res.get("report", ""),
                "citations": res.get("citations", []),
                "source_url": res.get("source_url", url),
            })
        except Exception as exc:  # noqa: BLE001
            _update_job(job_id, state="error", error=f"saved nothing: {type(exc).__name__}: {exc}")
            _release_active()
            return
        _set_auth_state(True, "import")
        _update_job(job_id, state="done", message="imported report saved",
                    result={
                        "source_url": res.get("source_url", url),
                        "citations": res.get("citations", []),
                        "report_chars": len(res.get("report", "")),
                    },
                    artifact=artifact)
    elif status == "needs_login":
        _set_auth_state(False, "import hit login wall")
        _update_job(job_id, state="needs_login",
                    message="Not logged in. Use 'Set up Perplexity login' once, then import.")
    elif status == "needs_captcha":
        _update_job(job_id, state="error",
                    error=("A human-verification check (CAPTCHA) appeared and was not "
                           "solved in time. Re-run the import and complete the check "
                           "when the browser window appears."))
    elif status == "needs_clarification":
        _update_job(job_id, state="error",
                    error="That run is still awaiting a clarifying answer. Answer it in "
                          "Perplexity, wait for it to finish, then import again.")
    else:
        _update_job(job_id, state="error", error=res.get("detail") or f"import {status}")
    _release_active()


def _start_import(body: dict) -> dict:
    segment = _slugify(str(body.get("segment") or ""))
    if not segment:
        raise ValueError("segment is required")
    date = str(body.get("date") or dt.datetime.now(dt.timezone.utc).date().isoformat())
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise ValueError("date must be YYYY-MM-DD")
    url = str(body.get("url") or "").strip()
    if "perplexity.ai" not in url:
        raise ValueError("a perplexity.ai run URL is required")
    if not _claim_active():
        raise RuntimeError("a deep research / login job is already running")
    job = _new_job("import", segment=segment, date=date)
    threading.Thread(target=_run_import_job,
                     args=(job["id"], segment, date, url),
                     daemon=True).start()
    return _job_public(job)


class Handler(BaseHTTPRequestHandler):
    server_version = "rebalancing-research/1.0"

    # ---- helpers -----------------------------------------------------------
    def _send_json(self, payload, status: int = 200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status: int, message: str):
        self._send_json({"error": message}, status=status)

    def _serve_static(self, rel: str):
        if rel in ("", "/"):
            rel = "index.html"
        clean = rel.lstrip("/")
        if clean.startswith("web/"):
            target = (WEB_DIR / clean.removeprefix("web/")).resolve()
            allowed_root = WEB_DIR
        elif _is_root_static_file(clean):
            target = (REPO_ROOT / clean).resolve()
            allowed_root = REPO_ROOT
        else:
            target = (WEB_DIR / clean).resolve()
            allowed_root = WEB_DIR
        if allowed_root not in target.parents and target != allowed_root:
            return self._send_error_json(403, "forbidden")
        if not target.is_file():
            return self._send_error_json(404, f"not found: {rel}")
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", _CONTENT_TYPES.get(target.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        # Localhost dev tool: never cache static assets, so an edit + refresh (or
        # the live-reload) always shows the latest code instead of a stale copy.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def log_message(self, fmt, *args):  # quieter, single-line logs
        sys.stderr.write(f"  {self.address_string()} {fmt % args}\n")

    # ---- routing -----------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return None
        if not path.startswith("/api/"):
            return self._serve_static(path)
        return self._handle_get_api(path, parse_qs(parsed.query))

    def _handle_get_api(self, path: str, query: dict[str, list[str]]):
        if path == "/api/dev/livereload":
            return self._send_json({"enabled": _RELOAD, "version": _assets_version()})
        if path == "/api/holdings":
            return self._send_json(holdings_payload())
        if path == "/api/segments":
            return self._send_json({"segments": _segments_list()})
        if path.startswith("/api/segment-def/"):
            name = path.rsplit("/", 1)[-1].lower()
            rec = _load(_segment_path(name))
            return self._send_json(rec) if rec else self._send_error_json(404, f"unknown segment {name}")
        if path == "/api/deep-runs":
            return self._send_json({"runs": _deep_runs()})
        if path == "/api/reports":
            return self._send_json({"reports": _static_reports()})
        if path == "/api/tickers":
            return self._send_json({"tickers": _known_tickers()})
        if path == "/api/ticker-index":
            return self._send_json({"tickers": _ticker_index()})
        if path == "/api/analysis-config":
            return self._send_json({
                "config": ticker_analysis.load_config(),
                "available": ticker_analysis.available_backends(),
                "labels": ticker_analysis.PROVIDER_LABELS,
            })
        if path == "/api/analysis-models":
            force = (query.get("refresh") or ["0"])[0] in ("1", "true")
            return self._send_json({"models": ticker_analysis.provider_models(force=force)})
        if path == "/api/deep-research/login-status":
            return self._send_json(_get_auth_state())
        if path == "/api/deep-job":
            job_id = (query.get("id") or [""])[0]
            pub = jobs.get_public(job_id)
            if not pub:
                return self._send_error_json(404, f"unknown job {job_id}")
            return self._send_json(pub)
        if path == "/api/deep-prompt":
            name = (query.get("segment") or [""])[0]
            try:
                return self._send_json(_segment_prompt(name))
            except ValueError as exc:
                return self._send_error_json(400, str(exc))
        if path.startswith("/api/deep-run/"):
            stem = _slugify(path.rsplit("/", 1)[-1])
            payload = {"stem": stem}
            for suffix, rel in {
                "report": DEEP_DIR / f"{stem}.md",
                "sources": DEEP_DIR / f"{stem}.sources.json",
                "review": DEEP_DIR / f"{stem}.review.md",
                "proposal": DEEP_DIR / f"{stem}.target-proposal.json",
            }.items():
                if rel.exists():
                    payload[suffix] = (
                        _load(rel) if rel.suffix == ".json" else rel.read_text(encoding="utf-8")
                    )
            return self._send_json(payload)
        if path == "/api/target-model":
            rec = _load(TARGET_MODEL_JSON)
            return self._send_json(rec) if rec else self._send_error_json(404, "target model not found")
        if path.startswith("/api/research/"):
            sym = path.rsplit("/", 1)[-1].upper()
            rec = _load(RESEARCH_DIR / f"{sym}.json")
            return self._send_json(rec) if rec else self._send_error_json(404, f"no cached research for {sym}")
        if path.startswith("/api/analysis/"):
            try:
                sym = _safe_symbol(path.rsplit("/", 1)[-1])
            except ValueError as exc:
                return self._send_error_json(400, str(exc))
            rec = _latest_analysis(sym)
            return self._send_json(rec) if rec else self._send_error_json(404, f"no analysis for {sym}")
        if path.startswith("/api/qa/"):
            try:
                sym = _safe_symbol(path.rsplit("/", 1)[-1])
            except ValueError as exc:
                return self._send_error_json(400, str(exc))
            return self._send_json(_load_qa(sym))
        if path.startswith("/api/history/"):
            sym = path.rsplit("/", 1)[-1].upper()
            return self._send_json({"symbol": sym, "history": research_pull.history_for(sym)})
        if path.startswith("/api/price-history/"):
            sym = path.rsplit("/", 1)[-1].upper()
            rng_key = (query.get("range") or ["1y"])[0].lower()
            rng, interval = PRICE_HISTORY_RANGES.get(rng_key, PRICE_HISTORY_RANGES["1y"])
            try:
                result = yahoo.chart(sym, rng=rng, interval=interval)
                ph = yahoo.price_history_from_chart(result, rng=rng, interval=interval)
            except Exception as exc:  # noqa: BLE001 - surface provider failure to UI
                return self._send_error_json(502, f"price history failed for {sym}: {exc}")
            if not ph:
                return self._send_error_json(404, f"no price history for {sym}")
            return self._send_json(ph)
        if path.startswith("/api/segment/"):
            name = path.rsplit("/", 1)[-1].lower()
            rec = _load(SEGMENT_OUT_DIR / f"{name}.json")
            return self._send_json(rec) if rec else self._send_error_json(404, f"no cached segment {name}")
        return self._send_error_json(404, "unknown endpoint")

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            return self._handle_post_api(path)
        except research_pull.ProviderError as exc:  # type: ignore[attr-defined]
            return self._send_error_json(502, f"data source error: {exc}")
        except Exception as exc:  # noqa: BLE001
            return self._send_error_json(500, f"{type(exc).__name__}: {exc}")

    def _handle_post_api(self, path: str):
        if path == "/api/segment-draft":
            body = self._read_body()
            return self._send_json(_draft_segment(str(body.get("query") or "")))

        if path.startswith("/api/segment-def/"):
            name = _slugify(path.rsplit("/", 1)[-1])
            body = self._read_body()
            definition = _validate_segment_definition(body.get("definition") or body)
            _write_json(SEGMENT_DEF_DIR / f"{name}.json", definition)
            return self._send_json({"name": name, "definition": definition, "segments": _segments_list()})

        if path == "/api/holdings/sync":
            try:
                return self._send_json(_sync_holdings())
            except ValueError as exc:
                return self._send_error_json(400, str(exc))

        if path.startswith("/api/analyze/"):
            try:
                sym = _safe_symbol(path.rsplit("/", 1)[-1])
            except ValueError as exc:
                return self._send_error_json(400, str(exc))
            body = self._read_body()
            try:
                return self._send_json(_start_analysis(sym, bool(body.get("refresh"))))
            except RuntimeError as exc:
                return self._send_error_json(409, str(exc))

        if path.startswith("/api/qa/"):
            try:
                sym = _safe_symbol(path.rsplit("/", 1)[-1])
            except ValueError as exc:
                return self._send_error_json(400, str(exc))
            body = self._read_body()
            if body.get("clear"):
                _write_json(_qa_path(sym), {"symbol": sym, "turns": []})
                return self._send_json(_load_qa(sym))
            try:
                return self._send_json(_start_qa(sym, str(body.get("question") or "")))
            except ValueError as exc:
                return self._send_error_json(400, str(exc))
            except RuntimeError as exc:
                return self._send_error_json(409, str(exc))

        if path == "/api/analysis-config":
            body = self._read_body()
            return self._send_json({
                "config": ticker_analysis.save_config(body.get("config") or body),
                "available": ticker_analysis.available_backends(),
                "labels": ticker_analysis.PROVIDER_LABELS,
            })

        if path == "/api/deep-research/save":
            body = self._read_body()
            return self._send_json(_save_deep_artifact(body))

        if path == "/api/deep-research/run":
            body = self._read_body()
            try:
                return self._send_json(_start_deep_research(body))
            except RuntimeError as exc:
                return self._send_error_json(409, str(exc))

        if path == "/api/deep-research/login":
            try:
                return self._send_json(_start_login())
            except RuntimeError as exc:
                return self._send_error_json(409, str(exc))

        if path == "/api/deep-research/import":
            body = self._read_body()
            try:
                return self._send_json(_start_import(body))
            except ValueError as exc:
                return self._send_error_json(400, str(exc))
            except RuntimeError as exc:
                return self._send_error_json(409, str(exc))

        if path == "/api/deep-research/verify-login":
            try:
                return self._send_json(_verify_login())
            except RuntimeError as exc:
                return self._send_error_json(409, str(exc))

        if path == "/api/deep-research/review":
            body = self._read_body()
            segment = str(body.get("segment") or "")
            date = str(body.get("date") or "")
            if not segment or not date:
                return self._send_error_json(400, "segment and date are required")
            # review() raises SystemExit (a BaseException) when the report or
            # segment definition is missing. That is NOT caught by do_POST's
            # `except Exception`, so it would kill the worker thread and return
            # nothing to the browser. Translate it into a clean 400 instead.
            try:
                return self._send_json(review_deep_research.review(segment, date))
            except SystemExit as exc:
                return self._send_error_json(400, str(exc) or "missing report for this segment + date")

        if path == "/api/target-proposal/apply":
            body = self._read_body()
            return self._send_json(_apply_target_proposal(
                str(body.get("segment") or ""),
                str(body.get("date") or ""),
                bool(body.get("confirm")),
                allow_blocked=bool(body.get("allow_blocked")),
            ))

        if path.startswith("/api/pull/"):
            sym = path.rsplit("/", 1)[-1].upper()
            if not sym.isascii() or not sym or len(sym) > 12:
                return self._send_error_json(400, "bad symbol")
            with _PULL_LOCK:
                rec = research_pull.pull_ticker(sym)
            return self._send_json(rec)

        if path.startswith("/api/pull-segment/"):
            name = path.rsplit("/", 1)[-1].lower()
            if not (SEGMENT_DEF_DIR / f"{name}.json").exists():
                return self._send_error_json(404, f"unknown segment {name}")
            with _PULL_LOCK:
                rec = research_pull.pull_segment(name)
            return self._send_json(rec)

        if path.startswith("/api/thesis/"):
            sym = path.rsplit("/", 1)[-1].upper()
            rec = _load(RESEARCH_DIR / f"{sym}.json")
            if not rec:
                return self._send_error_json(404, f"pull {sym} before saving a thesis")
            body = self._read_body()
            import datetime as dt
            rec["thesis"] = {
                "summary": body.get("summary", ""),
                "action": body.get("action", ""),
                "drivers": body.get("drivers", []),
                "downside_triggers": body.get("downside_triggers", []),
                "source_confidence": body.get("source_confidence", ""),
                "review_after": body.get("review_after", ""),
                "source_artifact": body.get("source_artifact", ""),
                "as_of": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            }
            (RESEARCH_DIR / f"{sym}.json").write_text(
                json.dumps(rec, indent=2) + "\n", encoding="utf-8"
            )
            return self._send_json(rec)

        return self._send_error_json(404, "unknown endpoint")


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--reload", action="store_true",
                        help="dev: auto-restart on tools/*.py edits and live-reload the browser on asset changes")
    args = parser.parse_args()

    # In --reload mode the first invocation is the supervisor; it re-launches
    # itself as a child (marked via env) that actually serves and self-restarts.
    if args.reload and os.environ.get("_REBAL_RELOAD_CHILD") != "1":
        return _run_reloader()

    global _RELOAD
    _RELOAD = args.reload

    _load_secrets_env()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print(f"Rebalancing research server on {url}  (Ctrl+C to stop)")
    print("  Static UI + JSON API. Localhost only. Pulls run live data sources.")
    if _RELOAD:
        print("  Dev reload ON: editing tools/*.py restarts the API; web/ + site.css edits reload the browser.")
        threading.Thread(target=_reload_watcher, daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        httpd.server_close()
    return 0


def _load_secrets_env():
    """Best-effort load of repo-root secrets.env (gitignored) for FMP_API_KEY."""
    env_path = REPO_ROOT / "secrets.env"
    if not env_path.exists():
        return
    import os
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


if __name__ == "__main__":
    raise SystemExit(main())
