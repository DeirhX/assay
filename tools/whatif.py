#!/usr/bin/env python3
"""What-if staged-trade simulator.

Confidence comes from seeing the *after* picture before committing a koruna. The
rebalance view shows current drift and a suggested trade per name; this takes a
whole staged basket and recomputes the resulting portfolio: post-trade weights
and band status (reusing the exact ``rebalance`` logic so the numbers can never
disagree), post-trade cash, and the realized Czech tax of the sells (via
``tax_lots``).

It is a pure recompute over an in-memory copy of the snapshot. It never writes
holdings, never places a trade, and never touches the network.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import portfolio  # noqa: E402
import rebalance  # noqa: E402
import risk_delta  # noqa: E402
import tax_lots  # noqa: E402

EPS = 0.01


def _coerce_trades(trades: Any) -> dict[str, float]:
    """Validate the staged basket and net duplicate symbols. Thin wrapper over the
    canonical ``portfolio.normalize_basket`` so the simulator and the live trade
    desk agree, byte-for-byte, on symbol normalization and netting."""
    return portfolio.normalize_basket(trades)


# Canonical cash extraction lives in portfolio.cash_base; keep the old private
# name as an alias so existing callers/tests stay valid.
_cash_base = portfolio.cash_base


def _after_positions(holdings: dict[str, Any], deltas: dict[str, float]) -> list[dict[str, Any]]:
    """Copy the snapshot's positions, apply the per-symbol CZK deltas, and add
    any freshly-bought symbol that wasn't held before.

    Stock rows are aggregated by symbol before applying a delta.  This avoids
    applying one net basket leg repeatedly when a broker snapshot has multiple
    rows for the same stock.
    """
    positions = holdings.get("positions", []) or []
    held = portfolio.stock_base_values(holdings)
    projected = {
        sym: max(0.0, held.get(sym, 0.0) + delta)
        for sym, delta in deltas.items()
    }
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for p in positions:
        if not isinstance(p, dict):
            continue
        sym = portfolio.clean_symbol(p.get("symbol"))
        is_option = str(p.get("asset_class") or "STK").upper() == "OPT"
        if is_option or sym not in projected:
            out.append(dict(p))
            continue
        if sym in seen:
            continue
        np = dict(p)
        np["base_market_value"] = projected[sym]
        out.append(np)
        seen.add(sym)
    for sym, delta in deltas.items():
        if sym not in seen and delta > EPS:
            out.append({
                "symbol": sym,
                "base_market_value": projected[sym],
                "asset_class": "STK",
            })
    return out


def simulate(holdings: dict[str, Any], model: dict[str, Any], trades: Any, *, as_of=None) -> dict[str, Any]:
    """Recompute the portfolio after applying a staged basket of trades."""
    deltas = _coerce_trades(trades)
    violations = portfolio.stock_sell_violations(holdings, deltas)
    applied_deltas = dict(deltas)
    for violation in violations:
        applied_deltas[violation["symbol"]] = -float(violation["held_czk"])

    after_holdings = {
        "net_asset_value": holdings.get("net_asset_value"),
        "base_currency": holdings.get("base_currency"),
        "generated_at": holdings.get("generated_at"),
        "positions": _after_positions(holdings, applied_deltas),
    }
    before_plan = rebalance.plan(model, holdings)
    after_plan = rebalance.plan(model, after_holdings)

    # Pre-trade risk delta: what the basket does to concentration/diversification.
    # Concentration is pure weight math (instant), computed on the same before/after
    # books the rest of this recompute uses, so it can never disagree with them.
    risk = risk_delta.delta(
        portfolio.holdings_weights(holdings),
        portfolio.holdings_weights(after_holdings),
    )

    # Realized tax: only sells (negative deltas) realize gains, lot-selected from
    # the *pre-trade* snapshot.
    per_symbol: list[dict[str, Any]] = []
    tax_total = {"proceeds": 0.0, "taxable_gain": 0.0, "exempt_proceeds": 0.0,
                 "taxable_proceeds": 0.0, "harvestable_loss": 0.0, "realized_gain": 0.0}
    for sym, delta in sorted(applied_deltas.items()):
        if delta >= -EPS:
            continue
        bd = tax_lots.breakdown_for_symbol(holdings, sym, -delta, as_of=as_of)
        per_symbol.append(bd)
        for k in tax_total:
            tax_total[k] += bd["totals"].get(k, 0.0)
    tax_total = {k: round(v, 2) for k, v in tax_total.items()}

    spend = round(sum(d for d in applied_deltas.values() if d > 0), 2)
    raised = round(-sum(d for d in applied_deltas.values() if d < 0), 2)
    net_delta = round(sum(applied_deltas.values()), 2)

    cash_before = _cash_base(holdings)
    cash_after = None if cash_before is None else round(cash_before - net_delta, 2)

    # Cash vs the model's target band (% of NAV). Trades swap cash for
    # positions, so pre-trade NAV is the right denominator for the after
    # picture too. None when the snapshot has no cash/NAV data.
    cash_band = rebalance.cash_block(model, holdings)
    cash_target = None
    if cash_band and cash_after is not None:
        after_pct = round(cash_after / cash_band["nav"] * 100.0, 2)
        cash_target = {
            "target_pct": cash_band["target_pct"],
            "low": cash_band["low"],
            "high": cash_band["high"],
            "before_pct": cash_band["pct_of_nav"],
            "after_pct": after_pct,
            "status_after": rebalance._status(after_pct, cash_band["low"], cash_band["high"]),
        }

    in_before, n_before = rebalance.band_counts(before_plan)
    in_after, n_after = rebalance.band_counts(after_plan)

    caveats = [
        "Value-neutral recompute: it ignores commissions, FX moves, and bid/ask "
        "slippage, and assumes you trade at the snapshot's marks.",
        "Realized tax is selected from pre-trade lots using the Czech 3-year rule; "
        "analysis, not tax advice.",
    ]
    if violations:
        caveats.insert(
            0,
            "Projection blocked: one or more staged sells exceed the held stock. "
            "Affected positions are floored at zero below; resize or remove those orders.",
        )
    if cash_after is not None and cash_after < -EPS:
        caveats.insert(0, "Cash goes negative after these trades — you would need "
                          "margin or more sells to fund the buys.")
    elif (cash_target and cash_target["target_pct"] > EPS
          and cash_target["after_pct"] < cash_target["low"] - EPS):
        caveats.insert(0, f"This basket leaves cash at {cash_target['after_pct']:.1f}% of NAV, "
                          f"under your {cash_target['low']:g}% floor "
                          f"(target {cash_target['target_pct']:g}%).")

    return {
        "as_of": model.get("as_of"),
        "snapshot": holdings.get("generated_at"),
        "currency": holdings.get("base_currency") or "CZK",
        "valid": not violations,
        "stock_sell_violations": violations,
        "trades": [{"symbol": s, "delta_czk": round(d, 2)} for s, d in sorted(deltas.items())],
        "applied_trades": [
            {"symbol": s, "delta_czk": round(d, 2)}
            for s, d in sorted(applied_deltas.items())
        ],
        "after": after_plan,
        "before_status": {r["name"]: r["status"]
                          for r in before_plan.get("rows", []) if r.get("kind") == "target"},
        "cash": {"before": cash_before, "after": cash_after,
                 "currency": holdings.get("base_currency") or "CZK",
                 "target": cash_target},
        "tax": {"totals": tax_total, "per_symbol": per_symbol},
        "risk": risk,
        "summary": {
            "spend_czk": spend,
            "raised_czk": raised,
            "net_cash_czk": round(-net_delta, 2),
            "bands_in_before": in_before,
            "bands_in_after": in_after,
            "bands_total": n_after,
            "realized_taxable_gain_czk": tax_total["taxable_gain"],
        },
        "caveats": caveats,
    }


def _main() -> int:
    import argparse
    import json

    import store

    parser = argparse.ArgumentParser(description="What-if staged-trade simulator.")
    parser.add_argument("--trade", action="append", default=[],
                        help="SYMBOL:DELTA_CZK, repeatable (negative = sell).")
    args = parser.parse_args()

    portfolio.require_data()
    root = Path(__file__).resolve().parent.parent / "data"
    holdings = store.load(root / "current-holdings.json")
    model = store.load(root / "target-model.json")
    if not holdings or not model:
        raise SystemExit("need both current-holdings.json and target-model.json")
    trades = []
    for spec in args.trade:
        sym, _, amt = spec.partition(":")
        trades.append({"symbol": sym, "delta_czk": float(amt)})
    print(json.dumps(simulate(holdings, model, trades), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
