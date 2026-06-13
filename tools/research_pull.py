#!/usr/bin/env python3
"""On-demand deep-dive puller for a ticker or a whole segment.

Pulls live numbers from multiple free sources (Yahoo for price/momentum/
fundamentals, SEC EDGAR for an independent filing-based cross-check, FMP if a key
is present) and writes a structured, dated, multi-source research file.

The cardinal rule of this repo is preserved: numbers and judgement are kept
apart. This tool only ever writes the *numbers* block; any human/LLM-authored
``thesis`` already on disk is carried over untouched, so a re-pull refreshes the
facts without nuking the analysis.

Usage::

    py -3 tools/research_pull.py --ticker NVDA
    py -3 tools/research_pull.py --segment semiconductors
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from portfolio import decision_label, holdings_weights, portfolio_context  # noqa: E402
from hygiene import rel_diff as _rel, worst_severity as _worst_severity  # noqa: E402
from store import load as _load, write_json as _store_write_json  # noqa: E402
from providers import fmp, sec_edgar, yahoo  # noqa: E402
from providers.common import (  # noqa: E402
    ProviderError,
    fmt_b,
    fmt_pct,
    fmt_price,
    fmt_x,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
RESEARCH_DIR = REPO_ROOT / "data" / "research"
SEGMENT_DEF_DIR = REPO_ROOT / "data" / "segments"
SEGMENT_OUT_DIR = RESEARCH_DIR / "segments"
CACHE_DIR = REPO_ROOT / "data" / "cache"
HISTORY_DIR = CACHE_DIR / "research-history"

# (metric key, preferred source order, formatter)
METRIC_SPECS: list[tuple[str, list[str], Any]] = [
    ("market_cap_usd_b", ["yahoo", "fmp"], fmt_b),
    ("shares_out_b", ["sec_edgar", "yahoo"], lambda v: fmt_b(v, prefix="")),
    ("pe_ttm", ["yahoo", "fmp"], fmt_x),
    ("pe_fwd", ["yahoo"], fmt_x),
    ("ps", ["yahoo", "fmp"], fmt_x),
    ("revenue_ttm_usd_b", ["yahoo", "sec_edgar"], fmt_b),
    ("net_income_ttm_usd_b", ["sec_edgar"], fmt_b),
    ("gross_margin_pct", ["yahoo"], lambda v: "n/a" if v is None else f"{v:.0f}%"),
    ("rev_growth_yoy_pct", ["yahoo"], fmt_pct),
]

IDENTITY_TOL = 0.05
SHARES_TOL = 0.05
REVENUE_TOL = 0.15  # SEC TTM is an approximation; only flag real divergence
PRICE_TOL = 0.03


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _val(node: dict[str, Any] | None) -> float | None:
    return node.get("value") if isinstance(node, dict) else None


def _merge_profile(*profiles: dict[str, Any] | None) -> dict[str, Any] | None:
    """Combine business profiles field-by-field, first non-empty wins. Pass the
    preferred source first (Yahoo), then fallbacks (FMP): Yahoo fills what it has,
    FMP backfills the gaps. Returns None if every source was empty."""
    out: dict[str, Any] = {}
    for prof in profiles:
        if not prof:
            continue
        for key, value in prof.items():
            if value and not out.get(key):
                out[key] = value
    return out or None


def _collect(*sources: dict[str, Any] | None) -> dict[str, list[dict[str, Any]]]:
    """metric_key -> list of per-source nodes that supplied it."""
    out: dict[str, list[dict[str, Any]]] = {}
    for src in sources:
        if not src:
            continue
        for key, node in src.items():
            if isinstance(node, dict) and "value" in node and "source" in node:
                out.setdefault(key, []).append(node)
    return out


def _merge_metrics(by_metric: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for key, prefs, fmt in METRIC_SPECS:
        candidates = by_metric.get(key, [])
        if not candidates:
            continue
        order = {s: i for i, s in enumerate(prefs)}
        chosen = min(candidates, key=lambda n: order.get(n["source"], 99))
        node = dict(chosen)
        node["display"] = fmt(node["value"])
        node["all_sources"] = {c["source"]: c["value"] for c in candidates}
        merged[key] = node
    return merged


def _cross_checks(
    symbol: str,
    momentum: dict[str, Any],
    y: dict[str, Any] | None,
    s: dict[str, Any] | None,
) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []

    def add(sev: str, metric: str, msg: str) -> None:
        findings.append({"severity": sev, "metric": metric, "message": msg})

    y_price = _val(y.get("price")) if y else None
    y_shares = _val(y.get("shares_out_b")) if y else None
    y_mcap = _val(y.get("market_cap_usd_b")) if y else None
    s_shares = _val(s.get("shares_out_b")) if s else None
    y_rev = _val(y.get("revenue_ttm_usd_b")) if y else None
    s_rev = _val(s.get("revenue_ttm_usd_b")) if s else None
    last = momentum.get("last")

    # 1) Yahoo internal identity: price x shares ~= market cap.
    if y_price and y_shares and y_mcap:
        implied = y_price * y_shares
        if _rel(implied, y_mcap) > IDENTITY_TOL:
            add("ERROR", "market_cap",
                f"Yahoo internal mismatch: {y_price:g} x {y_shares:g}B = "
                f"${implied:.0f}B, but market cap says ${y_mcap:.0f}B "
                f"({_rel(implied, y_mcap):.0%} off).")

    # 2) Independent share count: Yahoo vs SEC filing.
    if y_shares and s_shares:
        if _rel(y_shares, s_shares) > SHARES_TOL:
            add("WARN", "shares_out",
                f"share count disagrees across sources: Yahoo {y_shares:.2f}B vs "
                f"SEC {s_shares:.2f}B ({_rel(y_shares, s_shares):.0%}).")
        else:
            add("INFO", "shares_out",
                f"share count agrees: Yahoo {y_shares:.2f}B ~ SEC {s_shares:.2f}B.")

    # 3) Independent revenue (TTM, approximate).
    if y_rev and s_rev:
        if _rel(y_rev, s_rev) > REVENUE_TOL:
            add("WARN", "revenue_ttm",
                f"TTM revenue disagrees: Yahoo ${y_rev:.1f}B vs SEC ~${s_rev:.1f}B "
                f"({_rel(y_rev, s_rev):.0%}). SEC TTM is an approximation.")

    # 4) Price freshness: fundamentals snapshot vs the chart's last close.
    if y_price and last and _rel(y_price, last) > PRICE_TOL:
        add("INFO", "price",
            f"fundamentals price ${y_price:g} differs from latest close "
            f"${last:g} ({_rel(y_price, last):.0%}) -- snapshot vs intraday.")

    # 5) No independent anchor available.
    if s is None:
        add("INFO", "coverage",
            "no SEC EDGAR cross-check (non-US filer or not in EDGAR); "
            "numbers rest on Yahoo alone -- treat with extra suspicion.")
    return findings


def _quarantine_metric(merged: dict[str, Any], key: str) -> None:
    """Withhold a metric we can't trust: null the value but keep the node so the
    consumer renders 'unreliable' instead of a confident wrong number."""
    node = merged.get(key)
    if not isinstance(node, dict):
        return
    node = dict(node)
    node["value"] = None
    node["display"] = "unreliable"
    node["quarantined"] = True
    merged[key] = node


def _reconcile_market_cap(
    merged: dict[str, Any],
    y: dict[str, Any] | None,
    s: dict[str, Any] | None,
    f: dict[str, Any] | None,
    price: float | None,
    checks: list[dict[str, str]],
) -> None:
    """When Yahoo's ``price x shares`` disagrees with its reported market cap, the
    merged value is untrustworthy. Try to correct it from an independent source
    (FMP market cap, or ``price x`` SEC/FMP shares); if nothing resolves it,
    quarantine market cap and the P/S that rides on it. Mutates merged + checks.
    """
    err = next(
        (c for c in checks if c.get("severity") == "ERROR" and c.get("metric") == "market_cap"),
        None,
    )
    if err is None:
        return  # identity held -> nothing to reconcile

    y_shares = _val(y.get("shares_out_b")) if y else None
    y_mcap = _val(y.get("market_cap_usd_b")) if y else None
    s_shares = _val(s.get("shares_out_b")) if s else None
    f_mcap = _val(f.get("market_cap_usd_b")) if f else None
    f_shares = _val(f.get("shares_out_b")) if f else None

    # An independent anchor for the true market cap, most-trusted first.
    anchor = anchor_label = None
    if f_mcap:
        anchor, anchor_label = f_mcap, "FMP market cap"
    elif price and s_shares:
        anchor, anchor_label = price * s_shares, "price x SEC shares"
    elif price and f_shares:
        anchor, anchor_label = price * f_shares, "price x FMP shares"

    # The two conflicting Yahoo views of market cap.
    views = []
    if y_mcap:
        views.append(y_mcap)
    if price and y_shares:
        views.append(price * y_shares)

    if anchor and views:
        best = min(views, key=lambda v: _rel(v, anchor))
        if _rel(best, anchor) <= IDENTITY_TOL:
            node = dict(merged.get("market_cap_usd_b") or {})
            node["value"] = round(best, 3)
            node["display"] = fmt_b(best)
            node["source"] = "reconciled"
            node["reconciled_via"] = anchor_label
            merged["market_cap_usd_b"] = node
            err["severity"] = "WARN"  # resolved -> no longer a blocking error
            err["message"] += f" Reconciled to ${best:.0f}B via {anchor_label}."
            return

    # Unresolvable: withhold the affected metrics rather than surface a wrong one.
    _quarantine_metric(merged, "market_cap_usd_b")
    _quarantine_metric(merged, "ps")
    err["message"] += " No independent source resolved it; market cap and P/S withheld."


def pull_ticker(symbol: str, *, write: bool = True) -> dict[str, Any]:
    symbol = symbol.upper().strip()
    errors: list[str] = []

    try:
        mo = yahoo.momentum(symbol)
    except ProviderError as exc:
        mo = {}
        errors.append(f"yahoo momentum: {exc}")
    price_history = mo.pop("price_history", None)

    y: dict[str, Any] | None = None
    try:
        y = yahoo.fundamentals(symbol)
    except ProviderError as exc:
        errors.append(f"yahoo fundamentals: {exc}")

    s = sec_edgar.fundamentals(symbol)
    f = fmp.fundamentals(symbol) if fmp.enabled() else None

    by_metric = _collect(y, s, f)
    merged = _merge_metrics(by_metric)
    checks = _cross_checks(symbol, mo, y, s)
    price_val = mo.get("last") or (_val(y.get("price")) if y else None)
    _reconcile_market_cap(merged, y, s, f, price_val, checks)

    name = (y or {}).get("name") or (s or {}).get("entity") or (f or {}).get("name") or symbol
    portfolio = portfolio_context(symbol)
    record: dict[str, Any] = {
        "symbol": symbol,
        "name": name,
        "profile": _merge_profile((y or {}).get("profile"), (f or {}).get("profile")),
        "as_of": _now(),
        "currency": (y or {}).get("currency") or mo.get("currency") or "USD",
        "price": {"value": mo.get("last"), "source": "yahoo"} if mo.get("last") else (y or {}).get("price"),
        "price_history": price_history,
        "momentum": mo,
        "metrics": merged,
        "cross_checks": checks,
        "portfolio": portfolio,
        "decision": decision_label(portfolio),
        "sources": {
            "yahoo": y is not None or bool(mo),
            "sec_edgar": s is not None,
            "fmp": f is not None,
        },
        "errors": errors,
    }

    if write:
        existing = _load(RESEARCH_DIR / f"{symbol}.json")
        if existing and "thesis" in existing:
            record["thesis"] = existing["thesis"]  # never clobber judgement
        _write(RESEARCH_DIR / f"{symbol}.json", record)
        if _has_usable_data(record):
            _write_history(symbol, record)
    return record


def _has_usable_data(record: dict[str, Any]) -> bool:
    """True when the pull actually retrieved market data. A total provider
    wipeout (no price and no metrics) is a failed analysis and must NOT be
    persisted to the change-log history, or the deep dive fills up with rows of
    n/a that pretend something happened."""
    price = record.get("price")
    if _val(price) is not None:
        return True
    metrics = record.get("metrics") or {}
    return any(_val(v) is not None for v in metrics.values())


def pull_segment(name: str, *, write: bool = True) -> dict[str, Any]:
    name = name.lower().strip()
    definition = _load(SEGMENT_DEF_DIR / f"{name}.json")
    if not definition:
        raise SystemExit(f"unknown segment '{name}' (expected {SEGMENT_DEF_DIR / (name + '.json')})")

    held = holdings_weights()
    members: list[dict[str, Any]] = []
    for entry in definition.get("members", []):
        sym = entry["symbol"].upper()
        rec = pull_ticker(sym, write=True)
        worst = _worst_severity(rec["cross_checks"])
        members.append({
            "symbol": sym,
            "name": rec["name"],
            "sleeve": entry.get("sleeve", "other"),
            "owned_pct_nav": held.get(sym),
            "price": rec.get("price", {}).get("value") if rec.get("price") else None,
            "market_cap_usd_b": _val(rec["metrics"].get("market_cap_usd_b")),
            "pe_fwd": _val(rec["metrics"].get("pe_fwd")),
            "ps": _val(rec["metrics"].get("ps")),
            "rev_growth_yoy_pct": _val(rec["metrics"].get("rev_growth_yoy_pct")),
            "gross_margin_pct": _val(rec["metrics"].get("gross_margin_pct")),
            "chg_3m_pct": rec["momentum"].get("chg_3m_pct"),
            "chg_12m_pct": rec["momentum"].get("chg_12m_pct"),
            "pct_below_52w_high": rec["momentum"].get("pct_below_52w_high"),
            "data_quality": worst,
            "research_score": _research_score(rec, worst),
            "decision": decision_label(rec.get("portfolio", {})),
            "score_reasons": _score_reasons(rec, worst),
        })

    record = {
        "segment": name,
        "title": definition.get("title", name.title()),
        "as_of": _now(),
        "sleeves": definition.get("sleeves", []),
        "members": members,
    }
    if write:
        _write(SEGMENT_OUT_DIR / f"{name}.json", record)
    return record


def _research_score(rec: dict[str, Any], worst: str) -> int:
    """Heuristic peer score. It ranks research candidates; it is not an order signal."""
    metrics = rec.get("metrics", {})
    portfolio = rec.get("portfolio", {})
    target = portfolio.get("target", {})
    score = 50

    rule = target.get("rule")
    status = portfolio.get("status")
    if rule == "accumulate":
        score += 18
        if status == "below_band":
            score += 18
    elif rule in {"trim_only", "do_not_add"}:
        score -= 12
        if status == "above_band":
            score -= 18
    elif rule == "reduce":
        score -= 25
    elif rule == "avoid":
        score -= 35
    elif rule == "hold":
        score += 4

    growth = _val(metrics.get("rev_growth_yoy_pct"))
    margin = _val(metrics.get("gross_margin_pct"))
    fwd_pe = _val(metrics.get("pe_fwd"))
    ps = _val(metrics.get("ps"))
    chg_3m = rec.get("momentum", {}).get("chg_3m_pct")
    chg_12m = rec.get("momentum", {}).get("chg_12m_pct")

    if growth is not None:
        score += max(-12, min(18, growth / 2.5))
    if margin is not None:
        score += max(-6, min(14, (margin - 35) / 2.5))
    if fwd_pe is not None:
        if fwd_pe <= 25:
            score += 10
        elif fwd_pe <= 45:
            score += 4
        elif fwd_pe >= 80:
            score -= 12
    if ps is not None:
        if ps <= 8:
            score += 8
        elif ps >= 25:
            score -= 10
    if chg_3m is not None:
        if chg_3m < -20:
            score -= 8
        elif chg_3m <= 25:
            score += 6
        elif chg_3m > 60:
            score -= 8
    if chg_12m is not None:
        score += max(-10, min(12, chg_12m / 8))
    if worst == "ERROR":
        score -= 30
    elif worst == "WARN":
        score -= 10

    return int(round(max(0, min(100, score))))

def _score_reasons(rec: dict[str, Any], worst: str) -> list[str]:
    reasons: list[str] = []
    portfolio = rec.get("portfolio", {})
    target = portfolio.get("target", {})
    rule = target.get("rule")
    status = portfolio.get("status")
    if rule:
        reasons.append(f"rule: {rule}")
    if status:
        reasons.append(status.replace("_", " "))
    if worst != "INFO":
        reasons.append(f"data trust: {worst}")
    metrics = rec.get("metrics", {})
    growth = _val(metrics.get("rev_growth_yoy_pct"))
    fwd_pe = _val(metrics.get("pe_fwd"))
    if growth is not None:
        reasons.append(f"growth {growth:+.1f}%")
    if fwd_pe is not None:
        reasons.append(f"fwd P/E {fwd_pe:.1f}x")
    return reasons[:5]


def history_for(symbol: str, *, limit: int = 12) -> list[dict[str, Any]]:
    folder = HISTORY_DIR / symbol.upper().strip()
    if not folder.exists():
        return []
    entries: list[dict[str, Any]] = []
    for path in sorted(folder.glob("*.json"), reverse=True)[:limit]:
        rec = _load(path)
        if not rec:
            continue
        metrics = rec.get("metrics", {})
        entries.append({
            "as_of": rec.get("as_of"),
            "price": _val(rec.get("price")),
            "market_cap_usd_b": _val(metrics.get("market_cap_usd_b")),
            "pe_fwd": _val(metrics.get("pe_fwd")),
            "ps": _val(metrics.get("ps")),
            "revenue_ttm_usd_b": _val(metrics.get("revenue_ttm_usd_b")),
            "data_quality": _worst_severity(rec.get("cross_checks", [])),
            "stamp": path.stem,
            "path": str(path.relative_to(REPO_ROOT)).replace("\\", "/"),
        })
    return entries


_HISTORY_STAMP_RE = re.compile(r"^\d{8}T\d{6}Z$")


def delete_history(symbol: str, stamp: str) -> bool:
    """Delete a single per-ticker history snapshot. Returns True if a file was
    removed, False if it was already gone. The stamp must match the
    YYYYMMDDTHHMMSSZ filename format so this can't be used for path traversal."""
    sym = symbol.upper().strip()
    if not _HISTORY_STAMP_RE.match(stamp or ""):
        raise ValueError(f"bad history stamp: {stamp!r}")
    folder = (HISTORY_DIR / sym).resolve()
    target = (folder / f"{stamp}.json").resolve()
    if folder not in target.parents:
        raise ValueError("history path escapes its folder")
    if not target.exists():
        return False
    target.unlink()
    return True


def _write_history(symbol: str, record: dict[str, Any]) -> None:
    snapshot = dict(record)
    snapshot.pop("thesis", None)
    stamp = _history_stamp(record.get("as_of") or _now())
    _write(HISTORY_DIR / symbol.upper() / f"{stamp}.json", snapshot)


def _history_stamp(as_of: str) -> str:
    try:
        parsed = dt.datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    except ValueError:
        parsed = dt.datetime.now(dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _write(path: Path, record: dict[str, Any]) -> None:
    # Research records keep their authored key order (thesis, metrics, ...);
    # unlike the server's config writes we deliberately do not sort_keys.
    _store_write_json(path, record, sort_keys=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ticker", help="single symbol, e.g. NVDA")
    group.add_argument("--segment", help="segment name, e.g. semiconductors")
    args = parser.parse_args()

    if args.ticker:
        rec = pull_ticker(args.ticker)
        print(f"{rec['symbol']} ({rec['name']}) @ {fmt_price(rec.get('price', {}).get('value') if rec.get('price') else None)}")
        for key, _, _ in METRIC_SPECS:
            node = rec["metrics"].get(key)
            if node:
                print(f"  {key:22} {node['display']:>10}  [{node['source']}]")
        for c in rec["cross_checks"]:
            print(f"  [{c['severity']}] {c['metric']}: {c['message']}")
        if rec["errors"]:
            print("  errors:", "; ".join(rec["errors"]))
    else:
        rec = pull_segment(args.segment)
        print(f"{rec['title']}: {len(rec['members'])} members @ {rec['as_of']}")
        for m in sorted(rec["members"], key=lambda x: (x["sleeve"], x["symbol"])):
            owned = f"{m['owned_pct_nav']:.1f}%NAV" if m["owned_pct_nav"] else "-"
            print(f"  {m['symbol']:7} {m['sleeve']:10} fwdPE={fmt_x(m['pe_fwd']):>7} "
                  f"PS={fmt_x(m['ps']):>7} g={fmt_pct(m['rev_growth_yoy_pct']):>7} "
                  f"3m={fmt_pct(m['chg_3m_pct']):>7} owned={owned:>8} [{m['data_quality']}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
