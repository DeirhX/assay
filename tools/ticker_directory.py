#!/usr/bin/env python3
"""The ticker directory: which symbols this workbench actually knows about.

Three related answers, all derived from what's on disk (dossiers, holdings,
segment definitions, saved analyses) rather than any external universe:

* ``known_tickers()`` -- the curated symbol set the UI may auto-link. A small,
  relevant set beats the full US/EU universe, which collides badly with English
  words (NOW, ON, ALL, IT...).
* ``ticker_index()`` -- every ticker we have material on, with timestamps and
  the latest analysis verdict, for the "jump back to a ticker" list.
* ``ticker_deep_prompt()`` -- the single-name Deep Research prompt builder (the
  expensive, on-demand counterpart to the cheap per-ticker CLI analysis).

Extracted from serve.py so the server owns routing and this module owns the
domain logic; nothing here touches HTTP.
"""

from __future__ import annotations

import datetime as dt
import re

import instruments
from analysis_jobs import latest_analysis
from config import ANALYSIS_DIR, HOLDINGS_JSON, RESEARCH_DIR, SEGMENT_DEF_DIR, SEGMENT_OUT_DIR
from portfolio import holdings_weights
from segments_service import freshness_directive
from store import load as _load

# What a plausible exchange ticker looks like: leading letter, short, upper.
TICKER_SHAPE = re.compile(r"^[A-Z][A-Z0-9.]{0,5}$")


def known_tickers() -> list[str]:
    """Curated universe of symbols we actually know about: pulled research
    dossiers, held positions, and segment members. The UI uses this to decide
    which bare uppercase tokens in a report are safe to turn into deep-dive
    links."""
    syms: set[str] = set()

    def add(value) -> None:
        if isinstance(value, dict):
            value = value.get("symbol")
        if not value:
            return
        s = str(value).strip().upper()
        if TICKER_SHAPE.match(s):
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


def verdict_line(report: str) -> str | None:
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


def ticker_index() -> list[dict]:
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
            latest = latest_analysis(row["symbol"])
            if latest:
                vl = verdict_line(latest.get("report") or "")
                if vl:
                    row["verdict"] = vl
    return sorted(out.values(), key=lambda r: r["symbol"])


def ticker_deep_prompt(symbol: str) -> dict:
    """Single-name Deep Research prompt: the expensive, on-demand counterpart to
    the cheap per-ticker CLI analysis. Subject is one company, not a segment, so
    the stem is namespaced ``ticker-<sym>`` and it reuses the same Perplexity
    run/save/Q&A machinery without polluting the segment list. The FORMAT block
    mirrors the segment prompt so the scraper's JSON guard and citation handling
    behave identically."""
    sym = (symbol or "").strip().upper()
    if not TICKER_SHAPE.match(sym):
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
        "End with a clear portfolio stance — accumulate, hold, trim, or avoid "
        "— and the specific evidence that would flip that stance.\n"
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
    prompt += freshness_directive(today)
    if weight is not None:
        prompt += (
            f"\nFor context only (do not let it bias your conclusion), I currently "
            f"hold {sym} at {weight:.2f}% of my invested book.\n"
        )
    return {"segment": f"ticker-{sym.lower()}", "symbol": sym, "date": today, "prompt": prompt}
