#!/usr/bin/env python3
"""Decision journal + outcome calibration.

A standing plan is only as trustworthy as the judgement behind it, and judgement
in a bull market feels great whether or not it is any good. This module records
each decision -- what, when, at what price, and why -- as an append-only log, then
calibrates it against later prices so you can see your actual hit rate instead of
your remembered one.

Storage lives in the private ``data/`` submodule (``data/journal.json``); it is
judgement/context, not secrets or raw broker data, so it can be committed. Pure
helpers (``add_entry``, ``record_outcome``, ``calibrate``) take an explicit path
so tests never touch the real file.
"""

from __future__ import annotations

import datetime as dt
import sys
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import portfolio  # noqa: E402
import store  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
JOURNAL_JSON = REPO_ROOT / "data" / "journal.json"

# Directional actions can be scored against later price; the rest are context.
ACTIONS = {"buy", "add", "accumulate", "trim", "sell", "reduce", "hold", "watch", "note"}
EXPECT_UP = {"buy", "add", "accumulate"}
EXPECT_DOWN = {"trim", "sell", "reduce"}


def _now(now: dt.datetime | None = None) -> dt.datetime:
    return now or dt.datetime.now(dt.timezone.utc)


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def load_entries(path: Path = JOURNAL_JSON) -> list[dict[str, Any]]:
    data = store.load(path)
    if isinstance(data, dict) and isinstance(data.get("entries"), list):
        return data["entries"]
    return []


def _save(entries: list[dict[str, Any]], path: Path = JOURNAL_JSON) -> None:
    store.write_json(path, {"entries": entries})


def add_entry(data: dict[str, Any], *, path: Path = JOURNAL_JSON, now: dt.datetime | None = None) -> dict[str, Any]:
    """Validate and append a decision. Raises ValueError on bad input."""
    symbol = portfolio.clean_symbol(data.get("symbol"))
    action = str(data.get("action") or "").lower().strip()
    if not symbol:
        raise ValueError("a journal entry needs a symbol")
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action!r} (expected one of {sorted(ACTIONS)})")
    thesis = str(data.get("thesis") or "").strip()
    if not thesis:
        raise ValueError("a journal entry needs a thesis (why you did this)")

    entry = {
        "id": uuid.uuid4().hex[:12],
        "created_at": _now(now).isoformat(timespec="seconds"),
        "symbol": symbol,
        "action": action,
        "size_czk": _num(data.get("size_czk")),
        "size_pct": _num(data.get("size_pct")),
        "price": _num(data.get("price")),
        "thesis": thesis,
        "expected": str(data.get("expected") or "").strip() or None,
        "source_artifact": str(data.get("source_artifact") or "").strip() or None,
        "review_after": str(data.get("review_after") or "").strip() or None,
        "outcome": None,
    }
    entries = load_entries(path)
    entries.append(entry)
    _save(entries, path)
    return entry


def record_outcome(entry_id: str, price: Any, note: str = "", *, path: Path = JOURNAL_JSON,
                   now: dt.datetime | None = None) -> dict[str, Any]:
    """Stamp a later observed price on an entry so calibration can score it."""
    px = _num(price)
    if px is None:
        raise ValueError("outcome needs a numeric price")
    entries = load_entries(path)
    for entry in entries:
        if entry.get("id") == entry_id:
            entry["outcome"] = {
                "recorded_at": _now(now).isoformat(timespec="seconds"),
                "price": px,
                "note": str(note or "").strip() or None,
            }
            _save(entries, path)
            return entry
    raise ValueError(f"no journal entry with id {entry_id!r}")


def _later_price(entry: dict[str, Any], price_map: dict[str, float]) -> float | None:
    outcome = entry.get("outcome")
    if isinstance(outcome, dict) and isinstance(outcome.get("price"), (int, float)):
        return float(outcome["price"])
    return price_map.get(portfolio.clean_symbol(entry.get("symbol")))


def calibrate(entries: list[dict[str, Any]], price_map: dict[str, float] | None = None) -> dict[str, Any]:
    """Score directional decisions against a later price.

    A buy/add is "right" if the price rose since; a trim/sell is "right" if it
    fell (you got out before the drop). Hold/watch/note are context and excluded
    from the hit rate. ``price_map`` (symbol -> current price) lets the live
    snapshot score entries that have no manually recorded outcome yet.
    """
    price_map = price_map or {}
    scored: list[dict[str, Any]] = []
    correct = 0
    n_dir = 0
    buy_moves: list[float] = []
    trim_moves: list[float] = []

    for e in entries:
        action = str(e.get("action") or "").lower()
        decision_px = _num(e.get("price"))
        later = _later_price(e, price_map)
        move = None
        is_correct = None
        if decision_px and decision_px > 0 and later and later > 0:
            move = (later / decision_px - 1.0) * 100.0
            if action in EXPECT_UP:
                is_correct = move > 0
                buy_moves.append(move)
            elif action in EXPECT_DOWN:
                is_correct = move < 0
                trim_moves.append(move)
        if is_correct is not None:
            n_dir += 1
            correct += 1 if is_correct else 0
        scored.append({
            "id": e.get("id"),
            "symbol": e.get("symbol"),
            "action": action,
            "move_pct": round(move, 2) if move is not None else None,
            "correct": is_correct,
            "has_outcome": isinstance(e.get("outcome"), dict),
        })

    return {
        "n_entries": len(entries),
        "n_scored": n_dir,
        "n_correct": correct,
        "hit_rate_pct": round(100.0 * correct / n_dir, 1) if n_dir else None,
        "avg_move_buys_pct": round(sum(buy_moves) / len(buy_moves), 2) if buy_moves else None,
        "avg_move_trims_pct": round(sum(trim_moves) / len(trim_moves), 2) if trim_moves else None,
        "scored": scored,
    }


def price_map_from_holdings(holdings: dict[str, Any] | None) -> dict[str, float]:
    """Current per-share marks from the snapshot, for live calibration."""
    out: dict[str, float] = {}
    if not isinstance(holdings, dict):
        return out
    for p in holdings.get("positions", []) or []:
        sym = portfolio.clean_symbol(p.get("symbol"))
        if not sym:
            continue
        mark = p.get("mark_price")
        if isinstance(mark, (int, float)) and mark > 0:
            out[sym] = float(mark)
            continue
        mv, qty = p.get("base_market_value"), p.get("quantity")
        if isinstance(mv, (int, float)) and isinstance(qty, (int, float)) and qty:
            out[sym] = abs(mv / qty)
    return out


def _main() -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Decision journal + calibration.")
    parser.add_argument("--show", action="store_true", help="print entries + calibration.")
    args = parser.parse_args()
    portfolio.require_data()
    entries = load_entries()
    if args.show or True:
        holdings = store.load(portfolio.HOLDINGS_JSON)
        print(json.dumps({
            "entries": entries,
            "calibration": calibrate(entries, price_map_from_holdings(holdings)),
        }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
