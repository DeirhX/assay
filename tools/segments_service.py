#!/usr/bin/env python3
"""Research segments: definition validation, drafting, listing, and prompts.

Extracted from serve.py. A "segment" is a named research lens (a title plus a
member ticker list, stored under data/segments/def/<slug>.json). This module
owns: validating/normalizing a definition, drafting one from a freeform theme
(keyword baseline + optional LLM-proposed tickers, run as a background job),
listing the saved segments with cache freshness, and building the Deep Research
prompt for a segment. Pure data + job machinery -- no HTTP.

freshness_directive lives here because both the segment prompt and serve's
single-ticker deep prompt share it; serve re-imports it.

Public names are underscore-free; serve.py imports them aliased to its existing
private call-site names.
"""

from __future__ import annotations

import datetime as dt
import re
import threading
from pathlib import Path

import jobs
import ticker_analysis
from config import SEGMENT_DEF_DIR, SEGMENT_OUT_DIR
from jobs import new_job, public, update_job
from portfolio import holdings_weights
from store import load, safe_symbol, slugify


def segment_path(name: str) -> Path:
    return SEGMENT_DEF_DIR / f"{slugify(name)}.json"


def validate_definition(raw: dict) -> dict:
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
        sym = safe_symbol(str(item.get("symbol") or ""))
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


def draft_segment(query: str) -> dict:
    query = (query or "").strip()
    if not query:
        raise ValueError("query is required")
    slug = slugify(query)
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


def merge_draft_members(baseline: list[dict], extra: list[dict]) -> list[dict]:
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
                sym = safe_symbol(str(item.get("symbol") or ""))
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


def run_draft_job(job_id: str, query: str) -> None:
    """Draft a research segment for any theme: start from the keyword baseline,
    then (if an analysis CLI is available) ask the LLM to propose real tickers
    for subjects we don't already hold, and merge the two."""
    def progress(msg: str) -> None:
        update_job(job_id, message=msg)

    try:
        baseline = draft_segment(query)
        definition = baseline["definition"]
        members = list(definition.get("members") or [])
        warnings: list[str] = []
        backend_label = None

        if any(ticker_analysis.available_backends().values()):
            update_job(job_id, state="running",
                       message="researching candidate tickers…")
            llm = ticker_analysis.draft_segment_members(
                query, progress=progress, cancel=lambda: jobs.is_cancelled(job_id))
            if jobs.is_cancelled(job_id):
                update_job(job_id, state="cancelled", message="cancelled")
                return
            if llm.get("ok"):
                members = merge_draft_members(members, llm.get("members") or [])
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
        update_job(job_id, state="error", error=f"{type(exc).__name__}: {exc}")
        return

    if not members:
        warnings.append(
            "No candidate tickers identified. Use the prompt below with an LLM, or "
            "paste/edit members, then validate before approving."
        )
    definition["members"] = members
    definition["sleeves"] = sorted({m["sleeve"] for m in members}) or ["other"]
    update_job(
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


def start_draft(query: str) -> dict:
    query = (query or "").strip()
    if not query:
        raise ValueError("query is required")
    # Like ticker analysis, drafting shells out to a CLI but not the browser, so
    # it does not take the single browser slot and can run alongside other work.
    job = new_job("segment_draft", query=query)
    threading.Thread(target=run_draft_job,
                     args=(job["id"], query), daemon=True).start()
    return public(job)


def freshness_directive(today: str) -> str:
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


def segment_prompt(name: str) -> dict:
    slug = slugify(name)
    definition = load(SEGMENT_DEF_DIR / f"{slug}.json")
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
    prompt += freshness_directive(today)
    if held_lines:
        prompt += (
            "\nFor context only (do not bias coverage toward these), I currently own:\n"
            + "\n".join(held_lines) + "\n"
        )
    return {"segment": slug, "date": today, "prompt": prompt}


def segments_list() -> list[dict]:
    out = []
    for path in sorted(SEGMENT_DEF_DIR.glob("*.json")):
        definition = load(path) or {}
        # "cached" == a deterministic ticker-data pull exists for this segment.
        # Surface its as_of so the UI can show freshness instead of a bare flag.
        cache_path = SEGMENT_OUT_DIR / path.name
        cached_at = None
        if cache_path.exists():
            cached_at = (load(cache_path) or {}).get("as_of")
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
