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
import math
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import market_data  # noqa: E402  -- shared fan-out width
import portfolio  # noqa: E402
import price_levels  # noqa: E402
import rebalance  # noqa: E402
import risk  # noqa: E402
import store  # noqa: E402
import tax_lots  # noqa: E402
import timeutil  # noqa: E402  -- shared Z-tolerant ISO parse + cache-freshness
from config import REPO_ROOT  # noqa: E402

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

EPS = 1e-6

# Option chains are fetched live from Yahoo; cache them so a second Exit-view
# load (or a config tweak) doesn't re-hammer the chain endpoint for every trim
# name. Same shape as risk.py's price-series cache.
_OPT_CACHE_DIR = REPO_ROOT / "data" / "cache" / "options"
OPT_CACHE_TTL_SECONDS = 3 * 3600
_RATE_CACHE = _OPT_CACHE_DIR / "risk-free-rate.json"
RATE_CACHE_TTL_SECONDS = 6 * 3600


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
        local_mv = p.get("market_value")
        fx = (mv_base / local_mv) if isinstance(local_mv, (int, float)) and abs(local_mv) > EPS else 1.0
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
    total_shares = max(0.0, float(total_shares))
    if total_shares <= EPS:
        return {"tranches": [], "n": 0, "adv": adv, "max_shares_per_day": None}

    max_per_day = (slice_pct * adv) if adv and adv > 0 else None
    if max_per_day and max_per_day > 0:
        n = max(1, min(horizon_days, math.ceil(total_shares / max_per_day)))
    else:
        n = max(1, min(horizon_days, default_tranches))

    per_shares = total_shares / n
    limits = _ladder_prices(level, price_local, n)
    tranches: list[dict[str, Any]] = []
    for i in range(n):
        # Spread evenly across the horizon (tranche 0 today, last on ~horizon).
        offset = 0 if n == 1 else round(i * (horizon_days - 1) / (n - 1))
        shares = per_shares
        tranches.append({
            "index": i + 1,
            "date": (start + dt.timedelta(days=offset)).isoformat(),
            "shares": round(shares, 4),
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
        exit_czk = max(0.0, pos["mv_base"] - target_czk)
        if exit_czk <= EPS:
            continue
        exit_shares = pos["qty"] * (exit_czk / pos["mv_base"]) if pos["mv_base"] else 0.0

        lots = tax_lots.classify_lots(holdings, sym, as_of=as_of)
        layers = _tax_layers(lots, exit_czk, near_exempt_days=near_exempt_days, tax_rate=tax_rate)

        # Schedule only the sell-now portion; the deferred bit waits on the clock.
        sell_now_shares = pos["qty"] * (layers["sell_now_czk"] / pos["mv_base"]) if pos["mv_base"] else 0.0
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
            "exit_shares": round(exit_shares, 4),
            "sell_now_shares": round(sell_now_shares, 4),
            "tax": layers,
            "schedule": sched,
        }
        if with_options:
            entry["options"] = _options_overlay(sym, pos, layers, series, cfg, as_of, rate=opt_rate)
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
    providers = list(dict.fromkeys(
        portfolio.provider_symbol_for(c["symbol"]) for c in cands
        if (posidx.get(c["symbol"]) or {}).get("mv_base", 0.0) > EPS
    ))
    if not providers:
        return

    def warm(provider: str) -> None:
        try:
            if fetch is not False:
                risk.load_price_series(provider, fetch=fetch)
            if with_options:
                _cached_option_chain(provider)
        except Exception:  # noqa: BLE001 -- warming is opportunistic, never fatal
            pass

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(FETCH_WORKERS, len(providers))) as pool:
        list(pool.map(warm, providers))


def _cache_fresh(iso: str | None, ttl: int = OPT_CACHE_TTL_SECONDS) -> bool:
    return timeutil.cache_fresh(iso, ttl)


def _cached_risk_free_rate() -> float | None:
    """Risk-free rate (decimal) for the options overlay, cached 6h. FRED's
    ``macro_snapshot()`` fetches nine CSV series (~10s); the rate barely moves
    intraday, so caching it keeps the whole Exit view snappy. None on total
    failure lets the overlay use its own neutral default."""
    cached = store.load(_RATE_CACHE)
    if isinstance(cached, dict) and _cache_fresh(cached.get("fetched_at"), RATE_CACHE_TTL_SECONDS):
        val = cached.get("rate")
        if isinstance(val, (int, float)):
            return float(val)
    try:
        import options_math
        from providers import fred
        # Only DGS10 is needed here; fetch that one series instead of FRED's full
        # nine-series macro snapshot so a cold Exit view isn't gated on ~8 CSVs.
        rate = options_math.risk_free_rate(snapshot=fred.series_snapshot("DGS10"))
    except Exception:  # noqa: BLE001
        rate = None
    if isinstance(rate, (int, float)):
        store.write_json(_RATE_CACHE, {
            "rate": rate,
            "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        })
    return rate


def _cached_option_chain(provider_sym: str) -> dict[str, Any] | None:
    """Yahoo option chain for a provider symbol, cached under data/cache/options.

    Live chain fetches are slow (crumb handshake + one request per expiry), so a
    fresh cache entry short-circuits the network. A miss/failure caches nothing
    and returns None; a stale hit beats a failed live pull (options are advisory,
    slightly-old strikes are fine)."""
    safe = "".join(ch for ch in provider_sym.upper() if ch.isalnum() or ch in "-._=")
    path = _OPT_CACHE_DIR / f"{safe}.json"
    cached = store.load(path)
    # A fresh entry short-circuits even when it recorded "no chain" (None), so a
    # foreign name with no listed options doesn't 404 on every single load.
    if isinstance(cached, dict) and "chain" in cached and _cache_fresh(cached.get("fetched_at")):
        return cached.get("chain")
    try:
        from providers import yahoo
        chain = yahoo.option_chain(provider_sym)
    except Exception:  # noqa: BLE001 -- no chain: BS fallback happens downstream
        chain = None
    store.write_json(path, {
        "symbol": provider_sym.upper(),
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "chain": chain,
    })
    return chain


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
        chain = _cached_option_chain(portfolio.provider_symbol_for(sym))
        return options_overlay.suggest_for_position(
            sym, pos, layers, series=series, cfg=cfg, as_of=as_of, chain=chain, rate=rate)
    except Exception:  # noqa: BLE001
        return None


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

    existing = trade_service.load_basket()
    merged = existing + [{"symbol": sym, "delta_czk": -abs(float(tranche["czk"]))}]
    basket = trade_service.save_basket(merged)
    return {"staged": True, "basket": basket, "tranche": tranche, "symbol": sym}


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
