#!/usr/bin/env python3
"""Review a Perplexity Deep Research artifact against local portfolio data.

This is the handoff between narrative research and the deterministic pipeline.
It does not fetch data, place trades, or mutate target-model.json. It reads a
segment definition, a saved Deep Research report, extracted source links, local
per-ticker research JSON, holdings, and the target model, then writes:

* data/research/deep/<segment>-<date>.review.md
* data/research/deep/<segment>-<date>.target-proposal.json

Usage::

    py -3 tools/review_deep_research.py --segment fintech-payments --date 2026-06-03
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
from pathlib import Path
from typing import Any
import sys
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hygiene import worst_severity  # noqa: E402  -- shared severity reducer
from portfolio import holdings_weights  # noqa: E402  -- single source of truth for weights
from store import load as load_json, write_json  # noqa: E402  -- shared forgiving JSON IO

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
SEGMENT_DEF_DIR = DATA_DIR / "segments"
RESEARCH_DIR = DATA_DIR / "research"
DEEP_DIR = RESEARCH_DIR / "deep"
HOLDINGS_JSON = DATA_DIR / "current-holdings.json"
TARGET_MODEL_JSON = DATA_DIR / "target-model.json"

SOURCE_BUCKETS = {
    "primary_ir": [
        "investor.",
        "investors.",
        "investidores.",
        "ir.",
        "newsroom.paypal-corp.com",
        "international.nubank.com.br",
        "businesswire.com",
        "sec.gov",
    ],
    "major_media": [
        "reuters.com",
        "cnbc.com",
        "finance.yahoo.com",
        "bloomberg.com",
        "wsj.com",
        "ft.com",
    ],
    "secondary": [
        "public.com",
        "simplywall.st",
        "247wallst.com",
        "capital.com",
        "seekingalpha.com",
        "fool.com",
    ],
    "weak": [
        "facebook.com",
        "reddit.com",
        "x.com",
        "twitter.com",
        "linkedin.com",
    ],
}

# Review-gate tiers: BLOCK halts an apply, WARN needs a deliberate decision, FYI
# is hygiene. ERROR-level deterministic data maps to BLOCK.
LEVEL_RANK = {"BLOCK": 0, "WARN": 1, "FYI": 2}


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if not slug:
        raise SystemExit("empty segment slug")
    return slug


def run_paths(segment: str, date: str) -> dict[str, Path]:
    stem = f"{segment}-{date}"
    return {
        "report": DEEP_DIR / f"{stem}.md",
        "sources": DEEP_DIR / f"{stem}.sources.json",
        "review": DEEP_DIR / f"{stem}.review.md",
        "proposal": DEEP_DIR / f"{stem}.target-proposal.json",
    }


def normalize_sources(raw: Any) -> list[dict[str, str]]:
    if isinstance(raw, dict):
        raw = raw.get("citations", [])
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        href = str(item.get("href") or item.get("url") or "").strip()
        if not href or href in seen:
            continue
        seen.add(href)
        label = str(item.get("label") or item.get("title") or urlparse(href).netloc or "source").strip()
        out.append({"label": label, "href": href})
    return out


def source_bucket(href: str) -> str:
    host = urlparse(href).netloc.lower()
    for bucket, needles in SOURCE_BUCKETS.items():
        if any(n in host for n in needles):
            return bucket
    return "other"


def source_summary(sources: list[dict[str, str]]) -> dict[str, Any]:
    buckets = {bucket: [] for bucket in [*SOURCE_BUCKETS, "other"]}
    for src in sources:
        buckets[source_bucket(src["href"])].append(src)
    return {
        "count": len(sources),
        "buckets": {k: len(v) for k, v in buckets.items()},
        "weak_sources": buckets["weak"],
        "primary_like_sources": buckets["primary_ir"],
    }


def current_weights(holdings: dict[str, Any]) -> dict[str, float]:
    """Single source of truth: see portfolio.holdings_weights (market value over
    invested book). Do not read the broker's notional-poisoned percent_of_nav."""
    return holdings_weights(holdings)


def metric_display(rec: dict[str, Any], key: str) -> str:
    node = rec.get("metrics", {}).get(key)
    if not isinstance(node, dict):
        return "n/a"
    return str(node.get("display") or node.get("value") or "n/a")


def infer_report_action(report_text: str, symbol: str) -> str:
    """Best-effort extraction only. The review remains a scaffold, not gospel."""
    pattern = re.compile(rf"\b{re.escape(symbol)}\b", re.IGNORECASE)
    matches = list(pattern.finditer(report_text))
    if not matches:
        return "not mentioned"
    verbs = [
        ("add", ["add", "accumulate", "overweight"]),
        ("hold", ["hold", "keep"]),
        ("wait", ["wait"]),
        ("trim", ["trim", "reduce"]),
        ("sell", ["sell", "exit"]),
    ]
    scores = {k: 0 for k, _ in verbs}
    for m in matches:
        start = max(0, m.start() - 900)
        end = min(len(report_text), m.end() + 1200)
        window = report_text[start:end].lower()
        for action, words in verbs:
            scores[action] += sum(1 for word in words if re.search(rf"\b{word}\b", window))
    best = max(scores, key=lambda k: scores[k])
    return best if scores[best] else "mentioned"


def review(segment: str, date: str, *, write: bool = True) -> dict[str, Any]:
    segment = slugify(segment)
    paths = run_paths(segment, date)
    segment_def = load_json(SEGMENT_DEF_DIR / f"{segment}.json")
    if not segment_def:
        raise SystemExit(f"missing segment definition: {SEGMENT_DEF_DIR / (segment + '.json')}")
    if not paths["report"].exists():
        raise SystemExit(f"missing report: {paths['report']}")

    report_text = paths["report"].read_text(encoding="utf-8")
    source_doc = load_json(paths["sources"], {"citations": []})
    sources = normalize_sources(source_doc)
    source_info = source_summary(sources)
    holdings = load_json(HOLDINGS_JSON, {}) or {}
    targets = (load_json(TARGET_MODEL_JSON, {}) or {}).get("targets", {})
    funding_order = (load_json(TARGET_MODEL_JSON, {}) or {}).get("funding_order", [])
    weights = current_weights(holdings)

    rows: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    proposal_changes: list[dict[str, Any]] = []

    # Funding-order names are meant to be sold to fund buys, so only these rules
    # genuinely conflict with a report that says hold/add. A name parked at `hold`
    # (e.g. SOFI) does not -- comparing to the actual rule kills that false alarm.
    SELL_DOWN_RULES = {"reduce", "trim_only", "do_not_add", "avoid"}

    def add_finding(level: str, message: str, symbol: str | None = None) -> None:
        findings.append({"level": level, "symbol": symbol, "message": message})

    if not segment_def.get("members"):
        add_finding(
            "BLOCK",
            "Segment has no members — nothing to cross-check or apply. "
            "Add tickers to the segment definition, then re-pull and re-review.",
        )

    for member in segment_def.get("members", []):
        sym = str(member.get("symbol", "")).upper()
        if not sym:
            continue
        rec = load_json(RESEARCH_DIR / f"{sym}.json", {}) or {}
        checks = rec.get("cross_checks", [])
        worst = worst_severity(checks)
        target = targets.get(sym)
        rule = target.get("rule", "") if target else ""
        report_action = infer_report_action(report_text, sym)
        held_pct = weights.get(sym)
        conflict = ""

        if target:
            if report_action in {"add"} and rule in {"reduce", "do_not_add", "trim_only", "avoid"}:
                conflict = f"report leans {report_action}, but target rule is {rule}"
            elif report_action in {"trim", "sell"} and rule == "accumulate":
                conflict = f"report leans {report_action}, but target rule is accumulate"
        elif held_pct:
            conflict = "held position is missing from target-model.json"
            proposal_changes.append({
                "symbol": sym,
                "action": "add_target",
                "status": "draft",
                "proposed_target": {
                    "low": max(0.0, round(held_pct - 1.0, 1)),
                    "high": round(held_pct + 1.0, 1),
                    "rule": "hold",
                    "note": f"Draft target from {segment} review; verify before applying.",
                },
                "rationale": "Held position has no explicit target rule.",
            })

        if conflict:
            add_finding("WARN", f"{sym}: {conflict}.", sym)
        if sym in funding_order and report_action in {"add", "hold"} and rule in SELL_DOWN_RULES:
            add_finding("WARN", f"{sym}: report says {report_action}, but it's a funding source (rule {rule}).", sym)
        if worst == "ERROR":
            add_finding("BLOCK",
                        f"{sym}: deterministic data has ERROR-level checks — valuation withheld; resolve before acting on {sym}.",
                        sym)

        rows.append({
            "symbol": sym,
            "sleeve": member.get("sleeve", ""),
            "held_pct_nav": held_pct,
            "report_action": report_action,
            "target_rule": target.get("rule") if target else None,
            "target_band": [target.get("low"), target.get("high")] if target else None,
            "data_quality": worst,
            "price": rec.get("price", {}).get("value") if isinstance(rec.get("price"), dict) else None,
            "market_cap": metric_display(rec, "market_cap_usd_b"),
            "pe_ttm": metric_display(rec, "pe_ttm"),
            "ps": metric_display(rec, "ps"),
            "cross_checks": checks,
            "conflict": conflict,
        })

    if source_info["count"] < 5:
        add_finding("FYI", "Source count is low; treat the report as thinly supported.")
    if not source_info["primary_like_sources"]:
        add_finding("WARN", "No primary/IR-like sources found; require primary-source follow-up before model changes.")
    if source_info["weak_sources"]:
        add_finding("FYI", "Weak/social sources present; treat them as pointers, not evidence.")

    findings.sort(key=lambda f: LEVEL_RANK.get(f["level"], 9))
    warnings = [f["message"] for f in findings]  # flat list, kept for back-compat
    blocked_symbols = sorted({f["symbol"] for f in findings if f["level"] == "BLOCK" and f["symbol"]})

    proposal = {
        "schema_version": 1,
        "segment": segment,
        "date": date,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "status": "draft",
        "message": "Human review required. This file is a proposal, not an allocation change.",
        "changes": proposal_changes,
        "warnings": warnings,
        "findings": findings,
        "blocked_symbols": blocked_symbols,
    }

    review_md = render_markdown(segment_def, date, paths["report"], source_info, rows, findings, proposal)
    result = {
        "segment": segment,
        "date": date,
        "review_path": str(paths["review"].relative_to(REPO_ROOT)),
        "proposal_path": str(paths["proposal"].relative_to(REPO_ROOT)),
        "source_summary": source_info,
        "rows": rows,
        "warnings": warnings,
        "findings": findings,
        "blocked_symbols": blocked_symbols,
        "proposal": proposal,
        "markdown": review_md,
        "report": report_text,
    }
    if write:
        paths["review"].write_text(review_md, encoding="utf-8")
        write_json(paths["proposal"], proposal)
    return result


def render_markdown(
    segment_def: dict[str, Any],
    date: str,
    report_path: Path,
    source_info: dict[str, Any],
    rows: list[dict[str, Any]],
    findings: list[dict[str, Any]],
    proposal: dict[str, Any],
) -> str:
    title = segment_def.get("title") or "Segment"
    lines = [
        f"# Review: {title} Deep Research",
        "",
        f"- Date: `{date}`",
        f"- Source report: `{report_path.relative_to(REPO_ROOT)}`",
        f"- Source count: `{source_info['count']}`",
        "",
        "## Source Quality",
        "",
        "| Bucket | Count |",
        "| --- | ---: |",
    ]
    for bucket, count in source_info["buckets"].items():
        lines.append(f"| `{bucket}` | {count} |")
    if source_info["weak_sources"]:
        lines.extend(["", "Weak/social sources found:"])
        for src in source_info["weak_sources"]:
            lines.append(f"- `{src['label']}`: {src['href']}")

    lines.extend([
        "",
        "## Deterministic Cross-Check",
        "",
        "| Symbol | Report Action | Held % NAV | Target Rule | Data Quality | Valuation Snapshot | Conflict |",
        "| --- | --- | ---: | --- | --- | --- | --- |",
    ])
    for row in rows:
        held = "" if row["held_pct_nav"] is None else f"{row['held_pct_nav']:.2f}%"
        valuation = f"{row['market_cap']}; P/E {row['pe_ttm']}; P/S {row['ps']}"
        lines.append(
            f"| `{row['symbol']}` | {row['report_action']} | {held} | "
            f"{row['target_rule'] or ''} | {row['data_quality']} | {valuation} | {row['conflict']} |"
        )

    lines.extend(["", "## Warnings", ""])
    if findings:
        groups = {
            "BLOCK": ("Blocking — resolve before applying", []),
            "WARN": ("Warnings — decide deliberately", []),
            "FYI": ("FYI", []),
        }
        for f in findings:
            groups.get(f["level"], groups["FYI"])[1].append(f["message"])
        for level in ("BLOCK", "WARN", "FYI"):
            heading, msgs = groups[level]
            if not msgs:
                continue
            lines.append(f"### {heading}")
            lines.extend(f"- {m}" for m in msgs)
            lines.append("")
        if proposal.get("blocked_symbols"):
            lines.append(f"> Apply is blocked for: {', '.join('`' + s + '`' for s in proposal['blocked_symbols'])}.")
            lines.append("")
    else:
        lines.append("- No review-gate warnings.")

    lines.extend([
        "",
        "## Target-Model Proposal",
        "",
        "This is a draft only. Applying changes requires an explicit website approval.",
        "",
    ])
    if proposal["changes"]:
        for change in proposal["changes"]:
            lines.append(f"- `{change['symbol']}`: {change['action']} ({change['rationale']})")
    else:
        lines.append("- No target-model changes proposed.")

    lines.extend([
        "",
        "## Judgment",
        "",
        "Use this as a review scaffold. Perplexity can surface thesis shifts, but deterministic data and human sizing judgement still control the target model.",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--segment", required=True, help="segment slug, e.g. fintech-payments")
    parser.add_argument("--date", required=True, help="artifact date, e.g. 2026-06-03")
    args = parser.parse_args()
    result = review(args.segment, args.date)
    print(f"wrote {result['review_path']}")
    print(f"wrote {result['proposal_path']}")
    print(f"{len(result['warnings'])} warning(s), {len(result['proposal']['changes'])} proposal change(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
