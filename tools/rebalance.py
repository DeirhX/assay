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
import sys
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import portfolio  # noqa: E402  -- single source of truth for position weights
from hygiene import SEV_RANK  # noqa: E402  -- shared severity rank
from portfolio import HOLDINGS_JSON, TARGET_MODEL_JSON  # noqa: E402  -- canonical data paths

EPS = 0.01  # weights are 2-decimal percents; tolerate rounding noise
COVERAGE_WARN_PCT = 1.0  # an untargeted held name at/above this size is a real gap, not a stub
# Cash basis decision (roadmap "make the cash target real", option 1): position
# weights stay on the *invested* book, and cash is a separate first-class line
# measured as percent of *NAV* — matching how cash_target_pct is documented.
# The band is target ± cash_band_pp (model-overridable). Cash is measured and
# flagged, never traded: no buy/sell action is ever emitted for cash drift.
CASH_BAND_PP_DEFAULT = 2.0

VALID_RULES = {"accumulate", "trim_only", "do_not_add", "reduce", "hold", "wait", "avoid"}
NO_BUY_RULES = {"trim_only", "do_not_add", "reduce", "avoid"}


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
    """symbol -> weight (percent of invested book).

    Single source of truth: delegates to ``portfolio.holdings_weights``, which
    recomputes from market value. We deliberately do NOT read the broker's
    ``percent_of_nav`` field -- it is notional-poisoned for options (a 2-lot put
    can be tagged 100%). The validator, the CLI, and the web planner all share
    this one definition so they can never disagree about "the weight".
    """
    return portfolio.holdings_weights(holdings)


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


def cash_block(model: dict[str, Any], holdings: dict[str, Any]) -> dict[str, Any] | None:
    """Cash as a first-class informational line: current cash (base currency)
    as % of NAV against the ``cash_target_pct`` band. None when the model sets
    no cash target (nothing to steer against) or the snapshot has no cash/NAV
    data — the planner then degrades to the old display-only figure. Not a
    plan row: cash isn't tradeable and gets no suggestion."""
    if not isinstance(holdings, dict):
        return None
    target = float(model.get("cash_target_pct", 0.0) or 0.0)
    if target <= EPS:
        return None
    cash = portfolio.cash_base(holdings)
    nav = holdings.get("net_asset_value")
    if cash is None or not isinstance(nav, (int, float)) or nav <= 0:
        return None
    tol = float(model.get("cash_band_pp", CASH_BAND_PP_DEFAULT) or CASH_BAND_PP_DEFAULT)
    low = max(0.0, target - tol)
    high = target + tol
    pct = cash / nav * 100.0
    return {
        "czk": round(cash),
        "nav": nav,
        "pct_of_nav": round(pct, 2),
        "target_pct": target,
        "band_pp": tol,
        "low": round(low, 2),
        "high": round(high, 2),
        "status": _status(pct, low, high),
    }


def check_model(model: dict[str, Any], holdings: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []

    def add(sev: str, area: str, msg: str) -> None:
        findings.append(Finding(sev, area, msg))

    weights = current_weights(holdings)
    targets: dict[str, Any] = model.get("targets", {})
    sleeves: dict[str, Any] = model.get("sleeves", {})
    cash_target = float(model.get("cash_target_pct", 0.0) or 0.0)

    # --- holdings hygiene (independent of the model) ---
    # Weights come from base_market_value (see current_weights), so validate that
    # field -- not the broker's misleading percent_of_nav tag.
    for pos in holdings.get("positions", []):
        sym = pos.get("symbol", "?")
        bmv = pos.get("base_market_value")
        if not isinstance(bmv, (int, float)):
            add("INFO", f"holdings:{sym}", "position has no base_market_value (skipped in weights).")
        elif bmv < 0.0:
            add("INFO", f"holdings:{sym}",
                "negative market value (short leg) -- counts negatively toward weights.")

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
        cur_val = cur if cur is not None else 0.0

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

    # Cash drift: measured against the cash_target_pct band (% of NAV). WARN
    # only — cash is never a trade command, but a breached floor should be as
    # visible as any out-of-band name.
    cb = cash_block(model, holdings)
    if cb and cash_target > EPS and cb["status"] != "IN":
        direction = "below" if cb["status"] == "BELOW" else "above"
        add("WARN", "cash",
            f"cash is {cb['pct_of_nav']:.1f}% of NAV, {direction} the target band "
            f"[{cb['low']:g}, {cb['high']:g}] (target {cash_target:g}%).")

    def mid(b: dict[str, Any]) -> float:
        return (float(b["low"]) + float(b["high"])) / 2.0

    # All positions-only (cash is not a position); keep the two bases separate.
    managed_target_mid = (
        sum(mid(t) for t in targets.values() if _band_ok(t.get("low"), t.get("high")))
        + sum(mid(s) for s in sleeves.values() if _band_ok(s.get("low"), s.get("high")))
    )
    managed_syms = set(targets) | seen_in_sleeve.keys()
    untargeted = sorted(
        ((sym, w) for sym, w in weights.items() if sym not in managed_syms),
        key=lambda kv: -kv[1],
    )
    untargeted_cur = sum(w for _, w in untargeted)
    n_untargeted = len(untargeted)

    # Standing coverage invariant: a held position with no band/rule is governed
    # by nothing -- drift can't be computed and it silently rides outside the
    # plan. Name each one (WARN if it's a meaningful size, INFO if a stub).
    for sym, w in untargeted:
        sev = "WARN" if w >= COVERAGE_WARN_PCT else "INFO"
        add(sev, f"coverage:{sym}",
            f"held at {w:.2f}% of book but absent from target-model.json "
            f"(no band/rule governs it; add a target or a sleeve).")

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
        print(f"  {verb:21} {name:11} ~{amt:4.1f}% book (now {cur:5.2f}% -> band {low:g}-{high:g})")
    for name, rule, cur, low, high, amt in buys:
        print(f"  {'ADD/BUY':21} {name:11} ~{amt:4.1f}% book (now {cur:5.2f}% -> band {low:g}-{high:g})")
    freed = sum(r[5] for r in trims if not r[1].endswith("?"))
    wanted = sum(r[5] for r in buys)
    print(f"  Named trims free ~{freed:.1f}% book; buys want ~{wanted:.1f}% book.", end="")
    if wanted - freed > EPS:
        print(f" ~{wanted - freed:.1f}% more must come from cash or the untargeted 'hold' bucket.")
    else:
        print(" trims cover the buys.")
    if funding_order:
        print(f"  Funding order: {', '.join(funding_order)}, then cash, then the hold bucket.")


def _suggest(rule: str, status: str, cur: float, low: float, high: float) -> tuple[str | None, float]:
    """The minimal band-closing action for one position, as (action, delta_pct)
    where delta is signed (negative = sell). Mirrors ``advice()`` so the UI and
    the CLI never disagree about what to do."""
    if status == "ABOVE":
        if rule in NO_BUY_RULES:
            return "trim", high - cur          # sell down to the ceiling
        if rule == "accumulate":
            return "review", high - cur        # over ceiling on a buy rule: flag, don't auto-count
    elif status == "BELOW":
        if rule in {"accumulate", "hold"}:
            return "buy", low - cur            # buy up to the floor
    return None, 0.0


def _band_landing_pct(action: str, low: float, high: float) -> float:
    """Land just inside a band so CZK/share rounding does not grade it outside."""
    buffer_pct = min(0.02, max(0.0, (high - low) / 2.0))
    return low + buffer_pct if action == "buy" else high - buffer_pct


def _coupled_band_suggestions(
    model: dict[str, Any],
    weights: dict[str, float],
    invested: float,
) -> dict[tuple[str, str], tuple[str | None, float]]:
    """Solve all automatic band-closing trades against one final denominator.

    Rows are disjoint governance groups (a target symbol or a sleeve aggregate).
    For active groups, ``value_after = target_weight * invested_after`` and the
    same ``invested_after`` is used for every row.  Ambiguous overlapping model
    rows remain review-only rather than double-counting the same stock.
    """
    if invested <= EPS:
        return {}
    specs: list[dict[str, Any]] = []
    targets: dict[str, Any] = model.get("targets", {})
    sleeves: dict[str, Any] = model.get("sleeves", {})

    def add(
        key: tuple[str, str],
        rule: str,
        cur: float,
        low: float,
        high: float,
        symbols: set[str],
    ) -> None:
        action, fallback_delta = _suggest(rule, _status(cur, low, high), cur, low, high)
        specs.append({
            "key": key,
            "action": action,
            "fallback_delta": fallback_delta,
            "current_value": cur / 100.0 * invested,
            "target_pct": (
                _band_landing_pct(action, low, high)
                if action in {"buy", "trim"} else None
            ),
            "symbols": symbols,
        })

    for sym, target in targets.items():
        if _band_ok(target.get("low"), target.get("high")):
            add(
                ("target", sym),
                str(target.get("rule")),
                weights.get(sym, 0.0),
                float(target["low"]),
                float(target["high"]),
                {portfolio.clean_symbol(sym)},
            )
    for name, sleeve in sleeves.items():
        if not _band_ok(sleeve.get("low"), sleeve.get("high")):
            continue
        members = {
            portfolio.clean_symbol(member)
            for member in sleeve.get("members", [])
            if member
        }
        add(
            ("sleeve", name),
            str(sleeve.get("rule", "accumulate")),
            sum(weights.get(member, 0.0) for member in members),
            float(sleeve["low"]),
            float(sleeve["high"]),
            members,
        )

    owners: dict[str, int] = {}
    for spec in specs:
        for symbol in spec["symbols"]:
            owners[symbol] = owners.get(symbol, 0) + 1
    result = {
        spec["key"]: (spec["action"], spec["fallback_delta"])
        for spec in specs
    }
    active = [
        spec for spec in specs
        if spec["action"] in {"buy", "trim"}
        and all(owners[symbol] == 1 for symbol in spec["symbols"])
    ]
    for spec in specs:
        if spec["action"] in {"buy", "trim"} and spec not in active:
            result[spec["key"]] = ("review", 0.0)

    # Coupling can change the required sign for an edge-case row. Remove such a
    # row and solve again rather than recommending a trade opposite its rule.
    while active:
        target_sum = sum(float(spec["target_pct"]) / 100.0 for spec in active)
        fixed_value = invested - sum(float(spec["current_value"]) for spec in active)
        if target_sum >= 1.0 - 1e-9 or fixed_value < -EPS:
            for spec in active:
                result[spec["key"]] = ("review", 0.0)
            break
        final_invested = fixed_value / (1.0 - target_sum)
        solved = {
            spec["key"]: (
                float(spec["target_pct"]) / 100.0 * final_invested
                - float(spec["current_value"])
            )
            for spec in active
        }
        wrong_sign = [
            spec for spec in active
            if (
                spec["action"] == "buy" and solved[spec["key"]] <= EPS
            ) or (
                spec["action"] == "trim" and solved[spec["key"]] >= -EPS
            )
        ]
        if wrong_sign:
            for spec in wrong_sign:
                result[spec["key"]] = ("review", 0.0)
                active.remove(spec)
            continue
        for spec in active:
            delta_pct = solved[spec["key"]] / invested * 100.0
            result[spec["key"]] = (spec["action"], delta_pct)
        break
    return result


def band_counts(plan: dict[str, Any]) -> tuple[int, int]:
    """In-band and total counts for every displayed target and sleeve row."""
    rows = [
        row for row in plan.get("rows", [])
        if row.get("kind") in {"target", "sleeve"}
    ]
    return sum(1 for row in rows if row.get("status") == "IN"), len(rows)


def _allocate_sleeve_members(
    sl: dict[str, Any], members: list[str], weights: dict[str, float],
    czk: Callable[[float | None], int | None], action: str | None, delta: float,
    provenance: dict[str, Any],
) -> list[dict[str, Any]]:
    """Turn a sleeve's *aggregate* buy/trim suggestion into per-member advice so
    the UI can show which names to act on, in what order, and stage member trades
    into the basket.

    Each member gets a target weight: an even split of the sleeve midpoint,
    capped by ``member_caps``. The sleeve's suggested delta is then spread across
    members in proportion to their room-to-target (for buys) or excess-over-target
    (for trims), so the per-member deltas sum back to the sleeve delta. Members
    already at/above their share get nothing; the most under-weight name leads the
    order. Pure advice — the human still edits the amounts."""
    low = float(sl["low"])
    high = float(sl["high"])
    mid = (low + high) / 2.0
    caps = sl.get("member_caps", {}) or {}
    n = max(1, len(members))
    base_share = mid / n

    def target_for(sym: str) -> float:
        cap = caps.get(sym)
        return min(base_share, float(cap)) if cap is not None else base_share

    curs = {m: weights.get(m, 0.0) for m in members}
    rooms = {m: max(0.0, target_for(m) - curs[m]) for m in members}
    excess = {m: max(0.0, curs[m] - target_for(m)) for m in members}
    sum_room = sum(rooms.values())
    sum_excess = sum(excess.values())

    out: list[dict[str, Any]] = []
    for m in members:
        cur = curs[m]
        cap = caps.get(m)
        mdelta = 0.0
        if action == "buy" and delta > EPS:
            mdelta = delta * (rooms[m] / sum_room) if sum_room > EPS else delta / n
        elif action == "trim" and delta < -EPS:
            mdelta = delta * (excess[m] / sum_excess) if sum_excess > EPS else delta / n
        prov = provenance.get(m) if isinstance(provenance, dict) else None
        out.append({
            "symbol": m,
            "current_pct": round(cur, 2),
            "current_czk": czk(cur),
            "cap": (float(cap) if cap is not None else None),
            "target_pct": round(target_for(m), 2),
            "conviction": (prov or {}).get("conviction") if isinstance(prov, dict) else None,
            "suggest_delta_pct": round(mdelta, 2),
            "suggest_delta_czk": czk(mdelta),
            "member_action": ("buy" if mdelta > EPS else "trim" if mdelta < -EPS else None),
        })
    # Order by the size of the suggested move (biggest first); ties and no-ops
    # keep the model's member order.
    for rank, i in enumerate(sorted(range(len(out)), key=lambda i: -abs(out[i]["suggest_delta_pct"])), start=1):
        out[i]["order"] = rank
    return out


def plan(model: dict[str, Any], holdings: dict[str, Any]) -> dict[str, Any]:
    """Structured drift + suggested-action data for the UI rebalance planner.

    Shares ``current_weights`` with ``check_model`` so the interactive view, the
    CLI preview, and the validator agree on every number. Weights are percent of
    the *invested* book (market value over invested value), so amounts are sized
    off invested value -- making ``current_czk`` equal the actual market value,
    not an inflated NAV-based figure. This is advice, not orders."""
    weights = current_weights(holdings)
    positions = holdings.get("positions", []) if isinstance(holdings, dict) else []
    invested = portfolio.invested_value(positions)
    nav = holdings.get("net_asset_value") if isinstance(holdings, dict) else None
    targets: dict[str, Any] = model.get("targets", {})
    sleeves: dict[str, Any] = model.get("sleeves", {})
    cash_target = float(model.get("cash_target_pct", 0.0) or 0.0)
    solved_suggestions = _coupled_band_suggestions(model, weights, invested)

    def czk(pct: float | None) -> int | None:
        if not invested or pct is None:
            return None
        return round(pct / 100.0 * invested)

    rows: list[dict[str, Any]] = []

    def add_row(key: str, name: str, kind: str, rule: str, cur: float, low: float,
                high: float, note: Any, members: list[dict[str, Any]] | None = None,
                interactive: bool = True) -> None:
        status = _status(cur, low, high)
        action, delta = solved_suggestions.get(
            (kind, name),
            _suggest(rule, status, cur, low, high),
        )
        mid = (low + high) / 2.0
        rows.append({
            "key": key, "name": name, "kind": kind, "rule": rule,
            "held": cur > EPS,
            "current_pct": round(cur, 2), "current_czk": czk(cur),
            "low": low, "high": high, "mid": round(mid, 2),
            "status": status, "drift_pct": round(cur - mid, 2),
            "action": action,
            "suggest_delta_pct": round(delta, 2), "suggest_delta_czk": czk(delta),
            "note": note, "members": members, "interactive": interactive,
        })

    for sym, t in targets.items():
        if _band_ok(t.get("low"), t.get("high")):
            add_row(sym, sym, "target", str(t.get("rule")), weights.get(sym, 0.0),
                    float(t["low"]), float(t["high"]), t.get("note"))

    provenance = model.get("provenance", {}) if isinstance(model, dict) else {}
    for name, sl in sleeves.items():
        if not _band_ok(sl.get("low"), sl.get("high")):
            continue
        members = sl.get("members", [])
        low = float(sl["low"])
        high = float(sl["high"])
        cur = sum(weights.get(m, 0.0) for m in members)
        rule = str(sl.get("rule", "accumulate"))
        action, delta = solved_suggestions.get(
            ("sleeve", name),
            _suggest(rule, _status(cur, low, high), cur, low, high),
        )
        member_rows = _allocate_sleeve_members(sl, members, weights, czk, action, delta, provenance)
        add_row(f"[{name}]", name, "sleeve", rule, cur, low, high, sl.get("note"),
                members=member_rows, interactive=False)

    managed = set(targets) | {m for sl in sleeves.values() for m in sl.get("members", [])}
    untargeted = sorted(((s, w) for s, w in weights.items() if s not in managed),
                        key=lambda kv: -kv[1])
    untargeted_pct = sum(w for _, w in untargeted)

    return {
        "as_of": model.get("as_of"),
        "snapshot": holdings.get("generated_at") if isinstance(holdings, dict) else None,
        "nav": nav,
        "invested": invested,
        "currency": (holdings.get("base_currency") if isinstance(holdings, dict) else None) or "CZK",
        "cash_target_pct": cash_target,
        # First-class cash line (or None when the snapshot has no cash/NAV):
        # current cash vs the target band, for display and what-if steering.
        "cash": cash_block(model, holdings),
        "funding_order": model.get("funding_order", []),
        "rows": rows,
        "untargeted": [{"symbol": s, "current_pct": round(w, 2), "current_czk": czk(w)}
                       for s, w in untargeted],
        "untargeted_pct": round(untargeted_pct, 2),
    }


def funding_candidates(model: dict[str, Any], holdings: dict[str, Any],
                       needed_czk: float, *, exclude: Any = ()) -> dict[str, Any]:
    """Deterministic funding suggestions when a plan's buys outrun its trims.

    Walks the model's ``funding_order`` first (the human's stated priority),
    then untargeted held names largest-first — the bucket the model itself says
    funds the plan. Each candidate can give at most its headroom: down to the
    band floor for a targeted name, the whole position for an untargeted one.
    Sleeve members are skipped (they're governed collectively). ``exclude``
    drops names the user is already trading in the edited plan. Advice only —
    nothing here creates trades; the amounts land as editable plan inputs."""
    weights = current_weights(holdings)
    invested = portfolio.invested_value(holdings.get("positions", []) or [])
    targets: dict[str, Any] = model.get("targets", {})
    sleeves: dict[str, Any] = model.get("sleeves", {})
    sleeve_members = {m for sl in sleeves.values() for m in (sl.get("members") or [])}
    excl = {str(s or "").strip().upper() for s in (exclude or ())}
    needed = max(0.0, float(needed_czk or 0.0))

    def czk(pct: float) -> float:
        return pct / 100.0 * invested if invested else 0.0

    candidates: list[dict[str, Any]] = []
    remaining = needed
    seen: set[str] = set()

    def consider(sym: str, source: str) -> None:
        nonlocal remaining
        if remaining <= EPS or not sym or sym in seen or sym in excl:
            return
        seen.add(sym)
        cur = weights.get(sym)
        if cur is None or cur <= EPS:
            return
        t = targets.get(sym)
        floor = None
        if isinstance(t, dict) and _band_ok(t.get("low"), t.get("high")):
            floor = float(t["low"])
            avail_pct = max(0.0, cur - floor)
        elif sym in sleeve_members:
            return  # collectively governed; per-member funding is the sleeve UI's job
        else:
            avail_pct = cur
        avail = czk(avail_pct)
        if avail < 1.0:
            return
        take = min(remaining, avail)
        candidates.append({
            "symbol": sym,
            "source": source,
            "current_pct": round(cur, 2),
            "floor_pct": floor,
            "available_czk": round(avail),
            "suggest_czk": round(take),
            "suggest_pct": round(-(take / invested * 100.0), 2) if invested else 0.0,
        })
        remaining -= take

    for sym in model.get("funding_order", []) or []:
        consider(str(sym or "").strip().upper(), "funding_order")
    managed = set(targets) | sleeve_members
    for sym, w in sorted(weights.items(), key=lambda kv: -kv[1]):
        if sym not in managed:
            consider(sym, "untargeted")

    covered = needed - remaining
    return {
        "needed_czk": round(needed),
        "covered_czk": round(covered),
        "shortfall_czk": round(max(0.0, remaining)),
        "candidates": candidates,
    }


def report(findings: list[Finding], strict: bool) -> int:
    findings.sort(key=lambda f: (SEV_RANK.get(f.severity, 9), f.area))
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

    portfolio.require_data()
    holdings = _load(HOLDINGS_JSON)
    model = _load(TARGET_MODEL_JSON)

    if args.check:
        return report(check_model(model, holdings), args.strict)

    preview(model, holdings)
    advice(model, holdings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
