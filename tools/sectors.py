"""Symbol -> sector resolution with a disk cache.

IBKR's trade history carries no sector tags, and the traded universe is far
wider than the curated research watchlist, so we join each traded *underlying*
to a GICS-style sector from two sources, cheapest first:

1. **Research dossiers** (``data/research/*.json`` -> ``profile.sector``) --
   free, exact, already on disk.
2. **Yahoo ``assetProfile``** -- fetched on demand and cached, because most
   traded names were never written up as a dossier.

Foreign / derivative IBKR local codes (e.g. ``AMV0``, ``BOSSD``, ``1211``)
frequently won't resolve on Yahoo; those are remembered as ``unresolved`` with a
timestamp so we don't re-hit the network on every page load. Everything that
fails to resolve lands in an "Unknown" bucket in the UI -- honestly labelled,
not hidden.

The cache lives in the gitignored data cache (it mirrors a personal trade
ledger). Pure functions here take an injectable ``fetch`` so the network path is
never exercised in tests.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import store  # noqa: E402  -- shared forgiving/atomic JSON IO

# How long to trust a prior "couldn't resolve this" before trying the network
# again -- tickers get listed/relisted, and Yahoo's handshake is moody.
UNRESOLVED_TTL_DAYS = 14


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize(symbol: str) -> str:
    """The key we look a sector up under: uppercased, with any option-contract
    residue ("AMD   240816P00135000") reduced to its leading token ("AMD")."""
    s = (symbol or "").strip().upper()
    if not s:
        return ""
    return s.split()[0]


def load_cache(path: Path) -> dict:
    """Read the cache file into ``{"updated_at": ..., "map": {SYM: entry}}``.
    A missing or corrupt file yields an empty, well-shaped cache."""
    raw = store.load(Path(path))
    if not isinstance(raw, dict):
        return {"updated_at": None, "map": {}}
    cmap = raw.get("map")
    if not isinstance(cmap, dict):
        cmap = {}
    return {"updated_at": raw.get("updated_at"), "map": cmap}


def save_cache(path: Path, cache: dict) -> None:
    cache["updated_at"] = _now_iso()
    store.write_json(Path(path), cache)


def seed_from_research(cache: dict, research_dir: Path) -> int:
    """Fill the cache from research dossiers' ``profile.sector``. Research is the
    authoritative, free source, so it always wins over a Yahoo/unresolved entry.
    Returns how many entries were (re)seeded. Idempotent."""
    cmap = cache.setdefault("map", {})
    seeded = 0
    for path in sorted(Path(research_dir).glob("*.json")):
        rec = store.load(path)
        if not isinstance(rec, dict):
            continue
        sector = ((rec.get("profile") or {}).get("sector") or "").strip()
        if not sector:
            continue
        key = path.stem.upper()
        cur = cmap.get(key)
        if cur and cur.get("source") == "research" and cur.get("sector") == sector:
            continue  # already seeded identically -> no churn
        cmap[key] = {"sector": sector, "source": "research", "ts": _now_iso()}
        seeded += 1
    return seeded


def sector_of(symbol: str, cache: dict) -> str:
    """The resolved sector for a symbol, or "" if unknown/unresolved. Tries the
    exact normalized key, then the base before any exchange suffix (FOO.DE ->
    FOO) so a dossier keyed without the suffix still matches."""
    cmap = cache.get("map") or {}
    key = normalize(symbol)
    for cand in (key, key.split(".")[0]):
        entry = cmap.get(cand)
        if entry and entry.get("sector"):
            return entry["sector"]
    return ""


def _is_stale_unresolved(entry: dict, now: datetime) -> bool:
    if entry.get("source") != "unresolved":
        return False
    ts = entry.get("ts")
    try:
        when = datetime.fromisoformat(ts) if ts else None
    except ValueError:
        when = None
    if when is None:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (now - when).days >= UNRESOLVED_TTL_DAYS


def needs_lookup(symbols: Iterable[str], cache: dict, *, now: datetime | None = None) -> list[str]:
    """Distinct normalized symbols not yet resolved: never seen, or marked
    unresolved longer ago than the TTL. Preserves first-seen order."""
    now = now or datetime.now(timezone.utc)
    cmap = cache.get("map") or {}
    out: list[str] = []
    seen: set[str] = set()
    for raw in symbols:
        key = normalize(raw)
        if not key or key in seen:
            continue
        seen.add(key)
        entry = cmap.get(key)
        if entry is None or _is_stale_unresolved(entry, now):
            out.append(key)
    return out


def backfill(
    symbols: Iterable[str],
    cache: dict,
    *,
    fetch: Callable[[str], str | None],
    now: datetime | None = None,
    limit: int | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict:
    """Resolve the still-unknown symbols via ``fetch`` and record the results.

    ``fetch(symbol)`` returns a sector string, or None/"" (or raises) when the
    symbol can't be resolved -- the latter is remembered as ``unresolved`` so we
    don't hammer the network on every load. Mutates ``cache`` in place; returns
    ``{"considered", "fetched", "resolved", "unresolved"}``."""
    now = now or datetime.now(timezone.utc)
    cmap = cache.setdefault("map", {})
    todo = needs_lookup(symbols, cache, now=now)
    considered = len(todo)
    if limit is not None:
        todo = todo[:limit]
    resolved = unresolved = 0
    for i, key in enumerate(todo, 1):
        if progress:
            progress(f"resolving sectors… {i}/{len(todo)} ({key})")
        try:
            sector = (fetch(key) or "").strip()
        except Exception:  # noqa: BLE001 -- a flaky feed shouldn't abort the batch
            sector = ""
        if sector:
            cmap[key] = {"sector": sector, "source": "yahoo", "ts": _now_iso()}
            resolved += 1
        else:
            cmap[key] = {"sector": "", "source": "unresolved", "ts": _now_iso()}
            unresolved += 1
    return {
        "considered": considered,
        "fetched": len(todo),
        "resolved": resolved,
        "unresolved": unresolved,
    }


def yahoo_fetch(symbol: str) -> str | None:
    """Default network resolver: Yahoo's assetProfile sector. Imported lazily so
    the module (and its tests) don't depend on the provider stack."""
    from providers import yahoo  # noqa: PLC0415 -- intentional lazy import

    profile = yahoo.asset_profile(symbol)
    return (profile or {}).get("sector") if profile else None
