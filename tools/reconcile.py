#!/usr/bin/env python3
"""Ledger-vs-snapshot drift: is the book the planner sizes off behind reality?

The planner sizes real trades off ``data/current-holdings.json``. If a fill
happened that the snapshot doesn't reflect -- a GTC order that filled overnight,
or (the case nothing else catches) a trade placed in TWS/mobile outside this app
-- every downstream number is quietly wrong, and the planner will happily size the
next basket off a stale book.

The honest, cheap signal for that is **executions dated after the snapshot's
``generated_at``**: the IBKR Flex trade ledger is proof that trading happened, and
any trade newer than the snapshot means the snapshot predates known activity.

Deliberately NOT done here: reconstructing absolute positions by netting the
ledger. That only works with every trade since a position was *opened*; the Flex
history is a rolling window, so any holding older than that window would net to
the wrong quantity and cry wolf. This module checks the one thing a partial ledger
can prove correctly, and nets per-symbol activity only for the trades it *can*
see (those after the snapshot), purely to describe what moved.

Pure over its two dict inputs; unit-tested with no disk or network.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import portfolio  # noqa: E402  -- clean_symbol only (pure)
import timeutil  # noqa: E402  -- shared Z-tolerant ISO parse

LIST_LIMIT = 8


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _not_checked(snap_at: Any, reason: str) -> dict:
    return {
        "checked": False,
        "reason": reason,
        "stale_vs_ledger": False,
        "snapshot_at": snap_at,
        "n_trades_after": 0,
        "last_trade_at": None,
        "trades_after": [],
        "by_symbol": [],
    }


def drift_report(holdings: dict | None, history: dict | None, *, list_limit: int = LIST_LIMIT) -> dict:
    """Flag when the execution ledger contains trades newer than the holdings
    snapshot. Returns a summary dict; ``checked`` is False (never an error) when
    there's no snapshot or no ledger to compare.

    ``stale_vs_ledger`` is the headline: True means the snapshot predates at least
    one execution, so the book (and the plan) is behind. ``by_symbol`` and
    ``trades_after`` describe what changed since, for a one-glance "what moved"."""
    snap_at = holdings.get("generated_at") if isinstance(holdings, dict) else None
    trades = history.get("trades") if isinstance(history, dict) else None
    if not snap_at:
        return _not_checked(snap_at, "no holdings snapshot")
    if not isinstance(trades, list):
        return _not_checked(snap_at, "no execution ledger (pull IBKR history first)")

    snap_dt = timeutil.parse_iso_utc(snap_at)
    if snap_dt is None:
        return _not_checked(snap_at, "snapshot timestamp is unparseable")

    after: list[dict] = []
    for t in trades:
        td = timeutil.parse_iso_utc(t.get("datetime")) if isinstance(t, dict) else None
        if td is not None and td > snap_dt:
            after.append(t)

    if not after:
        return {
            "checked": True,
            "reason": None,
            "stale_vs_ledger": False,
            "snapshot_at": snap_at,
            "n_trades_after": 0,
            "last_trade_at": None,
            "trades_after": [],
            "by_symbol": [],
        }

    # Net the post-snapshot trades per symbol, purely to describe what moved. This
    # is NOT an absolute position -- it's the change the snapshot is missing.
    by_symbol: dict[str, dict] = {}
    for t in after:
        sym = portfolio.clean_symbol(t.get("symbol"))
        qty = _num(t.get("quantity"))  # signed: sells negative (IBKR convention)
        rec = by_symbol.setdefault(sym, {"symbol": sym, "net_qty": 0.0, "buys": 0, "sells": 0})
        rec["net_qty"] += qty
        if str(t.get("side") or "").upper() == "SELL" or qty < 0:
            rec["sells"] += 1
        else:
            rec["buys"] += 1

    ordered = sorted(after, key=lambda t: str(t.get("datetime") or ""))
    by_symbol_rows = sorted(by_symbol.values(), key=lambda r: -abs(r["net_qty"]))
    return {
        "checked": True,
        "reason": None,
        "stale_vs_ledger": True,
        "snapshot_at": snap_at,
        "n_trades_after": len(after),
        "last_trade_at": ordered[-1].get("datetime"),
        "trades_after": [
            {"date": t.get("date"), "datetime": t.get("datetime"),
             "symbol": portfolio.clean_symbol(t.get("symbol")),
             "side": t.get("side"), "quantity": _num(t.get("quantity"))}
            for t in ordered[-list_limit:]
        ],
        "by_symbol": [{**r, "net_qty": round(r["net_qty"], 4)} for r in by_symbol_rows[:list_limit]],
    }
