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
import tax_lots  # noqa: E402

EPS = 0.01


def _coerce_trades(trades: Any) -> dict[str, float]:
    """Validate the staged basket and net duplicate symbols. Thin wrapper over the
    canonical ``portfolio.normalize_basket`` so the simulator and the live trade
    desk agree, byte-for-byte, on symbol normalization and netting."""
    return portfolio.normalize_basket(trades)


def _cash_base(holdings: dict[str, Any]) -> float | None:
    rows = holdings.get("cash") or []
    for c in rows:
        if c.get("currency") == "BASE_SUMMARY" and isinstance(c.get("ending_cash"), (int, float)):
            return float(c["ending_cash"])
    total = 0.0
    found = False
    for c in rows:
        if isinstance(c.get("ending_cash"), (int, float)):
            total += float(c["ending_cash"])
            found = True
    return total if found else None


def _after_positions(holdings: dict[str, Any], deltas: dict[str, float]) -> list[dict[str, Any]]:
    """Copy the snapshot's positions, apply the per-symbol CZK deltas, and add
    any freshly-bought symbol that wasn't held before."""
    positions = holdings.get("positions", []) or []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for p in positions:
        sym = portfolio.clean_symbol(p.get("symbol"))
        np = dict(p)
        bmv = p.get("base_market_value")
        if sym and sym in deltas and isinstance(bmv, (int, float)):
            new_bmv = bmv + deltas[sym]
            np["base_market_value"] = 0.0 if abs(new_bmv) < EPS else new_bmv
        out.append(np)
        if sym:
            seen.add(sym)
    for sym, delta in deltas.items():
        if sym not in seen and abs(delta) >= EPS:
            out.append({"symbol": sym, "base_market_value": delta, "asset_class": "STK"})
    return out


def _in_band_count(plan: dict[str, Any]) -> tuple[int, int]:
    rows = [r for r in plan.get("rows", []) if r.get("kind") == "target"]
    in_band = sum(1 for r in rows if r.get("status") == "IN")
    return in_band, len(rows)


def simulate(holdings: dict[str, Any], model: dict[str, Any], trades: Any, *, as_of=None) -> dict[str, Any]:
    """Recompute the portfolio after applying a staged basket of trades."""
    deltas = _coerce_trades(trades)

    after_holdings = {
        "net_asset_value": holdings.get("net_asset_value"),
        "base_currency": holdings.get("base_currency"),
        "generated_at": holdings.get("generated_at"),
        "positions": _after_positions(holdings, deltas),
    }
    before_plan = rebalance.plan(model, holdings)
    after_plan = rebalance.plan(model, after_holdings)

    # Realized tax: only sells (negative deltas) realize gains, lot-selected from
    # the *pre-trade* snapshot.
    per_symbol: list[dict[str, Any]] = []
    tax_total = {"proceeds": 0.0, "taxable_gain": 0.0, "exempt_proceeds": 0.0,
                 "taxable_proceeds": 0.0, "harvestable_loss": 0.0, "realized_gain": 0.0}
    for sym, delta in sorted(deltas.items()):
        if delta >= -EPS:
            continue
        bd = tax_lots.breakdown_for_symbol(holdings, sym, -delta, as_of=as_of)
        per_symbol.append(bd)
        for k in tax_total:
            tax_total[k] += bd["totals"].get(k, 0.0)
    tax_total = {k: round(v, 2) for k, v in tax_total.items()}

    spend = round(sum(d for d in deltas.values() if d > 0), 2)
    raised = round(-sum(d for d in deltas.values() if d < 0), 2)
    net_delta = round(sum(deltas.values()), 2)

    cash_before = _cash_base(holdings)
    cash_after = None if cash_before is None else round(cash_before - net_delta, 2)

    in_before, n_before = _in_band_count(before_plan)
    in_after, n_after = _in_band_count(after_plan)

    caveats = [
        "Value-neutral recompute: it ignores commissions, FX moves, and bid/ask "
        "slippage, and assumes you trade at the snapshot's marks.",
        "Realized tax is selected from pre-trade lots using the Czech 3-year rule; "
        "analysis, not tax advice.",
    ]
    if cash_after is not None and cash_after < -EPS:
        caveats.insert(0, "Cash goes negative after these trades — you would need "
                          "margin or more sells to fund the buys.")

    return {
        "as_of": model.get("as_of"),
        "snapshot": holdings.get("generated_at"),
        "currency": holdings.get("base_currency") or "CZK",
        "trades": [{"symbol": s, "delta_czk": round(d, 2)} for s, d in sorted(deltas.items())],
        "after": after_plan,
        "before_status": {r["name"]: r["status"]
                          for r in before_plan.get("rows", []) if r.get("kind") == "target"},
        "cash": {"before": cash_before, "after": cash_after,
                 "currency": holdings.get("base_currency") or "CZK"},
        "tax": {"totals": tax_total, "per_symbol": per_symbol},
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
