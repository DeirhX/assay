#!/usr/bin/env python3
"""Harvest ticker-shaped symbols from a Deep Research report's prose.

This is the server-side mirror of the frontend harvest in
``web/src/analyses/linkify.ts``: the Deep Research prompt asks the model to
$-tag the first mention of every company and to write exchange-qualified
mentions like "(NASDAQ: NVDA)", so those high-confidence shapes give a
report-local universe of real tickers. We use it to surface the names a segment
report *discovered* beyond the segment's own member list, so they can be starred
into the optimizer pool instead of being lost in prose.

Pure stdlib + regex; no IO, no project imports, so it's safe to import anywhere.
"""

from __future__ import annotations

import re

# All-caps tokens that are common finance/English shorthand, not tickers. Mirror
# of TICKER_STOP in the frontend; guards the parenthetical (weaker) path.
_STOP = {
    "US", "EU", "UK", "USA", "EV", "AI", "AR", "VR", "ML", "LLM", "GPU", "CPU", "API", "SDK",
    "UI", "UX", "CEO", "CFO", "CTO", "COO", "IPO", "ETF", "ETFS", "NAV", "EPS", "PE", "PEG",
    "ROE", "ROI", "ROIC", "FCF", "GAAP", "YOY", "QOQ", "CAGR", "ARR", "MRR", "TAM", "SAM", "SOM",
    "FY", "H1", "H2", "Q1", "Q2", "Q3", "Q4", "USD", "EUR", "GBP", "JPY", "KPI", "OEM", "ESG",
    "IRR", "WACC", "DCF", "EBITDA", "IT", "OK", "NO", "AND", "THE", "FOR", "WITH", "FROM",
    "THAT", "THIS", "ARE", "NOT", "ALL", "ANY", "OS", "PC", "TV", "IOT", "SAAS", "B2B", "B2C",
    "RD", "IP", "ID", "VS", "ETC", "CES", "FDA", "SEC", "GDP",
}

# $-prefixed and exchange-qualified mentions are explicit author intent (trusted
# even if the symbol collides with a stoplisted word). Bare parentheticals are
# weaker and still respect the stoplist. A numeric base must carry a suffix so
# "$5" / "(KRX: 000660)" without ".KS" never harvest a bogus symbol.
_DOLLAR = re.compile(r"\$([A-Z]{1,5}(?:\.[A-Z]{1,3})?|[A-Z0-9]{1,6}\.[A-Z]{1,3})\b")
_PAREN = re.compile(r"\(\s*([A-Z]{2,5}(?:\.[A-Z]{1,3})?|[A-Z0-9]{1,6}\.[A-Z]{1,3})\s*\)")
_EXCH = re.compile(
    r"\(\s*(?:NYSE(?:\s+American)?|NASDAQ|AMEX|CBOE|OTCMKTS|OTC|TSXV?|LSE|ASX|HKEX|HKG|"
    r"EURONEXT|KRX|KOSPI|KOSDAQ|SEHK|TSE|SSE|SZSE)[:\s]+"
    r"([A-Z]{1,5}(?:\.[A-Z]{1,3})?|[A-Z0-9]{1,6}\.[A-Z]{1,3})\s*\)",
    re.IGNORECASE,
)

# Action verbs around a mention, mirroring review_deep_research.infer_report_action.
_VERBS = [
    ("add", ["add", "accumulate", "overweight", "buy", "initiate"]),
    ("hold", ["hold", "keep", "maintain"]),
    ("wait", ["wait", "watch", "monitor"]),
    ("trim", ["trim", "reduce", "underweight"]),
    ("sell", ["sell", "exit", "avoid"]),
]


def harvest_symbols(text: str) -> set[str]:
    """The set of high-confidence ticker symbols a report self-identifies."""
    found: set[str] = set()
    for m in _DOLLAR.finditer(text):
        found.add(m.group(1).upper())
    for m in _EXCH.finditer(text):
        found.add(m.group(1).upper())
    for m in _PAREN.finditer(text):
        t = m.group(1).upper()
        if t in _STOP or t.split(".")[0] in _STOP:
            continue
        found.add(t)
    return found


def _action_for(text: str, symbol: str) -> str:
    """Best-effort stance for a symbol from the verbs near its mentions."""
    base = re.escape(symbol.split(".")[0])
    matches = list(re.finditer(rf"\b{base}\b", text, re.IGNORECASE))
    if not matches:
        return "mentioned"
    scores = {k: 0 for k, _ in _VERBS}
    for m in matches:
        window = text[max(0, m.start() - 700): m.end() + 900].lower()
        for action, words in _VERBS:
            scores[action] += sum(1 for w in words if re.search(rf"\b{w}\b", window))
    best = max(scores, key=lambda k: scores[k])
    return best if scores[best] else "mentioned"


def _context(text: str, symbol: str) -> str:
    """A short snippet around the symbol's first mention, for the candidate row."""
    base = re.escape(symbol.split(".")[0])
    m = re.search(rf"\b{base}\b", text, re.IGNORECASE)
    if not m:
        return ""
    snippet = text[max(0, m.start() - 90): m.end() + 110]
    snippet = re.sub(r"\s+", " ", snippet).strip()
    return ("\u2026" + snippet + "\u2026") if snippet else ""


def discovered_candidates(report_text: str, *, exclude=()) -> list[dict]:
    """Report-mentioned symbols not in ``exclude`` (typically the segment's own
    members), each with a best-effort action + a short context snippet. Sorted by
    symbol for a stable list. ``exclude`` is matched on both the full token and
    its base (so a held "NVDA" suppresses a "$NVDA" mention)."""
    excl = {str(s).upper() for s in exclude}
    out: list[dict] = []
    for sym in sorted(harvest_symbols(report_text)):
        if sym in excl or sym.split(".")[0] in excl:
            continue
        out.append({
            "symbol": sym,
            "action": _action_for(report_text, sym),
            "context": _context(report_text, sym),
        })
    return out
