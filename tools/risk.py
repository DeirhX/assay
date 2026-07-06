#!/usr/bin/env python3
"""Portfolio-level risk lens: correlation, volatility, effective bets, stress.

The rest of this repo measures risk one name at a time -- a band per symbol. That
is blind to the thing that actually sinks a concentrated semis/AI book: the names
move *together*. Every position can sit inside its band while ~all of NAV rides a
single macro factor. This module computes the portfolio-level numbers that make
that correlated concentration visible:

* a pairwise correlation matrix of daily returns,
* per-name and portfolio annualized volatility,
* an "effective number of independent bets" (correlation-aware, not just a
  weight count), and the share of portfolio variance that comes from co-movement
  rather than idiosyncratic risk,
* a parameterized factor-shock stress test (e.g. "semis -25%") via each holding's
  beta to a factor ETF.

Honest limitations -- surfaced, never smoothed (see ``CAVEATS``):

* Correlation from free daily closes is noisy and **regime-dependent**. In a real
  crash, cross-asset correlation converges toward 1.0, so the diversification this
  shows in calm markets *overstates* the protection you get in the drawdown you
  actually care about. This is a decision aid, not a risk oracle.
* Betas are simple OLS slopes over the sample window; they drift.
* Options/derivative legs and names with no usable price series are excluded and
  reported, not silently dropped.

The math is split from the IO on purpose: :func:`analyze` is a pure function over
price series (unit-tested with fixtures, no network), while :func:`risk_report`
does the Yahoo fetching/caching and assembles the API payload.
"""

from __future__ import annotations

import datetime as dt
import math
import sys
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import portfolio  # noqa: E402  -- shared weight/holdings layer
import store  # noqa: E402
from config import REPO_ROOT  # noqa: E402

CACHE_DIR = REPO_ROOT / "data" / "cache" / "risk"

TRADING_DAYS = 252
MIN_OBS = 30  # below this, correlations/betas are too thin to trust
CACHE_TTL_SECONDS = 12 * 3600
FETCH_WORKERS = 8  # cold cache: fan the per-symbol Yahoo pulls out instead of serial

# Factor-shock scenarios. Each maps a factor ETF to a hypothetical move; every
# holding's exposure is its OLS beta to that factor over the sample window. SOXX
# is the headline because the book is a semis bet; SPY is the broad-market check.
# A rates scenario needs a rate-proxy ETF (e.g. TLT) and a duration assumption,
# which is deliberately left out rather than faked -- add one to DEFAULT_SCENARIOS
# if you want it, and own the duration caveat.
DEFAULT_SCENARIOS: list[dict[str, Any]] = [
    {"key": "semis_-25", "label": "Semis selloff", "factor": "SOXX", "shock_pct": -25.0,
     "note": "iShares Semiconductor ETF down 25% (a 2022-style sector drawdown)."},
    {"key": "broad_-15", "label": "Broad market drop", "factor": "SPY", "shock_pct": -15.0,
     "note": "S&P 500 down 15% (a garden-variety correction)."},
]

CAVEATS = [
    "Correlation from free daily closes is regime-dependent: in a crash, "
    "cross-asset correlation converges toward 1.0, so calm-market diversification "
    "overstates the protection you actually get in a drawdown.",
    "Betas are simple OLS slopes over the sample window and drift over time.",
    "This is a decision aid, not a risk oracle. It does not predict returns.",
]


# --------------------------------------------------------------------------- #
# pure math (stdlib only -- no numpy/pandas)
# --------------------------------------------------------------------------- #
def _log_returns(closes: list[float]) -> list[float]:
    out: list[float] = []
    for i in range(1, len(closes)):
        prev, cur = closes[i - 1], closes[i]
        if prev and cur and prev > 0 and cur > 0:
            out.append(math.log(cur / prev))
    return out


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _variance(xs: list[float]) -> float:
    """Sample variance (n-1). 0.0 for degenerate input."""
    n = len(xs)
    if n < 2:
        return 0.0
    m = _mean(xs)
    return sum((x - m) ** 2 for x in xs) / (n - 1)


def _stdev(xs: list[float]) -> float:
    return math.sqrt(_variance(xs))


def _covariance(xs: list[float], ys: list[float]) -> float:
    n = min(len(xs), len(ys))
    if n < 2:
        return 0.0
    xs, ys = xs[:n], ys[:n]
    mx, my = _mean(xs), _mean(ys)
    return sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / (n - 1)


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    sx, sy = _stdev(xs), _stdev(ys)
    if sx == 0.0 or sy == 0.0:
        return None
    return _covariance(xs, ys) / (sx * sy)


def _beta(asset: list[float], factor: list[float]) -> float | None:
    var = _variance(factor)
    if var == 0.0:
        return None
    return _covariance(asset, factor) / var


def _series_to_map(points: list[dict[str, Any]] | None) -> dict[str, float]:
    """{date(YYYY-MM-DD): close} from price-history points, positives only."""
    out: dict[str, float] = {}
    for p in points or []:
        date = p.get("date")
        close = p.get("close")
        if date and isinstance(close, (int, float)) and close > 0:
            out[str(date)[:10]] = float(close)
    return out


def _aligned(maps: dict[str, dict[str, float]], symbols: list[str]) -> tuple[list[str], dict[str, list[float]]]:
    """Closes for ``symbols`` aligned to the dates they all share."""
    present = [s for s in symbols if len(maps.get(s, {})) >= 2]
    if not present:
        return [], {}
    common = set(maps[present[0]])
    for s in present[1:]:
        common &= set(maps[s])
    dates = sorted(common)
    return dates, {s: [maps[s][d] for d in dates] for s in present}


def analyze(
    weights: dict[str, float],
    series: dict[str, list[dict[str, Any]]],
    *,
    scenarios: list[dict[str, Any]] | None = None,
    factor_series: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Pure risk computation over price series. No network, no holdings IO.

    ``weights`` is symbol -> percent of book (any positive scale; renormalized
    over the names that have a usable series). ``series``/``factor_series`` are
    symbol -> price-history points (``{"date","close"}``). Returns the structured
    risk payload the API and UI consume.
    """
    scenarios = scenarios if scenarios is not None else DEFAULT_SCENARIOS
    factor_series = factor_series or {}

    maps = {s: _series_to_map(pts) for s, pts in series.items()}
    factor_maps = {s: _series_to_map(pts) for s, pts in (factor_series or {}).items()}

    # Names we can actually measure (positive weight + a usable series).
    usable = [s for s in weights if weights.get(s, 0.0) > 0 and len(maps.get(s, {})) >= 2]
    excluded = sorted(
        {s for s in weights if weights.get(s, 0.0) > 0} - set(usable)
    )

    dates, closes = _aligned(maps, usable)
    n_obs = max(0, len(dates) - 1)
    included = sorted(closes.keys(), key=lambda s: -weights.get(s, 0.0))

    raw = {s: weights[s] for s in included}
    total_w = sum(raw.values())
    norm = {s: (raw[s] / total_w) for s in included} if total_w else {}
    excluded_weight_pct = round(
        sum(weights[s] for s in excluded), 2
    ) if excluded else 0.0

    returns = {s: _log_returns(closes[s]) for s in included}
    daily_vol = {s: _stdev(returns[s]) for s in included}
    ann_vol = {s: daily_vol[s] * math.sqrt(TRADING_DAYS) for s in included}

    # Correlation matrix (daily returns).
    matrix: dict[str, dict[str, float | None]] = {}
    for a in included:
        row: dict[str, float | None] = {}
        for b in included:
            if a == b:
                row[b] = 1.0
            else:
                c = _pearson(returns[a], returns[b])
                row[b] = round(c, 4) if c is not None else None
        matrix[a] = row

    # Portfolio variance with Sigma_ij = corr_ij * annvol_i * annvol_j.
    port_var = 0.0
    diag_var = 0.0
    for a in included:
        wa, va = norm.get(a, 0.0), ann_vol[a]
        diag_var += (wa * va) ** 2
        for b in included:
            wb, vb = norm.get(b, 0.0), ann_vol[b]
            c = matrix[a][b]
            corr = c if c is not None else 0.0
            port_var += wa * wb * corr * va * vb
    port_vol = math.sqrt(port_var) if port_var > 0 else 0.0

    weighted_avg_vol = sum(norm.get(s, 0.0) * ann_vol[s] for s in included)
    hhi = sum(w * w for w in norm.values())
    effective_names = (1.0 / hhi) if hhi else 0.0
    diversification_ratio = (weighted_avg_vol / port_vol) if port_vol else None
    effective_bets = (diversification_ratio ** 2) if diversification_ratio else None
    # Share of portfolio variance that comes from co-movement rather than each
    # name's own idiosyncratic risk. High => the book is really one correlated bet.
    covariance_share = ((port_var - diag_var) / port_var) if port_var > 0 else None

    avg_pair_corr = _avg_pairwise_corr(matrix, included)

    positions = [
        {
            "symbol": s,
            "weight_pct": round(weights[s], 2),
            "norm_weight_pct": round(norm.get(s, 0.0) * 100.0, 2),
            "ann_vol_pct": round(ann_vol[s] * 100.0, 2),
        }
        for s in included
    ]

    stress = _stress(scenarios, included, norm, returns, maps, factor_maps)

    enough = n_obs >= MIN_OBS
    caveats = list(CAVEATS)
    if not enough and included:
        caveats.insert(0, f"Only {n_obs} overlapping observations across held names "
                          f"(< {MIN_OBS}); correlations and betas are weak here.")
    if excluded:
        caveats.append(
            f"Excluded {len(excluded)} name(s) with no usable price series "
            f"(~{excluded_weight_pct}% of book): {', '.join(excluded)}.")

    return {
        "as_of": dates[-1] if dates else None,
        "source": "yahoo",
        "n_obs": n_obs,
        "enough_data": enough,
        "positions": positions,
        "excluded": excluded,
        "excluded_weight_pct": excluded_weight_pct,
        "correlation": {"symbols": included, "matrix": matrix},
        "metrics": {
            "portfolio_vol_pct": round(port_vol * 100.0, 2) if port_vol else None,
            "weighted_avg_vol_pct": round(weighted_avg_vol * 100.0, 2) if included else None,
            "diversification_ratio": round(diversification_ratio, 3) if diversification_ratio else None,
            "effective_bets": round(effective_bets, 2) if effective_bets else None,
            "effective_names": round(effective_names, 2) if effective_names else None,
            "covariance_share_pct": round(covariance_share * 100.0, 1) if covariance_share is not None else None,
            "avg_pairwise_corr": round(avg_pair_corr, 3) if avg_pair_corr is not None else None,
            "n_names": len(included),
        },
        "stress": stress,
        "caveats": caveats,
    }


def _avg_pairwise_corr(matrix: dict[str, dict[str, float | None]], symbols: list[str]) -> float | None:
    vals: list[float] = []
    for i, a in enumerate(symbols):
        for b in symbols[i + 1:]:
            c = matrix[a][b]
            if c is not None:
                vals.append(c)
    return _mean(vals) if vals else None


def _stress(
    scenarios: list[dict[str, Any]],
    included: list[str],
    norm: dict[str, float],
    returns: dict[str, list[float]],
    maps: dict[str, dict[str, float]],
    factor_maps: dict[str, dict[str, float]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for sc in scenarios:
        factor = sc.get("factor")
        shock = float(sc.get("shock_pct", 0.0))
        fmap = factor_maps.get(factor, {}) if factor is not None else {}
        contributions: list[dict[str, Any]] = []
        total = 0.0
        measurable = bool(fmap)
        for s in included:
            beta = None
            if fmap:
                # Pairwise alignment maximizes the beta sample for each name.
                common = sorted(set(maps.get(s, {})) & set(fmap))
                if len(common) >= 3:
                    a_ret = _log_returns([maps[s][d] for d in common])
                    f_ret = _log_returns([fmap[d] for d in common])
                    beta = _beta(a_ret, f_ret)
            impact = (norm.get(s, 0.0) * beta * shock) if beta is not None else None
            if impact is not None:
                total += impact
            contributions.append({
                "symbol": s,
                "beta": round(beta, 3) if beta is not None else None,
                "impact_pct": round(impact, 2) if impact is not None else None,
            })
        out.append({
            "key": sc.get("key"),
            "label": sc.get("label"),
            "factor": factor,
            "shock_pct": shock,
            "note": sc.get("note"),
            "measurable": measurable,
            "nav_impact_pct": round(total, 2) if measurable else None,
            "contributions": sorted(
                contributions, key=lambda c: (c["impact_pct"] is None, c["impact_pct"] or 0.0)
            ),
        })
    return out


# --------------------------------------------------------------------------- #
# IO layer: fetch + cache price series, assemble the API payload
# --------------------------------------------------------------------------- #
def _cache_path(symbol: str, rng: str) -> Path:
    safe = "".join(ch for ch in symbol.upper() if ch.isalnum() or ch in "-._=")
    return CACHE_DIR / f"{safe}-{rng}.json"


def load_price_series(symbol: str, *, rng: str = "1y", fetch: Callable[[str, str], list[dict[str, Any]] | None] | None = None) -> list[dict[str, Any]] | None:
    """Daily-close points for ``symbol``, cached under ``data/cache/risk``.

    ``fetch`` is injectable so tests never touch the network; the default hits
    Yahoo's chart endpoint via the shared provider.
    """
    cached = store.load(_cache_path(symbol, rng))
    if isinstance(cached, dict):
        fetched_at = cached.get("fetched_at")
        if _fresh(fetched_at) and isinstance(cached.get("series"), list):
            return cached["series"]

    fetch = fetch or _yahoo_fetch
    try:
        series = fetch(symbol, rng)
    except Exception:  # noqa: BLE001 -- provider hiccup: treat as "no series"
        series = None
    if series:
        store.write_json(_cache_path(symbol, rng), {
            "symbol": symbol.upper(),
            "range": rng,
            "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "series": series,
        })
    elif isinstance(cached, dict) and isinstance(cached.get("series"), list):
        return cached["series"]  # stale beats nothing when the live pull fails
    return series


def _fresh(iso: str | None) -> bool:
    if not iso:
        return False
    try:
        when = dt.datetime.fromisoformat(iso)
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    age = (dt.datetime.now(dt.timezone.utc) - when).total_seconds()
    return 0 <= age < CACHE_TTL_SECONDS


def _yahoo_fetch(symbol: str, rng: str) -> list[dict[str, Any]] | None:
    from providers import yahoo  # lazy: keeps import cost off pure-math callers
    result = yahoo.chart(symbol, rng=rng, interval="1d")
    ph = yahoo.price_history_from_chart(result, rng=rng, interval="1d")
    return ph.get("points") if ph else None


def _researchable_weights(holdings: dict[str, Any]) -> list[dict[str, str | float]]:
    """[{display, provider, weight_pct}] for held, researchable, positive names."""
    weights = portfolio.holdings_weights(holdings)
    aliases = portfolio.symbol_aliases()
    rows: list[dict[str, str | float]] = []
    for pos in holdings.get("positions", []):
        if not portfolio.is_researchable_position(pos):
            continue
        sym = portfolio.clean_symbol(pos.get("symbol"))
        w = weights.get(sym)
        if not sym or w is None or w <= 0:
            continue
        rows.append({
            "display": sym,
            "provider": portfolio.provider_symbol_for(sym, aliases),
            "weight_pct": w,
        })
    return rows


def _fetch_series_many(
    providers: list[str],
    *,
    rng: str,
    fetch: Callable[[str, str], list[dict[str, Any]] | None] | None,
) -> dict[str, list[dict[str, Any]]]:
    """Load price series for a set of provider symbols, fanned out across a small
    thread pool. Cold, this turns ~30 serial Yahoo round-trips into a handful of
    parallel batches; warm, every call is a disk hit and the pool is trivial.
    Each symbol writes its own cache file, so there is no shared mutable state."""
    uniq = list(dict.fromkeys(p for p in providers if p))
    if not uniq:
        return {}
    from concurrent.futures import ThreadPoolExecutor
    out: dict[str, list[dict[str, Any]]] = {}
    with ThreadPoolExecutor(max_workers=min(FETCH_WORKERS, len(uniq))) as pool:
        for provider, pts in zip(uniq, pool.map(lambda p: load_price_series(p, rng=rng, fetch=fetch), uniq)):
            if pts:
                out[provider] = pts
    return out


def risk_report(
    holdings: dict[str, Any],
    *,
    rng: str = "1y",
    scenarios: list[dict[str, Any]] | None = None,
    fetch: Callable[[str, str], list[dict[str, Any]] | None] | None = None,
) -> dict[str, Any]:
    """Fetch series for held names + factor ETFs and assemble the risk payload."""
    scenarios = scenarios if scenarios is not None else DEFAULT_SCENARIOS
    rows = _researchable_weights(holdings)

    # Weights keyed by the display symbol; fetch by provider symbol but report
    # the broker-facing name the rest of the UI uses. All the network pulls
    # (holdings + factor ETFs) go out together so a cold load isn't serial.
    weights: dict[str, float] = {}
    display_provider: list[tuple[str, str]] = []
    for r in rows:
        display, provider = str(r["display"]), str(r["provider"])
        weights[display] = float(r["weight_pct"])
        display_provider.append((display, provider))

    factors = [str(sc["factor"]) for sc in scenarios if sc.get("factor")]
    by_provider = _fetch_series_many(
        [p for _d, p in display_provider] + factors, rng=rng, fetch=fetch)

    series = {display: by_provider[provider] for display, provider in display_provider
              if provider in by_provider}
    factor_series = {f: by_provider[f] for f in factors if f in by_provider}

    report = analyze(weights, series, scenarios=scenarios, factor_series=factor_series)
    report["range"] = rng
    report["snapshot"] = holdings.get("generated_at")
    report["currency"] = holdings.get("base_currency") or "CZK"
    report["nav"] = holdings.get("net_asset_value")
    report["invested"] = portfolio.invested_value(holdings.get("positions", []))
    missing_factors = [sc.get("factor") for sc in scenarios if sc.get("factor") not in factor_series]
    if missing_factors:
        report["caveats"].append(
            "Could not fetch factor series for: " + ", ".join(sorted(set(filter(None, missing_factors))))
            + " -- those scenarios are not measurable right now.")
    return report


def _main() -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Portfolio risk lens (correlation, vol, stress).")
    parser.add_argument("--range", default="1y", help="Yahoo range for price history (default 1y).")
    args = parser.parse_args()

    portfolio.require_data()
    holdings = store.load(portfolio.HOLDINGS_JSON)
    if not holdings:
        raise SystemExit("no holdings snapshot — sync from IBKR first")
    print(json.dumps(risk_report(holdings, rng=args.range), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
