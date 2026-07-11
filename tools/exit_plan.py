#!/usr/bin/env python3
"""Advisory graceful-exit planner for unwanted positions.

``rebalance.plan()`` says which names to shrink and by how much; ``tax_lots``
says which lots are cheapest to sell. This module turns that into a *humane* way
out of a position: instead of one market dump, it produces a tax-timed,
liquidity-aware, multi-day scale-out with a suggested GTC limit ladder -- which
the human reviews and stages into the existing trade desk one tranche at a time.

Nothing here trades. It never places an order, never modifies a working order.
"Repricing" is advisory: if a tranche's limit doesn't fill you cancel/re-stage
on the desk and rebuild the plan.

The core lever is **tax layering** (Czech 3-year rule via :mod:`tax_lots`):

* **sell-now** lots -- exempt (3y+) gains and any losses -- carry no tax cost, so
  they fill the early tranches.
* **defer** lots -- taxable gains, *especially* those within ``NEAR_EXEMPT_DAYS``
  of clearing the 3-year clock -- are held back with a "wait until <date>" note
  and a ``tax_saved_by_waiting`` figure, because selling them early converts a
  soon-to-be-tax-free gain into a taxable one.

Money is in the account base currency (CZK) unless noted; limit prices are in
the instrument's own trading currency (what the desk actually sends).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ibkr_trade  # noqa: E402
import market_data  # noqa: E402  -- shared fan-out width
import option_market  # noqa: E402
import portfolio  # noqa: E402
import price_levels  # noqa: E402
import rebalance  # noqa: E402
import risk  # noqa: E402
import tax_lots  # noqa: E402
import timeutil  # noqa: E402  -- shared Z-tolerant ISO parse + cache-freshness

# --------------------------------------------------------------------------- #
# Config knobs (defaults; the API/UI can override per request).
# --------------------------------------------------------------------------- #
EXIT_HORIZON_DAYS = 10      # calendar days to spread the scale-out across
ADV_SLICE_PCT = 0.12        # max child slice as a fraction of average daily volume
DEFAULT_TRANCHES = 4        # slices when ADV is unknown (foreign/thin names)
ADV_WINDOW = 20             # trailing sessions for the ADV estimate
NEAR_EXEMPT_DAYS = 120      # a taxable-gain lot this close to exemption is "defer"
CZ_TAX_RATE = 0.15          # Czech personal income tax on securities gains
STUB_PCT = 0.5              # residual weight left when end-state is "stub"
LADDER_RUNGS_PCT = (0.0, 0.02, 0.04)  # default GTC limit rungs above the mark
FETCH_WORKERS = market_data.FETCH_WORKERS  # shared fan-out width (warms series + chains)
OPTION_MULTIPLIER = ibkr_trade.OPTION_MULTIPLIER
COVERED_CALL_ROUND_UP_MAX_DEVIATION_PCT = 0.15

EPS = 1e-6

EXECUTION_QUOTE_MAX_AGE_SECONDS = ibkr_trade.OPTION_QUOTE_MAX_AGE_SECONDS


def _quote_age_seconds(raw: Any, *, now: dt.datetime | None = None) -> float | None:
    current = now
    if current is not None and current.tzinfo is None:
        current = current.replace(tzinfo=dt.timezone.utc)
    return timeutil.age_seconds(raw, now=current)


def _covered_call_exit_contracts(exit_shares: Any, current_shares: Any) -> int:
    """Whole calls matching the planned exit, with a tightly bounded round-up.

    A call may cover slightly more shares than the exact plan when the deviation
    is at most 15%. It may never cover more shares than are currently held.
    """
    try:
        planned = max(0, int(float(exit_shares or 0)))
        held = max(0, int(float(current_shares or 0)))
    except (TypeError, ValueError):
        return 0
    contracts = planned // OPTION_MULTIPLIER
    rounded_contracts = contracts + 1
    rounded_shares = rounded_contracts * OPTION_MULTIPLIER
    if (
        planned > 0
        and rounded_shares <= held
        and (rounded_shares - planned) / planned
        <= COVERED_CALL_ROUND_UP_MAX_DEVIATION_PCT + EPS
    ):
        contracts = rounded_contracts
    return min(contracts, held // OPTION_MULTIPLIER)


def _execution_routes(entry: dict[str, Any], *, now: dt.datetime | None = None) -> dict[str, Any]:
    """Server-computed route availability; staging repeats every safety check."""
    import trade_service

    schedule = entry.get("schedule") or {}
    sell_ok = bool(schedule.get("tranches"))
    sell_reasons = [] if sell_ok else ["No sell-now tranche; the planned exit is tax-deferred."]
    options = entry.get("options") or {}
    ladder = options.get("covered_call_ladder") or []
    capacity = trade_service.covered_call_capacity(entry["symbol"])
    exit_shares = max(0, int(float(entry.get("exit_shares") or 0)))
    intended = _covered_call_exit_contracts(
        exit_shares,
        capacity.get("current_shares"),
    )
    capacity_contracts = min(int(capacity.get("capacity_contracts") or 0), intended)
    executable: list[dict] = []
    stageable_contracts: list[dict] = []
    for rung in ladder:
        age = _quote_age_seconds(rung.get("quote_timestamp"), now=now)
        rung["quote_age_seconds"] = round(age, 1) if age is not None else None
        rung["quote_fresh"] = age is not None and age <= EXECUTION_QUOTE_MAX_AGE_SECONDS
        bid, ask = rung.get("bid"), rung.get("ask")
        contract_stageable = bool(rung.get("stageable") and rung.get("conid"))
        if contract_stageable:
            stageable_contracts.append(rung)
        if rung.get("executable") and rung["quote_fresh"] and bid and ask:
            # Display estimate only. stage_covered_call obtains the exact tick and
            # recomputes this from a fresh quote.
            rung["limit_price"] = math.floor((((float(bid) + float(ask)) / 2.0) + EPS) * 100) / 100
            executable.append(rung)
        else:
            rung["limit_price"] = None
            if contract_stageable and (bid is None or ask is None):
                rung["staging_warning"] = (
                    "No live bid/ask right now. Staging is allowed, but preview and placement "
                    "remain blocked until IBKR returns a fresh two-sided quote."
                )
            elif contract_stageable and not rung["quote_fresh"]:
                rung["staging_warning"] = (
                    "The displayed quote is stale. Staging will refresh it from IBKR "
                    "before calculating a limit price."
                )

    reasons: list[str] = []
    if capacity_contracts < 1:
        if int(capacity.get("capacity_contracts") or 0) > 0 and exit_shares < OPTION_MULTIPLIER:
            reasons.append(
                f"The planned {exit_shares}-share exit is too far from one "
                f"{OPTION_MULTIPLIER}-share option contract."
            )
        else:
            reasons.append(
                f"No uncovered capacity for this exit ({capacity.get('current_shares', 0)} shares; "
                f"{capacity.get('held_short_calls', 0)} held short call(s))."
            )
    if not ladder:
        reasons.append("No covered-call strike ladder is available for this exit.")
    elif not executable and not stageable_contracts:
        if any(r.get("executable") and not r.get("quote_fresh") for r in ladder):
            reasons.append(
                "Covered-call levels are available, but the IBKR quote is stale; "
                "rebuild before staging."
            )
        else:
            source = str(options.get("source") or "market data").replace("_", " ").title()
            reasons.append(
                f"Indicative covered-call levels from {source} are available; staging needs an "
                "exact IBKR contract with a live two-sided quote."
            )
    covered_ok = capacity_contracts > 0 and bool(ladder)
    stageable = capacity_contracts > 0 and bool(stageable_contracts)
    return {
        "sell_shares": {"eligible": sell_ok, "reasons": sell_reasons},
        "covered_call": {
            "eligible": covered_ok,
            "stageable": stageable,
            "reasons": reasons,
            "capacity_contracts": capacity_contracts,
            "planned_exit_shares": exit_shares,
            "assignment_shares": capacity_contracts * OPTION_MULTIPLIER,
            "share_deviation": capacity_contracts * OPTION_MULTIPLIER - exit_shares,
            "rounded_up": capacity_contracts * OPTION_MULTIPLIER > exit_shares,
        },
        "recommended": "covered_call" if not sell_ok and covered_ok else "sell_shares",
    }


# --------------------------------------------------------------------------- #
# Liquidity
# --------------------------------------------------------------------------- #
def average_daily_volume(points: list[dict[str, Any]] | None, *, window: int = ADV_WINDOW) -> float | None:
    """Mean traded share volume over the trailing ``window`` sessions.

    Reads the ``volume`` field the Yahoo chart passthrough now carries on each
    price point. Returns None when no session has a usable volume (foreign names
    where Yahoo omits it), so the caller falls back to even time-slices.
    """
    vols = [float(v) for p in (points or [])
            if isinstance((v := p.get("volume")), (int, float)) and v]
    if not vols:
        return None
    tail = vols[-window:] if window > 0 else vols
    return sum(tail) / len(tail) if tail else None


# --------------------------------------------------------------------------- #
# Holdings view
# --------------------------------------------------------------------------- #
def _position_index(holdings: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """symbol -> {qty, price_base, price_local, currency, fx, mv_base}. Prices in
    base come from ``base_market_value``; the local mark + FX are what the desk's
    limit orders use, derived per-position (base_mv / local_mv) so foreign names
    are handled without trusting a possibly-absent ``fx_rate_to_base``."""
    out: dict[str, dict[str, Any]] = {}
    for p in holdings.get("positions", []) or []:
        sym = portfolio.clean_symbol(p.get("symbol"))
        qty = p.get("quantity")
        mv_base = p.get("base_market_value")
        if not sym or not isinstance(qty, (int, float)) or abs(qty) < EPS:
            continue
        if not isinstance(mv_base, (int, float)):
            continue
        fx = portfolio.position_fx_to_base(p)
        out[sym] = {
            "qty": float(qty),
            "mv_base": float(mv_base),
            "price_base": float(mv_base) / float(qty),
            "price_local": float(p.get("mark_price")) if isinstance(p.get("mark_price"), (int, float)) else float(mv_base) / float(qty) / fx,
            "currency": (p.get("currency") or "").upper() or None,
            "fx": float(fx),
        }
    return out


# --------------------------------------------------------------------------- #
# End-state
# --------------------------------------------------------------------------- #
def _end_state(row: dict[str, Any], force_zero: bool) -> tuple[str, float]:
    """(label, target_weight_pct) for a candidate. ``avoid`` and explicit
    full-exits go to zero; other trim rules go to the band ceiling; a stub keeps
    a token residual. Returns the *target* weight the exit shrinks the name to."""
    rule = str(row.get("rule") or "")
    high = row.get("high")
    if force_zero or rule == "avoid":
        return "zero", 0.0
    if isinstance(high, (int, float)):
        return "ceiling", float(high)
    return "stub", STUB_PCT


# --------------------------------------------------------------------------- #
# Tax layering
# --------------------------------------------------------------------------- #
def _tax_layers(
    lots: list[dict[str, Any]],
    exit_czk: float,
    *,
    near_exempt_days: int,
    tax_rate: float,
) -> dict[str, Any]:
    """Split the ``exit_czk`` we want to raise into sell-now vs defer against the
    tax-classified ``lots`` for one name.

    Fill order: no-tax-cost lots first (exempt gains, harvestable/neutral losses),
    then taxable-gain lots *furthest* from exemption. Near-exempt taxable-gain
    lots are held back -- selling them early forfeits a nearly-free gain -- and we
    report the tax that waiting would save.
    """
    def is_near_exempt(lot: dict[str, Any]) -> bool:
        d = lot.get("days_to_exempt")
        return lot["bucket"] == "taxable_gain" and isinstance(d, int) and d <= near_exempt_days

    no_cost = [c for c in lots if c["bucket"] in ("exempt_gain", "taxable_loss", "exempt_loss")]
    # tax-optimal within the no-cost tier: bank biggest exempt gains, harvest
    # biggest losses first (reuse tax_lots' bucket priority ordering intent).
    no_cost.sort(key=lambda c: (tax_lots._BUCKET_PRIORITY[c["bucket"]], -abs(c["gain"])))
    taxable_far = sorted(
        [c for c in lots if c["bucket"] == "taxable_gain" and not is_near_exempt(c)],
        key=lambda c: (c["gain"] / c["mv"] if c["mv"] else 0.0),  # least gain/koruna first
    )
    near_exempt = sorted(
        [c for c in lots if is_near_exempt(c)],
        key=lambda c: (c.get("days_to_exempt") or 0),  # closest to exempt first
    )

    remaining = max(0.0, float(exit_czk))
    sell_now: list[dict[str, Any]] = []
    taxable_gain_now = 0.0
    exempt_gain_now = 0.0
    harvested_loss = 0.0
    for c in no_cost + taxable_far:
        if remaining <= EPS:
            break
        take = min(c["mv"], remaining)
        frac = take / c["mv"] if c["mv"] else 0.0
        gain_take = c["gain"] * frac
        sell_now.append({
            "bucket": c["bucket"],
            "shares": round(c["qty"] * frac, 6),
            "proceeds": round(take, 2),
            "gain": round(gain_take, 2),
            "exempt": c["exempt"],
            "days_to_exempt": c.get("days_to_exempt"),
            "open_datetime": c.get("open_datetime"),
        })
        if c["bucket"] == "taxable_gain":
            taxable_gain_now += gain_take
        elif c["bucket"] == "exempt_gain":
            exempt_gain_now += gain_take
        elif c["bucket"] in ("taxable_loss", "exempt_loss"):
            harvested_loss += -min(0.0, gain_take)
        remaining -= take

    sell_now_czk = sum(s["proceeds"] for s in sell_now)

    # Whatever the exit still wants beyond the sell-now pool would force us into
    # the near-exempt lots. Only those actually needed to complete the exit are
    # "deferred" (held back with a wait note); untouched near-exempt lots simply
    # stay in the book and are not part of this exit's tax accounting.
    defer_czk = max(0.0, float(exit_czk) - sell_now_czk)
    defer: list[dict[str, Any]] = []
    tax_saved_by_waiting = 0.0
    still = defer_czk
    for c in near_exempt:
        if still <= EPS:
            break
        gain = max(0.0, c["gain"])
        would_sell = min(c["mv"], still)          # portion needed to finish the exit
        frac = (would_sell / c["mv"]) if c["mv"] else 0.0
        saved = gain * frac * tax_rate
        tax_saved_by_waiting += saved
        defer.append({
            "bucket": c["bucket"],
            "shares": round(c["qty"] * frac, 6),
            "market_value": round(would_sell, 2),
            "gain": round(gain * frac, 2),
            "days_to_exempt": c.get("days_to_exempt"),
            "exempt_on": c.get("exempt_on"),
            "tax_if_sold_now": round(gain * frac * tax_rate, 2),
            "note": f"wait until {c.get('exempt_on')} to sell tax-free"
                    if c.get("exempt_on") else "near the 3-year exemption",
        })
        still -= would_sell

    return {
        "sell_now_czk": round(sell_now_czk, 2),
        "defer_czk": round(defer_czk, 2),
        "sell_now_lots": sell_now,
        "defer_lots": defer,
        "taxable_gain_now": round(taxable_gain_now, 2),
        "exempt_gain_now": round(exempt_gain_now, 2),
        "harvested_loss_now": round(harvested_loss, 2),
        "tax_cost_now": round(max(0.0, taxable_gain_now) * tax_rate, 2),
        "tax_saved_by_waiting": round(tax_saved_by_waiting, 2),
    }


# --------------------------------------------------------------------------- #
# Schedule + limit ladder
# --------------------------------------------------------------------------- #
def _ladder_prices(level: dict[str, Any] | None, mark_local: float | None, n: int) -> list[float | None]:
    """One suggested GTC limit per tranche, in the instrument's currency.

    Prefer a locked trim ladder's rung prices (the levels you already set); else
    step the mark up by the default rungs, anchoring the top rung to fair value
    when the level carries one above the mark. None entries mean "market"."""
    rungs = (level or {}).get("trim_ladder") if isinstance(level, dict) else None
    if rungs:
        prices = [float(r["price"]) for r in rungs if isinstance(r.get("price"), (int, float))]
        if prices:
            return [prices[min(i, len(prices) - 1)] for i in range(n)]
    if not isinstance(mark_local, (int, float)) or mark_local <= 0:
        return [None] * n
    fair = (level or {}).get("fair_value") if isinstance(level, dict) else None
    out: list[float | None] = []
    for i in range(n):
        pct = LADDER_RUNGS_PCT[min(i, len(LADDER_RUNGS_PCT) - 1)]
        if i >= len(LADDER_RUNGS_PCT):  # keep stepping +2% beyond the presets
            pct = LADDER_RUNGS_PCT[-1] + 0.02 * (i - len(LADDER_RUNGS_PCT) + 1)
        price = mark_local * (1.0 + pct)
        if isinstance(fair, (int, float)) and fair > mark_local and i == n - 1:
            price = max(price, float(fair))
        out.append(round(price, 4))
    return out


def _schedule(
    total_shares: float,
    price_base: float,
    price_local: float | None,
    currency: str | None,
    adv: float | None,
    level: dict[str, Any] | None,
    *,
    horizon_days: int,
    slice_pct: float,
    default_tranches: int,
    start: dt.date | None = None,
) -> dict[str, Any]:
    """Split the sell-now share quantity into GTC-limit tranches over the horizon.

    Poor-man's TWAP: each slice is capped at ``slice_pct * ADV`` shares when ADV
    is known, otherwise even ``default_tranches`` slices. Returns the tranche
    rows plus the derived cap so the UI can explain the sizing."""
    start = start or dt.date.today()
    total_shares = int(math.floor(max(0.0, float(total_shares)) + 0.5))
    if total_shares < 1:
        return {"tranches": [], "n": 0, "adv": adv, "max_shares_per_day": None}

    max_per_day = (slice_pct * adv) if adv and adv > 0 else None
    if max_per_day and max_per_day > 0:
        n = max(1, min(horizon_days, math.ceil(total_shares / max_per_day)))
    else:
        n = max(1, min(horizon_days, default_tranches))
    n = min(n, total_shares)

    per_shares, extra_shares = divmod(total_shares, n)
    limits = _ladder_prices(level, price_local, n)
    tranches: list[dict[str, Any]] = []
    for i in range(n):
        # Spread evenly across the horizon (tranche 0 today, last on ~horizon).
        offset = 0 if n == 1 else round(i * (horizon_days - 1) / (n - 1))
        shares = per_shares + (1 if i < extra_shares else 0)
        tranches.append({
            "index": i + 1,
            "date": (start + dt.timedelta(days=offset)).isoformat(),
            "shares": shares,
            "czk": round(shares * price_base, 2),
            "limit_price": limits[i],
            "limit_currency": currency,
            "over_adv_cap": bool(max_per_day and shares > max_per_day + EPS),
        })
    return {
        "tranches": tranches,
        "n": n,
        "adv": round(adv, 0) if adv else None,
        "max_shares_per_day": round(max_per_day, 0) if max_per_day else None,
    }


# --------------------------------------------------------------------------- #
# Candidate discovery
# --------------------------------------------------------------------------- #
def _candidates(
    plan: dict[str, Any],
    *,
    include: set[str],
    full_exit: set[str],
) -> list[dict[str, Any]]:
    """Trim rows from the plan (rule in NO_BUY_RULES, above band) plus opt-in
    untargeted names and explicit requests. Each is a light dict the builder
    fleshes out: {symbol, rule, high, source}."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in plan.get("rows", []) or []:
        if row.get("kind") != "target" or row.get("action") != "trim":
            continue
        sym = portfolio.clean_symbol(row.get("name") or row.get("key"))
        if not sym or sym in seen:
            continue
        seen.add(sym)
        out.append({"symbol": sym, "rule": row.get("rule"), "high": row.get("high"), "source": "trim"})
    for u in plan.get("untargeted", []) or []:
        sym = portfolio.clean_symbol(u.get("symbol"))
        if sym and sym in include and sym not in seen:
            seen.add(sym)
            out.append({"symbol": sym, "rule": "avoid", "high": 0.0, "source": "untargeted"})
    for sym in sorted(include | full_exit):
        sym = portfolio.clean_symbol(sym)
        if sym and sym not in seen:
            seen.add(sym)
            out.append({"symbol": sym, "rule": "avoid", "high": 0.0, "source": "explicit"})
    return out


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #
def build_exit_plan(
    model: dict[str, Any],
    holdings: dict[str, Any],
    *,
    include: list[str] | None = None,
    full_exit: list[str] | None = None,
    cfg: dict[str, Any] | None = None,
    as_of: dt.datetime | None = None,
    fetch: Any = None,
    with_options: bool = True,
) -> dict[str, Any]:
    """Assemble advisory exit plans for every position worth shrinking.

    ``fetch`` is injectable so tests never touch the network (it feeds both the
    ADV volume series and, indirectly, the options overlay's realized vol).
    """
    cfg = cfg or {}
    horizon = int(cfg.get("horizon_days", EXIT_HORIZON_DAYS))
    slice_pct = float(cfg.get("adv_slice_pct", ADV_SLICE_PCT))
    default_tranches = int(cfg.get("default_tranches", DEFAULT_TRANCHES))
    near_exempt_days = int(cfg.get("near_exempt_days", NEAR_EXEMPT_DAYS))
    tax_rate = float(cfg.get("tax_rate", CZ_TAX_RATE))

    plan = rebalance.plan(model, holdings)
    invested = portfolio.invested_value(holdings.get("positions", []) or [])
    posidx = _position_index(holdings)

    include_set = {portfolio.clean_symbol(s) for s in (include or []) if s}
    full_set = {portfolio.clean_symbol(s) for s in (full_exit or []) if s}
    cands = _candidates(plan, include=include_set, full_exit=full_set)

    # Fetch the risk-free rate ONCE (FRED macro snapshot is a network round-trip)
    # and reuse it across every position's options overlay -- otherwise each name
    # re-hits FRED, turning a 4-name plan into ~40s of duplicated fetches.
    opt_rate: float | None = None
    if with_options and cands:
        opt_rate = _cached_risk_free_rate()

    # Warm the per-name caches (ADV price series + option chain) concurrently so
    # the assembly loop below is all disk hits. Cold, this is the difference
    # between N serial crumb+chain round-trips and one parallel batch.
    _prewarm_caches(cands, posidx, with_options=with_options, fetch=fetch)

    positions: list[dict[str, Any]] = []
    for cand in cands:
        sym = cand["symbol"]
        pos = posidx.get(sym)
        if not pos or pos["mv_base"] <= EPS:
            continue  # nothing held to exit
        cur_pct = (pos["mv_base"] / invested * 100.0) if invested else 0.0
        row = {"rule": cand["rule"], "high": cand["high"]}
        state_label, target_pct = _end_state(row, force_zero=(sym in full_set))
        target_czk = (target_pct / 100.0 * invested) if invested else 0.0
        desired_exit_czk = max(0.0, pos["mv_base"] - target_czk)
        raw_exit_shares = (
            pos["qty"] * (desired_exit_czk / pos["mv_base"])
            if pos["mv_base"] else 0.0
        )
        # Stock orders are whole-share only. Use the nearest executable quantity
        # and never exceed the whole shares actually held.
        exit_shares = min(
            int(math.floor(max(0.0, pos["qty"]) + EPS)),
            int(math.floor(max(0.0, raw_exit_shares) + 0.5)),
        )
        if exit_shares < 1:
            continue
        exit_czk = min(pos["mv_base"], exit_shares * pos["price_base"])

        lots = tax_lots.classify_lots(holdings, sym, as_of=as_of)
        layers = _tax_layers(lots, exit_czk, near_exempt_days=near_exempt_days, tax_rate=tax_rate)

        # Schedule only the sell-now portion; the deferred bit waits on the clock.
        raw_sell_now_shares = (
            pos["qty"] * (layers["sell_now_czk"] / pos["mv_base"])
            if pos["mv_base"] else 0.0
        )
        sell_now_shares = min(
            exit_shares,
            int(math.floor(max(0.0, raw_sell_now_shares) + 0.5)),
        )
        provider_sym = portfolio.provider_symbol_for(sym)
        series = risk.load_price_series(provider_sym, fetch=fetch) if fetch is not False else None
        adv = average_daily_volume(series)
        level = price_levels.get(provider_sym)
        sched = _schedule(
            sell_now_shares, pos["price_base"], pos["price_local"], pos["currency"], adv, level,
            horizon_days=horizon, slice_pct=slice_pct, default_tranches=default_tranches,
        )

        entry: dict[str, Any] = {
            "symbol": sym,
            "source": cand["source"],
            "rule": cand["rule"],
            "currency": pos["currency"],
            "mark_price": round(pos["price_local"], 4) if pos["price_local"] else None,
            "quantity": round(pos["qty"], 4),
            "current_pct": round(cur_pct, 2),
            "current_czk": round(pos["mv_base"], 2),
            "end_state": state_label,
            "target_pct": round(target_pct, 2),
            "exit_czk": round(exit_czk, 2),
            "exit_shares": exit_shares,
            "sell_now_shares": sell_now_shares,
            "tax": layers,
            "schedule": sched,
        }
        if with_options:
            entry["options"] = _options_overlay(sym, pos, layers, series, cfg, as_of, rate=opt_rate)
            entry["routes"] = _execution_routes(entry, now=as_of)
        positions.append(entry)

    positions.sort(key=lambda e: -e["exit_czk"])
    return {
        "as_of": (as_of or dt.datetime.now(dt.timezone.utc)).date().isoformat(),
        "snapshot": holdings.get("generated_at") if isinstance(holdings, dict) else None,
        "currency": (holdings.get("base_currency") if isinstance(holdings, dict) else None) or "CZK",
        "invested": round(invested, 2) if invested else None,
        "config": {
            "horizon_days": horizon,
            "adv_slice_pct": slice_pct,
            "near_exempt_days": near_exempt_days,
            "tax_rate": tax_rate,
        },
        "positions": positions,
        "totals": {
            "exit_czk": round(sum(p["exit_czk"] for p in positions), 2),
            "sell_now_czk": round(sum(p["tax"]["sell_now_czk"] for p in positions), 2),
            "defer_czk": round(sum(p["tax"]["defer_czk"] for p in positions), 2),
            "tax_cost_now": round(sum(p["tax"]["tax_cost_now"] for p in positions), 2),
            "tax_saved_by_waiting": round(sum(p["tax"]["tax_saved_by_waiting"] for p in positions), 2),
        },
    }


def _prewarm_caches(
    cands: list[dict[str, Any]],
    posidx: dict[str, dict[str, Any]],
    *,
    with_options: bool,
    fetch: Any,
) -> None:
    """Populate the ADV price-series and option-chain disk caches for every held
    candidate, in parallel. After this returns, ``build_exit_plan``'s serial loop
    hits warm caches instead of paying each name's network cost in sequence.
    Best-effort: a failed warm just leaves that name cold for the loop to retry."""
    names = list(dict.fromkeys(
        c["symbol"] for c in cands
        if (posidx.get(c["symbol"]) or {}).get("mv_base", 0.0) > EPS
    ))
    if not names:
        return

    def warm(sym: str) -> None:
        try:
            # Price series keys off the provider (Yahoo) symbol; the option chain
            # keys off the canonical ticker (its IBKR-first path resolves that, and
            # its Yahoo fallback maps to the provider symbol internally).
            if fetch is not False:
                risk.load_price_series(portfolio.provider_symbol_for(sym), fetch=fetch)
            if with_options:
                _cached_option_chain(sym)
        except Exception:  # noqa: BLE001 -- warming is opportunistic, never fatal
            pass

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(FETCH_WORKERS, len(names))) as pool:
        list(pool.map(warm, names))


def _cached_risk_free_rate() -> float | None:
    """Compatibility seam for tests and callers that patch the old helper."""
    return option_market.cached_risk_free_rate()


def _cached_option_chain(symbol: str) -> dict[str, Any] | None:
    """Compatibility seam around the option-market acquisition service."""
    return option_market.cached_option_chain(symbol)


def _options_overlay(sym, pos, layers, series, cfg, as_of, *, rate=None) -> dict[str, Any] | None:
    """Thin bridge to the options overlay module (Phase 3). Imported lazily and
    guarded so a missing/failed overlay never sinks the exit plan. The chain is
    fetched (and cached) here rather than inside the overlay so the network cost
    lives in one place and stays off the pure-math module. ``rate`` is the
    pre-fetched risk-free rate so the overlay doesn't re-hit FRED per name."""
    try:
        import options_overlay
    except Exception:  # noqa: BLE001
        return None
    try:
        chain = _cached_option_chain(sym)
        return options_overlay.suggest_for_position(
            sym, pos, layers, series=series, cfg=cfg, as_of=as_of, chain=chain, rate=rate)
    except Exception:  # noqa: BLE001
        return None


def _staged_exit_share_sales(existing: list[dict], symbol: str) -> int:
    shares = 0
    for row in existing:
        if row.get("type") not in (None, "stock") or row.get("symbol") != symbol:
            continue
        provenance_rows = row.get("provenance") or []
        if isinstance(provenance_rows, dict):
            provenance_rows = [provenance_rows]
        shares += sum(
            max(0, int(float(p.get("intended_shares") or 0)))
            for p in provenance_rows
            if isinstance(p, dict) and p.get("route") == "sell_shares"
        )
    return shares


def stage_tranche(plan: dict[str, Any], symbol: str, index: int) -> dict[str, Any]:
    """Merge tranche ``index`` of ``symbol`` into the staged trade-desk basket.

    Returns {staged: bool, basket, tranche}. The delta is negative (a sell); the
    desk re-derives shares + any locked limit at preview time, so we only hand it
    a CZK size. Idempotent-ish: staging the same tranche twice sums (the desk
    nets by symbol), which is intentional -- restaging accumulates the exit."""
    import trade_service

    sym = portfolio.clean_symbol(symbol)
    pos = next((p for p in plan.get("positions", []) if p["symbol"] == sym), None)
    if pos is None:
        raise ValueError(f"{symbol} is not in the exit plan")
    tranche = next((t for t in pos["schedule"]["tranches"] if t["index"] == index), None)
    if tranche is None:
        raise ValueError(f"{symbol} has no tranche #{index}")

    fingerprint = _plan_fingerprint(plan, pos)
    provenance = {
        "source": "exit_plan",
        "route": "sell_shares",
        "plan_as_of": plan.get("as_of"),
        "plan_snapshot": plan.get("snapshot"),
        "plan_fingerprint": fingerprint,
        "tranche_index": index,
        "intended_shares": tranche.get("shares"),
    }
    existing = trade_service.load_basket()
    already_staged = any(
        isinstance(p, dict)
        and p.get("source") == "exit_plan"
        and p.get("route") == "sell_shares"
        and p.get("plan_fingerprint") == fingerprint
        and int(p.get("tranche_index") or 0) == index
        for row in existing
        if row.get("type") in (None, "stock") and row.get("symbol") == sym
        for p in (
            [row.get("provenance")]
            if isinstance(row.get("provenance"), dict)
            else row.get("provenance") or []
        )
    )
    if already_staged:
        return {
            "staged": True,
            "already_staged": True,
            "route": "sell_shares",
            "basket": existing,
            "tranche": tranche,
            "symbol": sym,
        }
    staged_calls = sum(
        int(row.get("contracts") or 0)
        for row in existing
        if row.get("type") == "covered_call" and row.get("symbol") == sym
    )
    capacity = trade_service.covered_call_capacity(sym)
    held_and_staged = int(capacity.get("held_short_calls") or 0) + staged_calls
    if held_and_staged:
        staged_stock_sells = (
            _staged_exit_share_sales(existing, sym)
            + max(0, int(float(tranche.get("shares") or 0)))
        )
        post_sell_shares = max(
            0, int(capacity.get("current_shares") or 0) - staged_stock_sells,
        )
        if post_sell_shares < held_and_staged * 100:
            raise ValueError(
                f"{sym}: this share tranche would leave {post_sell_shares} shares "
                f"covering {held_and_staged} held/staged short call contract(s)"
            )
    merged = existing + [{
        "type": "stock",
        "symbol": sym,
        "delta_czk": -abs(float(tranche["czk"])),
        "provenance": provenance,
    }]
    trade_service.validate_stock_sell_capacity(merged)
    basket = trade_service.save_basket(merged)
    return {"staged": True, "route": "sell_shares", "basket": basket, "tranche": tranche, "symbol": sym}


def _plan_fingerprint(plan: dict[str, Any], position: dict[str, Any]) -> str:
    payload = {
        "as_of": plan.get("as_of"),
        "snapshot": plan.get("snapshot"),
        "symbol": position.get("symbol"),
        "exit_czk": position.get("exit_czk"),
        "exit_shares": position.get("exit_shares"),
        "target_pct": position.get("target_pct"),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def stage_covered_call(
    plan: dict[str, Any],
    symbol: str,
    *,
    conid: int,
    expiry: str,
    strike: float,
    contracts: int,
) -> dict[str, Any]:
    """Validate and idempotently stage one exact covered-call exit leg."""
    import trade_service

    sym = portfolio.clean_symbol(symbol)
    pos = next((p for p in plan.get("positions", []) if p["symbol"] == sym), None)
    if pos is None:
        raise ValueError(f"{symbol} is not in the exit plan")
    routes = pos.get("routes") or {}
    route = routes.get("covered_call") or {}
    if not route.get("eligible"):
        raise ValueError("; ".join(route.get("reasons") or ["covered-call route is not eligible"]))
    if not isinstance(contracts, int) or isinstance(contracts, bool) or contracts <= 0:
        raise ValueError("contracts must be a positive integer")
    if contracts > int(route.get("capacity_contracts") or 0):
        raise ValueError(f"{sym}: requested contracts exceed uncovered exit capacity")

    rungs = ((pos.get("options") or {}).get("covered_call_ladder") or [])
    rung = next((
        r for r in rungs
        if int(r.get("conid") or 0) == int(conid)
        and str(r.get("expiry") or "") == str(expiry)
        and abs(float(r.get("strike") or 0) - float(strike)) < EPS
    ), None)
    if not rung:
        raise ValueError(f"{sym}: selected call is not in the current server-built Exit ladder")
    if not rung.get("stageable") or not rung.get("conid"):
        raise ValueError(f"{sym}: selected call is not an exact stageable IBKR contract")

    try:
        raw_working = ibkr_trade.live_orders()
    except ibkr_trade.CPAPIError as exc:
        raise ValueError(f"{sym}: working option orders could not be verified") from exc
    try:
        account_id = trade_service._resolve_trade_account(None)
        capacity = trade_service.covered_call_capacity(
            sym, raw_working, live_account_id=account_id,
        )
    except (ValueError, ibkr_trade.CPAPIError) as exc:
        raise ValueError(f"{sym}: live positions could not be verified") from exc
    leg_id = f"covered_call:{sym}:{int(conid)}"
    existing = trade_service.load_basket()
    staged_stock_sells = _staged_exit_share_sales(existing, sym)
    intended = _covered_call_exit_contracts(
        pos.get("exit_shares"),
        capacity.get("current_shares"),
    )
    post_sell_capacity = max(
        0,
        max(0, int(capacity.get("current_shares") or 0) - staged_stock_sells) // 100
        - int(capacity.get("held_short_calls") or 0)
        - int(capacity.get("working_short_calls") or 0),
    )
    available = min(post_sell_capacity, intended)
    staged_other = sum(
        int(row.get("contracts") or 0)
        for row in existing
        if row.get("type") == "covered_call"
        and row.get("symbol") == sym
        and row.get("leg_id") != leg_id
    )
    if staged_other + contracts > available:
        raise ValueError(
            f"{sym}: only {available} covered-call contract(s) remain after held/working calls, "
            f"{staged_stock_sells} staged share sales, and {staged_other} other staged contract(s)"
        )

    try:
        exact = ibkr_trade.resolve_executable_call(
            sym,
            expiry,
            strike,
            expected_conid=int(conid),
            max_quote_age_seconds=EXECUTION_QUOTE_MAX_AGE_SECONDS,
            allow_missing_quote=True,
        )
    except ibkr_trade.ExecutableCallError as exc:
        if exc.reason in {"contract_missing", "contract_changed"}:
            message = f"{sym}: exact call contract no longer resolves"
        elif exc.reason == "quote_invalid":
            message = f"{sym}: option bid/ask is missing or crossed"
        elif exc.reason == "quote_stale":
            message = f"{sym}: option quote is stale"
        else:
            message = f"{sym}: no valid tick-rounded sell limit"
        raise ValueError(message) from exc
    limit = exact.get("limit_price")

    fingerprint = _plan_fingerprint(plan, pos)
    provenance = {
        "source": "exit_plan",
        "route": "covered_call",
        "plan_as_of": plan.get("as_of"),
        "plan_snapshot": plan.get("snapshot"),
        "plan_fingerprint": fingerprint,
        "rung": {
            "conid": int(conid), "expiry": expiry, "strike": float(strike),
        },
        "intended_assigned_shares": contracts * 100,
    }
    leg = {
        "type": "covered_call",
        "leg_id": leg_id,
        "symbol": sym,
        "route": "covered_call",
        "conid": int(conid),
        "expiry": str(expiry),
        "strike": float(strike),
        "right": "C",
        "contracts": contracts,
        "multiplier": 100,
        "limit_price": limit,
        "quote_timestamp": exact.get("quote_timestamp"),
        "staging_warning": exact.get("staging_warning"),
        "provenance": [provenance],
    }
    basket = trade_service.save_basket(existing + [leg])
    stored = next((row for row in basket if row.get("leg_id") == leg["leg_id"]), leg)
    return {
        "staged": True,
        "route": "covered_call",
        "basket": basket,
        "leg": stored,
        "symbol": sym,
        "coverage": capacity,
    }


def _main() -> int:
    import argparse
    import json
    import store
    from config import HOLDINGS_JSON, TARGET_MODEL_JSON

    ap = argparse.ArgumentParser(description="Advisory graceful-exit planner.")
    ap.add_argument("--include", default="", help="Comma list of untargeted names to also exit.")
    ap.add_argument("--full-exit", default="", help="Comma list to force to zero.")
    args = ap.parse_args()

    holdings = store.load(HOLDINGS_JSON)
    model = store.load(TARGET_MODEL_JSON)
    if not holdings or not model:
        raise SystemExit("need both a holdings snapshot and a target model")
    plan = build_exit_plan(
        model, holdings,
        include=[s for s in args.include.split(",") if s.strip()],
        full_exit=[s for s in args.full_exit.split(",") if s.strip()],
    )
    print(json.dumps(plan, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
