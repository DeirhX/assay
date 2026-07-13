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

import portfolio
from config import DATA_DIR, HOLDINGS_JSON, TARGET_MODEL_JSON
from store import load as _load, safe_symbol as _safe_symbol, write_json as _write_json
from timeutil import now_iso

BASKET_JSON = DATA_DIR / "basket.json"

# Where a pick was starred from. Free-form would rot; an unknown source is
# coerced to "manual" so the provenance label stays meaningful.
_SOURCES = {"manual", "deepdive", "rebalance", "strategy", "analyses", "suggestion", "segment"}

# Interest tier. The basket is now the optimizer's candidate pool: "want" is a
# real intent to size into the plan, "curious" is parked (sized only when the
# optimizer is told to include curious picks). Legacy items with no tier read as
# "want" — a star always meant active interest before this split.
_TIERS = {"want", "curious"}


def _norm_source(source: str | None) -> str:
    s = str(source or "").strip().lower()
    return s if s in _SOURCES else "manual"


def _norm_tier(tier: str | None) -> str:
    s = str(tier or "").strip().lower()
    return s if s in _TIERS else "want"


def load_basket() -> dict:
    """The raw basket ``{"items": [...]}``; tolerant of a missing/garbled file."""
    data = _load(BASKET_JSON)
    items = data.get("items") if isinstance(data, dict) else None
    return {"items": items if isinstance(items, list) else []}


def _save(items: list[dict]) -> None:
    _write_json(BASKET_JSON, {"items": items, "updated_at": now_iso()})


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
            "tier": _norm_tier(it.get("tier")),
            # Provenance for picks starred out of a segment analysis, so the pool
            # can show where a candidate came from and the optimizer can credit
            # the run's conviction without re-reading the report.
            "segment": it.get("segment"),
            "run": it.get("run"),
            "conviction": it.get("conviction"),
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


def pool_candidates(*, include_curious: bool = True) -> list[dict]:
    """Basket picks as portfolio-optimizer pool candidates: symbol + sleeve +
    interest tier + any carried conviction/provenance. ``curious`` picks are
    dropped when ``include_curious`` is False, so the optimizer can size only the
    high-interest set. This is the basket's contribution to the candidate pool;
    held names are unioned in by the optimizer itself."""
    _, member_of = _target_index()
    out: list[dict] = []
    for it in load_basket()["items"]:
        sym = it.get("symbol")
        if not sym:
            continue
        tier = _norm_tier(it.get("tier"))
        if tier == "curious" and not include_curious:
            continue
        out.append({
            "symbol": sym,
            "sleeve": member_of.get(sym) or "other",
            "tier": tier,
            "source": it.get("source") or "manual",
            "conviction": it.get("conviction"),
            "segment": it.get("segment"),
            "run": it.get("run"),
            "note": it.get("note") or "",
        })
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
def add_symbol(symbol: str, *, source: str = "manual", note: str = "",
               tier: str = "want", segment: str | None = None,
               run: str | None = None, conviction: str | None = None) -> dict:
    """Star a ticker into the pool. Validates/normalizes the symbol (so a typo or
    a dollar amount never lands in the basket), dedupes case-insensitively, and is
    idempotent: re-adding an existing pick refreshes a non-empty note, updates the
    tier, and backfills any provenance that arrives later. Raises ValueError on an
    unusable symbol."""
    sym = _safe_symbol(symbol)  # raises ValueError on junk
    basket = load_basket()
    items = basket["items"]
    existing = _find(items, sym)
    if existing is not None:
        if note:
            existing["note"] = str(note).strip()
        existing["tier"] = _norm_tier(tier)
        if segment and not existing.get("segment"):
            existing["segment"] = str(segment)
        if run and not existing.get("run"):
            existing["run"] = str(run)
        if conviction and not existing.get("conviction"):
            existing["conviction"] = str(conviction)
        _save(items)
        return view()
    item = {
        "symbol": sym,
        "source": _norm_source(source),
        "note": str(note or "").strip(),
        "tier": _norm_tier(tier),
        "added_at": now_iso(),
    }
    if segment:
        item["segment"] = str(segment)
    if run:
        item["run"] = str(run)
    if conviction:
        item["conviction"] = str(conviction)
    items.append(item)
    _save(items)
    return view()


def set_tier(symbol: str, tier: str) -> dict:
    """Move a pick between ``want`` and ``curious``. Raises ValueError if the
    symbol isn't in the basket (the UI only offers this on existing picks)."""
    sym = _safe_symbol(symbol)
    items = load_basket()["items"]
    it = _find(items, sym)
    if it is None:
        raise ValueError(f"{sym} is not in the basket")
    it["tier"] = _norm_tier(tier)
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
