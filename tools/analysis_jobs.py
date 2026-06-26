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

import concurrent.futures
import datetime as dt
import re
import threading
from pathlib import Path

import jobs
import optimizer
import portfolio
import research_pull
import ticker_analysis
from apierror import Conflict
from config import ANALYSIS_DIR, DEEP_DIR, HOLDINGS_JSON, RESEARCH_DIR
from jobs import new_job, public, update_job
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
    job = new_job("ticker_analysis", symbol=sym, refresh=bool(refresh))
    threading.Thread(target=run_analysis_job,
                     args=(job["id"], sym, bool(refresh)), daemon=True).start()
    return public(job)


# --------------------------------------------------------------------------- #
# Batch portfolio review: analyse every held name (reusing a recent note where
# present, else a cheap CLI run), distil each ## Verdict into a conviction, then
# a whole-book synthesis. The per-name convictions feed the optimizer pool via
# optimizer.save_portfolio_review; held names with no other signal inherit them.
# --------------------------------------------------------------------------- #
def _held_symbols() -> list[str]:
    """Held equity symbols, heaviest first, so the batch surfaces big positions
    early. Option/derivative contracts (e.g. ``SPY 260618P00655000``) can't be
    analysed as tickers, so we drop anything ``safe_symbol`` rejects."""
    holdings = load(HOLDINGS_JSON) or {}
    weights = portfolio.holdings_weights(holdings) if isinstance(holdings, dict) else {}
    out: list[str] = []
    for sym in sorted(weights, key=lambda s: -weights.get(s, 0.0)):
        try:
            out.append(safe_symbol(sym))
        except ValueError:
            continue
    return out


def _extract_verdict(report: str) -> str:
    """The text under the report's ``## Verdict`` heading (the analyst's stance +
    confidence), collapsed to one line. Falls back to the report's opening."""
    if not report:
        return ""
    m = re.search(r"(?im)^#{1,6}\s*verdict\s*$([\s\S]*?)(?=^#{1,6}\s|\Z)", report)
    seg = m.group(1).strip() if m else report[:400]
    return " ".join(seg.split())[:400]


def _verdict_stance(text: str) -> str:
    """The analyst's stance. Read the *headline* clause first (the bold lead-in
    like "Hold — medium confidence"); only fall back to the full blob if the
    head carries no stance. Verdict bodies routinely discuss the rulebook
    ("below band so the rulebook suggests accumulate"), so a whole-text scan
    would flip a Hold into an Accumulate."""
    t = (text or "").replace("*", "").lower()
    head = re.split(r"[\u2014,./:(]", t, maxsplit=1)[0]  # clause before em-dash/comma/etc.
    for scope in (head, t):
        if re.search(r"\b(accumulate|buy|add|overweight)\b", scope):
            return "accumulate"
        if re.search(r"\b(trim|reduce|sell|exit|avoid|underweight)\b", scope):
            return "trim"
        if re.search(r"\bwait\b", scope):
            return "wait"
        if re.search(r"\b(hold|keep|maintain)\b", scope):
            return "hold"
    return ""


def _stance_conviction(stance: str, text: str) -> str:
    t = (text or "").lower()
    # Prefer the explicit "<level> confidence/conviction" phrase; a bare "high"
    # in "52-week high" or "high-quality" must not pass for analyst confidence.
    m = re.search(r"\b(high|medium|low)\b\s*(?:conviction|confidence|conf)", t)
    if m:
        conf = m.group(1)
    else:
        conf = "high" if re.search(r"\bhigh\b", t) else "low" if re.search(r"\blow\b", t) else "medium"
    if stance == "accumulate":
        return "high" if conf == "high" else "medium"
    if stance == "wait":
        return "low"
    if stance == "trim":
        return "avoid"
    return "medium"  # hold / unknown -> carry near current


def _analyze_one(sym: str, *, progress=None, cancel=None) -> str:
    """Run (or skip to) a single-name analysis and return its report text. Pulls
    deterministic data first if we have none cached. The data pull is serialised
    by ``PULL_LOCK`` (provider rate-limits); the LLM analysis itself is a plain
    subprocess and safe to run concurrently from several worker threads."""
    rec = load(RESEARCH_DIR / f"{sym}.json")
    if rec is None:
        with PULL_LOCK:
            # Re-check under the lock: another worker may have pulled it while we
            # waited, so we don't double-fetch the same name.
            rec = load(RESEARCH_DIR / f"{sym}.json") or research_pull.pull_ticker(sym)
    result = ticker_analysis.analyze(rec, progress=progress, cancel=cancel)
    if result.get("ok"):
        try:
            save_analysis_artifact(sym, result["report"], result)
        except Exception:  # noqa: BLE001 - a save miss shouldn't abort the batch
            pass
        return result["report"]
    return ""


def _synthesize_portfolio(notes: dict[str, str]) -> tuple[dict, str]:
    """Heuristic per-name conviction from each verdict, optionally refined by one
    LLM pass that also writes a rebalance summary. Returns (convictions, summary).
    """
    convictions: dict[str, dict] = {}
    for sym, verdict in notes.items():
        stance = _verdict_stance(verdict)
        convictions[sym] = {
            "conviction": _stance_conviction(stance, verdict),
            "stance": stance,
            "rationale": verdict,
        }
    summary = ""
    cfg = ticker_analysis.load_config()
    if not any(ticker_analysis.available_backends().values()):
        return convictions, summary
    lines = "\n".join(f"{sym}: {v or '(no verdict)'}" for sym, v in notes.items())
    prompt = (
        "You are reviewing a whole investment portfolio. Each held name has a "
        "one-line analyst verdict below. Produce ONLY a JSON object:\n"
        '{"holdings": {TICKER: {"conviction": "high|medium|low|avoid", '
        '"rationale": "one short sentence"}}, '
        '"summary": "2-4 sentences: where to add, where to trim, the biggest risks"}\n'
        "Do not invent tickers outside the list. No prose outside the JSON.\n\n"
        f"VERDICTS:\n{lines}\n"
    )
    res = ticker_analysis._run_with_fallback(prompt, cfg, None, None, label="portfolio-review")
    if not res.get("ok"):
        return convictions, summary
    parsed = ticker_analysis._extract_json_object(res.get("report") or "")
    if isinstance(parsed, dict):
        summary = str(parsed.get("summary") or "")
        for sym, node in (parsed.get("holdings") or {}).items():
            key = str(sym).upper().strip()
            if key not in convictions or not isinstance(node, dict):
                continue
            conv = str(node.get("conviction") or "").lower().strip()
            if conv in {"high", "medium", "low", "avoid"}:
                convictions[key]["conviction"] = conv
            if node.get("rationale"):
                convictions[key]["rationale"] = str(node["rationale"]).strip()
    return convictions, summary


# How many single-name analyses to run at once in a portfolio review. Each is a
# separate LLM subprocess; ~8 keeps the batch quick without swamping the machine
# or the backend's rate limits (the data pull stays serialised under PULL_LOCK).
PORTFOLIO_REVIEW_WORKERS = 8


def run_portfolio_review_job(job_id: str, refresh: bool) -> None:
    try:
        update_job(job_id, state="running", message="gathering holdings…")
        syms = _held_symbols()
        if not syms:
            update_job(job_id, state="error", error="no holdings to review")
            return
        total = len(syms)

        def cancelled() -> bool:
            return jobs.is_cancelled(job_id)

        def analyse(sym: str) -> tuple[str, str]:
            """Worker: reuse a cached note (unless refreshing) else run one
            analysis. Never raises — a bad name yields an empty verdict so it
            can't sink the whole batch."""
            if cancelled():
                return sym, ""
            try:
                latest = latest_analysis(sym)
                if latest and not refresh:
                    report = latest.get("report") or ""
                else:
                    report = _analyze_one(sym, cancel=cancelled)
            except Exception:  # noqa: BLE001 - one bad name shouldn't kill the batch
                report = ""
            return sym, _extract_verdict(report)

        notes: dict[str, str] = {}
        done = 0
        workers = max(1, min(PORTFOLIO_REVIEW_WORKERS, total))
        update_job(job_id, message=f"analysing {total} holding(s), {workers} at a time…")
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(analyse, s) for s in syms]
            try:
                for fut in concurrent.futures.as_completed(futures):
                    sym, verdict = fut.result()
                    notes[sym] = verdict
                    done += 1
                    update_job(job_id, message=f"analysed {done}/{total} holdings…")
                    if cancelled():
                        # Stop scheduling queued names; in-flight analyses get the
                        # cancel signal via their subprocess and unwind on their own.
                        pool.shutdown(wait=False, cancel_futures=True)
                        update_job(job_id, state="cancelled", message="cancelled")
                        return
            except Exception:  # noqa: BLE001
                pool.shutdown(wait=False, cancel_futures=True)
                raise

        update_job(job_id, message="synthesising convictions across the book…")
        convictions, summary = _synthesize_portfolio(notes)
        optimizer.save_portfolio_review(convictions, summary=summary)
    except Exception as exc:  # noqa: BLE001
        update_job(job_id, state="error", error=f"{type(exc).__name__}: {exc}")
        return
    update_job(job_id, state="done", message=f"reviewed {len(notes)} holding(s)",
               result={"reviewed": len(notes),
                       "convictions": len({k for k, v in convictions.items() if v.get("conviction")}),
                       "summary_chars": len(summary)})


def portfolio_review_running() -> bool:
    return jobs.running("portfolio_review")


def start_portfolio_review(refresh: bool = False) -> dict:
    if portfolio_review_running():
        raise Conflict("a portfolio review is already running")
    job = new_job("portfolio_review", refresh=bool(refresh))
    threading.Thread(target=run_portfolio_review_job,
                     args=(job["id"], bool(refresh)), daemon=True).start()
    return public(job)


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
    job = new_job("ticker_qa", symbol=sym)
    threading.Thread(target=run_qa_job, args=(job["id"], sym, question), daemon=True).start()
    return public(job)


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
    job = new_job("deep_qa", stem=stem)
    threading.Thread(target=run_deep_qa_job, args=(job["id"], stem, question), daemon=True).start()
    return public(job)
