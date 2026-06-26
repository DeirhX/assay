#!/usr/bin/env python3
"""On-demand single-ticker analysis + the two Q&A thread subsystems.

Extracted from serve.py. Three cohesive job families that all run a CLI tier
(Claude -> Cursor fallback) in a background thread and persist their artifacts to
the gitignored research cache so they survive restarts:

* Single-ticker analysis -- pulls fresh deterministic data if needed, runs the
  analyst, and saves a dated markdown report plus a provenance sidecar (including
  any price levels parsed out of the report).
* Ticker Q&A -- archived, continuable follow-up threads per ticker, grounded in
  the latest saved analysis.
* Deep-research Q&A -- continuable follow-up threads about a saved Deep Research
  run, grounded in the report markdown and its citations.

Pure data + job machinery -- no HTTP. Public names are underscore-free; serve.py
imports them aliased to its existing private call-site names.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import jobs
import research_pull
import ticker_analysis
from apierror import Conflict
from config import ANALYSIS_DIR, DEEP_DIR, RESEARCH_DIR
from jobs import update_job
from research_pull import PULL_LOCK
from store import load, safe_symbol, slugify, write_json, write_text


# --------------------------------------------------------------------------- #
# On-demand single-ticker analysis (cheap CLI tier: Claude -> Cursor fallback)
# --------------------------------------------------------------------------- #
def save_analysis_artifact(symbol: str, report: str, meta: dict) -> dict:
    """Persist a CLI analysis as dated markdown + a sidecar of provenance, so it
    survives restarts and can be shown next to the dossier."""
    sym = safe_symbol(symbol)
    date = dt.datetime.now(dt.timezone.utc).date().isoformat()
    stem = f"{sym}-{date}"
    # Currency comes from the dossier so suggested levels and later price
    # comparisons all live in the instrument's own trading currency.
    dossier = load(RESEARCH_DIR / f"{sym}.json") or {}
    currency = str(dossier.get("currency") or "").upper()
    info = {
        "symbol": sym,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "backend": meta.get("backend"),
        "backend_label": meta.get("backend_label"),
        "model": meta.get("model"),
        "attempts": meta.get("attempts") or [],
        "currency": currency,
        "price_levels_suggested": ticker_analysis.parse_price_levels(report, currency),
    }
    write_text(ANALYSIS_DIR / f"{stem}.md", report.strip() + "\n")
    write_json(ANALYSIS_DIR / f"{stem}.meta.json", info)
    return {"stem": stem, "meta": info}


def latest_analysis(symbol: str) -> dict | None:
    """Most recent saved analysis for a symbol (markdown + provenance), or None."""
    sym = safe_symbol(symbol)
    md = sorted(ANALYSIS_DIR.glob(f"{sym}-*.md"), reverse=True)
    if not md:
        return None
    path = md[0]
    meta = load(path.with_name(path.stem + ".meta.json")) or {}
    return {
        "symbol": sym,
        "stem": path.stem,
        "report": path.read_text(encoding="utf-8"),
        "meta": meta,
    }


def run_analysis_job(job_id: str, symbol: str, refresh: bool) -> None:
    def progress(msg: str) -> None:
        update_job(job_id, message=msg)

    try:
        sym = safe_symbol(symbol)
        rec = load(RESEARCH_DIR / f"{sym}.json")
        if rec is None or refresh:
            progress("pulling fresh deterministic data…")
            with PULL_LOCK:
                rec = research_pull.pull_ticker(sym)
        update_job(job_id, state="running", message="analysing…")
        result = ticker_analysis.analyze(rec, progress=progress)
    except Exception as exc:  # noqa: BLE001
        update_job(job_id, state="error", error=f"{type(exc).__name__}: {exc}")
        return

    if not result.get("ok"):
        update_job(job_id, state="error",
                   error=result.get("error") or "all analysis backends failed")
        return
    try:
        saved = save_analysis_artifact(sym, result["report"], result)
    except Exception as exc:  # noqa: BLE001
        update_job(job_id, state="error", error=f"analysis produced but not saved: {type(exc).__name__}: {exc}")
        return
    update_job(job_id, state="done", message=f"analysed via {result.get('backend_label')}",
               result={
                   "symbol": sym,
                   "backend": result.get("backend"),
                   "backend_label": result.get("backend_label"),
                   "model": result.get("model"),
                   "report_chars": len(result["report"]),
               },
               artifact=saved)


# At most one analysis per symbol in flight; the CLIs are cheap but not free.
def analysis_running(symbol: str) -> bool:
    return jobs.running("ticker_analysis", symbol=symbol)


def start_analysis(symbol: str, refresh: bool) -> dict:
    sym = safe_symbol(symbol)
    if analysis_running(sym):
        raise Conflict(f"an analysis for {sym} is already running")
    # Unlike Perplexity runs, CLI analyses don't touch the shared browser, so we
    # do NOT take claim_active -- they may run alongside a deep-research job.
    return jobs.spawn("ticker_analysis", run_analysis_job, sym, bool(refresh),
                      symbol=sym, refresh=bool(refresh))


# --------------------------------------------------------------------------- #
# Deep-dive Q&A: archived, continuable follow-up threads per ticker. Stored next
# to the analyses (gitignored cache) so they survive restarts and can be resumed.
# --------------------------------------------------------------------------- #
def qa_path(symbol: str) -> Path:
    return ANALYSIS_DIR / f"{safe_symbol(symbol)}.qa.json"


def load_qa(symbol: str) -> dict:
    sym = safe_symbol(symbol)
    data = load(qa_path(sym))
    if not isinstance(data, dict):
        return {"symbol": sym, "turns": []}
    data.setdefault("symbol", sym)
    if not isinstance(data.get("turns"), list):
        data["turns"] = []
    return data


def run_qa_job(job_id: str, symbol: str, question: str) -> None:
    def progress(msg: str) -> None:
        update_job(job_id, message=msg)

    try:
        sym = safe_symbol(symbol)
        rec = load(RESEARCH_DIR / f"{sym}.json")
        if rec is None:
            progress("pulling deterministic data…")
            with PULL_LOCK:
                rec = research_pull.pull_ticker(sym)
        thread = load_qa(sym)
        latest = latest_analysis(sym)
        note = latest.get("report") if latest else None
        update_job(job_id, state="running", message="thinking…")
        result = ticker_analysis.ask(rec, thread.get("turns") or [], question,
                                     note=note, session=thread.get("session"), progress=progress,
                                     cancel=lambda: jobs.is_cancelled(job_id))
    except Exception as exc:  # noqa: BLE001
        update_job(job_id, state="error", error=f"{type(exc).__name__}: {exc}")
        return

    # Cancelled mid-flight: the subprocess was killed and the answer discarded;
    # leave the archived thread untouched so the user can ask something else.
    if result.get("cancelled") or jobs.is_cancelled(job_id):
        update_job(job_id, state="cancelled", message="cancelled")
        return

    if not result.get("ok"):
        update_job(job_id, state="error", error=result.get("error") or "all Q&A backends failed")
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
        write_json(qa_path(sym), thread)
    except Exception as exc:  # noqa: BLE001
        update_job(job_id, state="error", error=f"answer produced but not saved: {type(exc).__name__}: {exc}")
        return
    update_job(job_id, state="done", message=f"answered via {result.get('backend_label')}",
               result={"symbol": sym, "turns": len(thread["turns"])})


def qa_running(symbol: str) -> bool:
    return jobs.running("ticker_qa", symbol=symbol)


def start_qa(symbol: str, question: str) -> dict:
    sym = safe_symbol(symbol)
    question = (question or "").strip()
    if not question:
        raise ValueError("empty question")
    if qa_running(sym):
        raise Conflict(f"a question for {sym} is already being answered")
    return jobs.spawn("ticker_qa", run_qa_job, sym, question, symbol=sym)


# --------------------------------------------------------------------------- #
# Deep-research Q&A: continuable follow-up threads about a saved run, grounded
# in the report markdown + its citations. Stored next to the run artifacts.
# --------------------------------------------------------------------------- #
def deep_qa_path(stem: str) -> Path:
    return DEEP_DIR / f"{slugify(stem)}.qa.json"


def load_deep_qa(stem: str) -> dict:
    stem = slugify(stem)
    data = load(deep_qa_path(stem))
    if not isinstance(data, dict):
        return {"stem": stem, "turns": []}
    data.setdefault("stem", stem)
    if not isinstance(data.get("turns"), list):
        data["turns"] = []
    return data


def drop_qa_exchange(thread: dict, index) -> bool:
    """Remove the user turn at *index* plus the assistant reply that follows it.

    Returns True if anything was removed. Any resumable provider session is
    dropped so the next turn reseeds from the trimmed history -- otherwise the
    session would still carry the deleted exchange and contradict what we show.
    """
    turns = thread.get("turns")
    if not isinstance(turns, list):
        return False
    try:
        i = int(index)
    except (TypeError, ValueError):
        return False
    if i < 0 or i >= len(turns) or turns[i].get("role") != "user":
        return False
    end = i + 1
    if end < len(turns) and turns[end].get("role") == "assistant":
        end += 1
    del turns[i:end]
    thread.pop("session", None)
    return True


def run_deep_qa_job(job_id: str, stem: str, question: str) -> None:
    def progress(msg: str) -> None:
        update_job(job_id, message=msg)

    try:
        report_path = DEEP_DIR / f"{stem}.md"
        if not report_path.exists():
            update_job(job_id, state="error", error=f"no saved report for {stem}")
            return
        document = report_path.read_text(encoding="utf-8")
        sources = load(DEEP_DIR / f"{stem}.sources.json") or {}
        citations = sources.get("citations") or []
        title = (load(DEEP_DIR / f"{stem}.target-proposal.json") or {}).get("title") or stem
        thread = load_deep_qa(stem)
        update_job(job_id, state="running", message="thinking…")
        result = ticker_analysis.ask_about_doc(
            title, document, citations, thread.get("turns") or [], question,
            progress=progress, cancel=lambda: jobs.is_cancelled(job_id))
    except Exception as exc:  # noqa: BLE001
        update_job(job_id, state="error", error=f"{type(exc).__name__}: {exc}")
        return

    if result.get("cancelled") or jobs.is_cancelled(job_id):
        update_job(job_id, state="cancelled", message="cancelled")
        return
    if not result.get("ok"):
        update_job(job_id, state="error", error=result.get("error") or "all Q&A backends failed")
        return

    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    thread.setdefault("created_at", now)
    thread["updated_at"] = now
    thread["turns"].append({"role": "user", "text": question, "ts": now})
    thread["turns"].append({
        "role": "assistant", "text": result["report"], "ts": now,
        "backend": result.get("backend"), "backend_label": result.get("backend_label"),
        "model": result.get("model"), "usage": result.get("usage") or {},
    })
    try:
        write_json(deep_qa_path(stem), thread)
    except Exception as exc:  # noqa: BLE001
        update_job(job_id, state="error", error=f"answer produced but not saved: {type(exc).__name__}: {exc}")
        return
    update_job(job_id, state="done", message=f"answered via {result.get('backend_label')}",
               result={"stem": stem, "turns": len(thread["turns"])})


def deep_qa_running(stem: str) -> bool:
    return jobs.running("deep_qa", stem=stem)


def start_deep_qa(stem: str, question: str) -> dict:
    stem = slugify(stem)
    question = (question or "").strip()
    if not question:
        raise ValueError("empty question")
    if not (DEEP_DIR / f"{stem}.md").exists():
        raise ValueError(f"no saved report for {stem}")
    if deep_qa_running(stem):
        raise Conflict(f"a question for {stem} is already being answered")
    return jobs.spawn("deep_qa", run_deep_qa_job, stem, question, stem=stem)
