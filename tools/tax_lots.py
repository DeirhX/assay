#!/usr/bin/env python3
"""Czech tax-lot-aware sell planner.

``rebalance.py`` says *how much* of a name to trim. It does not say *which lots*
to sell -- and for a Czech tax-resident individual that is the difference between
a clean rebalance and an avoidable tax bill. This module closes that gap: given a
symbol and an amount to raise, it picks specific tax lots and reports the result
bucketed into exempt / harvestable-loss / taxable-gain.

Czech context (default assumptions; verify with a professional before acting):

* Listed securities held **3 years or longer** are exempt from personal income
  tax on the gain. The clock is the lot's ``open_datetime`` -- never IBKR's
  ``ST``/``LT`` code, which uses US rules.
* Taxable securities losses can offset taxable securities gains **in the same tax
  period** -- but losses on exempt (3y+) sales are not usable.
* The CZK 100,000 annual gross-proceeds exemption is ignored here; it is
  irrelevant at serious rebalancing sizes.

So to minimize tax for a given trim, the default lot order is:

1. exempt lots sitting on a gain   -- realize the gain tax-free,
2. taxable lots sitting on a loss  -- harvest the loss (offsets other gains),
3. exempt lots sitting on a loss   -- neutral (no tax either way),
4. taxable lots sitting on a gain  -- taxable; smallest gain-per-koruna first.

This is analysis, not tax advice, and it never trades.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import portfolio  # noqa: E402

EXEMPT_YEARS = 3
EPS = 1e-6

# Bucket -> selection priority (lower sells first). The order is the whole point:
# realize tax-free gains and harvest usable losses before touching taxable gains.
_BUCKET_PRIORITY = {
    "exempt_gain": 0,
    "taxable_loss": 1,
    "exempt_loss": 2,
    "taxable_gain": 3,
}


def add_years(d: dt.date, years: int) -> dt.date:
    """``d`` plus ``years`` calendar years, collapsing Feb 29 to Feb 28."""
    try:
        return d.replace(year=d.year + years)
    except ValueError:
        return d.replace(month=2, day=28, year=d.year + years)


def _parse_open(value: Any) -> dt.datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        # Date-only or odd formats: try the leading date.
        try:
            return dt.datetime.fromisoformat(text[:10]).replace(tzinfo=dt.timezone.utc)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _as_of(as_of: dt.datetime | None) -> dt.datetime:
    if as_of is None:
        return dt.datetime.now(dt.timezone.utc)
    if as_of.tzinfo is None:
        return as_of.replace(tzinfo=dt.timezone.utc)
    return as_of


def held_days(open_dt: dt.datetime | None, as_of: dt.datetime) -> int | None:
    if open_dt is None:
        return None
    return (as_of - open_dt).days


def is_exempt(open_dt: dt.datetime | None, as_of: dt.datetime) -> bool:
    """True when the lot has cleared the Czech 3-year holding period."""
    if open_dt is None:
        return False
    return as_of.date() >= add_years(open_dt.date(), EXEMPT_YEARS)


def lots_for_symbol(holdings: dict[str, Any], symbol: str) -> list[dict[str, Any]]:
    sym = portfolio.clean_symbol(symbol)
    out: list[dict[str, Any]] = []
    for lot in holdings.get("lots", []) or []:
        if portfolio.clean_symbol(lot.get("symbol")) == sym:
            out.append(lot)
    return out


def _lot_economics(lot: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize a raw IBKR lot into the fields the selector needs.

    IBKR reports ``base_market_value`` in the account base currency but
    ``cost_basis_money`` / ``market_value`` / ``unrealized_pnl`` in the *lot's
    own* currency (e.g. KRW for 005930.KS, USD for ARM). Converting the cost to
    base with the lot's FX ratio (base_market_value / market_value) keeps the
    gain a like-for-like subtraction — otherwise a foreign lot's gain is a
    CZK-minus-KRW nonsense that shows up as a huge phantom loss.
    """
    qty = lot.get("quantity")
    mv = lot.get("base_market_value")
    cost = lot.get("cost_basis_money")
    if not isinstance(qty, (int, float)) or abs(qty) < EPS:
        return None
    if not isinstance(mv, (int, float)):
        return None
    local_mv = lot.get("market_value")
    fx = (mv / local_mv) if isinstance(local_mv, (int, float)) and abs(local_mv) > EPS else 1.0
    if isinstance(cost, (int, float)):
        cost = cost * fx                      # local cost basis -> base currency
    else:
        # Fall back to market value minus reported P/L (also lot-local) if cost
        # is absent.
        pnl = lot.get("unrealized_pnl")
        cost = (mv - pnl * fx) if isinstance(pnl, (int, float)) else None
    if not isinstance(cost, (int, float)):
        return None
    price = mv / qty if qty else 0.0
    return {"qty": float(qty), "mv": float(mv), "cost": float(cost), "price": float(price)}


def _bucket(exempt: bool, gain: float) -> str:
    if exempt:
        return "exempt_gain" if gain >= 0 else "exempt_loss"
    return "taxable_gain" if gain >= 0 else "taxable_loss"


def _candidates(lots: list[dict[str, Any]], as_of: dt.datetime) -> list[dict[str, Any]]:
    """Normalize raw lots into economics-and-tax-classified candidates.

    Shared by :func:`select_lots` (which then sorts + fills a trim amount) and
    :func:`classify_lots` (which returns them as-is for the exit planner's tax
    layering). One place computes exemption, days-to-exempt, gain and bucket so
    the two callers can never disagree.
    """
    out: list[dict[str, Any]] = []
    for lot in lots:
        econ = _lot_economics(lot)
        if econ is None or econ["mv"] <= EPS:
            continue
        open_dt = _parse_open(lot.get("open_datetime"))
        exempt = is_exempt(open_dt, as_of)
        gain = econ["mv"] - econ["cost"]
        out.append({
            "open_datetime": lot.get("open_datetime"),
            "open_dt": open_dt,
            "exempt": exempt,
            "held_days": held_days(open_dt, as_of),
            "days_to_exempt": (None if exempt or open_dt is None
                               else max(0, (add_years(open_dt.date(), EXEMPT_YEARS) - as_of.date()).days)),
            "exempt_on": (None if exempt or open_dt is None
                          else add_years(open_dt.date(), EXEMPT_YEARS).isoformat()),
            "qty": econ["qty"],
            "mv": econ["mv"],
            "cost": econ["cost"],
            "price": econ["price"],
            "gain": gain,
            "bucket": _bucket(exempt, gain),
        })
    return out


def classify_lots(
    holdings: dict[str, Any],
    symbol: str,
    *,
    as_of: dt.datetime | None = None,
) -> list[dict[str, Any]]:
    """Every lot for ``symbol``, tax-classified (bucket, days_to_exempt, gain).

    The exit planner uses this to decide, per lot, sell-now vs defer -- it needs
    the *full* picture, not just the subset :func:`select_lots` would consume for
    a given trim amount. Sorted newest-open first so near-exempt gain lots (the
    ones worth waiting on) surface at the top.
    """
    as_of = _as_of(as_of)
    cands = _candidates(lots_for_symbol(holdings, symbol), as_of)
    for c in cands:
        c.pop("open_dt", None)  # datetime isn't JSON-serializable; drop the internal
    cands.sort(key=lambda c: (c.get("days_to_exempt") is None, -(c.get("days_to_exempt") or 0)))
    return cands


def select_lots(
    lots: list[dict[str, Any]],
    trim_money: float,
    *,
    as_of: dt.datetime | None = None,
) -> dict[str, Any]:
    """Pick lots to raise ~``trim_money`` (base currency) at minimal tax.

    Pure function: it reads lot dicts and returns a breakdown. It does not look
    at the broker, the model, or the network.
    """
    as_of = _as_of(as_of)
    trim_money = abs(float(trim_money or 0.0))

    candidates = _candidates(lots, as_of)

    def sort_key(c: dict[str, Any]):
        prio = _BUCKET_PRIORITY[c["bucket"]]
        if c["bucket"] == "exempt_gain":
            return (prio, -c["gain"])                      # bank the biggest tax-free gain
        if c["bucket"] == "taxable_loss":
            return (prio, c["gain"])                       # harvest the biggest loss first
        if c["bucket"] == "exempt_loss":
            return (prio, c["gain"])
        # taxable_gain: least taxable gain per koruna raised first
        return (prio, c["gain"] / c["mv"] if c["mv"] else 0.0)

    candidates.sort(key=sort_key)

    available = sum(c["mv"] for c in candidates)
    selected: list[dict[str, Any]] = []
    remaining = trim_money
    for c in candidates:
        if remaining <= EPS:
            break
        take_mv = min(c["mv"], remaining)
        frac = take_mv / c["mv"] if c["mv"] else 0.0
        shares = c["qty"] * frac
        proceeds = take_mv
        cost_portion = c["cost"] * frac
        gain_portion = proceeds - cost_portion
        selected.append({
            "open_datetime": c["open_datetime"],
            "exempt": c["exempt"],
            "bucket": c["bucket"],
            "held_days": c["held_days"],
            "days_to_exempt": c["days_to_exempt"],
            "shares": round(shares, 6),
            "proceeds": round(proceeds, 2),
            "cost": round(cost_portion, 2),
            "gain": round(gain_portion, 2),
            "price": round(c["price"], 4),
        })
        remaining -= take_mv

    proceeds_total = sum(s["proceeds"] for s in selected)
    taxable_gain = sum(s["gain"] for s in selected if not s["exempt"])
    exempt_gain = sum(s["gain"] for s in selected if s["exempt"])
    harvestable_loss = -sum(min(0.0, s["gain"]) for s in selected if not s["exempt"])
    realized_gain = sum(s["gain"] for s in selected)
    exempt_proceeds = sum(s["proceeds"] for s in selected if s["exempt"])
    taxable_proceeds = sum(s["proceeds"] for s in selected if not s["exempt"])

    return {
        "as_of": as_of.date().isoformat(),
        "requested": round(trim_money, 2),
        "available": round(available, 2),
        "raised": round(proceeds_total, 2),
        "shortfall": round(max(0.0, trim_money - proceeds_total), 2),
        "lots": selected,
        "n_lots_used": len(selected),
        "n_lots_total": len(candidates),
        "totals": {
            "proceeds": round(proceeds_total, 2),
            "exempt_proceeds": round(exempt_proceeds, 2),
            "taxable_proceeds": round(taxable_proceeds, 2),
            "realized_gain": round(realized_gain, 2),
            # Net taxable gain for the period: gains on <3y lots, with <3y losses
            # netted in (they offset other taxable gains). Can be negative.
            "taxable_gain": round(taxable_gain, 2),
            "exempt_gain": round(exempt_gain, 2),
            "harvestable_loss": round(harvestable_loss, 2),
        },
    }


def breakdown_for_symbol(
    holdings: dict[str, Any],
    symbol: str,
    trim_money: float,
    *,
    as_of: dt.datetime | None = None,
) -> dict[str, Any]:
    """Lot breakdown for trimming ``symbol`` by ``trim_money`` of base currency."""
    lots = lots_for_symbol(holdings, symbol)
    result = select_lots(lots, trim_money, as_of=as_of)
    result["symbol"] = portfolio.clean_symbol(symbol)
    result["currency"] = holdings.get("base_currency") or "CZK"
    result["has_lots"] = bool(lots)
    return result


def enrich_plan(plan: dict[str, Any], holdings: dict[str, Any], *, as_of: dt.datetime | None = None) -> dict[str, Any]:
    """Attach a tax-lot ``tax`` block to each single-name trim row in a rebalance
    plan. Sleeves are spread across members by hand, so they are left alone.

    Mutates and returns ``plan`` so the server can call it inline after
    ``rebalance.plan(...)``. Missing lots degrade to ``has_lots: False`` rather
    than raising -- a bare snapshot without lots should still render a plan.
    """
    for row in plan.get("rows", []):
        if row.get("kind") != "target" or row.get("action") != "trim":
            continue
        delta_czk = row.get("suggest_delta_czk")
        if not isinstance(delta_czk, (int, float)) or delta_czk >= 0:
            continue
        row["tax"] = breakdown_for_symbol(holdings, row["name"], abs(delta_czk), as_of=as_of)
    return plan


def _main() -> int:
    import argparse
    import json

    import store

    parser = argparse.ArgumentParser(description="Czech tax-lot-aware sell planner.")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--amount", type=float, required=True, help="base-currency amount to raise.")
    args = parser.parse_args()

    portfolio.require_data()
    holdings = store.load(Path(__file__).resolve().parent.parent / "data" / "current-holdings.json")
    if not holdings:
        raise SystemExit("no holdings snapshot — sync from IBKR first")
    print(json.dumps(breakdown_for_symbol(holdings, args.symbol, args.amount), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
