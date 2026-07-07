#!/usr/bin/env python3
"""Process attribution -- "is this system earning its keep?"

Everything else in this repo measures a *position* (a band, a lot, a name's
risk). This measures the *process*: over a window, did the actual book beat the
honest alternatives that took no skill --

* **never-rebalanced**: freeze the book as it stood at the window start and let
  prices run (the "I should've just held" baseline), and
* **the benchmark**: put the same koruna -- the starting NAV and every subsequent
  deposit -- into SPY/QQQ instead.

The comparison is only honest if three things are true, and this module enforces
all three:

1. **Flows are neutralized.** A deposit is not performance. Every curve receives
   the *same* external cash flows on the same days (`external_flows` pulls them
   from the IBKR ledger), and the actual return is a **time-weighted return**
   that removes them -- so a big transfer in doesn't masquerade as a good month.
2. **FX-clean.** A CZK-base book holding USD names earns the stock's return *and*
   the exchange rate's. Every foreign price is converted day-by-day through the
   FX panel (`fx_history`), never a single point-in-time rate, so the benchmark's
   USD curve and each held name are compared in the same currency.
3. **Same starting capital.** All curves are seeded with the actual NAV at the
   window start, so they diverge only on *what the money was invested in*.

The math is pure -- plain dicts/lists in, plain dicts out, no disk or network --
so every curve and TWR is unit-tested with fixtures, exactly like ``risk.analyze``
and ``overview``. Only ``attribution_report`` does IO (history/holdings/prices/
FX), and its price fetch is injectable. Read-only; it never trades.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402  -- DATA_DIR for the verdict cache
import fx_history  # noqa: E402  -- daily FX panel (rate_on) for FX-clean conversion
import portfolio  # noqa: E402  -- clean_symbol / provider_symbol_for
import store  # noqa: E402
import timeutil  # noqa: E402

# Display range -> lookback days (mirrors serve.PRICE_HISTORY_RANGES + risk).
RANGE_DAYS = {"3mo": 90, "6mo": 180, "1y": 365, "2y": 730, "5y": 1825}
DEFAULT_RANGE = "1y"
DEFAULT_BASE = "CZK"
DEFAULT_BENCHMARK = "SPY"

# A compact headline verdict cached from the last computed report, so the "Today"
# cockpit can surface "is the process earning its keep?" without recomputing (the
# full report fetches prices; Today must not hang a network hop off its load).
ATTRIBUTION_VERDICT_JSON = config.DATA_DIR / "cache" / "attribution-verdict.json"

# A price fetch takes (provider_symbol, range) and returns [{date, close}] | None,
# same seam as risk.load_price_series so the two share a provider/cache path.
Fetch = Callable[[str, str], "list[dict[str, Any]] | None"]

# Cash-transaction rows whose type names a deposit or withdrawal are the only
# *external* flows; dividends/fees/interest/tax are internal book return and must
# NOT be neutralized. Substring match is robust to IBKR's exact spelling of its
# one external type, "Deposits/Withdrawals".
_EXTERNAL_FLOW_MARKERS = ("deposit", "withdraw")


def _num(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f


def _is_external_flow(txn_type: Any) -> bool:
    t = str(txn_type or "").lower()
    return any(m in t for m in _EXTERNAL_FLOW_MARKERS)


# --------------------------------------------------------------------------- #
# Flows + time-weighted return (pure)
# --------------------------------------------------------------------------- #
def external_flows(cash_transactions: list[dict] | None) -> dict[str, float]:
    """Net external cash flow per date, in base currency: deposits positive,
    withdrawals negative (IBKR already signs ``base_amount`` that way). Only
    deposit/withdrawal rows count -- dividends, fees, interest and tax are
    internal to the book and stay in its return."""
    flows: dict[str, float] = {}
    for c in cash_transactions or []:
        if not isinstance(c, dict) or not _is_external_flow(c.get("type")):
            continue
        date = c.get("date")
        amt = _num(c.get("base_amount"))
        if amt is None:
            amt = _num(c.get("amount"))  # base==native fallback for a base-ccy flow
        if isinstance(date, str) and amt is not None:
            flows[date] = flows.get(date, 0.0) + amt
    return flows


def time_weighted_return(points: list[dict[str, Any]], flows: dict[str, float] | None = None) -> float | None:
    """Flow-neutralized TWR over a value curve: chain each day's market return,
    removing the external flow that landed that day so it doesn't count as
    performance. ``points`` is ``[{date, value}]`` sorted ascending. Returns the
    total fractional return, or None with fewer than two usable points.

    Per-day sub-return, flow treated as arriving at period end (so it earns
    nothing that day): ``r_t = (V_t - F_t) / V_{t-1} - 1``; TWR = Prod(1+r_t) - 1."""
    flows = flows or {}
    clean = [(p["date"], float(p["value"])) for p in points
             if isinstance(p, dict) and p.get("date") and isinstance(p.get("value"), (int, float)) and p["value"] > 0]
    if len(clean) < 2:
        return None
    growth = 1.0
    prev_v = clean[0][1]
    for date, v in clean[1:]:
        f = flows.get(date, 0.0)
        if prev_v <= 0:
            prev_v = v
            continue
        growth *= (v - f) / prev_v
        prev_v = v
    return growth - 1.0


# --------------------------------------------------------------------------- #
# Position reconstruction + FX-clean price series (pure)
# --------------------------------------------------------------------------- #
def positions_at(current_positions: list[dict], trades: list[dict] | None, as_of: str) -> dict[str, float]:
    """Reconstruct share quantities as they stood at ``as_of`` by unwinding every
    trade dated after it: ``qty_at = qty_now - sum(signed qty of later trades)``.
    IBKR signs sells negative, so subtracting the post-window net removes exactly
    what was traded since. Symbols that net to ~zero are dropped."""
    qty: dict[str, float] = {}
    for p in current_positions or []:
        if not isinstance(p, dict):
            continue
        sym = portfolio.clean_symbol(p.get("symbol"))
        q = _num(p.get("quantity"))
        if sym and q is not None:
            qty[sym] = qty.get(sym, 0.0) + q
    for t in trades or []:
        if not isinstance(t, dict):
            continue
        td = t.get("date")
        if not (isinstance(td, str) and td > as_of):
            continue
        sym = portfolio.clean_symbol(t.get("symbol"))
        q = _num(t.get("quantity"))
        if sym and q is not None:
            qty[sym] = qty.get(sym, 0.0) - q
    return {s: q for s, q in qty.items() if abs(q) > 1e-9}


def czk_price_series(native_points: list[dict] | None, panel: dict, *, currency: str, base: str = DEFAULT_BASE) -> dict[str, float]:
    """Convert a native daily-close series to base currency, day by day, through
    the FX panel: ``czk[date] = close * rate_on(<CCY><BASE>, date)``. A base-
    currency name passes through unconverted. A date with no FX quote on/before it
    is dropped (better a gap than a wrong rate)."""
    out: dict[str, float] = {}
    ccy = (currency or base).upper()
    same = ccy == base.upper()
    pair = f"{ccy}{base.upper()}"
    for p in native_points or []:
        if not isinstance(p, dict):
            continue
        date = p.get("date")
        close = _num(p.get("close"))
        if not (isinstance(date, str) and close is not None):
            continue
        if same:
            out[date] = close
            continue
        rate = fx_history.rate_on(panel, pair, date)
        if rate:
            out[date] = close * rate
    return out


def _asof(series: dict[str, float], date: str) -> float | None:
    """Most recent value on or before ``date`` (series keys are ISO dates)."""
    best: str | None = None
    for k in series:
        if k <= date and (best is None or k > best):
            best = k
    return series[best] if best is not None else None


def hold_index(base_values_at_start: dict[str, float], price_czk: dict[str, dict[str, float]],
               dates: list[str], start: str) -> dict[str, float]:
    """A synthetic price index for the *frozen* book: the start-weighted sum of
    each name's CZK total return since ``start`` (level 1.0 at start). Names
    without a usable start price are dropped from the index (and surfaced as a
    caveat by the caller). Empty when nothing can be priced."""
    weights: dict[str, float] = {}
    start_px: dict[str, float] = {}
    total = 0.0
    for sym, bv in base_values_at_start.items():
        sp = _asof(price_czk.get(sym) or {}, start)
        if sp and bv:
            start_px[sym] = sp
            weights[sym] = bv
            total += bv
    if total <= 0:
        return {}
    weights = {s: w / total for s, w in weights.items()}
    index: dict[str, float] = {}
    for d in dates:
        level = 0.0
        for sym, w in weights.items():
            px = _asof(price_czk.get(sym) or {}, d)
            if px:
                level += w * (px / start_px[sym])
        if level > 0:
            index[d] = level
    return index


def flow_curve(index: dict[str, float], seed_value: float, flows: dict[str, float], dates: list[str]) -> list[dict[str, Any]]:
    """Value curve of a strategy whose per-unit price is ``index``: seed the start
    NAV into it, then buy more units with each external flow at that day's index
    level, and mark to the index daily. This is how both counterfactuals receive
    the *same* flows as the actual book -- they differ only in ``index``."""
    if not index or not dates:
        return []
    start = dates[0]
    i0 = _asof(index, start)
    if not i0:
        return []
    units = seed_value / i0
    curve: list[dict[str, Any]] = []
    for d in dates:
        lvl = _asof(index, d)
        if not lvl:
            continue
        f = flows.get(d)
        if f:
            units += f / lvl
        curve.append({"date": d, "value": round(units * lvl, 2)})
    return curve


# --------------------------------------------------------------------------- #
# IO assembler
# --------------------------------------------------------------------------- #
def _yahoo_fetch(symbol: str, rng: str) -> "list[dict[str, Any]] | None":
    from providers import yahoo  # lazy, mirrors risk._yahoo_fetch
    result = yahoo.chart(symbol, rng=rng, interval="1d")
    ph = yahoo.price_history_from_chart(result, rng=rng, interval="1d")
    return ph.get("points") if ph else None


def _range_for_days(days: int) -> str:
    for key in ("3mo", "6mo", "1y", "2y", "5y"):
        if days <= RANGE_DAYS[key]:
            return key
    return "5y"


def _window_nav(nav_series: list[dict], days: int) -> list[dict[str, Any]]:
    """The ``[{date, value}]`` NAV points inside the trailing window (value = the
    base-currency ``nav`` field), sorted ascending."""
    pts = sorted(
        ({"date": r["date"], "value": float(r["nav"])}
         for r in nav_series
         if isinstance(r, dict) and r.get("date") and isinstance(r.get("nav"), (int, float))),
        key=lambda p: p["date"],
    )
    if not pts:
        return []
    end = timeutil.parse_iso_utc(pts[-1]["date"])
    if end is None:
        return pts
    open_iso = (end - dt.timedelta(days=days)).date().isoformat()
    inside = [p for p in pts if p["date"] >= open_iso]
    return inside if len(inside) >= 2 else pts


def attribution_report(
    history: dict | None,
    holdings: dict | None,
    *,
    rng: str = DEFAULT_RANGE,
    benchmark: str = DEFAULT_BENCHMARK,
    fetch: Fetch | None = None,
    panel: dict | None = None,
) -> dict:
    """Assemble the attribution payload: actual TWR + the never-rebalanced and
    benchmark counterfactuals over the window, FX-clean and flow-neutralized.
    Read-only. Degrades with caveats (never errors) when history/prices are thin."""
    rng = rng if rng in RANGE_DAYS else DEFAULT_RANGE
    base = str((holdings or {}).get("base_currency") or (history or {}).get("base_currency") or DEFAULT_BASE).upper()
    benchmark = portfolio.clean_symbol(benchmark) or DEFAULT_BENCHMARK
    caveats: list[str] = []

    nav_series = (history or {}).get("nav_series") or []
    nav_pts = _window_nav(nav_series, RANGE_DAYS[rng])
    if len(nav_pts) < 2:
        return {"as_of": None, "base": base, "range": rng, "benchmark": benchmark,
                "twr": {}, "curves": {}, "caveats": [
                    "No portfolio history yet — sync IBKR history to attribute the process."],
                "enough_data": False}

    dates = [p["date"] for p in nav_pts]
    start, as_of = dates[0], dates[-1]
    seed = float(nav_pts[0]["value"])
    flows = external_flows((history or {}).get("cash_transactions"))
    window_flows = {d: v for d, v in flows.items() if start < d <= as_of}

    fetch = fetch or _yahoo_fetch
    panel = panel if panel is not None else fx_history.load_panel()
    fetch_range = _range_for_days(RANGE_DAYS[rng])

    # --- actual ---
    twr: dict[str, float | None] = {"actual": _pct(time_weighted_return(nav_pts, window_flows))}
    curves: dict[str, list] = {"actual": nav_pts}

    # --- benchmark counterfactual (USD -> CZK through the panel) ---
    aliases = portfolio.symbol_aliases()
    bench_provider = portfolio.provider_symbol_for(benchmark, aliases)
    bench_native = _safe_fetch(fetch, bench_provider, fetch_range)
    bench_czk = czk_price_series(bench_native, panel, currency="USD", base=base)
    bench_curve = flow_curve(bench_czk, seed, window_flows, dates)
    if bench_curve:
        curves["benchmark"] = bench_curve
        twr["benchmark"] = _pct(time_weighted_return(bench_curve, window_flows))
    else:
        caveats.append(f"Could not price the {benchmark} benchmark in {base}; that curve is omitted.")

    # --- never-rebalanced counterfactual (freeze the book at window start) ---
    hist_from = (history or {}).get("from_date")
    if isinstance(hist_from, str) and hist_from > start:
        caveats.append(
            f"Trade ledger begins {hist_from}, after the window start {start}: the "
            "frozen book is reconstructed from a partial ledger and may be approximate.")
    qty_at_start = positions_at((holdings or {}).get("positions") or [],
                                (history or {}).get("trades"), start)
    price_czk: dict[str, dict[str, float]] = {}
    base_values_at_start: dict[str, float] = {}
    priced_missing: list[str] = []
    for sym, q in qty_at_start.items():
        ccy = _currency_for(holdings, sym) or base
        native = _safe_fetch(fetch, portfolio.provider_symbol_for(sym, aliases), fetch_range)
        series = czk_price_series(native, panel, currency=ccy, base=base)
        sp = _asof(series, start)
        if not sp:
            priced_missing.append(sym)
            continue
        price_czk[sym] = series
        base_values_at_start[sym] = q * sp
    hidx = hold_index(base_values_at_start, price_czk, dates, start)
    hold_curve = flow_curve(hidx, seed, window_flows, dates)
    if hold_curve:
        curves["hold"] = hold_curve
        twr["hold"] = _pct(time_weighted_return(hold_curve, window_flows))
        if priced_missing:
            caveats.append(
                "Frozen book excludes names with no price history: "
                + ", ".join(sorted(priced_missing)) + ".")
    else:
        caveats.append("Could not reconstruct the frozen book (no priceable start positions); "
                       "the never-rebalanced curve is omitted.")

    return {
        "as_of": as_of,
        "base": base,
        "range": rng,
        "benchmark": benchmark,
        "start": start,
        "twr": twr,
        "curves": curves,
        "flows_total": round(sum(window_flows.values()), 2),
        "caveats": caveats,
        "enough_data": True,
    }


def _pct(frac: float | None) -> float | None:
    return round(frac * 100.0, 2) if frac is not None else None


def _safe_fetch(fetch: Fetch, symbol: str, rng: str) -> "list[dict[str, Any]] | None":
    if not symbol:
        return None
    try:
        return fetch(symbol, rng)
    except Exception:  # noqa: BLE001 -- a provider miss is a caveat, not an error
        return None


def _currency_for(holdings: dict | None, symbol: str) -> str | None:
    for p in (holdings or {}).get("positions") or []:
        if isinstance(p, dict) and portfolio.clean_symbol(p.get("symbol")) == symbol:
            c = str(p.get("currency") or "").upper()
            return c or None
    return None


# --------------------------------------------------------------------------- #
# Headline verdict cache (for the "Today" cockpit)
# --------------------------------------------------------------------------- #
def verdict_from_report(report: dict) -> dict:
    """The one-line "did the process earn its keep?" digest of a full report:
    the actual TWR and its edge (in percentage points) over each counterfactual.
    Pure -- no disk."""
    twr = report.get("twr") or {}
    actual, hold, bench = twr.get("actual"), twr.get("hold"), twr.get("benchmark")

    def _delta(a: Any, b: Any) -> float | None:
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            return round(float(a) - float(b), 2)
        return None

    return {
        "enough_data": bool(report.get("enough_data")),
        "as_of": report.get("as_of"),
        "start": report.get("start"),
        "range": report.get("range"),
        "benchmark": report.get("benchmark"),
        "actual_pct": actual,
        "hold_pct": hold,
        "benchmark_pct": bench,
        "vs_hold_pp": _delta(actual, hold),
        "vs_benchmark_pp": _delta(actual, bench),
    }


def cache_verdict(report: dict, *, path: Path = ATTRIBUTION_VERDICT_JSON) -> dict:
    """Persist the headline verdict (best-effort). A write failure is swallowed --
    a missing cockpit tile is never worth failing the attribution request over."""
    verdict = verdict_from_report(report)
    verdict["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    try:
        store.write_json(path, verdict)
    except OSError:
        pass
    return verdict


def load_verdict(path: Path = ATTRIBUTION_VERDICT_JSON) -> dict | None:
    """The last cached verdict, or None when attribution has never been run."""
    raw = store.load(path)
    return raw if isinstance(raw, dict) else None


def _main() -> int:
    import json

    from holdings_sync import history_payload
    hist = history_payload()
    holds = store.load(config.HOLDINGS_JSON)
    print(json.dumps(attribution_report(hist, holds), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
