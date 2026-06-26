#!/usr/bin/env python3
"""IBKR portfolio sync: holdings snapshot, trade+NAV history, and sectors.

Extracted from serve.py. All read-only against IBKR Flex: it shells out to the
vendored ibkr_portfolio.py reader (the Flex query cannot trade) and caches the
results. Each long pull runs as a registered background job (jobs.py) so it
survives navigation and surfaces in the global task pill. The HTTP layer keeps
only thin handlers that call the start_* / *_payload helpers here. Public names
are underscore-free; serve.py imports them aliased to its private call sites.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import errorlog
import generate_site
import ibkr_history  # full trade + NAV history via windowed Flex
import jobs
import sectors  # symbol -> sector map (research seed + Yahoo backfill)
from apierror import Conflict as _Conflict
from config import DATA_DIR, HOLDINGS_JSON, REPO_ROOT, RESEARCH_DIR, TOOLS_SECRETS
from ibkr_portfolio import load_env_file as _read_env_file
from jobs import new_job as _new_job, public as _job_public, update_job as _update_job
from portfolio import holdings_payload
from store import load as _load, write_json as _write_json

# The read-only IBKR Flex reader is vendored alongside the server (stdlib only).
# Credentials are NEVER committed: the reader resolves IBKR_FLEX_TOKEN /
# IBKR_FLEX_QUERY_ID from the environment or a gitignored tools/secrets.env.
IBKR_READER = Path(__file__).resolve().parent / "ibkr_portfolio.py"
# Credentials file the reader reads (gitignored). The Settings UI writes here.
IBKR_SECRETS = TOOLS_SECRETS
# Raw pulls + snapshots are personal data -> keep them under data/cache (gitignored
# and inside the private submodule), never in the public working tree.
IBKR_CACHE_DIR = DATA_DIR / "cache" / "ibkr"


def ibkr_status() -> dict:
    """Whether IBKR Flex credentials are configured. The token is NEVER echoed;
    the query id is returned so the form can prefill it (useless without the
    token). Placeholders (<...>) and blanks count as unset, matching the reader."""
    file_vals = _read_env_file(IBKR_SECRETS)

    def resolve(key: str) -> str:
        val = (os.environ.get(key) or file_vals.get(key) or "").strip()
        return "" if (not val or val.startswith("<")) else val

    token = resolve("IBKR_FLEX_TOKEN")
    query_id = resolve("IBKR_FLEX_QUERY_ID")
    # The full trade + NAV history needs its own Activity Flex query (Trades +
    # "Net Asset Value (NAV) in Base"); the reader falls back to the snapshot
    # query id, but that one lacks those sections, so report it separately.
    history_query_id = resolve("IBKR_FLEX_HISTORY_QUERY_ID")
    return {
        "token_set": bool(token),
        "query_id": query_id,
        "history_query_id": history_query_id,
        "history_query_set": bool(history_query_id),
        "configured": bool(token and query_id),
        # History needs a dedicated query; the snapshot-query fallback won't carry
        # the Trades / NAV sections, so true readiness means an explicit one.
        "history_configured": bool(token and history_query_id),
        "from_env": bool((os.environ.get("IBKR_FLEX_TOKEN") or "").strip()),
        "history_from_env": bool((os.environ.get("IBKR_FLEX_HISTORY_QUERY_ID") or "").strip()),
        "secrets_path": str(IBKR_SECRETS.relative_to(REPO_ROOT)).replace("\\", "/"),
    }


def save_ibkr_secrets(body: dict) -> dict:
    """Upsert IBKR Flex credentials into the gitignored tools/secrets.env and the
    live process env. Blank fields are left untouched, so the query id can be
    updated without re-pasting the token. Returns the (token-free) status."""
    token = str(body.get("token") or "").strip()
    query_id = str(body.get("query_id") or "").strip()
    history_query_id = str(body.get("history_query_id") or "").strip()
    if not token and not query_id and not history_query_id:
        raise ValueError("nothing to save: provide a Flex token and/or query id")

    existing = _read_env_file(IBKR_SECRETS)
    if token:
        existing["IBKR_FLEX_TOKEN"] = token
        os.environ["IBKR_FLEX_TOKEN"] = token
    if query_id:
        existing["IBKR_FLEX_QUERY_ID"] = query_id
        os.environ["IBKR_FLEX_QUERY_ID"] = query_id
    if history_query_id:
        existing["IBKR_FLEX_HISTORY_QUERY_ID"] = history_query_id
        os.environ["IBKR_FLEX_HISTORY_QUERY_ID"] = history_query_id

    lines = [
        "# IBKR Flex Web Service credentials -- gitignored, never commit.",
        "# Written by the Settings tab; read by tools/ibkr_portfolio.py.",
    ]
    known = ("IBKR_FLEX_TOKEN", "IBKR_FLEX_QUERY_ID", "IBKR_FLEX_HISTORY_QUERY_ID")
    # Keep the known keys first, then preserve any other keys already present.
    for key in known:
        if existing.get(key):
            lines.append(f"{key}={existing[key]}")
    for key, val in existing.items():
        if key not in known and val:
            lines.append(f"{key}={val}")
    IBKR_SECRETS.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return ibkr_status()


def _sync_holdings(progress=None) -> dict:
    """Re-pull the portfolio via the vendored read-only IBKR Flex reader and
    refresh data/current-holdings.json. Read-only: the Flex query cannot trade.
    Credentials come from IBKR_FLEX_TOKEN / IBKR_FLEX_QUERY_ID in the environment
    or a gitignored tools/secrets.env. Raw output stays in data/cache/ibkr/ (also
    gitignored). Returns the fresh holdings payload.

    ``progress`` is an optional ``callable(str)`` used by the background job runner
    to stream status to the UI; it is a no-op when called synchronously."""
    import subprocess

    def _p(msg: str) -> None:
        if progress:
            progress(msg)

    if not IBKR_READER.exists():  # vendored next to serve.py; should always be here
        raise ValueError(f"IBKR reader missing at {IBKR_READER}")

    IBKR_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out_json = IBKR_CACHE_DIR / "portfolio.json"
    snap_dir = IBKR_CACHE_DIR / "snapshots"
    cmd = [sys.executable, str(IBKR_READER), "--json",
           "--out", str(out_json), "--snapshot-dir", str(snap_dir)]
    _p("contacting IBKR Flex (read-only)…")
    try:
        proc = subprocess.run(cmd, cwd=str(IBKR_CACHE_DIR), capture_output=True,
                              text=True, timeout=240)
    except subprocess.TimeoutExpired:
        raise ValueError("IBKR reader timed out after 240s")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        last = detail[-1] if detail else f"exit {proc.returncode}"
        # The reader exits with a clear "no Flex token / Query ID" message when it
        # is unconfigured; point the user at the gitignored secrets file.
        if "Flex token" in last or "Query ID" in last:
            raise ValueError(
                "IBKR credentials not configured. Set IBKR_FLEX_TOKEN and "
                "IBKR_FLEX_QUERY_ID, or paste them into tools/secrets.env "
                "(gitignored). Underlying error: " + last)
        raise ValueError("IBKR reader failed: " + last)

    fresh = _load(out_json)
    if not isinstance(fresh, dict) or "positions" not in fresh or fresh.get("net_asset_value") is None:
        raise ValueError("IBKR reader produced no usable portfolio.json")

    _p("merging snapshot…")
    current = _load(HOLDINGS_JSON) or {}
    _write_json(HOLDINGS_JSON, _merge_holdings_snapshot(current, fresh))
    # A fresh snapshot makes the derived holdings summary stale, so regenerate it
    # in the same call. Best-effort: a render hiccup must not fail the sync itself.
    _p("regenerating holdings summary…")
    payload = holdings_payload()
    payload["site"] = regenerate_site()
    return payload


# IBKR sync runs as a registered background job (like the deep-research/analysis
# runners) so it survives navigation, surfaces in the global task pill, and counts
# as "active" for the reload watcher. One sync at a time -- the Flex pull hits a
# shared cache dir and there is no point racing two.
def _sync_running() -> bool:
    return jobs.running("ibkr_sync")


def _run_holdings_sync_job(job_id: str) -> None:
    _update_job(job_id, state="running", message="pulling portfolio from IBKR (read-only)…")
    try:
        payload = _sync_holdings(progress=lambda msg: _update_job(job_id, message=msg))
    except ValueError as exc:  # expected, user-actionable (bad creds, timeout, …)
        _update_job(job_id, state="error", error=str(exc))
        return
    except Exception as exc:  # noqa: BLE001 -- never let the worker thread die silently
        _update_job(job_id, state="error", error=f"{type(exc).__name__}: {exc}")
        return
    # Keep the public result small: the UI re-fetches /api/holdings on done; we
    # only need the site-regen summary (for the "plan regenerated" line) and the
    # snapshot stamp.
    _update_job(job_id, state="done", message="synced",
                result={"site": payload.get("site"), "generated_at": payload.get("generated_at")})


def start_holdings_sync() -> dict:
    if _sync_running():
        raise _Conflict("an IBKR sync is already running")
    job = _new_job("ibkr_sync")
    threading.Thread(target=_run_holdings_sync_job, args=(job["id"],), daemon=True).start()
    return _job_public(job)


# Full trade + NAV history is a separate, slower pull: it walks the account back
# to inception one ≤365-day Flex window at a time. Like the snapshot sync it runs
# as a registered background job so it survives navigation and shows in the pill.
# The normalized result lands in the gitignored cache (it is the entire personal
# trade ledger), not the curated, committed snapshot.
IBKR_HISTORY_JSON = IBKR_CACHE_DIR / "portfolio-history.json"


def _sync_history(progress=None, *, full: bool = False) -> dict:
    """Reconstruct or top up the trade + NAV history via the vendored Flex reader
    and cache the normalized payload. Read-only. Credentials resolve from the
    gitignored tools/secrets.env (IBKR_FLEX_HISTORY_QUERY_ID, falling back to
    IBKR_FLEX_QUERY_ID).

    Incremental by default: if a cache already exists it fetches only the days
    since it was last covered and merges them in (usually a single Flex request).
    ``full=True`` forces a complete rebuild back to inception. Returns the payload."""
    token, query_id = ibkr_history.resolve_history_credentials()
    existing = None if full else history_payload()
    if existing:
        payload = ibkr_history.extend_history(existing, token, query_id, progress=progress)
    else:
        payload = ibkr_history.build_history(token, query_id, progress=progress)
    IBKR_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _write_json(IBKR_HISTORY_JSON, payload, sort_keys=False)
    return payload


def _history_running() -> bool:
    return jobs.running("ibkr_history")


def _run_history_sync_job(job_id: str, full: bool = False) -> None:
    verb = "rebuilding full" if full else "updating"
    _update_job(job_id, state="running",
                message=f"{verb} trade + NAV history from IBKR (read-only)…")
    try:
        payload = _sync_history(progress=lambda msg: _update_job(job_id, message=msg), full=full)
    except ValueError as exc:  # expected, user-actionable (bad creds, query missing sections)
        _update_job(job_id, state="error", error=str(exc))
        return
    except Exception as exc:  # noqa: BLE001 -- never let the worker thread die silently
        _update_job(job_id, state="error", error=f"{type(exc).__name__}: {exc}")
        return
    s = payload.get("summary", {})
    upd = s.get("update")
    if upd:
        msg = (f"updated: +{upd['new_trades']} trades, +{upd['new_nav_points']} NAV "
               f"points since {upd['previous_to_date'] or 'last pull'}")
    else:
        msg = f"reconstructed {s.get('n_trades', 0)} trades over {s.get('windows', 0)} window(s)"
    _update_job(job_id, state="done", message=msg,
                result={"summary": s, "from_date": payload.get("from_date"),
                        "to_date": payload.get("to_date")})


def start_history_sync(full: bool = False) -> dict:
    if _history_running():
        raise _Conflict("a portfolio-history pull is already running")
    job = _new_job("ibkr_history")
    threading.Thread(target=_run_history_sync_job, args=(job["id"], full), daemon=True).start()
    return _job_public(job)


SECTORS_JSON = IBKR_CACHE_DIR / "sectors.json"  # gitignored (mirrors trade ledger)


def _attach_sectors(payload: dict | None) -> dict | None:
    """Tag each ``by_symbol`` row with a sector for the "By sector" view.

    Read-only and free: loads the cached sector map and seeds it from research
    dossiers in memory (no network, no write -- the Yahoo backfill is a separate
    on-demand job). Rows whose underlying isn't resolved get sector "" and land
    in the UI's "Unknown" bucket."""
    if not payload:
        return payload
    cache = sectors.load_cache(SECTORS_JSON)
    sectors.seed_from_research(cache, RESEARCH_DIR)
    rows = (payload.get("summary") or {}).get("by_symbol") or []
    for r in rows:
        r["sector"] = sectors.sector_of(r.get("underlying") or r.get("symbol") or "", cache)
    payload["sectors_updated_at"] = cache.get("updated_at")
    return payload


def history_payload() -> dict | None:
    """The cached normalized history, or None if it hasn't been pulled yet.

    Enriched on read so caches written before the grouping/currency fields
    existed still carry ``underlying`` / ``is_option`` / ``base_realized_pnl``
    and a per-row ``sector`` (idempotent; no re-pull needed)."""
    return _attach_sectors(ibkr_history.enrich_history_payload(_load(IBKR_HISTORY_JSON)))


def _sectors_running() -> bool:
    return jobs.running("ibkr_sectors")


def _run_sectors_job(job_id: str) -> None:
    """Seed the sector map from research dossiers, then resolve the still-unknown
    traded underlyings via Yahoo (cached, so this is a one-time-ish cost)."""
    _update_job(job_id, state="running", message="loading traded names…")
    payload = ibkr_history.enrich_history_payload(_load(IBKR_HISTORY_JSON))
    if not payload:
        _update_job(job_id, state="error", error="no portfolio history yet — pull it first")
        return
    rows = (payload.get("summary") or {}).get("by_symbol") or []
    symbols = [r.get("underlying") or r.get("symbol") or "" for r in rows]
    cache = sectors.load_cache(SECTORS_JSON)
    sectors.seed_from_research(cache, RESEARCH_DIR)
    sectors.save_cache(SECTORS_JSON, cache)  # persist the free research seeds first
    try:
        stats = sectors.backfill(
            symbols, cache, fetch=sectors.yahoo_fetch,
            progress=lambda msg: _update_job(job_id, message=msg))
    except Exception as exc:  # noqa: BLE001 -- never let the worker thread die silently
        errorlog.log("sectors_backfill_failed", str(exc), level="warning")
        _update_job(job_id, state="error", error=f"{type(exc).__name__}: {exc}")
        return
    sectors.save_cache(SECTORS_JSON, cache)
    msg = (f"resolved {stats['resolved']} new sector(s); "
           f"{stats['unresolved']} unresolved of {stats['considered']} unknown")
    _update_job(job_id, state="done", message=msg, result=stats)


def start_sectors_sync() -> dict:
    if _sectors_running():
        raise _Conflict("a sector lookup is already running")
    job = _new_job("ibkr_sectors")
    threading.Thread(target=_run_sectors_job, args=(job["id"],), daemon=True).start()
    return _job_public(job)


def regenerate_site() -> dict:
    """Re-render the derived markdown holdings summary from the current data
    snapshot. Wraps generate_site.regenerate() so a failure degrades gracefully
    into the payload instead of raising."""
    try:
        return generate_site.regenerate(write=True)
    except Exception as exc:  # noqa: BLE001 -- never let rendering break a sync
        return {"ok": False, "error": str(exc), "written": [], "stale": []}


def _merge_holdings_snapshot(existing: dict, fresh: dict) -> dict:
    """Refresh the curated snapshot from a fresh pull WITHOUT widening its shape.
    Only top-level keys already present in the curated file are updated, and for
    list-of-dict sections each item is restricted to field names already seen in
    the curated file. This guarantees a refresh can never introduce new fields
    (e.g. account identifiers the sanitization deliberately omits). Keys the
    reader doesn't emit (privacy/source markers, sizing_legend) are preserved."""
    if not existing:  # nothing to model the shape on -> trust the pull as-is
        return fresh
    out = dict(existing)
    for key, cur_val in existing.items():
        if key not in fresh:
            continue
        new_val = fresh[key]
        if (isinstance(cur_val, list) and cur_val and isinstance(cur_val[0], dict)
                and isinstance(new_val, list)):
            allowed = set().union(*(set(d) for d in cur_val if isinstance(d, dict)))
            out[key] = [{k: v for k, v in item.items() if k in allowed}
                        for item in new_val if isinstance(item, dict)]
        else:
            out[key] = new_val
    return out
