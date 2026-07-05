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


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _as_date(as_of: dt.datetime | None) -> dt.date:
    return (as_of or dt.datetime.now(dt.timezone.utc)).date()


def _years_between(target: dt.date, base: dt.date) -> float:
    return max(0.0, (target - base).days / 365.0)


def _vol_from_series(series: list[dict[str, Any]] | None) -> tuple[float, bool]:
    """(annualized_vol, is_estimate_fallback)."""
    closes = [p.get("close") for p in (series or []) if isinstance(p.get("close"), (int, float))]
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
                    source = "yahoo"

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
        "estimate": source != "yahoo",
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
                    source = "yahoo"

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
        "estimate": source != "yahoo",
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

    covered = _covered_call(float(spot), vol, rate, as_of_d, chain if isinstance(chain, dict) else None,
                            contracts=contracts, fx=fx, guard_after=guard_after)
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
        "source": (chain.get("source") if isinstance(chain, dict) else "black_scholes"),
        "covered_call": covered,
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
