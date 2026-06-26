#!/usr/bin/env python3
"""The cross-surface basket: a lightweight, persistent shortlist of tickers the
user has starred from anywhere in the app (a deep-dive, a rebalance row, a
strategy proposal).

This sits UPSTREAM of the working draft. The working draft holds sized
*decisions* (bands + provenance); the basket holds bare *interest* — symbols you
want to keep an eye on, with no sizing and no budget yet. A later phase turns a
basket into a drafted plan (segment -> construct -> stage); this module is just
the store + a read-side enrichment so the basket view can show, per pick, where
it came from, whether you already hold it, and whether the plan already governs
it.

Stdlib + project leaves only (store/config/portfolio), so it's safe to import
from serve without a cycle. A missing/corrupt file reads as an empty basket.
"""

from __future__ import annotations

import datetime as dt

import portfolio
from config import DATA_DIR, HOLDINGS_JSON, TARGET_MODEL_JSON
from store import load as _load, safe_symbol as _safe_symbol, write_json as _write_json

BASKET_JSON = DATA_DIR / "basket.json"

# Where a pick was starred from. Free-form would rot; an unknown source is
# coerced to "manual" so the provenance label stays meaningful.
_SOURCES = {"manual", "deepdive", "rebalance", "strategy", "analyses", "suggestion"}


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _norm_source(source: str | None) -> str:
    s = str(source or "").strip().lower()
    return s if s in _SOURCES else "manual"


def load_basket() -> dict:
    """The raw basket ``{"items": [...]}``; tolerant of a missing/garbled file."""
    data = _load(BASKET_JSON)
    items = data.get("items") if isinstance(data, dict) else None
    return {"items": items if isinstance(items, list) else []}


def _save(items: list[dict]) -> None:
    _write_json(BASKET_JSON, {"items": items, "updated_at": _now_iso()})


def _find(items: list[dict], symbol: str) -> dict | None:
    return next((it for it in items if it.get("symbol") == symbol), None)


# --------------------------------------------------------------------------- #
# Read-side enrichment
# --------------------------------------------------------------------------- #
def _target_index() -> tuple[dict, dict]:
    """(symbol -> band, symbol -> sleeve name) from the live target model, so the
    basket can show whether the standing plan already governs a pick."""
    model = _load(TARGET_MODEL_JSON) or {}
    targets = model.get("targets") or {}
    member_of: dict[str, str] = {}
    for name, sleeve in (model.get("sleeves") or {}).items():
        for m in (sleeve.get("members") or []):
            member_of.setdefault(m, name)
    return targets, member_of


def enriched_items() -> list[dict]:
    """Each basket item joined with what we already know: current weight (if
    held), the standing target band (if any), and sleeve membership. Pure reads;
    never mutates the basket."""
    items = load_basket()["items"]
    holdings = _load(HOLDINGS_JSON)
    weights = portfolio.holdings_weights(holdings) if isinstance(holdings, dict) else {}
    targets, member_of = _target_index()
    out: list[dict] = []
    for it in items:
        sym = it.get("symbol")
        if not sym:
            continue
        band = targets.get(sym)
        sleeve = member_of.get(sym)
        out.append({
            "symbol": sym,
            "source": it.get("source") or "manual",
            "note": it.get("note") or "",
            "added_at": it.get("added_at"),
            "held_pct": round(weights[sym], 2) if sym in weights else None,
            "targeted": bool(band) or bool(sleeve),
            "target_band": {k: band.get(k) for k in ("low", "high", "rule") if k in band} if isinstance(band, dict) else None,
            "in_sleeve": sleeve,
        })
    return out


def basket_members() -> list[dict]:
    """The basket as research-segment members (``[{symbol, sleeve}]``), mapping
    each pick to its standing-model sleeve where known (else ``"other"``). This is
    the bridge that lets a hand-picked basket feed the very same construct
    pipeline a guided strategy run uses, instead of a parallel reimplementation."""
    _, member_of = _target_index()
    out: list[dict] = []
    for it in load_basket()["items"]:
        sym = it.get("symbol")
        if not sym:
            continue
        out.append({"symbol": sym, "sleeve": member_of.get(sym) or "other"})
    return out


def view() -> dict:
    """The canonical response for every basket endpoint: the enriched list plus a
    count and the bare symbol set (so the client can toggle ★ across surfaces)."""
    items = enriched_items()
    return {"items": items, "count": len(items),
            "symbols": [it["symbol"] for it in items]}


# --------------------------------------------------------------------------- #
# Mutations
# --------------------------------------------------------------------------- #
def add_symbol(symbol: str, *, source: str = "manual", note: str = "") -> dict:
    """Star a ticker. Validates/normalizes the symbol (so a typo or a dollar
    amount never lands in the basket), dedupes case-insensitively, and is
    idempotent: re-adding an existing pick just refreshes a non-empty note. Raises
    ValueError on an unusable symbol."""
    sym = _safe_symbol(symbol)  # raises ValueError on junk
    basket = load_basket()
    items = basket["items"]
    existing = _find(items, sym)
    if existing is not None:
        if note:
            existing["note"] = str(note).strip()
        _save(items)
        return view()
    items.append({
        "symbol": sym,
        "source": _norm_source(source),
        "note": str(note or "").strip(),
        "added_at": _now_iso(),
    })
    _save(items)
    return view()


def remove_symbol(symbol: str) -> dict:
    """Un-star a ticker. Idempotent — removing one that isn't there is a no-op."""
    sym = _safe_symbol(symbol)
    items = [it for it in load_basket()["items"] if it.get("symbol") != sym]
    _save(items)
    return view()


def clear() -> dict:
    """Empty the basket entirely."""
    _save([])
    return view()
