#!/usr/bin/env python3
"""Verify the structured research claims for internal consistency (Phase 0).

This is offline and deterministic. It does NOT fetch live quotes; it checks the
numbers in ``data/research-claims.json`` against each other and against the
broker marks already captured in ``data/current-holdings.json``. The point is to
catch figures that were *never* arithmetically possible (the class of bug the
detail pages' "Data Hygiene" sections describe in prose), not to detect a market
that has simply moved.

Severities:
  ERROR  internal arithmetic is impossible (e.g. price x shares != market cap)
  WARN   a claim diverges from the broker snapshot beyond tolerance
  INFO   minor / advisory (range edges, confirmed-bogus regression guards)

Exit code is non-zero when any ERROR is found, or any WARN under ``--strict``.

Usage::

    py -3 tools/verify_claims.py
    py -3 tools/verify_claims.py --strict
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOLDINGS_JSON = REPO_ROOT / "data" / "current-holdings.json"
CLAIMS_JSON = REPO_ROOT / "data" / "research-claims.json"

# price x shares vs stated market cap; loose because share-count methodology
# (basic/diluted, ADR ratios) legitimately varies.
IDENTITY_TOL = 0.05
# claim price vs the broker mark for the same (roughly contemporaneous) snapshot.
SNAPSHOT_PRICE_TOL = 0.03
# snapshot freshness: warn after a few days, error once badly stale.
STALE_WARN_DAYS = 5
STALE_ERROR_DAYS = 30

SEVERITY_ORDER = {"ERROR": 0, "WARN": 1, "INFO": 2}


@dataclass
class Finding:
    severity: str
    symbol: str
    message: str


def representative(node: dict | None) -> float | None:
    """A single comparable number from a metric node (point or range)."""
    if not isinstance(node, dict):
        return None
    if "value" in node:
        return float(node["value"])
    if "low" in node and "high" in node:
        return (float(node["low"]) + float(node["high"])) / 2
    return None


def rel_diff(a: float, b: float) -> float:
    if b == 0:
        return float("inf")
    return abs(a - b) / abs(b)


def check_snapshot_age(holdings: dict) -> list[Finding]:
    """Flag a stale snapshot. Uses the current time, so this is run-time only."""
    raw = holdings.get("generated_at")
    if not raw:
        return []
    try:
        generated = datetime.fromisoformat(raw)
    except ValueError:
        return [Finding("WARN", "snapshot", f"unparseable generated_at: {raw!r}.")]
    if generated.tzinfo is None:
        generated = generated.replace(tzinfo=timezone.utc)
    age_days = (datetime.now(timezone.utc) - generated).total_seconds() / 86400
    msg = f"snapshot is {age_days:.1f} days old (generated {raw}); refresh IBKR before acting."
    if age_days > STALE_ERROR_DAYS:
        return [Finding("ERROR", "snapshot", msg)]
    if age_days > STALE_WARN_DAYS:
        return [Finding("WARN", "snapshot", msg)]
    return []


def check_symbol(symbol: str, claim: dict, mark: float | None) -> list[Finding]:
    findings: list[Finding] = []
    price = representative(claim.get("price_usd"))
    shares = claim.get("shares_out_b")
    mcap = representative(claim.get("market_cap_usd_b"))

    # 1) Internal identity: price x shares ~= market cap.
    if price is not None and shares is not None and mcap is not None:
        implied = price * float(shares)
        diff = rel_diff(implied, mcap)
        if diff > IDENTITY_TOL:
            findings.append(Finding(
                "ERROR", symbol,
                f"price x shares != market cap: {price:g} x {shares:g}B = "
                f"${implied:.0f}B, but claim says ${mcap:.0f}B ({diff:.0%} off).",
            ))

    # 2) Claim price vs broker snapshot mark.
    if price is not None and mark is not None:
        diff = rel_diff(price, mark)
        if diff > SNAPSHOT_PRICE_TOL:
            findings.append(Finding(
                "WARN", symbol,
                f"claimed price {price:g} diverges from snapshot mark "
                f"{mark:g} by {diff:.0%}.",
            ))
        node = claim.get("price_usd", {})
        if "low" in node and "high" in node and not (node["low"] <= mark <= node["high"]):
            findings.append(Finding(
                "INFO", symbol,
                f"snapshot mark {mark:g} is outside the claimed range "
                f"{node['low']:g}-{node['high']:g}.",
            ))

    # 3) Regression guard: figures documented as wrong must stay wrong.
    if price is not None and shares is not None:
        implied = price * float(shares)
        for bogus in claim.get("disproven_market_cap_usd_b", []):
            if rel_diff(float(bogus), implied) <= IDENTITY_TOL:
                findings.append(Finding(
                    "ERROR", symbol,
                    f"a market cap documented as wrong (${bogus:g}B) now passes the "
                    f"identity (implied ${implied:.0f}B) -- claims have drifted.",
                ))
            else:
                findings.append(Finding(
                    "INFO", symbol,
                    f"confirmed bogus: ${bogus:g}B is inconsistent with "
                    f"{price:g} x {shares:g}B = ${implied:.0f}B.",
                ))

    # 4) Multiples must be positive numbers.
    for field in ("pe_ttm", "pe_fwd", "ps"):
        val = representative(claim.get(field))
        if val is not None and val <= 0:
            findings.append(Finding(
                "ERROR", symbol, f"{field} is non-positive ({val:g}).",
            ))

    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--strict", action="store_true",
        help="Also exit non-zero on WARN findings.",
    )
    args = parser.parse_args()

    if not CLAIMS_JSON.exists():
        print(f"error: claims file not found: {CLAIMS_JSON}", file=sys.stderr)
        return 2

    claims = json.loads(CLAIMS_JSON.read_text(encoding="utf-8"))
    marks: dict[str, float] = {}
    findings: list[Finding] = []
    if HOLDINGS_JSON.exists():
        holdings = json.loads(HOLDINGS_JSON.read_text(encoding="utf-8"))
        marks = {
            p["symbol"]: p["mark_price"]
            for p in holdings.get("positions", [])
            if p.get("mark_price") is not None
        }
        findings += check_snapshot_age(holdings)

    for symbol, claim in claims.get("symbols", {}).items():
        findings += check_symbol(symbol, claim, marks.get(symbol))

    findings.sort(key=lambda f: (SEVERITY_ORDER[f.severity], f.symbol))
    for f in findings:
        print(f"[{f.severity}] {f.symbol}: {f.message}")

    counts = {sev: sum(1 for f in findings if f.severity == sev) for sev in SEVERITY_ORDER}
    print(
        f"\n{counts['ERROR']} error(s), {counts['WARN']} warning(s), "
        f"{counts['INFO']} info across {len(claims.get('symbols', {}))} symbol(s)."
    )

    if counts["ERROR"] or (args.strict and counts["WARN"]):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
