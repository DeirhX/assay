#!/usr/bin/env python3
"""Before -> after portfolio risk delta for the pre-trade path.

``risk.py`` is a *destination*: you only see concentration and effective bets by
navigating to the Risk view. But those are exactly the facts that should confront
you while deciding, not after. This puts the same lens on the decision -- given the
book before a basket and the book after, what does the trade do to concentration
and diversification, and does it cross a line worth a pre-flight warning?

Two tiers, split by cost:

* **Concentration** (top-N weight, HHI -> effective *names*) is pure weight math --
  instant, no network -- so it rides along on every what-if and trade preview.
* **Correlation-aware** metrics (effective *bets*, portfolio vol) need a price
  series. :func:`delta` computes them via :func:`risk.analyze` only when the caller
  supplies ``series`` (e.g. assembled from the risk cache), and omits them
  otherwise rather than forcing a network fetch into the preview path.

Everything here is pure over its inputs and unit-tested without IO.
"""
from __future__ import annotations

from typing import Any

TOP_N = 5

# Pre-flight thresholds (percent of invested book / effective-bet count).
CONCENTRATION_CEILING_PCT = 60.0    # a top-5 above this is an already-loud book
CONCENTRATION_JUMP_PCT = 3.0        # a basket adding >= this to top-5 is worth a flag
EFFECTIVE_BETS_DROP = 0.25          # a correlation-aware diversification loss worth a flag


def concentration(weights: dict[str, float]) -> dict[str, Any]:
    """Pure concentration snapshot of a weight map (symbol -> percent of book).

    ``top5_pct`` is the share in the five largest names; ``effective_names`` is the
    HHI-inverse (1/sum(w_i^2) on normalized weights) -- the count of equally-sized
    positions the book behaves like, which falls as weight piles into a few names.
    Weight-only: it says nothing about whether those names move together (that's
    ``effective_bets``, which needs correlations)."""
    vals = sorted((float(w) for w in weights.values() if isinstance(w, (int, float)) and w > 0),
                  reverse=True)
    total = sum(vals)
    top1 = vals[0] if vals else 0.0
    top5 = sum(vals[:TOP_N])
    if total > 0:
        norm = [w / total for w in vals]
        hhi = sum(x * x for x in norm)
        eff = (1.0 / hhi) if hhi else 0.0
    else:
        hhi = 0.0
        eff = 0.0
    return {
        "top1_pct": round(top1, 2),
        "top5_pct": round(top5, 2),
        "hhi": round(hhi, 4),
        "effective_names": round(eff, 2),
        "n": len(vals),
    }


def _pair(before: dict, after: dict, key: str, *, ndigits: int = 2) -> dict[str, Any]:
    b, a = before.get(key), after.get(key)
    d = round(a - b, ndigits) if isinstance(a, (int, float)) and isinstance(b, (int, float)) else None
    return {"before": b, "after": a, "delta": d}


def delta(
    before_weights: dict[str, float],
    after_weights: dict[str, float],
    *,
    series: dict[str, list[dict[str, Any]]] | None = None,
    scenarios: list[dict[str, Any]] | None = None,
    factor_series: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Before/after/delta for the concentration metrics (always), plus the
    correlation-aware metrics when ``series`` is supplied, plus pre-flight
    warnings. Pure: the ``series`` path delegates to the pure ``risk.analyze``."""
    bc = concentration(before_weights)
    ac = concentration(after_weights)
    out: dict[str, Any] = {
        "top1_pct": _pair(bc, ac, "top1_pct"),
        "top5_pct": _pair(bc, ac, "top5_pct"),
        "effective_names": _pair(bc, ac, "effective_names"),
        "before": bc,
        "after": ac,
        "has_correlation": False,
    }
    if series:
        import risk  # lazy: keep the pure concentration path free of risk's imports
        rb = risk.analyze(before_weights, series, scenarios=scenarios, factor_series=factor_series)
        ra = risk.analyze(after_weights, series, scenarios=scenarios, factor_series=factor_series)
        out["effective_bets"] = _pair(rb["metrics"], ra["metrics"], "effective_bets")
        out["portfolio_vol_pct"] = _pair(rb["metrics"], ra["metrics"], "portfolio_vol_pct")
        out["has_correlation"] = True
    out["warnings"] = preflight_warnings(out)
    return out


def preflight_warnings(d: dict[str, Any]) -> list[str]:
    """Turn a delta into the short list of things worth stopping on before placing.
    Deliberately terse and few: a wall of advisories is ignored."""
    warnings: list[str] = []
    top5 = d.get("top5_pct") or {}
    b, a, dl = top5.get("before"), top5.get("after"), top5.get("delta")
    if isinstance(a, (int, float)) and a >= CONCENTRATION_CEILING_PCT and isinstance(dl, (int, float)) and dl > 0:
        warnings.append(
            f"This basket pushes top-5 concentration to {a:.0f}% of the book "
            f"(already past the {CONCENTRATION_CEILING_PCT:.0f}% mark).")
    elif isinstance(dl, (int, float)) and dl >= CONCENTRATION_JUMP_PCT and isinstance(b, (int, float)) and isinstance(a, (int, float)):
        warnings.append(
            f"This basket raises top-5 concentration by {dl:.1f}pp ({b:.0f}% -> {a:.0f}%).")
    eff = d.get("effective_bets") or {}
    eb, ea = eff.get("before"), eff.get("after")
    if isinstance(eb, (int, float)) and isinstance(ea, (int, float)) and ea < eb - EFFECTIVE_BETS_DROP:
        warnings.append(
            f"Correlation-aware diversification falls: effective bets {eb:.1f} -> {ea:.1f}.")
    return warnings
