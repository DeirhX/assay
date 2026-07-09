#!/usr/bin/env python3
"""Options overlay for the graceful-exit planner -- ANALYSIS ONLY.

Two ideas, both advisory (this module never places an option order; the trade
desk can't even send one):

* **Covered call** on a name you're exiting: sell an OTM call ~30-45 DTE at or
  above your exit target. You get paid to wait, and if it's assigned you exit at
  a price you already liked (strike + premium). Only makes sense at >= 100 shares
  (one contract).

* **Protective put / collar** on a name where you're *deferring* a near-exempt
  taxable-gain lot (see :mod:`exit_plan` tax layering): buy a put expiring after
  the 3-year exemption date to hedge the downside while the clock runs out,
  optionally financed by selling an OTM call (a collar).

The **tax-aware assignment guard** is the subtle bit: if you're deferring a
near-exempt gain lot, a covered call that gets assigned *before* the exemption
date forces exactly the taxable sale you were trying to avoid. So when a defer
lot exists, the covered-call suggestion is pushed far-OTM with an expiry after
the exemption date, and it carries a loud caveat.

Premiums come from Yahoo's live-ish chain when the name has one; otherwise from a
Black-Scholes estimate (:mod:`options_math`) fed by realized vol and the FRED
risk-free rate, and clearly labelled ``"black_scholes"`` so nobody mistakes an
estimate for a quote.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import options_math  # noqa: E402

_UNSET = object()  # "fetch the chain yourself"; None means "force the BS path".

# Config defaults.
COVERED_CALL_DTE = (30, 45)
CALL_OTM_PCT = 0.05        # baseline OTM cushion for a covered call
GUARD_CALL_OTM_PCT = 0.15  # far-OTM cushion when guarding a near-exempt lot
PUT_OTM_PCT = 0.07         # protective-put strike below the mark
DEFAULT_VOL = 0.35         # fallback annualized vol when no series is usable
CONTRACT_SIZE = 100

# Covered-call strike ladder (the StrikePeek-style yield-vs-assignment view).
LADDER_SIZE = 6            # OTM strikes to surface, cheapest cushion first
LADDER_STEP_PCT = 0.025    # synthetic strike spacing when no chain lists strikes
LIQ_MIN_OI = 100           # open interest below this is "thin" (when OI is known)
LIQ_MIN_VOLUME = 10        # day volume below this is "thin" (when volume is known)
LIQ_MAX_SPREAD_PCT = 0.15  # (ask - bid) / mid above this is "thin"


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _as_date(as_of: dt.datetime | None) -> dt.date:
    return (as_of or dt.datetime.now(dt.timezone.utc)).date()


def _years_between(target: dt.date, base: dt.date) -> float:
    return max(0.0, (target - base).days / 365.0)


def _vol_from_series(series: list[dict[str, Any]] | None) -> tuple[float, bool]:
    """(annualized_vol, is_estimate_fallback)."""
    closes = [float(c) for p in (series or [])
              if isinstance((c := p.get("close")), (int, float))]
    vol = options_math.annualized_vol(closes) if len(closes) >= 3 else None
    if vol and vol > 0:
        return vol, False
    return DEFAULT_VOL, True


def _mid(contract: dict[str, Any]) -> float | None:
    """Mid of bid/ask, falling back to last. None on a totally empty quote."""
    bid, ask, last = contract.get("bid"), contract.get("ask"), contract.get("last")
    if isinstance(bid, (int, float)) and isinstance(ask, (int, float)) and bid > 0 and ask > 0:
        return (bid + ask) / 2.0
    if isinstance(last, (int, float)) and last > 0:
        return float(last)
    return None


def _chain_source(chain: dict[str, Any] | None) -> str:
    """Provider label for a chain's quotes: the chain's own ``source`` (``ibkr``,
    ``alpaca`` or ``yahoo``), defaulting to ``yahoo`` for a source-less chain, and
    ``black_scholes`` when there is no chain at all (the premium is modeled). A
    suggestion whose premium came off the chain inherits this; one computed by
    Black-Scholes keeps ``black_scholes`` regardless of the chain."""
    if not isinstance(chain, dict):
        return "black_scholes"
    return str(chain.get("source") or "yahoo")


def _spread_pct(contract: dict[str, Any]) -> float | None:
    """Relative bid/ask spread ``(ask - bid) / mid``, or None without a two-sided
    quote. The core liquidity signal every source can supply."""
    bid, ask = contract.get("bid"), contract.get("ask")
    if isinstance(bid, (int, float)) and isinstance(ask, (int, float)) and bid > 0 and ask > 0:
        mid = (bid + ask) / 2.0
        return (ask - bid) / mid if mid > 0 else None
    return None


def _as_int(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _liquidity(contract: dict[str, Any], premium_source: str) -> tuple[str, float | None]:
    """Classify a contract's tradeability as ``ok`` / ``thin`` / ``unknown`` plus
    the spread. A modeled (Black-Scholes) premium is ``unknown`` -- there is no
    live quote to judge. Otherwise the spread is the primary gate; open interest
    and day volume, when the source reports them (IBKR/Yahoo do, Alpaca's data
    feed doesn't carry OI), demote an otherwise-tight strike that nobody trades."""
    if premium_source == "black_scholes":
        return "unknown", None
    spread = _spread_pct(contract)
    if spread is None:
        return "unknown", None
    if spread > LIQ_MAX_SPREAD_PCT:
        return "thin", spread
    oi, vol = _as_int(contract.get("open_interest")), _as_int(contract.get("volume"))
    if oi is not None or vol is not None:
        active = (oi is not None and oi >= LIQ_MIN_OI) or (vol is not None and vol >= LIQ_MIN_VOLUME)
        if not active:
            return "thin", spread
    return "ok", spread


def _call_candidate(
    call: dict[str, Any], spot: float, vol: float, rate: float, as_of: dt.date,
    edate: dt.date, chain_source: str, *, contracts: int, fx: float,
) -> dict[str, Any] | None:
    """One covered-call ladder rung for a listed (or synthetic) call strike:
    premium (chain mid, else Black-Scholes), annualized yield, assignment
    probability (chain delta when present, else modeled), and a liquidity tag."""
    strike = call.get("strike")
    if not isinstance(strike, (int, float)) or strike <= 0:
        return None
    t_years = _years_between(edate, as_of)
    iv = call.get("implied_vol")
    use_vol = iv if isinstance(iv, (int, float)) and iv > 0 else vol
    premium = _mid(call)
    source = chain_source if premium is not None else "black_scholes"
    if premium is None:
        premium = options_math.bs_price(spot, strike, t_years, use_vol, rate=rate, kind="call")
    if premium is None or premium <= 0:
        return None
    raw_delta = call.get("delta")
    delta = (float(raw_delta) if isinstance(raw_delta, (int, float)) and 0.0 < abs(raw_delta) <= 1.0
             else options_math.bs_delta(spot, strike, t_years, use_vol, rate=rate, kind="call"))
    dte = max(1, (edate - as_of).days)
    yield_annual = (premium / strike) * (365.0 / dte)
    liq, spread = _liquidity(call, source)
    return {
        "strike": round(float(strike), 2),
        "premium": round(premium, 4),
        "premium_czk": round(premium * CONTRACT_SIZE * contracts * fx, 2),
        "effective_exit": round(strike + premium, 4),
        "moneyness_pct": round((strike / spot - 1.0) * 100.0, 2),
        "premium_yield_annual_pct": round(yield_annual * 100.0, 2),
        "assignment_prob_pct": round(delta * 100.0, 1) if delta is not None else None,
        "open_interest": _as_int(call.get("open_interest")),
        "volume": _as_int(call.get("volume")),
        "spread_pct": round(spread * 100.0, 1) if spread is not None else None,
        "liquidity": liq,
        "source": source,
        "estimate": source == "black_scholes",
    }


def covered_call_ladder(
    spot: float, vol: float, rate: float, as_of: dt.date, chain: dict[str, Any] | None,
    *, contracts: int, fx: float, guard_after: dt.date | None,
) -> list[dict[str, Any]]:
    """Yield-ranked ladder of OTM covered-call strikes for the recommended expiry
    -- the tradeoff view: near-the-money rungs pay the fattest annualized yield but
    carry the highest assignment odds. Uses listed strikes when a chain resolves
    one, else models a ladder off spot. Ranked richest-yield first; each rung
    carries its own source/liquidity so a wide, illiquid quote is visibly demoted.
    Empty when there's no whole contract to write."""
    if contracts < 1 or spot <= 0:
        return []
    otm = GUARD_CALL_OTM_PCT if guard_after else CALL_OTM_PCT
    floor_strike = spot * (1.0 + otm)
    chain_source = _chain_source(chain)

    edate: dt.date | None = None
    expiry_iso: str | None = None
    calls: list[dict[str, Any]] = []
    if chain and chain.get("expiries"):
        exp = _pick_expiry(chain["expiries"], as_of, dte_min=COVERED_CALL_DTE[0],
                           dte_max=COVERED_CALL_DTE[1], after=guard_after)
        if exp:
            expiry_iso = exp["expiry"]
            edate = dt.date.fromisoformat(expiry_iso)
            calls = [c for c in (exp.get("calls") or [])
                     if isinstance(c.get("strike"), (int, float)) and c["strike"] >= floor_strike]
            calls.sort(key=lambda c: c["strike"])

    if edate is None:
        # No chain (or no expiry after the tax guard): model an expiry + ladder.
        edate = (guard_after + dt.timedelta(days=14)) if guard_after else as_of + dt.timedelta(days=37)
        expiry_iso = edate.isoformat()
    if not calls:
        # Thin window or modeled path: synthesize evenly-spaced OTM strikes.
        calls = [{"strike": round(spot * (1.0 + otm + i * LADDER_STEP_PCT), 2)}
                 for i in range(LADDER_SIZE)]

    rungs: list[dict[str, Any]] = []
    for call in calls[:LADDER_SIZE]:
        cand = _call_candidate(call, spot, vol, rate, as_of, edate, chain_source,
                               contracts=contracts, fx=fx)
        if cand:
            cand["expiry"] = expiry_iso
            cand["dte"] = max(1, (edate - as_of).days)
            rungs.append(cand)
    rungs.sort(key=lambda r: r["premium_yield_annual_pct"], reverse=True)
    return rungs


def _pick_expiry(
    expiries: list[dict[str, Any]], as_of: dt.date, *, dte_min: int, dte_max: int, after: dt.date | None = None
) -> dict[str, Any] | None:
    """Nearest expiry to the middle of the DTE window (and after ``after`` when a
    tax deferral demands it). Falls back to the first expiry past ``after``."""
    target_dte = (dte_min + dte_max) / 2.0
    scored: list[tuple[float, dict[str, Any]]] = []
    fallback: list[tuple[int, dict[str, Any]]] = []
    for e in expiries:
        try:
            edate = dt.date.fromisoformat(e["expiry"])
        except (KeyError, ValueError):
            continue
        dte = (edate - as_of).days
        if dte <= 0:
            continue
        if after and edate <= after:
            continue
        fallback.append((dte, e))
        if dte_min <= dte <= dte_max:
            scored.append((abs(dte - target_dte), e))
    if scored:
        return min(scored, key=lambda t: t[0])[1]
    if fallback:
        return min(fallback, key=lambda t: t[0])[1]  # soonest valid expiry
    return None


def _nearest_call(strikes: list[dict[str, Any]], floor_strike: float) -> dict[str, Any] | None:
    """Lowest listed call strike at/above ``floor_strike``."""
    up = [c for c in strikes if c.get("strike") and c["strike"] >= floor_strike]
    return min(up, key=lambda c: c["strike"]) if up else (max(strikes, key=lambda c: c["strike"]) if strikes else None)


def _nearest_put(strikes: list[dict[str, Any]], cap_strike: float) -> dict[str, Any] | None:
    """Highest listed put strike at/below ``cap_strike``."""
    dn = [p for p in strikes if p.get("strike") and p["strike"] <= cap_strike]
    return max(dn, key=lambda p: p["strike"]) if dn else (min(strikes, key=lambda p: p["strike"]) if strikes else None)


# --------------------------------------------------------------------------- #
# Covered call
# --------------------------------------------------------------------------- #
def _covered_call(
    spot: float, vol: float, rate: float, as_of: dt.date, chain: dict[str, Any] | None,
    *, contracts: int, fx: float, guard_after: dt.date | None,
) -> dict[str, Any] | None:
    if contracts < 1 or spot <= 0:
        return None
    otm = GUARD_CALL_OTM_PCT if guard_after else CALL_OTM_PCT
    floor_strike = spot * (1.0 + otm)

    expiry_iso: str | None = None
    strike: float | None = None
    premium: float | None = None
    iv: float | None = None
    source = "black_scholes"
    chain_source = _chain_source(chain)

    if chain and chain.get("expiries"):
        exp = _pick_expiry(chain["expiries"], as_of, dte_min=COVERED_CALL_DTE[0],
                           dte_max=COVERED_CALL_DTE[1], after=guard_after)
        if exp:
            call = _nearest_call(exp.get("calls") or [], floor_strike)
            if call:
                expiry_iso = exp["expiry"]
                strike = call["strike"]
                premium = _mid(call)
                iv = call.get("implied_vol")
                if premium is not None:
                    source = chain_source

    edate = dt.date.fromisoformat(expiry_iso) if expiry_iso else (
        (guard_after + dt.timedelta(days=14)) if guard_after else as_of + dt.timedelta(days=37))
    if expiry_iso is None:
        expiry_iso = edate.isoformat()
    if strike is None:
        # No listed strike: round the OTM floor to a tidy increment for display.
        strike = round(floor_strike, 2)
    t_years = _years_between(edate, as_of)
    use_vol = iv if isinstance(iv, (int, float)) and iv > 0 else vol
    if premium is None:
        premium = options_math.bs_price(spot, strike, t_years, use_vol, rate=rate, kind="call")
    if premium is None or premium <= 0:
        return None

    delta = options_math.bs_delta(spot, strike, t_years, use_vol, rate=rate, kind="call")
    dte = max(1, (edate - as_of).days)
    yield_annual = (premium / strike) * (365.0 / dte) if strike else None
    premium_czk = premium * CONTRACT_SIZE * contracts * fx
    return {
        "type": "covered_call",
        "source": source,
        "contracts": contracts,
        "expiry": expiry_iso,
        "dte": dte,
        "strike": round(strike, 2),
        "premium": round(premium, 4),
        "premium_czk": round(premium_czk, 2),
        "effective_exit": round(strike + premium, 4),
        "premium_yield_annual_pct": round(yield_annual * 100.0, 2) if yield_annual else None,
        "assignment_prob_pct": round(delta * 100.0, 1) if delta is not None else None,
        "vol_used": round(use_vol, 4),
        "estimate": source == "black_scholes",
    }


# --------------------------------------------------------------------------- #
# Protective put / collar
# --------------------------------------------------------------------------- #
def _protective_put(
    spot: float, vol: float, rate: float, as_of: dt.date, chain: dict[str, Any] | None,
    *, contracts: int, fx: float, exempt_on: dt.date, tax_saved: float,
) -> dict[str, Any] | None:
    if contracts < 1 or spot <= 0:
        return None
    cap_strike = spot * (1.0 - PUT_OTM_PCT)
    # The put must survive until the gain turns exempt, so expire after that date.
    min_expiry = exempt_on + dt.timedelta(days=5)

    expiry_iso: str | None = None
    strike: float | None = None
    put_premium: float | None = None
    iv: float | None = None
    source = "black_scholes"
    chain_source = _chain_source(chain)

    if chain and chain.get("expiries"):
        exp = _pick_expiry(chain["expiries"], as_of, dte_min=0, dte_max=10_000, after=min_expiry)
        if exp:
            put = _nearest_put(exp.get("puts") or [], cap_strike)
            if put:
                expiry_iso = exp["expiry"]
                strike = put["strike"]
                put_premium = _mid(put)
                iv = put.get("implied_vol")
                if put_premium is not None:
                    source = chain_source

    edate = dt.date.fromisoformat(expiry_iso) if expiry_iso else (min_expiry + dt.timedelta(days=14))
    if expiry_iso is None:
        expiry_iso = edate.isoformat()
    if strike is None:
        strike = round(cap_strike, 2)
    t_years = _years_between(edate, as_of)
    use_vol = iv if isinstance(iv, (int, float)) and iv > 0 else vol
    if put_premium is None:
        put_premium = options_math.bs_price(spot, strike, t_years, use_vol, rate=rate, kind="put")
    if put_premium is None or put_premium <= 0:
        return None

    # Finance it with an OTM call for a collar (strike a symmetric step above).
    call_strike = spot * (1.0 + PUT_OTM_PCT)
    call_prem = None
    if chain and chain.get("expiries"):
        exp = _pick_expiry(chain["expiries"], as_of, dte_min=0, dte_max=10_000, after=min_expiry)
        if exp:
            call = _nearest_call(exp.get("calls") or [], call_strike)
            if call:
                call_strike = call["strike"]
                call_prem = _mid(call)
    if call_prem is None:
        call_prem = options_math.bs_price(spot, call_strike, t_years, use_vol, rate=rate, kind="call")

    net_collar = (put_premium - call_prem) if isinstance(call_prem, (int, float)) else None
    dte = max(1, (edate - as_of).days)
    return {
        "type": "protective_put",
        "source": source,
        "contracts": contracts,
        "expiry": expiry_iso,
        "dte": dte,
        "days_to_exempt": max(0, (exempt_on - as_of).days),
        "exempt_on": exempt_on.isoformat(),
        "put_strike": round(strike, 2),
        "put_premium": round(put_premium, 4),
        "put_cost_czk": round(put_premium * CONTRACT_SIZE * contracts * fx, 2),
        "protected_floor": round(strike - put_premium, 4),
        "collar_call_strike": round(call_strike, 2),
        "collar_call_premium": round(call_prem, 4) if isinstance(call_prem, (int, float)) else None,
        "net_collar_premium": round(net_collar, 4) if net_collar is not None else None,
        "net_collar_czk": round(net_collar * CONTRACT_SIZE * contracts * fx, 2) if net_collar is not None else None,
        "tax_saved_by_waiting_czk": round(tax_saved, 2),
        "vol_used": round(use_vol, 4),
        "estimate": source == "black_scholes",
    }


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def suggest_for_position(
    symbol: str,
    pos: dict[str, Any],
    layers: dict[str, Any],
    *,
    series: list[dict[str, Any]] | None = None,
    cfg: dict[str, Any] | None = None,
    as_of: dt.datetime | None = None,
    chain: Any = _UNSET,
    rate: float | None = None,
    snapshot: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Covered-call and (when a near-exempt lot is being deferred) protective-put
    overlay for one exit position.

    ``chain`` defaults to a live Yahoo fetch; pass ``None`` to force the
    Black-Scholes estimate, or a dict to inject a chain (tests). ``rate`` and
    ``snapshot`` let tests pin the risk-free rate without hitting FRED.
    """
    cfg = cfg or {}
    as_of_d = _as_date(as_of)
    qty = abs(float(pos.get("qty") or 0.0))
    contracts = int(qty // CONTRACT_SIZE)
    spot = pos.get("price_local") or pos.get("price_base")
    fx = float(pos.get("fx") or 1.0)
    if not isinstance(spot, (int, float)) or spot <= 0:
        return None

    if chain is _UNSET:
        try:
            from providers import yahoo
            import portfolio
            chain = yahoo.option_chain(portfolio.provider_symbol_for(symbol))
        except Exception:  # noqa: BLE001 -- no chain: fall back to BS below
            chain = None

    vol, vol_estimate = _vol_from_series(series)
    if rate is None:
        rate = options_math.risk_free_rate(snapshot=snapshot)

    # A deferred near-exempt gain lot drives both the assignment guard and the
    # protective-put suggestion. Use the furthest-out exemption date so the hedge
    # / guard covers every deferred lot.
    defer_lots = layers.get("defer_lots") or []
    exempt_dates = [dt.date.fromisoformat(lot["exempt_on"]) for lot in defer_lots
                    if isinstance(lot.get("exempt_on"), str) and _safe_iso(lot["exempt_on"])]
    guard_after = max(exempt_dates) if exempt_dates else None
    tax_saved = float(layers.get("tax_saved_by_waiting") or 0.0)

    notes: list[str] = []
    if vol_estimate:
        notes.append("No usable price history; volatility defaulted -- premiums are rough estimates.")

    chain_dict = chain if isinstance(chain, dict) else None
    covered = _covered_call(float(spot), vol, rate, as_of_d, chain_dict,
                            contracts=contracts, fx=fx, guard_after=guard_after)
    ladder = covered_call_ladder(float(spot), vol, rate, as_of_d, chain_dict,
                                 contracts=contracts, fx=fx, guard_after=guard_after)
    if covered and ladder:
        # Flag the rung matching the headline pick so the UI can highlight it.
        for rung in ladder:
            if abs(rung["strike"] - covered["strike"]) < 1e-6 and rung["expiry"] == covered["expiry"]:
                rung["recommended"] = True
                break
    if contracts < 1:
        notes.append(f"Position is {qty:g} shares (< {CONTRACT_SIZE}); no whole option contract to write.")
    if covered and guard_after:
        covered["assignment_guard"] = True
        notes.append(
            f"Deferring a near-exempt lot: covered call pushed far-OTM and to an expiry after "
            f"{guard_after.isoformat()} so assignment can't force a pre-exemption taxable sale.")

    protective = None
    if guard_after and contracts >= 1:
        protective = _protective_put(float(spot), vol, rate, as_of_d, chain if isinstance(chain, dict) else None,
                                     contracts=contracts, fx=fx, exempt_on=guard_after, tax_saved=tax_saved)

    if not covered and not protective:
        return None
    return {
        "symbol": symbol,
        "underlying": round(float(spot), 4),
        "currency": pos.get("currency"),
        "source": _chain_source(chain),
        "covered_call": covered,
        "covered_call_ladder": ladder,
        "protective_put": protective,
        "notes": notes,
    }


def _safe_iso(value: str) -> bool:
    try:
        dt.date.fromisoformat(value)
        return True
    except ValueError:
        return False


def _main() -> int:
    import argparse
    import json
    import store
    import tax_lots
    import exit_plan as _ep
    from config import HOLDINGS_JSON

    ap = argparse.ArgumentParser(description="Options overlay for one held name (analysis only).")
    ap.add_argument("symbol")
    args = ap.parse_args()
    holdings = store.load(HOLDINGS_JSON)
    if not holdings:
        raise SystemExit("no holdings snapshot")
    posidx = _ep._position_index(holdings)
    pos = posidx.get(args.symbol.upper())
    if not pos:
        raise SystemExit(f"{args.symbol} not held")
    lots = tax_lots.classify_lots(holdings, args.symbol)
    layers = _ep._tax_layers(lots, pos["mv_base"], near_exempt_days=_ep.NEAR_EXEMPT_DAYS, tax_rate=_ep.CZ_TAX_RATE)
    print(json.dumps(suggest_for_position(args.symbol.upper(), pos, layers), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
