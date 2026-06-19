#!/usr/bin/env python3
"""Local, interactive research server for the rebalancing app.

Serves the single-page UI in ``web/`` and a small JSON API that runs the
on-demand deep-dive puller. Stdlib only -- no Flask, no FastAPI, no pip install,
no wheel roulette on bleeding-edge Python. Just::

    py -3 tools/serve.py
    # then open http://127.0.0.1:6060

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

import copy
import datetime as dt
import json
import os
import re
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import DATA_DIR, HOLDINGS_JSON, REPO_ROOT, RESEARCH_DIR, WEB_DIR  # noqa: E402

WEB_DIST = WEB_DIR / "dist"  # Vite build output; served in prod when present
DEEP_DIR = RESEARCH_DIR / "deep"
ANALYSIS_DIR = RESEARCH_DIR / "analysis"  # on-demand single-ticker CLI analyses
SEGMENT_DEF_DIR = DATA_DIR / "segments"
SEGMENT_OUT_DIR = RESEARCH_DIR / "segments"
TARGET_MODEL_JSON = DATA_DIR / "target-model.json"
SYMBOL_ALIASES_JSON = DATA_DIR / "symbol-aliases.json"
AUTH_STATE_FILE = DATA_DIR / "cache" / "pplx-auth.json"  # gitignored
# Must match pplx_deep_research.default_profile_dir(): the automation worker uses
# a dedicated profile so it never fights the MCP browser for the profile lock.
DEFAULT_PPLX_PROFILE_DIR = Path.home() / ".cursor" / "pplx-automation-profile"
ROOT_STATIC_SUFFIXES = {".html", ".css", ".js"}

from portfolio import holdings_payload, holdings_weights, provider_symbol_for, symbol_aliases  # noqa: E402
from providers import yahoo  # noqa: E402
import instruments  # noqa: E402
import research_pull  # noqa: E402
import review_deep_research  # noqa: E402
import ticker_analysis  # noqa: E402
import rebalance  # noqa: E402
import risk  # noqa: E402
import tax_lots  # noqa: E402
import whatif  # noqa: E402
import journal  # noqa: E402
import jobs  # noqa: E402
import orchestrate  # noqa: E402  -- durable state machine for the guided strategy run
import target_construct  # noqa: E402  -- LLM + deterministic target-model synthesis
import hygiene  # noqa: E402  -- shared worst_severity for the research overlay
import errorlog  # noqa: E402
from peer_stats import _peer_stats  # noqa: E402  -- dossier peer-percentile math
from holdings_sync import (  # noqa: E402  -- read-only IBKR Flex sync (thin handlers below)
    _history_payload, _ibkr_status, _regenerate_site, _save_ibkr_secrets,
    _start_history_sync, _start_holdings_sync, _start_sectors_sync,
)
from ibkr_portfolio import load_env_file as _read_env_file  # noqa: E402  -- one KEY=VALUE parser
from trade_service import (  # noqa: E402  -- gated live-trading service (thin handlers below)
    _trade_cancel, _trade_orders, _trade_place, _trade_preview, _trade_status,
)
# Disk + identifier helpers and the job registry now live in their own modules;
# alias them so the rest of this file's call sites stay unchanged.
from store import (  # noqa: E402
    load as _load, write_json as _write_json, write_text as _write_text,
    slugify as _slugify, safe_symbol as _safe_symbol,
)
from jobs import (  # noqa: E402
    new_job as _new_job, update_job as _update_job, public as _job_public,
    claim_active as _claim_active, release_active as _release_active,
    any_active as _any_active_deep_job, active_count as _active_browser_count,
    max_slots as _max_browser_slots,
)


# HTTP error vocabulary (apierror): handlers and the services they call `raise`
# an outcome; _dispatch() maps each _HttpError to its .status. _Conflict is used
# by the in-flight-work guards below; _BadRequest by _read_body; _Forbidden and
# _BadGateway live with the trade service that raises them.
from apierror import (  # noqa: E402
    BadRequest as _BadRequest, Conflict as _Conflict, HttpError as _HttpError,
)


# Selectable chart windows -> (Yahoo range, interval). Short windows use intraday
# bars; longer windows step to a coarser interval so we don't ship thousands of
# daily points for a 10y view. (Yahoo has no "1w" range; 5d covers a trading week.)
PRICE_HISTORY_RANGES: dict[str, tuple[str, str]] = {
    "1d": ("1d", "5m"),
    "1w": ("5d", "30m"),
    "1mo": ("1mo", "1d"),
    "3mo": ("3mo", "1d"),
    "6mo": ("6mo", "1d"),
    "1y": ("1y", "1d"),
    "2y": ("2y", "1wk"),
    "5y": ("5y", "1wk"),
    "max": ("max", "1mo"),
}

_PULL_LOCK = threading.Lock()  # serialize outbound pulls; be polite to sources

# The deep-research / login / analysis job registry lives in jobs.py; concurrent
# browser runs are bounded by jobs.claim_active / jobs.release_active (a counting
# limit, default PPLX_MAX_CONCURRENT=3), each on its own cloned Chrome profile.
def _slots_busy_msg() -> str:
    return (f"all {_max_browser_slots()} Perplexity browser slots are busy "
            "— wait for a run to finish, or raise PPLX_MAX_CONCURRENT")


JOBS_LIST_LIMIT = 100  # cap the central Task Center feed (newest first)

# Dev live-reload. Off unless started with --reload. _BOOT_TOKEN is recomputed
# each time the process (re)starts, so the browser can tell an API restart apart
# from a no-op poll and reload itself even when no static asset changed.
_RELOAD = False
_BOOT_TOKEN = f"{time.time():.3f}"
# Let the strategy state machine recognize runs orphaned by a restart: any guided
# run still parked in a "running" state from a previous _BOOT_TOKEN had its worker
# thread killed by that restart and must be reaped instead of spun on forever.
orchestrate.set_boot_token(_BOOT_TOKEN)

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}


def _symbol_aliases() -> dict[str, str]:
    return {
        src: dst
        for src, dst in symbol_aliases().items()
        if len(src) <= 16 and len(dst) <= 16
        and re.match(r"^[A-Z0-9.=\- ]+$", src)
        and re.match(r"^[A-Z0-9.=\- ]+$", dst)
    }


def _resolve_symbol(symbol: str) -> str:
    sym = _safe_symbol(symbol)
    return provider_symbol_for(sym, _symbol_aliases())


def _annotate_symbol_record(rec: dict, input_symbol: str, provider_symbol: str) -> dict:
    if input_symbol != provider_symbol:
        rec = dict(rec)
        rec["input_symbol"] = input_symbol
        rec["provider_symbol"] = provider_symbol
    # Always hand the UI a resolved instrument type, even for older cached
    # records that predate quote_type capture (classify() falls back to
    # symbol/profile heuristics in that case).
    rec["instrument_type"] = instruments.classify(
        provider_symbol,
        quote_type=rec.get("quote_type") or rec.get("instrument_type"),
        profile=rec.get("profile"),
    )
    return rec


def _save_symbol_alias(body: dict) -> dict:
    src = _safe_symbol(str(body.get("input_symbol") or body.get("input") or ""))
    dst = _safe_symbol(str(body.get("provider_symbol") or body.get("provider") or ""))
    aliases = _symbol_aliases()
    if src == dst:
        aliases.pop(src, None)
    else:
        aliases[src] = dst
    _write_json(SYMBOL_ALIASES_JSON, aliases)
    return {"aliases": aliases, "input_symbol": src, "provider_symbol": aliases.get(src, src)}


def _symbol_candidates(body: dict) -> dict:
    src = _safe_symbol(str(body.get("input_symbol") or body.get("symbol") or ""))
    raw_candidates = body.get("candidates") or []
    if not isinstance(raw_candidates, list):
        raise ValueError("candidates must be a list")

    seen: set[str] = set()
    valid: list[dict[str, str]] = []
    invalid: list[dict[str, str]] = []
    for raw in raw_candidates[:16]:
        try:
            candidate = _safe_symbol(str(raw))
        except ValueError as exc:
            invalid.append({"symbol": str(raw), "error": str(exc)})
            continue
        if candidate in seen or candidate == src:
            continue
        seen.add(candidate)
        try:
            result = yahoo.chart(candidate, rng="5d", interval="1d")
            meta = result.get("meta") or {}
            valid.append({
                "symbol": candidate,
                "exchange": str(meta.get("exchangeName") or meta.get("fullExchangeName") or ""),
                "currency": str(meta.get("currency") or ""),
            })
        except Exception as exc:  # noqa: BLE001 - candidate failed validation
            invalid.append({"symbol": candidate, "error": str(exc)})
    return {"input_symbol": src, "candidates": valid, "invalid": invalid}


def _symbol_search(query: str, *, limit: int = 8) -> dict:
    """Substring / company-name ticker search via Yahoo. Best-effort: a provider
    hiccup returns an empty list rather than an error so the UI degrades cleanly."""
    q = (query or "").strip()
    if not q:
        return {"query": "", "results": []}
    try:
        results = yahoo.search(q, limit=limit)
    except Exception as exc:  # noqa: BLE001 - search is a nicety, never fatal
        return {"query": q, "results": [], "error": str(exc)}
    return {"query": q, "results": results}


def _setup_status(*, run_checks: bool = False) -> dict:
    return {
        "llm": ticker_analysis.setup_status(run_checks=run_checks),
        "perplexity": _get_auth_state(),
        "ibkr": _ibkr_status(),
        "data": _data_status(),
        "environment": {
            "sec_user_agent": bool(os.environ.get("SEC_USER_AGENT")),
            "fmp_api_key": bool(os.environ.get("FMP_API_KEY")),
            "pplx_profile_dir": os.environ.get("PPLX_PROFILE_DIR") or str(DEFAULT_PPLX_PROFILE_DIR),
            "pplx_max_concurrent": _max_browser_slots(),
            "pplx_browsers_active": _active_browser_count(),
        },
    }


def _data_status() -> dict:
    holdings = _load(HOLDINGS_JSON)
    model = _load(TARGET_MODEL_JSON)
    positions = holdings.get("positions") if isinstance(holdings, dict) else []
    has_holdings = isinstance(positions, list) and len(positions) > 0
    has_model = isinstance(model, dict) and bool(model)
    return {
        "ready": bool(has_holdings and has_model),
        "holdings": {
            "exists": HOLDINGS_JSON.exists(),
            "positions": len(positions) if isinstance(positions, list) else 0,
        },
        "target_model": {
            "exists": TARGET_MODEL_JSON.exists(),
        },
        "empty": not (has_holdings and has_model),
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

    status = str(raw.get("status") or "draft").strip().lower() or "draft"
    # A hollow segment (no members) is fine as a *draft* you're still filling in,
    # but approving one is the bug that silently breaks the review gate: the pull
    # runs against nothing, the proposal is empty, and Apply is dead with no
    # explanation. Refuse to approve until tickers are identified.
    if status == "approved" and not cleaned_members:
        raise ValueError(
            "cannot approve a segment with no members — identify its tickers "
            "(use the LLM prompt, paste/edit members) before approving"
        )

    cleaned = {
        "title": title,
        "kind": raw.get("kind") or "research",
        "status": status,
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


def _merge_draft_members(baseline: list[dict], extra: list[dict]) -> list[dict]:
    """Combine keyword-baseline members with LLM-proposed ones, deduped by
    symbol (baseline wins on a tie) and skipping anything with an invalid or
    empty symbol. Keeps rationale/confidence when present."""
    out: list[dict] = []
    seen: set[str] = set()
    for source in (baseline or [], extra or []):
        for item in source:
            if not isinstance(item, dict):
                continue
            try:
                sym = _safe_symbol(str(item.get("symbol") or ""))
            except ValueError:
                continue
            if sym in seen:
                continue
            seen.add(sym)
            member = {
                "symbol": sym,
                "sleeve": str(item.get("sleeve") or "other").strip().lower() or "other",
            }
            for key in ("rationale", "confidence"):
                if item.get(key):
                    member[key] = str(item[key]).strip()
            out.append(member)
    return out


def _run_segment_draft_job(job_id: str, query: str) -> None:
    """Draft a research segment for any theme: start from the keyword baseline,
    then (if an analysis CLI is available) ask the LLM to propose real tickers
    for subjects we don't already hold, and merge the two."""
    def progress(msg: str) -> None:
        _update_job(job_id, message=msg)

    try:
        baseline = _draft_segment(query)
        definition = baseline["definition"]
        members = list(definition.get("members") or [])
        warnings: list[str] = []
        backend_label = None

        if any(ticker_analysis.available_backends().values()):
            _update_job(job_id, state="running",
                        message="researching candidate tickers…")
            llm = ticker_analysis.draft_segment_members(
                query, progress=progress, cancel=lambda: jobs.is_cancelled(job_id))
            if jobs.is_cancelled(job_id):
                _update_job(job_id, state="cancelled", message="cancelled")
                return
            if llm.get("ok"):
                members = _merge_draft_members(members, llm.get("members") or [])
                backend_label = llm.get("backend_label")
                if llm.get("title"):
                    definition["title"] = llm["title"]
                if llm.get("comment"):
                    definition["comment"] = llm["comment"]
            else:
                warnings.append(
                    "LLM draft failed (" + (llm.get("error") or "unknown")
                    + "); showing keyword matches only. Use the prompt below to fill members."
                )
        else:
            warnings.append(
                "No analysis CLI is configured, so tickers weren't auto-researched. "
                "Use the prompt below with an LLM (or paste members), then approve."
            )
    except Exception as exc:  # noqa: BLE001
        _update_job(job_id, state="error", error=f"{type(exc).__name__}: {exc}")
        return

    if not members:
        warnings.append(
            "No candidate tickers identified. Use the prompt below with an LLM, or "
            "paste/edit members, then validate before approving."
        )
    definition["members"] = members
    definition["sleeves"] = sorted({m["sleeve"] for m in members}) or ["other"]
    _update_job(
        job_id, state="done",
        message=(f"drafted {len(members)} names via {backend_label}"
                 if backend_label else f"drafted {len(members)} names"),
        result={
            "slug": baseline["slug"],
            "definition": definition,
            "llm_prompt": baseline["llm_prompt"],
            "warnings": warnings,
            "backend_label": backend_label,
            "member_count": len(members),
        },
    )


def _start_segment_draft(query: str) -> dict:
    query = (query or "").strip()
    if not query:
        raise ValueError("query is required")
    # Like ticker analysis, drafting shells out to a CLI but not the browser, so
    # it does not take the single browser slot and can run alongside other work.
    job = _new_job("segment_draft", query=query)
    threading.Thread(target=_run_segment_draft_job,
                     args=(job["id"], query), daemon=True).start()
    return _job_public(job)


def _freshness_directive(today: str) -> str:
    """Force time-sensitive numbers to be live as-of the run date.

    Deep Research models otherwise quietly fall back to their training cutoff and
    present, e.g., "1-year returns to mid-2025" in a 2026 report (numbers that are
    a year stale and simply wrong). This directive bans that and demands an
    as-of date on every price/return/multiple."""
    return (
        "FRESHNESS (critical): every time-sensitive number \u2014 share prices, "
        "trailing returns, market caps, valuation multiples, the latest reported "
        "quarter, and guidance \u2014 must reflect data retrieved live as of "
        f"{today}, NOT your training data. Do NOT anchor to a prior-year "
        "snapshot: a phrase like 'returns over ~12 months to mid-2025' in a "
        f"report dated {today} is a failure. State the as-of date next to any "
        f"price, return, or multiple, and that date must be within a few weeks of "
        f"{today}; compute trailing returns ending at the most recent close. If "
        "you cannot retrieve a current value, say so and omit it rather than "
        "presenting a stale figure as if it were current.\n"
    )


def _segment_prompt(name: str) -> dict:
    slug = _slugify(name)
    definition = _load(SEGMENT_DEF_DIR / f"{slug}.json")
    if not definition:
        raise ValueError(f"unknown segment {slug}")
    held = holdings_weights()  # single source of truth (percent of invested book)
    symbols = [m["symbol"] for m in definition.get("members", [])]
    held_lines = [
        f"- {sym}: {held[sym]:.2f}% of book"
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
        "FORMAT: write a prose report in Markdown with section headings, "
        "paragraphs, bullet lists, and Markdown tables. Do NOT return the answer "
        "as a JSON object or array, and do NOT wrap the whole response in a code "
        "block. Structured data (e.g. the comparison) belongs in Markdown tables, "
        "not JSON.\n"
    )
    prompt += _freshness_directive(today)
    if held_lines:
        prompt += (
            "\nFor context only (do not bias coverage toward these), I currently own:\n"
            + "\n".join(held_lines) + "\n"
        )
    return {"segment": slug, "date": today, "prompt": prompt}


def _ticker_deep_prompt(symbol: str) -> dict:
    """Single-name Deep Research prompt: the expensive, on-demand counterpart to
    the cheap per-ticker CLI analysis. Subject is one company, not a segment, so
    the stem is namespaced ``ticker-<sym>`` and it reuses the same Perplexity
    run/save/Q&A machinery without polluting the segment list. The FORMAT block
    mirrors the segment prompt so the scraper's JSON guard and citation handling
    behave identically."""
    sym = (symbol or "").strip().upper()
    if not _TICKER_SHAPE.match(sym):
        raise ValueError(f"not a recognisable ticker: {symbol!r}")
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    weight = holdings_weights().get(sym)
    prompt = (
        f"Deep research on {sym} as a long-term investment, as of {today}.\n"
        "Cover, with evidence: what the business does and how it earns money; the "
        "most recent quarterly results and management guidance; valuation versus "
        "its own history and its closest peers; competitive positioning and moat; "
        "growth drivers and near-term catalysts; the main risks and red flags; and "
        "an explicit bull case and bear case over the next 6-24 months.\n"
        f"Identify {sym}'s closest public peers and compare them head-to-head; call "
        f"out any peer that looks more attractive than {sym} on its own merits.\n"
        "End with a clear portfolio stance \u2014 accumulate, hold, trim, or avoid "
        "\u2014 and the specific evidence that would flip that stance.\n"
        "Include source citations and distinguish facts from opinion. Call out which "
        "numeric claims need deterministic verification.\n"
        "On first mention of any public company, append its primary exchange ticker "
        "with a $ prefix, e.g. 'ServiceNow ($NOW)'. Include a peer comparison table "
        "with a 'Ticker' column.\n"
        "Do not ask clarifying questions; if anything is ambiguous, state your "
        "assumptions and proceed.\n"
        "FORMAT: write a prose report in Markdown with section headings, paragraphs, "
        "bullet lists, and Markdown tables. Do NOT return the answer as a JSON object "
        "or array, and do NOT wrap the whole response in a code block. Structured "
        "data belongs in Markdown tables, not JSON.\n"
    )
    prompt += _freshness_directive(today)
    if weight is not None:
        prompt += (
            f"\nFor context only (do not let it bias your conclusion), I currently "
            f"hold {sym} at {weight:.2f}% of my invested book.\n"
        )
    return {"segment": f"ticker-{sym.lower()}", "symbol": sym, "date": today, "prompt": prompt}


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


def _save_deep_artifact(body: dict) -> dict:
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


def _delete_deep_run(stem: str) -> dict:
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
    return {"stem": stem, "removed": removed, "runs": _deep_runs()}


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
    return jobs.running("ticker_analysis", symbol=symbol)


def _start_analysis(symbol: str, refresh: bool) -> dict:
    sym = _safe_symbol(symbol)
    if _analysis_running(sym):
        raise _Conflict(f"an analysis for {sym} is already running")
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
                                     note=note, session=thread.get("session"), progress=progress,
                                     cancel=lambda: jobs.is_cancelled(job_id))
    except Exception as exc:  # noqa: BLE001
        _update_job(job_id, state="error", error=f"{type(exc).__name__}: {exc}")
        return

    # Cancelled mid-flight: the subprocess was killed and the answer discarded;
    # leave the archived thread untouched so the user can ask something else.
    if result.get("cancelled") or jobs.is_cancelled(job_id):
        _update_job(job_id, state="cancelled", message="cancelled")
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
    return jobs.running("ticker_qa", symbol=symbol)


def _start_qa(symbol: str, question: str) -> dict:
    sym = _safe_symbol(symbol)
    question = (question or "").strip()
    if not question:
        raise ValueError("empty question")
    if _qa_running(sym):
        raise _Conflict(f"a question for {sym} is already being answered")
    job = _new_job("ticker_qa", symbol=sym)
    threading.Thread(target=_run_qa_job, args=(job["id"], sym, question), daemon=True).start()
    return _job_public(job)


# --------------------------------------------------------------------------- #
# Deep-research Q&A: continuable follow-up threads about a saved run, grounded
# in the report markdown + its citations. Stored next to the run artifacts.
# --------------------------------------------------------------------------- #
def _deep_qa_path(stem: str) -> Path:
    return DEEP_DIR / f"{_slugify(stem)}.qa.json"


def _load_deep_qa(stem: str) -> dict:
    stem = _slugify(stem)
    data = _load(_deep_qa_path(stem))
    if not isinstance(data, dict):
        return {"stem": stem, "turns": []}
    data.setdefault("stem", stem)
    if not isinstance(data.get("turns"), list):
        data["turns"] = []
    return data


def _drop_qa_exchange(thread: dict, index) -> bool:
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


def _run_deep_qa_job(job_id: str, stem: str, question: str) -> None:
    def progress(msg: str) -> None:
        _update_job(job_id, message=msg)

    try:
        report_path = DEEP_DIR / f"{stem}.md"
        if not report_path.exists():
            _update_job(job_id, state="error", error=f"no saved report for {stem}")
            return
        document = report_path.read_text(encoding="utf-8")
        sources = _load(DEEP_DIR / f"{stem}.sources.json") or {}
        citations = sources.get("citations") or []
        title = (_load(DEEP_DIR / f"{stem}.target-proposal.json") or {}).get("title") or stem
        thread = _load_deep_qa(stem)
        _update_job(job_id, state="running", message="thinking…")
        result = ticker_analysis.ask_about_doc(
            title, document, citations, thread.get("turns") or [], question,
            progress=progress, cancel=lambda: jobs.is_cancelled(job_id))
    except Exception as exc:  # noqa: BLE001
        _update_job(job_id, state="error", error=f"{type(exc).__name__}: {exc}")
        return

    if result.get("cancelled") or jobs.is_cancelled(job_id):
        _update_job(job_id, state="cancelled", message="cancelled")
        return
    if not result.get("ok"):
        _update_job(job_id, state="error", error=result.get("error") or "all Q&A backends failed")
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
        _write_json(_deep_qa_path(stem), thread)
    except Exception as exc:  # noqa: BLE001
        _update_job(job_id, state="error", error=f"answer produced but not saved: {type(exc).__name__}: {exc}")
        return
    _update_job(job_id, state="done", message=f"answered via {result.get('backend_label')}",
                result={"stem": stem, "turns": len(thread["turns"])})


def _deep_qa_running(stem: str) -> bool:
    return jobs.running("deep_qa", stem=stem)


def _start_deep_qa(stem: str, question: str) -> dict:
    stem = _slugify(stem)
    question = (question or "").strip()
    if not question:
        raise ValueError("empty question")
    if not (DEEP_DIR / f"{stem}.md").exists():
        raise ValueError(f"no saved report for {stem}")
    if _deep_qa_running(stem):
        raise _Conflict(f"a question for {stem} is already being answered")
    job = _new_job("deep_qa", stem=stem)
    threading.Thread(target=_run_deep_qa_job, args=(job["id"], stem, question), daemon=True).start()
    return _job_public(job)


# Only these keys belong in a target-model entry; the synthesis engine carries
# extra metadata (conviction, sleeve, rationale) on a change that must never be
# written into the model itself.
_TARGET_WRITE_KEYS = ("low", "high", "rule", "note", "structural")
TARGET_MODEL_BACKUP_DIR = DATA_DIR / "backups"


def _clean_target(raw: dict) -> dict:
    return {k: raw[k] for k in _TARGET_WRITE_KEYS if k in raw}


def _apply_changes_to_model(model: dict, changes: list, *, blocked: set) -> tuple[list, list]:
    """Apply proposal change records onto `model` IN PLACE. Pure with respect to
    disk so it is shared by the live apply (which then writes) and the Gate-2
    preview (which works on a throwaway copy). Returns (applied, skipped).

    Supported actions: add_target (new band), modify_target (merge onto the
    existing band, preserving keys the proposal didn't touch), and a guarded
    sleeve upsert. Anything else is recorded as skipped rather than silently
    dropped, so an unexpected action is visible instead of a no-op."""
    targets = model.setdefault("targets", {})
    sleeves = model.setdefault("sleeves", {})
    applied: list = []
    skipped: list = []
    for change in changes or []:
        action = change.get("action")
        if action in ("add_target", "modify_target"):
            try:
                sym = _safe_symbol(change.get("symbol", ""))
            except ValueError:
                skipped.append({"symbol": change.get("symbol"), "reason": "invalid symbol"})
                continue
            # Never derive a band from a ticker whose deterministic data failed an
            # ERROR-level check (override only on an explicit allow_blocked).
            if sym in blocked:
                skipped.append({"symbol": sym, "reason": "blocked: ERROR-level deterministic data; resolve before applying"})
                continue
            pt = _clean_target(dict(change.get("proposed_target") or {}))
            if not pt:
                skipped.append({"symbol": sym, "reason": "missing proposed_target"})
                continue
            if action == "add_target":
                if sym in targets:
                    skipped.append({"symbol": sym, "reason": "target already exists"})
                    continue
                targets[sym] = pt
            else:  # modify_target merges so structural bands / unrelated keys survive
                cur = dict(targets.get(sym) or {})
                cur.update(pt)
                targets[sym] = cur
            applied.append(sym)
        elif action in ("add_sleeve", "modify_sleeve", "set_sleeve"):
            name = str(change.get("sleeve") or change.get("name") or "").strip()
            proposed = change.get("proposed_sleeve")
            if not name or not isinstance(proposed, dict):
                skipped.append({"symbol": name or "(sleeve)", "reason": "missing sleeve name or definition"})
                continue
            cur = dict(sleeves.get(name) or {})
            cur.update(proposed)
            sleeves[name] = cur
            applied.append(f"[{name}]")
        else:
            skipped.append({"symbol": change.get("symbol"), "reason": f"unsupported action: {action}"})
    return applied, skipped


def _backup_target_model() -> str | None:
    """Snapshot the current target model before mutating it, so an apply is
    reversible. Returns the backup's repo-relative path, or None if there was
    nothing to back up."""
    model = _load(TARGET_MODEL_JSON)
    if not model:
        return None
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = TARGET_MODEL_BACKUP_DIR / f"target-model-{ts}.json"
    _write_json(backup, model)
    return str(backup.relative_to(REPO_ROOT))


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
    blocked = set(proposal.get("blocked_symbols", [])) if not allow_blocked else set()
    backup = _backup_target_model()
    applied, skipped = _apply_changes_to_model(model, proposal.get("changes", []), blocked=blocked)
    proposal["status"] = "applied" if applied else "reviewed"
    proposal["applied_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    proposal["applied_symbols"] = applied
    proposal["skipped"] = skipped
    _write_json(TARGET_MODEL_JSON, model)
    _write_json(proposal_path, proposal)
    # Keep the static plan in lockstep with the model the apply just changed.
    site = _regenerate_site()
    return {"applied": applied, "skipped": skipped, "proposal": proposal, "backup": backup, "site": site}


def _preview_plan_for_proposal(proposal: dict, *, allow_blocked: bool = False) -> dict:
    """Compute the rebalance plan that WOULD result from a proposal, against a
    throwaway copy of the model -- nothing is written. Powers the Gate-2 preview
    (with the proposal's changes) and the final recommendation (empty changes,
    i.e. the already-committed model)."""
    model = _load(TARGET_MODEL_JSON)
    holdings = _load(HOLDINGS_JSON)
    if not model or not holdings:
        return {"available": False, "reason": "need both a target model and a holdings snapshot to preview a rebalance"}
    draft = copy.deepcopy(model)
    blocked = set(proposal.get("blocked_symbols", [])) if not allow_blocked else set()
    applied, skipped = _apply_changes_to_model(draft, proposal.get("changes", []), blocked=blocked)
    try:
        plan = tax_lots.enrich_plan(rebalance.plan(draft, holdings), holdings)
    except Exception as exc:  # noqa: BLE001 - a bad band shouldn't kill the gate
        return {"available": False, "reason": f"could not compute plan: {exc}"}
    return {"available": True, "applied": applied, "skipped": skipped, "plan": plan}


# Research-overlay classification: how a thesis verdict leans. Used only to flag
# when the deterministic band action and the human thesis disagree -- never to
# size a trade. Thesis actions are free text from the dossier form, so the match
# is loose and lowercased.
_THESIS_ADD_LIKE = {"add", "accumulate", "buy", "build", "increase", "overweight"}
_THESIS_TRIM_LIKE = {"trim", "sell", "reduce", "exit", "avoid", "underweight", "do_not_add"}


def _research_conflict(row_action: str | None, thesis_action: str | None) -> bool:
    """True when the band's suggested action and the thesis verdict point opposite
    ways: trimming a name the thesis wants more of, or buying one it wants less
    of. Anything else (no thesis, agreement, a neutral hold/wait) is not a
    conflict."""
    ta = (thesis_action or "").lower().strip()
    if not ta:
        return False
    if row_action == "trim" and ta in _THESIS_ADD_LIKE:
        return True
    if row_action == "buy" and ta in _THESIS_TRIM_LIKE:
        return True
    return False


def _research_overlay(provider_sym: str) -> dict | None:
    """Compact, independent research context for one rebalance row, read from the
    per-ticker dossier. Returns None when there's no dossier so the row reads as
    'no signal'. Nothing here feeds the trade math -- it is decision support
    only. ``research_score`` is deliberately omitted (it is segment-only, not on
    per-ticker files)."""
    rec = _load(RESEARCH_DIR / f"{provider_sym}.json")
    if not isinstance(rec, dict):
        return None
    thesis = rec.get("thesis") if isinstance(rec.get("thesis"), dict) else {}
    momentum = rec.get("momentum") if isinstance(rec.get("momentum"), dict) else {}
    return {
        "as_of": rec.get("as_of"),
        "data_quality": hygiene.worst_severity(rec.get("cross_checks") or []),
        "decision": rec.get("decision"),
        "momentum_3m_pct": momentum.get("chg_3m_pct"),
        "thesis_action": (thesis.get("action") or "").strip() or None,
        "thesis_summary": (thesis.get("summary") or "").strip() or None,
        "thesis_as_of": thesis.get("as_of"),
    }


def _attach_research_overlay(plan: dict) -> None:
    """Enrich each held target row of a rebalance plan, in place, with a compact
    ``research`` object + a ``research_conflict`` flag. Best-effort: a missing or
    malformed dossier is skipped silently so the planner always renders."""
    for row in plan.get("rows") or []:
        if row.get("kind") != "target" or not row.get("held"):
            continue
        try:
            overlay = _research_overlay(_resolve_symbol(row.get("name") or ""))
        except Exception:  # noqa: BLE001 - the overlay is optional; never break the plan
            overlay = None
        if not overlay:
            continue
        row["research"] = overlay
        row["research_conflict"] = _research_conflict(row.get("action"), overlay["thesis_action"])


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
            "type": instruments.classify(
                sym,
                quote_type=rec.get("quote_type") or rec.get("instrument_type"),
                profile=rec.get("profile"),
            ),
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
            "symbol": sym, "name": sym, "type": instruments.OTHER, "as_of": None,
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
        raise _Conflict(_slots_busy_msg())
    try:
        import pplx_deep_research as worker
        res = worker.check_login()
        if res.get("status") == "error":
            raise _Conflict(res.get("detail") or "login check failed")
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


# Appended to the import-failure message of the deep-research job so a user who
# never set Playwright up gets the install commands inline.
_PLAYWRIGHT_INSTALL_HINT = (
    ". Install with `py -3 -m pip install playwright` then "
    "`py -3 -m playwright install chromium`."
)


def _browser_job(job_id: str, *, running_msg: str, call, handle, install_hint: str = "") -> None:
    """Shared scaffold for the three Playwright-backed jobs (deep run, login,
    import). It owns the boilerplate that was duplicated across all three: import
    the worker (mapping a missing Playwright to an error), flip the job to
    running, capture worker exceptions, and ALWAYS release the single active-job
    slot via finally. `call(worker, progress)` performs the actual worker call
    and returns its result dict; `handle(res)` maps that result to job state and
    must not release the slot itself."""
    def progress(msg: str) -> None:
        _update_job(job_id, message=msg)

    try:
        import pplx_deep_research as worker
    except Exception as exc:  # noqa: BLE001
        _update_job(job_id, state="error",
                    error=f"Playwright not available: {type(exc).__name__}: {exc}{install_hint}")
        _release_active()
        return

    _update_job(job_id, state="running", message=running_msg)
    try:
        res = call(worker, progress)
    except Exception as exc:  # noqa: BLE001
        _update_job(job_id, state="error", error=f"{type(exc).__name__}: {exc}")
        _release_active()
        return

    try:
        handle(res)
    finally:
        _release_active()


def _save_run_result(job_id: str, res: dict, segment: str, date: str, *,
                     source_url, auth_label: str, done_msg: str) -> None:
    """Shared 'done' handling for the deep-run and import jobs: persist the
    artifact, refresh auth state, and finish the job with a uniform result.
    Leaves the active-slot release to the _browser_job scaffold."""
    try:
        artifact = _save_deep_artifact({
            "segment": segment,
            "date": date,
            "report": res.get("report", ""),
            "citations": res.get("citations", []),
            "source_url": source_url,
        })
    except Exception as exc:  # noqa: BLE001
        _update_job(job_id, state="error", error=f"saved nothing: {type(exc).__name__}: {exc}")
        return
    _set_auth_state(True, auth_label)
    _update_job(job_id, state="done", message=done_msg,
                result={
                    "source_url": source_url,
                    "citations": res.get("citations", []),
                    "report_chars": len(res.get("report", "")),
                },
                artifact=artifact)


def _run_deep_job(job_id: str, segment: str, date: str, prompt: str, window_mode: str) -> None:
    def call(worker, progress):
        return worker.run_deep_research(
            prompt, window_mode=window_mode,
            clarify_answer=_clarify_answer_for(segment), progress=progress,
            clone_profile=True,  # run on a throwaway clone so runs can parallelize
            on_url=lambda url: _update_job(job_id, source_url=url),
            cancel=lambda: jobs.is_cancelled(job_id),
        )

    def handle(res: dict) -> None:
        status = res.get("status")
        if status == "done":
            _save_run_result(job_id, res, segment, date,
                             source_url=res.get("source_url"),
                             auth_label="deep run", done_msg="report saved")
        elif status == "cancelled":
            _update_job(job_id, state="cancelled", message="cancelled")
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

    _browser_job(job_id, running_msg="starting browser", call=call, handle=handle,
                 install_hint=_PLAYWRIGHT_INSTALL_HINT)


def _run_login_job(job_id: str) -> None:
    def call(worker, progress):
        return worker.ensure_login(progress=progress,
                                   cancel=lambda: jobs.is_cancelled(job_id))

    def handle(res: dict) -> None:
        if res.get("status") == "logged_in":
            _set_auth_state(True, "login window")
            _update_job(job_id, state="done", message="Perplexity login confirmed")
        elif res.get("status") == "cancelled":
            _update_job(job_id, state="cancelled", message="cancelled")
        else:
            _update_job(job_id, state="error", message="login window timed out",
                        error="login not completed in time")

    _browser_job(job_id, running_msg="opening login window", call=call, handle=handle)


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
        raise _Conflict(_slots_busy_msg())
    job = _new_job("deep_research", segment=segment, date=date, window_mode=window_mode)
    threading.Thread(target=_run_deep_job,
                     args=(job["id"], segment, date, prompt, window_mode),
                     daemon=True).start()
    return _job_public(job)


def _start_login() -> dict:
    if not _claim_active():
        raise _Conflict(_slots_busy_msg())
    job = _new_job("login")
    threading.Thread(target=_run_login_job, args=(job["id"],), daemon=True).start()
    return _job_public(job)


def _run_import_job(job_id: str, segment: str, date: str, url: str) -> None:
    def call(worker, progress):
        # The import URL is known up front -> surface it as the live link now.
        _update_job(job_id, source_url=url)
        return worker.fetch_by_url(url, clone_profile=True, progress=progress,
                                   cancel=lambda: jobs.is_cancelled(job_id))

    def handle(res: dict) -> None:
        status = res.get("status")
        if status == "done":
            _save_run_result(job_id, res, segment, date,
                             source_url=res.get("source_url", url),
                             auth_label="import", done_msg="imported report saved")
        elif status == "cancelled":
            _update_job(job_id, state="cancelled", message="cancelled")
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

    _browser_job(job_id, running_msg="opening run URL", call=call, handle=handle)


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
        raise _Conflict(_slots_busy_msg())
    job = _new_job("import", segment=segment, date=date)
    threading.Thread(target=_run_import_job,
                     args=(job["id"], segment, date, url),
                     daemon=True).start()
    return _job_public(job)


# --------------------------------------------------------------------------- #
# Guided "Direction -> Rebalance" strategy orchestration.
#
# The durable state machine lives in orchestrate.py; these runners do the
# per-leg work on daemon threads, exactly like the deep-research job runners
# above. A run pauses at a gate by simply landing in an awaiting_* state -- no
# thread is left blocked on a human. The synthesis leg reuses the existing deep
# research job wholesale (login walls, clarify, auto-save) by starting it and
# polling its sub-job to completion.
# --------------------------------------------------------------------------- #
def _strategy_progress(run_id: str, job_id: str | None):
    def progress(msg: str) -> None:
        if job_id:
            _update_job(job_id, message=msg)
        orchestrate.update_run(run_id, message=msg)
    return progress


def _run_strategy_draft(run_id: str) -> None:
    run = orchestrate.load_run(run_id)
    if not run:
        return
    direction = run["direction"]
    try:
        baseline = _draft_segment(direction)
        definition = baseline["definition"]
        members = list(definition.get("members") or [])
        warnings = list(baseline.get("warnings") or [])
        backend_label = None
        if any(ticker_analysis.available_backends().values()):
            orchestrate.update_run(run_id, message="researching candidate tickers…")
            llm = ticker_analysis.draft_segment_members(direction)
            if llm.get("ok"):
                members = _merge_draft_members(members, llm.get("members") or [])
                backend_label = llm.get("backend_label")
                if llm.get("title"):
                    definition["title"] = llm["title"]
                if llm.get("comment"):
                    definition["comment"] = llm["comment"]
            else:
                warnings.append(
                    "LLM draft failed (" + (llm.get("error") or "unknown")
                    + "); showing keyword matches only — edit the members before approving."
                )
        definition["members"] = members
        definition["sleeves"] = sorted({m["sleeve"] for m in members}) or ["other"]
        definition["status"] = "draft"
        orchestrate.set_state(
            run_id, orchestrate.AWAITING_SEGMENT,
            segment=baseline["slug"],
            message=f"Review the drafted segment ({len(members)} names), then approve.",
            draft={
                "slug": baseline["slug"],
                "definition": definition,
                "llm_prompt": baseline["llm_prompt"],
                "warnings": warnings,
                "backend_label": backend_label,
            },
        )
    except Exception as exc:  # noqa: BLE001
        orchestrate.set_state(run_id, orchestrate.ERROR,
                              error=f"{type(exc).__name__}: {exc}", message="drafting failed")


def _start_strategy(direction: str) -> dict:
    run = orchestrate.new_run(direction)
    threading.Thread(target=_run_strategy_draft, args=(run["run_id"],), daemon=True).start()
    return orchestrate.public(run)


def _approve_strategy_segment(run_id: str, definition_raw: dict | None) -> dict:
    run = orchestrate.load_run(run_id)
    if not run:
        raise ValueError(f"unknown strategy run {run_id}")
    if run.get("state") not in (orchestrate.AWAITING_SEGMENT, orchestrate.NEEDS_LOGIN):
        raise _Conflict(f"run {run_id} is not awaiting segment approval")
    raw = dict(definition_raw or (run.get("draft") or {}).get("definition") or {})
    raw["status"] = "approved"  # approving requires members; _validate enforces it
    definition = _validate_segment_definition(raw)
    slug = run.get("segment") or _slugify(definition.get("title") or "segment")
    _write_json(SEGMENT_DEF_DIR / f"{slug}.json", definition)
    orchestrate.set_state(run_id, orchestrate.SYNTHESIS_RUNNING, segment=slug,
                          message="starting synthesis…", error=None)
    threading.Thread(target=_run_strategy_synthesis, args=(run_id,), daemon=True).start()
    return orchestrate.public(orchestrate.load_run(run_id))


def _run_strategy_synthesis(run_id: str) -> None:
    run = orchestrate.load_run(run_id)
    if not run:
        return
    seg = run["segment"]
    job = _new_job("strategy", segment=seg, run_id=run_id)
    orchestrate.update_run(run_id, job_id=job["id"])
    progress = _strategy_progress(run_id, job["id"])

    def fail(msg: str) -> None:
        orchestrate.set_state(run_id, orchestrate.ERROR, error=msg, message="synthesis failed")
        _update_job(job["id"], state="error", error=msg)

    try:
        progress("building the Deep Research prompt…")
        prompt_info = _segment_prompt(seg)
        date = prompt_info["date"]
        orchestrate.update_run(run_id, date=date)
        stem = f"{seg}-{date}"

        if (DEEP_DIR / f"{stem}.md").exists():
            progress("reusing the existing Deep Research report (no quota spent)…")
        else:
            try:
                sub = _start_deep_research({
                    "segment": seg, "date": date,
                    "prompt": prompt_info["prompt"], "window_mode": "offscreen",
                })
            except RuntimeError as exc:
                return fail(str(exc))
            sub_id = sub["id"]
            while True:
                time.sleep(3)
                pub = jobs.get_public(sub_id)
                if not pub:
                    return fail("Deep Research job vanished")
                if pub.get("message"):
                    progress(pub["message"])
                state = pub.get("state")
                if state == "done":
                    break
                if state == "needs_login":
                    orchestrate.set_state(
                        run_id, orchestrate.NEEDS_LOGIN,
                        message="Perplexity login required. Set it up, then resume the run.")
                    _update_job(job["id"], state="done", message="paused for login")
                    return
                if state in ("error", "cancelled"):
                    return fail(pub.get("error") or f"Deep Research {state}")

        progress("pulling deterministic segment data…")
        try:
            with _PULL_LOCK:
                research_pull.pull_segment(seg)
        except Exception as exc:  # noqa: BLE001 - deterministic data is best-effort
            progress(f"deterministic pull skipped: {exc}")

        progress("running the review gate…")
        review = review_deep_research.review(seg, date, write=True)
        progress("synthesizing target bands…")
        proposal = target_construct.construct(seg, date, review, progress=progress)
        progress("computing the rebalance preview…")
        preview = _preview_plan_for_proposal(proposal)

        orchestrate.set_state(
            run_id, orchestrate.AWAITING_PROPOSAL,
            proposal=proposal, preview=preview,
            review={
                "findings": review.get("findings"),
                "blocked_symbols": review.get("blocked_symbols"),
                "source_summary": review.get("source_summary"),
                "review_path": review.get("review_path"),
            },
            message=f"Review {len(proposal.get('changes') or [])} proposed target change(s), then approve.")
        _update_job(job["id"], state="done", message="synthesis complete")
    except SystemExit as exc:
        fail(str(exc) or "missing report for this segment + date")
    except Exception as exc:  # noqa: BLE001
        fail(f"{type(exc).__name__}: {exc}")


def _approve_strategy_proposal(run_id: str, changes, *, allow_blocked: bool = False) -> dict:
    run = orchestrate.load_run(run_id)
    if not run:
        raise ValueError(f"unknown strategy run {run_id}")
    if run.get("state") != orchestrate.AWAITING_PROPOSAL:
        raise _Conflict(f"run {run_id} is not awaiting proposal approval")
    seg, date = run.get("segment"), run.get("date")
    # Persist any edits made at the gate back into the proposal file the apply
    # step reads, so what the user approved is exactly what gets applied.
    if changes is not None:
        ppath = DEEP_DIR / f"{seg}-{date}.target-proposal.json"
        proposal = _load(ppath) or (run.get("proposal") or {})
        proposal["changes"] = changes
        _write_json(ppath, proposal)
    orchestrate.set_state(run_id, orchestrate.APPLYING, message="applying target changes…")
    try:
        applied = _apply_target_proposal(seg, date, True, allow_blocked=allow_blocked)
    except Exception as exc:  # noqa: BLE001
        orchestrate.set_state(run_id, orchestrate.ERROR,
                              error=f"{type(exc).__name__}: {exc}", message="apply failed")
        raise
    # The committed model now drives the real recommendation (no further changes).
    final = _preview_plan_for_proposal({"changes": [], "blocked_symbols": []})
    orchestrate.set_state(
        run_id, orchestrate.DONE, applied=applied, preview=final,
        message=f"Applied {len(applied.get('applied') or [])} change(s). Rebalance recommendation ready.")
    return orchestrate.public(orchestrate.load_run(run_id))


# Generous ceiling for JSON POST bodies (Deep Research reports run to a few
# hundred KB); anything bigger is a bug or abuse, not a legitimate request.
_MAX_BODY_BYTES = 5 * 1024 * 1024


# --------------------------------------------------------------------------- #
# Declarative API route tables. Each entry maps a path to a Handler method name;
# do_GET/do_POST resolve an exact match first, then the longest matching prefix.
# Adding an endpoint is a one-line table edit plus a handler method, instead of
# growing a 100-line if/elif chain. Prefix handlers read their own tail (symbol,
# segment, stem) off the path exactly as the old inline branches did.
# --------------------------------------------------------------------------- #
_GET_EXACT = {
    "/api/dev/livereload": "_get_livereload",
    "/api/holdings": "_get_holdings",
    "/api/portfolio-history": "_get_portfolio_history",
    "/api/ibkr/status": "_get_ibkr_status",
    "/api/rebalance": "_get_rebalance",
    "/api/risk": "_get_risk",
    "/api/journal": "_get_journal",
    "/api/segments": "_get_segments",
    "/api/peer-stats": "_get_peer_stats",
    "/api/deep-runs": "_get_deep_runs",
    "/api/error-log": "_get_error_log",
    "/api/tickers": "_get_tickers",
    "/api/ticker-index": "_get_ticker_index",
    "/api/analysis-config": "_get_analysis_config",
    "/api/setup/status": "_get_setup_status",
    "/api/analysis-models": "_get_analysis_models",
    "/api/trade/status": "_get_trade_status",
    "/api/trade/orders": "_get_trade_orders",
    "/api/deep-research/login-status": "_get_login_status",
    "/api/deep-job": "_get_deep_job",
    "/api/jobs": "_get_jobs",
    "/api/deep-prompt": "_get_deep_prompt",
    "/api/deep-qa": "_get_deep_qa",
    "/api/target-model": "_get_target_model",
    "/api/symbol-aliases": "_get_symbol_aliases",
    "/api/symbol-search": "_get_symbol_search",
    "/api/strategy/runs": "_get_strategy_runs",
}
_GET_PREFIX = [
    ("/api/strategy/", "_get_strategy"),
    ("/api/segment-def/", "_get_segment_def"),
    ("/api/deep-run/", "_get_deep_run"),
    ("/api/research/", "_get_research"),
    ("/api/analysis/", "_get_analysis"),
    ("/api/qa/", "_get_qa"),
    ("/api/history/", "_get_history"),
    ("/api/price-history/", "_get_price_history"),
    ("/api/segment/", "_get_segment"),
]
_POST_EXACT = {
    "/api/segment-draft": "_post_segment_draft",
    "/api/strategy/start": "_post_strategy_start",
    "/api/holdings/sync": "_post_holdings_sync",
    "/api/portfolio-history/sync": "_post_portfolio_history_sync",
    "/api/portfolio-history/sectors": "_post_portfolio_history_sectors",
    "/api/site/regenerate": "_post_site_regenerate",
    "/api/deep-job/cancel": "_post_deep_job_cancel",
    "/api/deep-run/delete": "_post_deep_run_delete",
    "/api/deep-qa": "_post_deep_qa",
    "/api/error-log": "_post_error_log",
    "/api/analysis-config": "_post_analysis_config",
    "/api/setup/check": "_post_setup_check",
    "/api/setup/ibkr": "_post_setup_ibkr",
    "/api/deep-research/save": "_post_deep_save",
    "/api/deep-research/run": "_post_deep_run",
    "/api/deep-research/login": "_post_deep_login",
    "/api/deep-research/import": "_post_deep_import",
    "/api/deep-research/verify-login": "_post_verify_login",
    "/api/deep-research/review": "_post_review",
    "/api/target-proposal/apply": "_post_proposal_apply",
    "/api/history/delete": "_post_history_delete",
    "/api/tax-plan": "_post_tax_plan",
    "/api/whatif": "_post_whatif",
    "/api/trade/preview": "_post_trade_preview",
    "/api/trade/place": "_post_trade_place",
    "/api/trade/cancel": "_post_trade_cancel",
    "/api/journal": "_post_journal",
    "/api/journal/outcome": "_post_journal_outcome",
    "/api/symbol-alias": "_post_symbol_alias",
    "/api/symbol-candidates": "_post_symbol_candidates",
}
_POST_PREFIX = [
    ("/api/strategy/", "_post_strategy_action"),
    ("/api/segment-def/", "_post_segment_def"),
    ("/api/analyze/", "_post_analyze"),
    ("/api/qa/", "_post_qa"),
    ("/api/pull-segment/", "_post_pull_segment"),
    ("/api/pull/", "_post_pull"),
    ("/api/thesis/", "_post_thesis"),
]
# Match the most specific (longest) prefix first so e.g. /api/pull-segment/ wins
# over /api/pull/ regardless of table order.
_GET_PREFIX.sort(key=lambda kv: -len(kv[0]))
_POST_PREFIX.sort(key=lambda kv: -len(kv[0]))


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

    def _dispatch(self, run):
        """Invoke a route handler, mapping our HTTP-error vocabulary to a JSON
        error envelope so individual handlers don't each repeat the try/except.
        An _HttpError carries its own status; a bare ValueError is the common
        validation case (400). Anything else propagates to _handle_unexpected."""
        try:
            return run()
        except _HttpError as exc:
            return self._send_error_json(exc.status, str(exc))
        except ValueError as exc:
            return self._send_error_json(400, str(exc))

    def _handle_unexpected(self, exc: Exception):
        # Single funnel for unexpected handler failures (GET and POST): log the
        # full traceback to the terminal so we can actually debug, but hand the
        # browser a clean JSON envelope the frontend's error center understands.
        if isinstance(exc, research_pull.ProviderError):  # type: ignore[attr-defined]
            # An upstream data source (Yahoo/SEC/FMP) misbehaved -- that's an
            # expected external hiccup (and "ticker not found" lives here too),
            # not one of our incidents, so it stays out of the error log.
            return self._send_error_json(502, f"data source error: {exc}")
        sys.stderr.write(f"[serve] unhandled error on {self.command} {self.path}:\n")
        traceback.print_exc()
        errorlog.error("server", f"{type(exc).__name__}: {exc}",
                       request=f"{self.command} {self.path}")
        return self._send_error_json(500, f"{type(exc).__name__}: {exc}")

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
            # Prefer the Vite build (web/dist) when it exists; fall back to raw
            # web/ source otherwise. The entry is TypeScript (web/src/main.ts),
            # so the raw fallback only fully works via `npm run dev`; serving
            # the SPA directly from this server requires `npm run build`.
            base = WEB_DIST if (WEB_DIST / "index.html").is_file() else WEB_DIR
            target = (base / clean).resolve()
            allowed_root = base
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
        if length > _MAX_BODY_BYTES:
            raise _BadRequest(f"request body too large ({length} bytes; max {_MAX_BODY_BYTES})")
        if not length:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            # A silent {} here used to turn client bugs into confusing downstream
            # "missing field" behavior; fail the request loudly instead.
            raise _BadRequest(f"malformed JSON body: {exc}") from exc
        if not isinstance(data, dict):
            raise _BadRequest("JSON body must be an object")
        return data

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
        try:
            if not path.startswith("/api/"):
                return self._serve_static(path)
            return self._handle_get_api(path, parse_qs(parsed.query))
        except Exception as exc:  # noqa: BLE001
            return self._handle_unexpected(exc)

    def _handle_get_api(self, path: str, query: dict[str, list[str]]):
        name = _GET_EXACT.get(path)
        if name is None:
            for prefix, handler in _GET_PREFIX:
                if path.startswith(prefix):
                    name = handler
                    break
        if name is None:
            return self._send_error_json(404, "unknown endpoint")
        return self._dispatch(lambda: getattr(self, name)(path, query))

    # ---- GET handlers (one per _GET_EXACT / _GET_PREFIX entry) -------------
    def _get_livereload(self, path, query):
        return self._send_json({"enabled": _RELOAD, "version": _assets_version()})

    def _get_holdings(self, path, query):
        return self._send_json(holdings_payload())

    def _get_portfolio_history(self, path, query):
        payload = _history_payload()
        if not payload:
            return self._send_error_json(404, "no portfolio history yet — pull it from IBKR (History tab)")
        return self._send_json(payload)

    def _get_ibkr_status(self, path, query):
        # Token-free credential status (see _ibkr_status); the History tab reads
        # history_configured to guide setup before a pull is attempted.
        return self._send_json(_ibkr_status())

    def _get_error_log(self, path, query):
        try:
            limit = int((query.get("limit") or ["200"])[0])
        except ValueError:
            limit = 200
        return self._send_json({"entries": errorlog.recent(max(1, min(limit, errorlog.MAX_ENTRIES)))})

    def _get_deep_qa(self, path, query):
        stem = (query.get("stem") or [""])[0]
        return self._send_json(_load_deep_qa(stem))

    def _get_rebalance(self, path, query):
        model = _load(TARGET_MODEL_JSON)
        holdings = _load(HOLDINGS_JSON)
        if not model:
            return self._send_error_json(404, "no target model — data/target-model.json missing")
        if not holdings:
            return self._send_error_json(404, "no holdings snapshot — sync from IBKR first")
        plan = tax_lots.enrich_plan(rebalance.plan(model, holdings), holdings)
        _attach_research_overlay(plan)
        return self._send_json(plan)

    def _get_risk(self, path, query):
        holdings = _load(HOLDINGS_JSON)
        if not holdings:
            return self._send_error_json(404, "no holdings snapshot — sync from IBKR first")
        rng_key = (query.get("range") or ["1y"])[0].lower()
        rng = rng_key if rng_key in PRICE_HISTORY_RANGES else "1y"
        with _PULL_LOCK:
            return self._send_json(risk.risk_report(holdings, rng=rng))

    def _get_journal(self, path, query):
        entries = journal.load_entries()
        price_map = journal.price_map_from_holdings(_load(HOLDINGS_JSON))
        return self._send_json({
            "entries": list(reversed(entries)),  # newest first for the UI
            "calibration": journal.calibrate(entries, price_map),
            "actions": sorted(journal.ACTIONS),
        })

    def _get_segments(self, path, query):
        return self._send_json({"segments": _segments_list()})

    def _get_peer_stats(self, path, query):
        sym = (query.get("symbol") or [""])[0]
        return self._send_json(_peer_stats(_resolve_symbol(sym)))

    def _get_strategy_runs(self, path, query):
        return self._send_json({"runs": orchestrate.list_runs()})

    def _get_strategy(self, path, query):
        run_id = path.rsplit("/", 1)[-1]
        run = orchestrate.load_run(run_id)
        if not run:
            return self._send_error_json(404, f"unknown strategy run {run_id}")
        # A run left "running" by a previous server process has a dead worker;
        # fail it now so the client stops polling a spinner that will never move.
        run = orchestrate.reap_if_orphaned(run)
        job = jobs.get_public(run["job_id"]) if run.get("job_id") else None
        return self._send_json(orchestrate.public(run, job=job))

    def _get_segment_def(self, path, query):
        name = path.rsplit("/", 1)[-1].lower()
        rec = _load(_segment_path(name))
        return self._send_json(rec) if rec else self._send_error_json(404, f"unknown segment {name}")

    def _get_deep_runs(self, path, query):
        return self._send_json({"runs": _deep_runs()})

    def _get_tickers(self, path, query):
        return self._send_json({"tickers": _known_tickers()})

    def _get_ticker_index(self, path, query):
        return self._send_json({"tickers": _ticker_index()})

    def _get_analysis_config(self, path, query):
        return self._send_json({
            "config": ticker_analysis.load_config(),
            "available": ticker_analysis.available_backends(),
            "labels": ticker_analysis.PROVIDER_LABELS,
        })

    def _get_setup_status(self, path, query):
        return self._send_json(_setup_status())

    def _get_analysis_models(self, path, query):
        force = (query.get("refresh") or ["0"])[0] in ("1", "true")
        return self._send_json({"models": ticker_analysis.provider_models(force=force)})

    def _get_trade_status(self, path, query):
        # Never errors: a down/locked gateway is a normal state the UI renders.
        return self._send_json(_trade_status())

    def _get_trade_orders(self, path, query):
        return self._send_json(_trade_orders())

    def _get_login_status(self, path, query):
        return self._send_json(_get_auth_state())

    def _get_deep_job(self, path, query):
        job_id = (query.get("id") or [""])[0]
        pub = jobs.get_public(job_id)
        if not pub:
            return self._send_error_json(404, f"unknown job {job_id}")
        return self._send_json(pub)

    def _get_jobs(self, path, query):
        # Central Task Center feed: every known job, newest first. Capped so a
        # long-lived dev session can't make the payload unbounded -- finished
        # jobs are never evicted from the in-memory registry.
        return self._send_json({"jobs": jobs.list_public()[:JOBS_LIST_LIMIT]})

    def _get_deep_prompt(self, path, query):
        ticker = (query.get("ticker") or [""])[0].strip()
        name = (query.get("segment") or [""])[0]
        if ticker:
            return self._send_json(_ticker_deep_prompt(ticker))
        return self._send_json(_segment_prompt(name))

    def _get_deep_run(self, path, query):
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

    def _get_target_model(self, path, query):
        rec = _load(TARGET_MODEL_JSON)
        return self._send_json(rec) if rec else self._send_error_json(404, "target model not found")

    def _get_symbol_aliases(self, path, query):
        return self._send_json({"aliases": _symbol_aliases()})

    def _get_research(self, path, query):
        sym = _safe_symbol(unquote(path.rsplit("/", 1)[-1]))
        provider_sym = _resolve_symbol(sym)
        rec = _load(RESEARCH_DIR / f"{provider_sym}.json")
        return self._send_json(_annotate_symbol_record(rec, sym, provider_sym)) if rec else self._send_error_json(404, f"no cached research for {sym}")

    def _get_analysis(self, path, query):
        sym = _safe_symbol(unquote(path.rsplit("/", 1)[-1]))
        rec = _latest_analysis(_resolve_symbol(sym))
        return self._send_json(rec) if rec else self._send_error_json(404, f"no analysis for {sym}")

    def _get_qa(self, path, query):
        sym = _safe_symbol(unquote(path.rsplit("/", 1)[-1]))
        return self._send_json(_load_qa(_resolve_symbol(sym)))

    def _get_history(self, path, query):
        sym = _safe_symbol(unquote(path.rsplit("/", 1)[-1]))
        provider_sym = _resolve_symbol(sym)
        return self._send_json({"symbol": sym, "provider_symbol": provider_sym, "history": research_pull.history_for(provider_sym)})

    def _get_price_history(self, path, query):
        sym = _safe_symbol(unquote(path.rsplit("/", 1)[-1]))
        provider_sym = _resolve_symbol(sym)
        rng_key = (query.get("range") or ["1y"])[0].lower()
        rng, interval = PRICE_HISTORY_RANGES.get(rng_key, PRICE_HISTORY_RANGES["1y"])
        try:
            result = yahoo.chart(provider_sym, rng=rng, interval=interval)
            ph = yahoo.price_history_from_chart(result, rng=rng, interval=interval)
        except Exception as exc:  # noqa: BLE001 - surface provider failure to UI
            return self._send_error_json(502, f"price history failed for {sym}: {exc}")
        if not ph:
            return self._send_error_json(404, f"no price history for {sym}")
        return self._send_json(ph)

    def _get_segment(self, path, query):
        name = path.rsplit("/", 1)[-1].lower()
        rec = _load(SEGMENT_OUT_DIR / f"{name}.json")
        return self._send_json(rec) if rec else self._send_error_json(404, f"no cached segment {name}")

    def _get_symbol_search(self, path, query):
        q = (query.get("q") or [""])[0]
        return self._send_json(_symbol_search(q))

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            return self._handle_post_api(path)
        except Exception as exc:  # noqa: BLE001
            return self._handle_unexpected(exc)

    def _handle_post_api(self, path: str):
        name = _POST_EXACT.get(path)
        if name is None:
            for prefix, handler in _POST_PREFIX:
                if path.startswith(prefix):
                    name = handler
                    break
        if name is None:
            return self._send_error_json(404, "unknown endpoint")
        return self._dispatch(lambda: getattr(self, name)(path))

    # ---- POST handlers (one per _POST_EXACT / _POST_PREFIX entry) ----------
    def _post_segment_draft(self, path):
        body = self._read_body()
        return self._send_json(_start_segment_draft(str(body.get("query") or "")))

    def _post_strategy_start(self, path):
        body = self._read_body()
        return self._send_json(_start_strategy(str(body.get("direction") or "")))

    def _post_strategy_action(self, path):
        # /api/strategy/{run_id}/{action}
        parts = path.strip("/").split("/")
        if len(parts) != 4:
            return self._send_error_json(404, "unknown strategy action")
        run_id, action = parts[2], parts[3]
        body = self._read_body()
        if action == "approve-segment":
            return self._send_json(_approve_strategy_segment(run_id, body.get("definition")))
        if action == "approve-proposal":
            return self._send_json(_approve_strategy_proposal(
                run_id, body.get("changes"), allow_blocked=bool(body.get("allow_blocked"))))
        return self._send_error_json(404, f"unknown strategy action {action}")

    def _post_segment_def(self, path):
        name = _slugify(path.rsplit("/", 1)[-1])
        body = self._read_body()
        definition = _validate_segment_definition(body.get("definition") or body)
        _write_json(SEGMENT_DEF_DIR / f"{name}.json", definition)
        return self._send_json({"name": name, "definition": definition, "segments": _segments_list()})

    def _post_holdings_sync(self, path):
        return self._send_json(_start_holdings_sync())

    def _post_portfolio_history_sync(self, path):
        full = bool(self._read_body().get("full"))
        return self._send_json(_start_history_sync(full=full))

    def _post_portfolio_history_sectors(self, path):
        return self._send_json(_start_sectors_sync())

    def _post_deep_qa(self, path):
        body = self._read_body()
        stem = _slugify(str(body.get("stem") or ""))
        if body.get("clear"):
            _write_json(_deep_qa_path(stem), {"stem": stem, "turns": []})
            return self._send_json(_load_deep_qa(stem))
        if "delete" in body:
            thread = _load_deep_qa(stem)
            if _drop_qa_exchange(thread, body.get("delete")):
                _write_json(_deep_qa_path(stem), thread)
            return self._send_json(_load_deep_qa(stem))
        return self._send_json(_start_deep_qa(stem, str(body.get("question") or "")))

    def _post_deep_run_delete(self, path):
        body = self._read_body()
        return self._send_json(_delete_deep_run(str(body.get("stem") or "")))

    def _post_error_log(self, path):
        body = self._read_body()
        if body.get("clear"):
            errorlog.clear()
        return self._send_json({"entries": errorlog.recent()})

    def _post_site_regenerate(self, path):
        res = _regenerate_site()
        if not res.get("ok"):
            return self._send_error_json(400, res.get("error") or "regeneration failed")
        return self._send_json(res)

    def _post_analyze(self, path):
        sym = _safe_symbol(unquote(path.rsplit("/", 1)[-1]))
        body = self._read_body()
        return self._send_json(_start_analysis(_resolve_symbol(sym), bool(body.get("refresh"))))

    def _post_qa(self, path):
        sym = _safe_symbol(unquote(path.rsplit("/", 1)[-1]))
        provider_sym = _resolve_symbol(sym)
        body = self._read_body()
        if body.get("clear"):
            _write_json(_qa_path(provider_sym), {"symbol": provider_sym, "turns": []})
            return self._send_json(_load_qa(provider_sym))
        if "delete" in body:
            thread = _load_qa(provider_sym)
            if _drop_qa_exchange(thread, body.get("delete")):
                _write_json(_qa_path(provider_sym), thread)
            return self._send_json(_load_qa(provider_sym))
        return self._send_json(_start_qa(provider_sym, str(body.get("question") or "")))

    def _post_deep_job_cancel(self, path):
        body = self._read_body()
        job_id = str(body.get("id") or "").strip()
        if not job_id:
            return self._send_error_json(400, "missing job id")
        ok = jobs.cancel_job(job_id)
        return self._send_json({"id": job_id, "cancelled": ok})

    def _post_analysis_config(self, path):
        body = self._read_body()
        return self._send_json({
            "config": ticker_analysis.save_config(body.get("config") or body),
            "available": ticker_analysis.available_backends(),
            "labels": ticker_analysis.PROVIDER_LABELS,
        })

    def _post_setup_check(self, path):
        return self._send_json(_setup_status(run_checks=True))

    def _post_setup_ibkr(self, path):
        body = self._read_body()
        try:
            return self._send_json(_save_ibkr_secrets(body))
        except ValueError as exc:
            return self._send_error_json(400, str(exc))
        except OSError as exc:
            return self._send_error_json(500, f"could not write secrets: {exc}")

    def _post_deep_save(self, path):
        body = self._read_body()
        return self._send_json(_save_deep_artifact(body))

    def _post_deep_run(self, path):
        body = self._read_body()
        return self._send_json(_start_deep_research(body))

    def _post_deep_login(self, path):
        return self._send_json(_start_login())

    def _post_deep_import(self, path):
        body = self._read_body()
        return self._send_json(_start_import(body))

    def _post_verify_login(self, path):
        return self._send_json(_verify_login())

    def _post_review(self, path):
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

    def _post_proposal_apply(self, path):
        body = self._read_body()
        return self._send_json(_apply_target_proposal(
            str(body.get("segment") or ""),
            str(body.get("date") or ""),
            bool(body.get("confirm")),
            allow_blocked=bool(body.get("allow_blocked")),
        ))

    def _post_history_delete(self, path):
        body = self._read_body()
        sym = _safe_symbol(str(body.get("symbol") or ""))
        provider_sym = _resolve_symbol(sym)
        removed = research_pull.delete_history(provider_sym, str(body.get("stamp") or ""))
        return self._send_json({
            "symbol": sym,
            "removed": removed,
            "history": research_pull.history_for(provider_sym),
        })

    def _post_tax_plan(self, path):
        body = self._read_body()
        holdings = _load(HOLDINGS_JSON)
        if not holdings:
            return self._send_error_json(404, "no holdings snapshot — sync from IBKR first")
        try:
            sym = _safe_symbol(str(body.get("symbol") or ""))
        except ValueError as exc:
            return self._send_error_json(400, str(exc))
        try:
            amount = float(body.get("amount_czk"))
        except (TypeError, ValueError):
            return self._send_error_json(400, "amount_czk must be a number")
        return self._send_json(tax_lots.breakdown_for_symbol(holdings, sym, amount))

    def _post_whatif(self, path):
        body = self._read_body()
        holdings = _load(HOLDINGS_JSON)
        model = _load(TARGET_MODEL_JSON)
        if not holdings or not model:
            return self._send_error_json(404, "need both a holdings snapshot and a target model")
        return self._send_json(whatif.simulate(holdings, model, body.get("trades")))

    def _post_trade_preview(self, path):
        return self._send_json(_trade_preview(self._read_body()))

    def _post_trade_place(self, path):
        return self._send_json(_trade_place(self._read_body()))

    def _post_trade_cancel(self, path):
        return self._send_json(_trade_cancel(self._read_body()))

    def _post_journal(self, path):
        body = self._read_body()
        journal.add_entry(body)
        entries = journal.load_entries()
        price_map = journal.price_map_from_holdings(_load(HOLDINGS_JSON))
        return self._send_json({
            "entries": list(reversed(entries)),
            "calibration": journal.calibrate(entries, price_map),
            "actions": sorted(journal.ACTIONS),
        })

    def _post_journal_outcome(self, path):
        body = self._read_body()
        journal.record_outcome(str(body.get("id") or ""), body.get("price"), str(body.get("note") or ""))
        entries = journal.load_entries()
        price_map = journal.price_map_from_holdings(_load(HOLDINGS_JSON))
        return self._send_json({
            "entries": list(reversed(entries)),
            "calibration": journal.calibrate(entries, price_map),
            "actions": sorted(journal.ACTIONS),
        })

    def _post_symbol_alias(self, path):
        body = self._read_body()
        return self._send_json(_save_symbol_alias(body))

    def _post_symbol_candidates(self, path):
        body = self._read_body()
        return self._send_json(_symbol_candidates(body))

    def _post_pull(self, path):
        try:
            sym = _safe_symbol(unquote(path.rsplit("/", 1)[-1]))
        except ValueError:
            return self._send_error_json(400, "bad symbol")
        provider_sym = _resolve_symbol(sym)
        with _PULL_LOCK:
            rec = research_pull.pull_ticker(provider_sym)
        return self._send_json(_annotate_symbol_record(rec, sym, provider_sym))

    def _post_pull_segment(self, path):
        name = path.rsplit("/", 1)[-1].lower()
        if not (SEGMENT_DEF_DIR / f"{name}.json").exists():
            return self._send_error_json(404, f"unknown segment {name}")
        with _PULL_LOCK:
            rec = research_pull.pull_segment(name)
        return self._send_json(rec)

    def _post_thesis(self, path):
        sym = _safe_symbol(unquote(path.rsplit("/", 1)[-1]))
        provider_sym = _resolve_symbol(sym)
        rec = _load(RESEARCH_DIR / f"{provider_sym}.json")
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
        (RESEARCH_DIR / f"{provider_sym}.json").write_text(
            json.dumps(rec, indent=2) + "\n", encoding="utf-8"
        )
        return self._send_json(_annotate_symbol_record(rec, sym, provider_sym))


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address; loopback only (the API has no auth and can write credentials)")
    parser.add_argument("--port", type=int, default=6060)
    parser.add_argument("--reload", action="store_true",
                        help="dev: auto-restart on tools/*.py edits and live-reload the browser on asset changes")
    args = parser.parse_args()

    # In --reload mode the first invocation is the supervisor; it re-launches
    # itself as a child (marked via env) that actually serves and self-restarts.
    if args.reload and os.environ.get("_REBAL_RELOAD_CHILD") != "1":
        return _run_reloader()

    # Hard refusal, not a warning: every endpoint is unauthenticated, several
    # write to disk (target model, IBKR credentials) or spawn browser/CLI jobs.
    # Exposing that beyond loopback is indistinguishable from a remote shell.
    if args.host not in ("127.0.0.1", "::1", "localhost"):
        print(f"ERROR: refusing to bind non-loopback host {args.host!r}.", file=sys.stderr)
        print("  This server has no authentication and can write credentials and", file=sys.stderr)
        print("  portfolio targets. Run it on 127.0.0.1 and use a tunnel if remote", file=sys.stderr)
        print("  access is genuinely needed.", file=sys.stderr)
        return 2

    global _RELOAD
    _RELOAD = args.reload

    _load_secrets_env()

    from portfolio import DATA_MISSING_HINT, data_initialized
    if not data_initialized():
        print("  WARNING: " + DATA_MISSING_HINT.rstrip().replace("\n", "\n  "))
        print("  The UI will load but holdings/target views will be empty until then.")

    if not (WEB_DIST / "index.html").is_file():
        print("  WARNING: web/dist/ is missing -- the console UI will NOT load from this server.")
        print("  The SPA entry is TypeScript and needs a build. Either:")
        print("    npm run build   (then this server serves web/dist)")
        print("    npm run dev     (Vite dev server on :5173, proxying API calls here)")

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
    from config import ROOT_SECRETS
    for key, value in _read_env_file(ROOT_SECRETS).items():
        os.environ.setdefault(key, value)


if __name__ == "__main__":
    raise SystemExit(main())
