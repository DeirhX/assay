#!/usr/bin/env python3
"""Rebalancing spine: validate the target model and preview drift vs holdings.

This is Phase A of the rebalancing spine. It does NOT place or generate trades
yet. It does two things, both offline and deterministic (no network):

* ``--check`` -- validate ``data/target-model.json`` for internal sanity AND
  against the broker snapshot in ``data/current-holdings.json``: rules that
  forbid buying must not target a band above the current weight, accumulate
  names must not already be over their ceiling, sleeves must not double-list a
  symbol, minimum targets + cash must fit in 100% of NAV, and holdings weights
  must look plausible. Severity model and exit codes mirror verify_claims.py.

* default (no ``--check``) -- print a read-only target-vs-current drift preview
  and plain-language *advice* (trim/add suggestions for out-of-band names). This
  is advice for a human, not an order generator: no share counts, no netting, no
  execution. The user decides and places trades.

Numbers vs judgement stays intact: the *targets* are human judgement (edited in
target-model.json); everything this script prints is *computed* from them.

Usage::

    py -3 tools/rebalance.py            # drift preview
    py -3 tools/rebalance.py --check    # validate; exit 1 on ERROR
    py -3 tools/rebalance.py --check --strict   # also exit 1 on WARN
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
HOLDINGS_JSON = REPO_ROOT / "data" / "current-holdings.json"
TARGET_MODEL_JSON = REPO_ROOT / "data" / "target-model.json"

EPS = 0.01  # weights are 2-decimal percents; tolerate rounding noise
IMPLAUSIBLE_WEIGHT = 50.0  # a single line over half of NAV is almost certainly a data bug

VALID_RULES = {"accumulate", "trim_only", "do_not_add", "reduce", "hold", "wait", "avoid"}
NO_BUY_RULES = {"trim_only", "do_not_add", "reduce", "avoid"}

SEV_ORDER = {"ERROR": 0, "WARN": 1, "INFO": 2}


class Finding:
    __slots__ = ("severity", "area", "message")

    def __init__(self, severity: str, area: str, message: str) -> None:
        self.severity = severity
        self.area = area
        self.message = message


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SystemExit(f"missing required file: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise SystemExit(f"could not read {path}: {exc}")


def current_weights(holdings: dict[str, Any]) -> dict[str, float]:
    """symbol -> percent_of_nav, only for plausible, non-null weights."""
    out: dict[str, float] = {}
    for pos in holdings.get("positions", []):
        pct = pos.get("percent_of_nav")
        sym = pos.get("symbol")
        if sym is None or pct is None:
            continue
        if 0.0 <= float(pct) <= IMPLAUSIBLE_WEIGHT:
            out[sym] = float(pct)
    return out


def _band_ok(low: Any, high: Any) -> bool:
    return (
        isinstance(low, (int, float))
        and isinstance(high, (int, float))
        and 0.0 <= low <= high <= 100.0
    )


def _status(current: float, low: float, high: float) -> str:
    if current < low - EPS:
        return "BELOW"
    if current > high + EPS:
        return "ABOVE"
    return "IN"


def check_model(model: dict[str, Any], holdings: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []

    def add(sev: str, area: str, msg: str) -> None:
        findings.append(Finding(sev, area, msg))

    weights = current_weights(holdings)
    targets: dict[str, Any] = model.get("targets", {})
    sleeves: dict[str, Any] = model.get("sleeves", {})
    cash_target = float(model.get("cash_target_pct", 0.0) or 0.0)

    # --- holdings hygiene (independent of the model) ---
    for pos in holdings.get("positions", []):
        sym = pos.get("symbol", "?")
        pct = pos.get("percent_of_nav")
        if pct is None:
            add("INFO", f"holdings:{sym}", "position has no percent_of_nav (skipped in weights).")
        elif float(pct) > IMPLAUSIBLE_WEIGHT:
            add("WARN", f"holdings:{sym}",
                f"implausible weight {pct:g}% of NAV -- looks like a data bug "
                f"(option/notional mislabel?); excluded from weights.")
        elif float(pct) < 0.0:
            add("WARN", f"holdings:{sym}", f"negative weight {pct:g}% of NAV.")

    # --- sleeves ---
    seen_in_sleeve: dict[str, str] = {}
    for name, sl in sleeves.items():
        area = f"sleeve:{name}"
        members = sl.get("members")
        if not isinstance(members, list) or not members or not all(isinstance(m, str) for m in members):
            add("ERROR", area, "members must be a non-empty list of symbol strings.")
            members = members if isinstance(members, list) else []
        low, high = sl.get("low"), sl.get("high")
        if not _band_ok(low, high):
            add("ERROR", area, f"invalid band low={low} high={high} (need 0<=low<=high<=100).")
            low, high = 0.0, 100.0
        rule = sl.get("rule", "accumulate")
        if rule not in VALID_RULES:
            add("ERROR", area, f"unknown rule '{rule}'.")
        for m in members:
            if m in seen_in_sleeve:
                add("ERROR", area, f"symbol {m} also listed in sleeve '{seen_in_sleeve[m]}'.")
            else:
                seen_in_sleeve[m] = name
            if m in targets:
                add("WARN", area, f"symbol {m} is both a sleeve member and a top-level target (ambiguous).")
        caps = sl.get("member_caps", {})
        for csym, cap in caps.items():
            if csym not in members:
                add("WARN", area, f"member_cap for {csym} which is not a sleeve member.")
            cur = weights.get(csym)
            if cur is not None and cur > float(cap) + EPS:
                add("WARN", area, f"{csym} at {cur:g}% exceeds its cap {cap:g}%.")
        # combined current weight vs band
        sleeve_cur = sum(weights.get(m, 0.0) for m in members)
        if sleeve_cur > high + EPS:
            add("WARN", area, f"combined current {sleeve_cur:.2f}% is above target ceiling {high:g}%.")

    # --- individual targets ---
    for sym, t in targets.items():
        area = sym
        low, high = t.get("low"), t.get("high")
        if not _band_ok(low, high):
            add("ERROR", area, f"invalid band low={low} high={high} (need 0<=low<=high<=100).")
            continue
        rule = t.get("rule")
        if rule not in VALID_RULES:
            add("ERROR", area, f"unknown rule '{rule}' (expected one of {sorted(VALID_RULES)}).")
            continue
        structural = t.get("structural")
        if structural is not None and not _band_ok(structural.get("low"), structural.get("high")):
            add("ERROR", area, f"invalid structural band {structural}.")

        cur = weights.get(sym)
        held = cur is not None
        cur_val = cur if held else 0.0

        if rule == "reduce":
            if not held or cur_val <= EPS:
                add("ERROR", area, "rule 'reduce' but the position is not held -- nothing to reduce.")
            elif low > cur_val + EPS:
                add("ERROR", area,
                    f"rule 'reduce' but target floor {low:g}% is above current {cur_val:g}% "
                    f"(would require buying).")
        elif rule in {"trim_only", "do_not_add"}:
            if low > cur_val + EPS:
                add("ERROR", area,
                    f"rule '{rule}' but target floor {low:g}% is above current {cur_val:g}% "
                    f"(reaching it requires buying, which the rule forbids).")
        elif rule == "avoid":
            if high > EPS:
                add("WARN", area, f"rule 'avoid' but band high is {high:g}% (expected 0).")
            if held and cur_val > EPS:
                add("INFO", area, f"marked 'avoid' but held at {cur_val:g}% -- consider trimming to 0.")
        elif rule == "accumulate":
            if held and cur_val > high + EPS:
                add("WARN", area,
                    f"rule 'accumulate' but current {cur_val:g}% already exceeds ceiling {high:g}% "
                    f"-- should this be 'trim_only'?")
        elif rule == "hold":
            if not held:
                add("WARN", area, "rule 'hold' but the position is not held.")
            elif _status(cur_val, low, high) != "IN":
                add("WARN", area, f"rule 'hold' but current {cur_val:g}% is outside band [{low:g}, {high:g}].")
        elif rule == "wait":
            if held and cur_val > high + EPS:
                add("INFO", area, f"marked 'wait' but already held at {cur_val:g}% (above band high {high:g}%).")

    # --- feasibility & coverage ---
    sum_low = (
        sum(float(t["low"]) for t in targets.values() if _band_ok(t.get("low"), t.get("high")))
        + sum(float(s["low"]) for s in sleeves.values() if _band_ok(s.get("low"), s.get("high")))
        + cash_target
    )
    if sum_low > 100.0 + EPS:
        add("ERROR", "model",
            f"infeasible: minimum targets + cash sum to {sum_low:.1f}% (> 100% of NAV).")

    def mid(b: dict[str, Any]) -> float:
        return (float(b["low"]) + float(b["high"])) / 2.0

    # All positions-only (cash is not a position); keep the two bases separate.
    managed_target_mid = (
        sum(mid(t) for t in targets.values() if _band_ok(t.get("low"), t.get("high")))
        + sum(mid(s) for s in sleeves.values() if _band_ok(s.get("low"), s.get("high")))
    )
    managed_syms = set(targets) | seen_in_sleeve.keys()
    untargeted_cur = sum(w for sym, w in weights.items() if sym not in managed_syms)
    n_untargeted = sum(1 for sym in weights if sym not in managed_syms)

    if managed_target_mid + cash_target > 100.0 + EPS:
        add("WARN", "model",
            f"midpoint targets ({managed_target_mid:.1f}%) + cash ({cash_target:g}%) exceed 100% of NAV; "
            f"bands would have to run near their lows to fit.")

    budget = 100.0 - managed_target_mid - cash_target
    gap = untargeted_cur - budget
    tail = ""
    if gap > EPS:
        tail = f" -- about {gap:.1f}% must be trimmed from it to fund the plan."
    elif gap < -EPS:
        tail = f" -- about {-gap:.1f}% of headroom remains there."
    add("INFO", "model",
        f"targets want managed names at ~{managed_target_mid:.1f}% + {cash_target:g}% cash, "
        f"leaving ~{budget:.1f}% for the {n_untargeted} untargeted 'hold' names "
        f"(currently {untargeted_cur:.1f}%){tail}")

    return findings


def preview(model: dict[str, Any], holdings: dict[str, Any]) -> None:
    weights = current_weights(holdings)
    targets: dict[str, Any] = model.get("targets", {})
    sleeves: dict[str, Any] = model.get("sleeves", {})

    print(f"Target model as_of {model.get('as_of')} vs snapshot {holdings.get('generated_at')}")
    print(f"{'name':12} {'rule':11} {'cur%':>7} {'band':>12} {'drift':>8}  status")
    print("-" * 62)

    def row(name: str, rule: str, cur: float, low: float, high: float) -> None:
        midpoint = (low + high) / 2.0
        drift = cur - midpoint
        band = f"{low:g}-{high:g}"
        print(f"{name:12} {rule:11} {cur:7.2f} {band:>12} {drift:+8.2f}  {_status(cur, low, high)}")

    for sym, t in targets.items():
        if not _band_ok(t.get("low"), t.get("high")):
            continue
        row(sym, str(t.get("rule")), weights.get(sym, 0.0), float(t["low"]), float(t["high"]))

    for name, sl in sleeves.items():
        if not _band_ok(sl.get("low"), sl.get("high")):
            continue
        members = sl.get("members", [])
        cur = sum(weights.get(m, 0.0) for m in members)
        row(f"[{name}]", str(sl.get("rule", "accumulate")), cur, float(sl["low"]), float(sl["high"]))

    cash_target = float(model.get("cash_target_pct", 0.0) or 0.0)
    print("-" * 62)
    print(f"cash target {cash_target:g}% of NAV. Drift is current minus band midpoint; "
          f"act only when status != IN.")


def advice(model: dict[str, Any], holdings: dict[str, Any]) -> None:
    """Plain-language suggestions from out-of-band drift. Advice, not orders."""
    weights = current_weights(holdings)
    targets: dict[str, Any] = model.get("targets", {})
    sleeves: dict[str, Any] = model.get("sleeves", {})
    funding_order: list[str] = model.get("funding_order", [])

    trims: list[tuple[str, str, float, float, float, float]] = []
    buys: list[tuple[str, str, float, float, float, float]] = []

    def consider(name: str, rule: str, cur: float, low: float, high: float) -> None:
        st = _status(cur, low, high)
        if st == "ABOVE" and rule in NO_BUY_RULES:
            trims.append((name, rule, cur, low, high, cur - high))
        elif st == "ABOVE" and rule == "accumulate":
            trims.append((name, "accumulate?", cur, low, high, cur - high))
        elif st == "BELOW" and rule in {"accumulate", "hold"}:
            buys.append((name, rule, cur, low, high, low - cur))

    for sym, t in targets.items():
        if _band_ok(t.get("low"), t.get("high")):
            consider(sym, str(t.get("rule")), weights.get(sym, 0.0), float(t["low"]), float(t["high"]))
    for nm, sl in sleeves.items():
        if _band_ok(sl.get("low"), sl.get("high")):
            cur = sum(weights.get(m, 0.0) for m in sl.get("members", []))
            consider(f"[{nm}]", str(sl.get("rule", "accumulate")), cur, float(sl["low"]), float(sl["high"]))

    def fpri(name: str) -> int:
        return funding_order.index(name) if name in funding_order else len(funding_order)

    trims.sort(key=lambda r: (fpri(r[0]), -r[5]))
    buys.sort(key=lambda r: -r[5])

    print("\nSuggested actions (advice only -- not orders; refresh IBKR before trading):")
    if not trims and not buys:
        print("  Everything is within its band. No action needed.")
        return
    for name, rule, cur, low, high, amt in trims:
        verb = "REVIEW (over ceiling)" if rule == "accumulate?" else "TRIM/SELL"
        print(f"  {verb:21} {name:11} ~{amt:4.1f}% NAV  (now {cur:5.2f}% -> band {low:g}-{high:g})")
    for name, rule, cur, low, high, amt in buys:
        print(f"  {'ADD/BUY':21} {name:11} ~{amt:4.1f}% NAV  (now {cur:5.2f}% -> band {low:g}-{high:g})")
    freed = sum(r[5] for r in trims if not r[1].endswith("?"))
    wanted = sum(r[5] for r in buys)
    print(f"  Named trims free ~{freed:.1f}% NAV; buys want ~{wanted:.1f}% NAV.", end="")
    if wanted - freed > EPS:
        print(f" ~{wanted - freed:.1f}% more must come from cash or the untargeted 'hold' bucket.")
    else:
        print(" trims cover the buys.")
    if funding_order:
        print(f"  Funding order: {', '.join(funding_order)}, then cash, then the hold bucket.")


def report(findings: list[Finding], strict: bool) -> int:
    findings.sort(key=lambda f: (SEV_ORDER.get(f.severity, 9), f.area))
    for f in findings:
        print(f"[{f.severity}] {f.area}: {f.message}")
    errors = sum(1 for f in findings if f.severity == "ERROR")
    warns = sum(1 for f in findings if f.severity == "WARN")
    infos = sum(1 for f in findings if f.severity == "INFO")
    print(f"\n{errors} error(s), {warns} warning(s), {infos} info.")
    if errors:
        return 1
    if strict and warns:
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="validate the target model; exit 1 on ERROR.")
    parser.add_argument("--strict", action="store_true", help="with --check, also exit 1 on WARN.")
    args = parser.parse_args()

    holdings = _load(HOLDINGS_JSON)
    model = _load(TARGET_MODEL_JSON)

    if args.check:
        return report(check_model(model, holdings), args.strict)

    preview(model, holdings)
    advice(model, holdings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
