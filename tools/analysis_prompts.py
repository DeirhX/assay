#!/usr/bin/env python3
"""Prompt construction for the on-demand analysis layer.

Pure, side-effect-free string builders that turn the deterministic dossier (and
the follow-up conversation) into the exact text we hand a backend CLI. Nothing
here shells out, reads config, or touches the filesystem -- it is the "what to
ask" half of ticker_analysis, kept separate from the "how to run it" half so
the prompt wording is trivially testable in isolation.

Extracted from ticker_analysis.py; the runner re-imports these so its own call
sites (and callers reaching ticker_analysis.build_prompt / build_qa_prompt /
build_segment_draft_prompt / build_doc_qa_prompt) are unchanged.
"""

from __future__ import annotations

import json
from typing import Any


def _compact_record(rec: dict[str, Any]) -> dict[str, Any]:
    """A trimmed, model-facing view of the deterministic dossier: only the
    fields worth reasoning over, with the giant price_history array dropped."""
    profile = rec.get("profile") or {}
    summary = (profile.get("summary") or "")[:1800]
    metrics = {}
    for key, node in (rec.get("metrics") or {}).items():
        if isinstance(node, dict):
            metrics[key] = {"value": node.get("value"), "display": node.get("display"), "source": node.get("source")}
    port = rec.get("portfolio") or {}
    target = port.get("target") or {}
    return {
        "symbol": rec.get("symbol"),
        "name": rec.get("name"),
        "currency": rec.get("currency"),
        "price": (rec.get("price") or {}).get("value") if isinstance(rec.get("price"), dict) else rec.get("price"),
        "as_of": rec.get("as_of"),
        "business": {
            "summary": summary,
            "sector": profile.get("sector"),
            "industry": profile.get("industry"),
            "country": profile.get("country"),
            "employees": profile.get("employees"),
        },
        "metrics": metrics,
        "momentum": rec.get("momentum") or {},
        "cross_checks": [
            {"severity": c.get("severity"), "metric": c.get("metric"), "message": c.get("message")}
            for c in (rec.get("cross_checks") or [])
        ],
        "portfolio": {
            "current_weight_pct": port.get("current_weight_pct"),
            "status": port.get("status"),
            "gap_to_band_pct": port.get("gap_to_band_pct"),
            "target_rule": target.get("rule"),
            "target_low": target.get("low"),
            "target_high": target.get("high"),
            "target_note": target.get("note"),
        },
        "data_errors": rec.get("errors") or [],
    }


def _data_rule(allow_web: bool) -> str:
    """The grounding rule swaps depending on whether web tools are live."""
    if not allow_web:
        return ('- Use ONLY the numbers in the DATA block below. Do not invent figures or cite '
                'prices/multiples not present. If something important is missing, say "not in the '
                'data" rather than guessing.')
    return (
        "- Anchor all position math and the valuation multiples in the DATA block; never invent "
        "those figures. You MAY use your web search / fetch tools for fresher qualitative context "
        "the DATA lacks: recent news, the latest earnings/guidance, analyst actions, regulatory or "
        "competitive developments.\n"
        "- Every web-derived fact MUST be followed by its source URL in parentheses, and prefer "
        "primary sources (company IR, SEC/EDGAR filings) over aggregators. Date any time-sensitive "
        'claim. If a web claim can\'t be verified, drop it rather than guess.\n'
        "- Keep web findings clearly distinct from the deterministic DATA so the reader knows which "
        "is which."
    )


def _qa_data_rule(allow_web: bool) -> str:
    if not allow_web:
        return ('- Answer ONLY from the DATA block (use the conversation and analyst note for '
                'continuity, not as new facts). Do not invent figures. If something needed isn\'t '
                'present, say "not in the data".')
    return (
        "- Anchor figures in the DATA block (don't invent them) and use the conversation/analyst "
        "note for continuity. You MAY use your web search / fetch tools for fresher facts the DATA lacks "
        "(recent news, latest earnings/guidance, analyst actions).\n"
        "- Cite every web-derived fact with its source URL in parentheses, preferring primary "
        "sources; date time-sensitive claims. Drop anything you can't verify."
    )


def _sources_section(allow_web: bool) -> str:
    if not allow_web:
        return ""
    return ("\n\n## Sources\nBullet every web source you used as `[title](url) — what it backed up`. "
            'If you did not search, write exactly: "None — built only from the structured data snapshot below."')


def build_prompt(rec: dict[str, Any], *, allow_web: bool = False) -> str:
    sym = rec.get("symbol", "?")
    data = json.dumps(_compact_record(rec), indent=2, default=str)
    return f"""You are a skeptical, evidence-driven equity analyst writing an in-depth note on ${sym} for a self-directed investor who already holds a diversified portfolio. Your job is to improve the quality of their decision, not to cheerlead.

GROUND RULES
{_data_rule(allow_web)}
- Be concise and direct. No hype, no filler, no flattery. Prefer specifics over adjectives.
- Surface the bear case honestly and weight it against the bull case.
- Tag every company ticker with a leading $ on first mention (e.g. $AMD, $NVDA) so they can be auto-linked.
- The DATA already includes this position's weight vs its target band; make your verdict portfolio-aware (room to add vs trim pressure).
- If the deterministic data has cross-check warnings or errors, factor that uncertainty into your confidence.

OUTPUT (Markdown, use these exact section headings):
## Verdict
One line: a stance (Accumulate / Hold / Trim / Avoid) + a confidence (low/medium/high) + a one-sentence justification.

## What the business is
2-4 sentences: what they actually do and where the moat is (or isn't).

## Momentum read
What the 1m/3m/6m/12m moves and distance-from-52w-high imply. Is this strength or a falling knife?

## Valuation read
Interpret the multiples vs the growth. Priced for perfection, fair, or cheap? State which metric drives the call.

## Bull case
2-3 tight bullets.

## Bear case
2-3 tight bullets.

## What would change the thesis
2-3 concrete, observable triggers (numbers, events) that would flip your verdict.

## Price levels
Valuation-anchored, actionable triggers in the instrument's own trading currency — NOT forecasts. First give your fair-value estimate. Then give an optional buy ladder and trim ladder: each tranche is a price level and a size (the fraction of a full position move to act on at that level), so you scale in as the price falls and scale out as it rises. Express each tranche by margin vs fair value (`discount_pct` for buys, `premium_pct` for trims, as decimals — 0.20 = 20%) or by an absolute `price`. Sizes on each side should sum to ~1.0. Use 1–3 tranches per side as the thesis warrants; leave a side's ladder empty (`[]`) if you would not gate it, or set `fair_value` to null if you have no anchor. Emit EXACTLY one fenced JSON block in this shape and nothing else for this section:
```json
{{"fair_value": 420, "buy_ladder": [{{"discount_pct": 0.15, "size_pct": 0.5}}, {{"discount_pct": 0.30, "size_pct": 0.5}}], "trim_ladder": [{{"premium_pct": 0.25, "size_pct": 1.0}}]}}
```{_sources_section(allow_web)}

DATA
```json
{data}
```
"""


def _segment_web_rule(allow_web: bool) -> str:
    if allow_web:
        return (
            "- Use your web search / fetch tools to confirm each ticker is real and CURRENTLY "
            "listed, and to catch recent IPOs, de-listings, or symbol changes. Do not put "
            "citations or URLs in the JSON."
        )
    return (
        "- Use only tickers you are confident are real and currently listed. Do not guess at "
        "symbols; omit a name rather than invent a ticker."
    )


def build_segment_draft_prompt(query: str, *, allow_web: bool = False) -> str:
    """Prompt the backend to return a themed public-equity watchlist as JSON."""
    return f"""You are assembling a public-equity RESEARCH SEGMENT (a themed watchlist) for this theme:

"{query.strip()}"

Identify the most relevant PUBLICLY TRADED companies and ETFs for the theme and group them into 3-6 sleeves (sub-themes). Aim for 8-20 names: enough to be a real research universe, not so many it's noise. Cover the value chain, not just the obvious mega-caps.

GROUND RULES
{_segment_web_rule(allow_web)}
- The `symbol` field must be the real primary-listing ticker (US ticker or ADR where applicable), e.g. RKLB, LMT, BA. Never put a company name in the symbol field.
- Exclude private companies. If a key player is private, omit it (you may mention a public proxy in another member's rationale).
- `sleeve` values are short lowercase slugs, e.g. "launch", "satellites", "defense-prime".
- `confidence` is one of: high, medium, low.

OUTPUT
Respond with ONLY a single JSON object -- no markdown code fences, no commentary before or after. Exactly this shape:
{{
  "title": "<concise human title for the theme>",
  "comment": "<one sentence describing what this segment covers>",
  "sleeves": ["<sleeve-slug>", "..."],
  "members": [
    {{"symbol": "TICKER", "sleeve": "<sleeve-slug>", "rationale": "<why it belongs, one line>", "confidence": "high|medium|low"}}
  ]
}}
"""


def build_qa_prompt(rec: dict[str, Any], history: list[dict] | None,
                    question: str, note: str | None = None, *, allow_web: bool = False) -> str:
    """A follow-up Q&A prompt: same deterministic DATA as the note, plus the
    prior conversation (bounded) and, if present, the latest analyst note for
    continuity. Keeps the model grounded and the thread coherent."""
    sym = rec.get("symbol", "?")
    data = json.dumps(_compact_record(rec), indent=2, default=str)
    convo = ""
    # Keep the last ~6 exchanges so the prompt stays bounded as a thread grows;
    # long prior answers are truncated (full text still lives on disk).
    for t in [t for t in (history or []) if t.get("text")][-12:]:
        who = "Q" if t.get("role") == "user" else "A"
        txt = t["text"].strip()
        if who == "A" and len(txt) > 1500:
            txt = txt[:1500] + " …[truncated]"
        convo += f"{who}: {txt}\n\n"
    note_block = ""
    if note:
        note_block = "PRIOR ANALYST NOTE (context only; may be stale):\n" + note.strip()[:4000] + "\n\n"
    convo_block = ("CONVERSATION SO FAR:\n" + convo) if convo else ""
    return f"""You are a skeptical, evidence-driven equity analyst answering a follow-up question about ${sym} for a self-directed investor. Improve their decision; do not cheerlead.

GROUND RULES
{_qa_data_rule(allow_web)}
- Be concise and direct. Answer the specific question asked; skip boilerplate restatement of the whole thesis.
- Tag every company ticker with a leading $ on first mention (e.g. $AMD).
- If the data has cross-check warnings, factor that uncertainty into your answer.

{note_block}{convo_block}NEW QUESTION:
{question.strip()}

DATA
```json
{data}
```

Answer in Markdown.{' End with a "Sources" line listing any URLs you used.' if allow_web else ''}"""


def _qa_followup_text(question: str, allow_web: bool = False) -> str:
    """The minimal payload for a RESUMED Claude session: the DATA, ground rules
    and prior turns already live in the session, so we send only the question.
    That's the whole point -- the heavy prefix is served from the prompt cache."""
    web = (" You may use WebSearch/WebFetch for fresher facts; cite every web fact with its URL."
           if allow_web else
           ' If something needed is not there, say "not in the data".')
    return (f"{question.strip()}\n\n"
            "(Answer using the data and context already in this conversation." + web +
            " Be concise; tag tickers with a leading $.)")


def build_doc_qa_prompt(title: str, document: str, citations: list[dict] | None,
                        history: list[dict] | None, question: str, *,
                        allow_web: bool = False) -> str:
    """A follow-up Q&A prompt grounded in a Deep Research report (not a ticker
    DATA block). The report is the evidence base; the prior conversation keeps
    the thread coherent. The report and history are bounded so the prompt stays
    a sane size as both grow."""
    doc = (document or "").strip()
    if len(doc) > 16000:
        doc = doc[:16000] + "\n…[report truncated]"
    src_lines = []
    for c in (citations or [])[:40]:
        href = str(c.get("href") or "").strip()
        if not href:
            continue
        label = str(c.get("label") or "").split("\n")[0].strip()
        src_lines.append(f"- {label} {href}".strip())
    src_block = ("\nSOURCES (citations from the run):\n" + "\n".join(src_lines) + "\n") if src_lines else ""
    convo = ""
    for t in [t for t in (history or []) if t.get("text")][-12:]:
        who = "Q" if t.get("role") == "user" else "A"
        txt = t["text"].strip()
        if who == "A" and len(txt) > 1500:
            txt = txt[:1500] + " …[truncated]"
        convo += f"{who}: {txt}\n\n"
    convo_block = ("CONVERSATION SO FAR:\n" + convo) if convo else ""
    web_rule = _qa_data_rule(allow_web).replace("DATA block", "REPORT").replace("the data", "the report")
    return f"""You are a skeptical, evidence-driven equity analyst answering a follow-up question about a Deep Research report titled "{title or 'this segment'}" for a self-directed investor. Improve their decision; do not cheerlead.

GROUND RULES
{web_rule}
- Be concise and direct. Answer the specific question asked; don't restate the whole report.
- Tag every company ticker with a leading $ on first mention (e.g. $AMD).
- The report is narrative synthesis: treat its numbers as claims, and say so when a figure should be verified against a primary source.

REPORT
{doc}
{src_block}
{convo_block}NEW QUESTION:
{question.strip()}

Answer in Markdown.{' End with a "Sources" line listing any URLs you used.' if allow_web else ''}"""
