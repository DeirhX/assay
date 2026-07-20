#!/usr/bin/env python3
"""Staging layer between exploratory rebalance runs and the live target model.

The exploratory pipeline (segment Deep Research -> construct -> proposal) used to
write straight into ``data/target-model.json``, last-write-wins, with no lineage.
This module inserts a single mutable *working draft* between the two so that:

* Many strategy runs and manual edits COMPOSE into one draft
  (``data/target-model.staged.json``) instead of clobbering the live model.
* Every band carries PROVENANCE (which run/segment/conviction set it, and when),
  with the existing hand-set bands backfilled as ``legacy-plan``.
* Conviction PINS (``source: "user-pin"``) ride on the same provenance layer:
  durable human intent that anchors future runs and is never auto-dropped, but
  can still be challenged (a run may propose against it; the change is flagged
  and parked, not silently applied).
* You review the WHOLE-BOOK diff (added/modified/removed, reconciliation totals,
  overlap warnings) and COMMIT once, which writes the draft to live (with a
  reversible backup), bumps ``as_of``/``basis_snapshot``, and refreshes the
  derived holdings summary.

Writes to the live model happen ONLY through ``commit_staged`` (and the pin
helpers, which edit standing human judgement directly). Everything else mutates
the draft. With no draft on disk, the system behaves exactly as before: the live
model is the single source of truth.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import threading
from pathlib import Path

import portfolio
import rebalance
import segment_home
from config import DATA_DIR, HOLDINGS_JSON, REPO_ROOT, TARGET_MODEL_JSON
from holdings_sync import regenerate_site as _regenerate_site
from store import load as _load, safe_symbol as _safe_symbol, write_json as _write_json
from timeutil import now_iso
from target_model import (
    TARGET_MODEL_BACKUP_DIR, _apply_changes_to_model, _backup_target_model,
)

# The working draft. Mirrors the live model schema plus a top-level ``provenance``
# map and a ``_runs`` audit list of contributing strategy runs.
STAGED_JSON = DATA_DIR / "target-model.staged.json"

# Append-only decision timeline: one JSON object per line, a band change and the
# provenance behind it, written on every commit. The live ``provenance`` map is
# point-in-time (the *current* source per band) and ``commit_staged`` drops the
# staging ``_runs`` audit, so nothing else preserves *when* a band moved and why
# -- which is exactly what attribution's "followed every suggestion" and
# by-source analysis need. Lives in the private ``data`` submodule; it is
# precious history, not a regenerable cache.
PROVENANCE_LOG = DATA_DIR / "provenance-log.jsonl"
_LOG_LOCK = threading.Lock()

_VALID_STANCES = {
    "accumulate", "hold", "trim_only", "do_not_add", "reduce", "avoid", "wait",
}


def _today() -> str:
    return dt.date.today().isoformat()


def _norm_key(key: str) -> str:
    """A target symbol or a ``[sleeve]`` key, validated. Sleeve keys pass through
    (they're our own bracket convention); symbols are run through safe_symbol."""
    raw = str(key or "").strip()
    if raw.startswith("[") and raw.endswith("]"):
        return raw
    return _safe_symbol(raw)


def _is_pin(rec) -> bool:
    return bool(isinstance(rec, dict) and rec.get("source") == "user-pin")


# --------------------------------------------------------------------------- #
# Live model + provenance backfill
# --------------------------------------------------------------------------- #
def load_live() -> dict:
    return _load(TARGET_MODEL_JSON) or {}


def ensure_provenance(model: dict) -> dict:
    """Backfill a ``provenance`` entry for every target/sleeve that lacks one,
    marking it ``legacy-plan`` as of the model's ``as_of`` so today's hand-set
    bands are explicitly labelled (and stale ones become visible) rather than
    masquerading as freshly research-derived. Also backfills ``home_segment``
    from sleeve membership / compatible tags. Returns the provenance map."""
    prov = model.setdefault("provenance", {})
    as_of = model.get("as_of")
    for sym in (model.get("targets") or {}):
        prov.setdefault(sym, {"source": "legacy-plan", "set_at": as_of})
    for name in (model.get("sleeves") or {}):
        prov.setdefault(f"[{name}]", {"source": "legacy-plan", "set_at": as_of})
    segment_home.ensure_homes(model)
    return prov


# --------------------------------------------------------------------------- #
# Conviction pins (soft-locks) -- standing human judgement on the LIVE model
# --------------------------------------------------------------------------- #
def load_pins() -> dict:
    """Every pinned key -> its pin record (read from the live model)."""
    prov = (load_live().get("provenance") or {})
    return {k: v for k, v in prov.items() if _is_pin(v)}


def is_locked(key: str) -> bool:
    rec = (load_live().get("provenance") or {}).get(_norm_key(key))
    return bool(isinstance(rec, dict) and _is_pin(rec) and rec.get("locked"))


def set_pin(key: str, *, stance: str, floor_pct=None, ceiling_pct=None,
            rationale: str = "") -> dict:
    """Pin a conviction: durable human intent that anchors future runs. Writes
    the live model directly (with a backup) so it takes effect immediately, and
    mirrors into the working draft if one exists.

    ``avoid`` is a hard exit mandate rather than a vague bearish opinion: it
    also sets the governed target/sleeve to a 0%-0% avoid band. That makes the
    current planner work toward zero and prevents later bullish research from
    silently resurrecting the position.
    """
    key = _norm_key(key)
    stance = str(stance or "").strip().lower() or "hold"
    if stance not in _VALID_STANCES:
        raise ValueError(f"unknown stance {stance!r}; expected one of {sorted(_VALID_STANCES)}")
    model = load_live()
    if not model:
        raise ValueError("target model not found")
    rec: dict = {
        "source": "user-pin",
        "locked": True,
        "stance": stance,
        "rationale": str(rationale or "").strip(),
        "set_at": _today(),
    }
    if floor_pct is not None and floor_pct != "":
        rec["floor_pct"] = float(floor_pct)
    if ceiling_pct is not None and ceiling_pct != "":
        rec["ceiling_pct"] = float(ceiling_pct)
    if stance == "avoid":
        rec["floor_pct"] = 0.0
        rec["ceiling_pct"] = 0.0

    def apply_exit_band(dst: dict) -> None:
        if stance != "avoid":
            return
        if key.startswith("["):
            bands = dst.setdefault("sleeves", {})
            band_key = key[1:-1]
        else:
            bands = dst.setdefault("targets", {})
            band_key = key
        current = bands.get(band_key)
        band = copy.deepcopy(current) if isinstance(current, dict) else {}
        band.update({
            "low": 0.0,
            "high": 0.0,
            "rule": "avoid",
            "note": (
                str(rationale or "").strip()
                or "Standing exit decision; do not re-add from research."
            ),
        })
        bands[band_key] = band

    _backup_target_model()
    apply_exit_band(model)
    model.setdefault("provenance", {})[key] = rec
    _write_json(TARGET_MODEL_JSON, model)
    staged = _load(STAGED_JSON)
    if isinstance(staged, dict):
        apply_exit_band(staged)
        staged.setdefault("provenance", {})[key] = rec
        _write_json(STAGED_JSON, staged)
    return rec


def clear_pin(key: str) -> dict:
    """Remove a pin (the band itself is untouched). Idempotent."""
    key = _norm_key(key)
    model = load_live()
    prov = model.get("provenance") or {}
    if _is_pin(prov.get(key)):
        _backup_target_model()
        prov.pop(key, None)
        _write_json(TARGET_MODEL_JSON, model)
        staged = _load(STAGED_JSON)
        if isinstance(staged, dict) and _is_pin((staged.get("provenance") or {}).get(key)):
            staged["provenance"].pop(key, None)
            _write_json(STAGED_JSON, staged)
        return {"key": key, "cleared": True}
    return {"key": key, "cleared": False}


# --------------------------------------------------------------------------- #
# Working draft
# --------------------------------------------------------------------------- #
def has_draft() -> bool:
    return STAGED_JSON.exists()


def active_model() -> dict:
    """The model the planner, what-if, and trade preview should reflect: the
    working draft if one exists, else the live committed model.

    Before this, the planner and trade desk always read the *live* model while
    edits sat in the draft -- so the drift, suggested trades, and what-if all
    described a portfolio the user had already staged away from. Previewing the
    draft (clearly labelled in the UI) keeps every downstream number consistent
    with what's actually being edited."""
    return load_staged() or load_live()


def load_staged(*, create: bool = False) -> dict | None:
    """The working draft, or None. With ``create``, seed a fresh draft from the
    live model (provenance backfilled) the first time something is staged."""
    staged = _load(STAGED_JSON)
    if isinstance(staged, dict):
        return staged
    if not create:
        return None
    live = load_live()
    if not live:
        raise ValueError("target model not found")
    staged = copy.deepcopy(live)
    ensure_provenance(staged)
    staged["_runs"] = []
    _write_json(STAGED_JSON, staged)
    return staged


def _proposed_home(change: dict, *, known_sleeves: set[str]) -> str | None:
    """Home implied by a change record (explicit field or target sleeve tag)."""
    explicit = str(change.get("home_segment") or "").strip()
    if explicit:
        return explicit
    pt = change.get("proposed_target") or {}
    if isinstance(pt, dict):
        tag = str(pt.get("sleeve") or "").strip()
        if tag and tag in known_sleeves:
            return tag
    return None


def _prov_record(change: dict, *, source: str, run_id, segment, now: str,
                 pin: dict | None, home_segment: str | None = None,
                 prior_home: str | None = None) -> dict:
    rec: dict = {"source": source, "set_at": now}
    if run_id:
        rec["run_id"] = run_id
    if segment:
        rec["segment"] = segment
    # Durable allocation home — preserved across research lineage overwrites.
    home = home_segment or prior_home
    if home:
        rec["home_segment"] = home
    if change.get("conviction"):
        rec["conviction"] = change["conviction"]
    if change.get("conviction_source"):
        rec["conviction_source"] = change["conviction_source"]
    if change.get("challenges_pin"):
        rec["challenges_pin"] = True
    # If this overwrites a pinned name, keep the prior intent visible so the
    # review UI can show "your pin vs this proposal" and the user can revert.
    if pin:
        rec["prior_pin"] = {k: pin.get(k) for k in ("stance", "floor_pct", "ceiling_pct") if pin.get(k) is not None}
    return rec


def stage_changes(changes, *, run_id=None, segment=None, source: str = "strategy",
                  blocked=None, allow_drop_pinned: bool = False,
                  allow_rehome: bool = False) -> dict:
    """Compose a set of proposal change records into the working draft, recording
    per-key provenance. Pinned names are guarded: a remove/drop of a pinned key
    is skipped unless ``allow_drop_pinned`` (an explicit user override).
    Home-segment moves are skipped unless ``allow_rehome``."""
    staged = load_staged(create=True)
    assert staged is not None  # create=True always returns a dict
    # Research/construct still emits add_target with a sleeve tag; fold those
    # into the allocation roster before home/pin guards (lazy import: migrate
    # calls stage_changes).
    from sleeve_migrate import fold_target_changes_into_sleeves
    changes = fold_target_changes_into_sleeves(
        list(changes or []), staged, allow_rehome=allow_rehome)

    prov = staged.setdefault("provenance", {})
    pins = load_pins()
    blocked = set(blocked or [])
    known = segment_home.sleeve_names(staged)

    guarded: list[dict] = []
    skipped_pre: list[dict] = []
    for ch in changes:
        act = ch.get("action")
        if act == "remove_target" and not allow_drop_pinned:
            try:
                sym = _safe_symbol(ch.get("symbol", ""))
            except ValueError:
                sym = None
            if sym and sym in pins:
                skipped_pre.append({"symbol": sym, "reason": "pinned: drop blocked (override required)"})
                continue
        if act in ("drop_sleeve", "zero_sleeve") and not allow_drop_pinned:
            name = str(ch.get("sleeve") or ch.get("name") or "").strip()
            if f"[{name}]" in pins:
                skipped_pre.append({"symbol": f"[{name}]", "reason": "pinned: drop blocked (override required)"})
                continue
        if act in ("add_target", "modify_target"):
            try:
                sym = _safe_symbol(ch.get("symbol", ""))
            except ValueError:
                sym = None
            if sym:
                proposed = _proposed_home(ch, known_sleeves=known)
                reason = segment_home.conflict_reason(
                    staged, sym, proposed, allow_rehome=allow_rehome)
                if reason:
                    skipped_pre.append({"symbol": sym, "reason": reason})
                    continue
        guarded.append(ch)

    applied, skipped = _apply_changes_to_model(staged, guarded, blocked=blocked)
    skipped = skipped_pre + skipped
    applied_set = set(applied)
    now = now_iso()

    for ch in guarded:
        act = ch.get("action")
        if act in ("add_target", "modify_target"):
            try:
                sym = _safe_symbol(ch.get("symbol", ""))
            except ValueError:
                continue
            if sym in applied_set:
                prior_home = None
                old = prov.get(sym)
                if isinstance(old, dict):
                    prior_home = str(old.get("home_segment") or "").strip() or None
                home = _proposed_home(ch, known_sleeves=known)
                prov[sym] = _prov_record(
                    ch, source=source, run_id=run_id, segment=segment, now=now,
                    pin=pins.get(sym), home_segment=home, prior_home=prior_home)
        elif act == "remove_target":
            try:
                sym = _safe_symbol(ch.get("symbol", ""))
            except ValueError:
                continue
            if f"-{sym}" in applied_set:
                if ch.get("preserve_provenance"):
                    # Sleeve migration: drop the top-level band but keep lineage
                    # + home so the name stays in the partition map.
                    prior_raw = prov.get(sym)
                    prior_prov: dict = prior_raw if isinstance(prior_raw, dict) else {}
                    home = (str(ch.get("home_segment") or "").strip()
                            or str(prior_prov.get("home_segment") or "").strip()
                            or None)
                    rec = dict(prior_prov)
                    rec.update({"source": source, "set_at": now})
                    if run_id:
                        rec["run_id"] = run_id
                    if segment:
                        rec["segment"] = segment
                    if home:
                        rec["home_segment"] = home
                    prov[sym] = rec
                else:
                    prov.pop(sym, None)
        elif act in ("add_sleeve", "modify_sleeve", "set_sleeve", "zero_sleeve"):
            name = str(ch.get("sleeve") or ch.get("name") or "").strip()
            if f"[{name}]" in applied_set:
                prov[f"[{name}]"] = _prov_record(ch, source=source, run_id=run_id,
                                                 segment=segment, now=now, pin=None)
                # Sleeve membership is authoritative home for each member.
                sleeve_raw = ch.get("proposed_sleeve")
                sleeve_def: dict = sleeve_raw if isinstance(sleeve_raw, dict) else {}
                for m in (sleeve_def.get("members") or []):
                    try:
                        msym = _safe_symbol(m)
                    except ValueError:
                        continue
                    mrec = prov.setdefault(msym, {"source": source, "set_at": now})
                    if isinstance(mrec, dict):
                        mrec["home_segment"] = name
        elif act == "drop_sleeve":
            name = str(ch.get("sleeve") or ch.get("name") or "").strip()
            if f"[{name}]" in applied_set:
                prov.pop(f"[{name}]", None)

    segment_home.ensure_homes(staged)

    if run_id or segment:
        staged.setdefault("_runs", []).append({
            "run_id": run_id, "segment": segment, "source": source,
            "applied": applied, "at": now,
        })
    _write_json(STAGED_JSON, staged)
    return {"applied": applied, "skipped": skipped, "staged_count": len(applied),
            "has_draft": True}


def stage_proposal(segment: str, date: str, *, changes=None, run_id=None,
                   source: str = "pipeline", allow_blocked: bool = False) -> dict:
    """Stage a persisted ``<segment>-<date>.target-proposal.json`` (the Pipeline
    apply path). ``changes`` overrides the proposal's own change list when given
    (gate edits)."""
    from config import DEEP_DIR
    from store import slugify as _slugify
    seg = _slugify(segment)
    proposal = _load(DEEP_DIR / f"{seg}-{date}.target-proposal.json")
    if not proposal:
        raise ValueError(f"proposal not found: {seg}-{date}.target-proposal.json")
    blocked = set(proposal.get("blocked_symbols", [])) if not allow_blocked else set()
    use = changes if changes is not None else proposal.get("changes", [])
    return stage_changes(use, run_id=run_id, segment=seg, source=source, blocked=blocked)


def revert_key(key: str) -> dict:
    """Restore one key in the draft to its live value (or remove it from the
    draft if it isn't in the live model), provenance included. The 'reject this
    change / keep my pin' action in the review UI."""
    key = _norm_key(key)
    staged = load_staged(create=True)
    assert staged is not None  # create=True always returns a dict
    live = load_live()
    lprov = (live.get("provenance") or {}).get(key)
    if key.startswith("["):
        name = key[1:-1]
        lv = (live.get("sleeves") or {}).get(name)
        sleeves = staged.setdefault("sleeves", {})
        if lv is None:
            sleeves.pop(name, None)
        else:
            sleeves[name] = copy.deepcopy(lv)
    else:
        lv = (live.get("targets") or {}).get(key)
        targets = staged.setdefault("targets", {})
        if lv is None:
            targets.pop(key, None)
        else:
            targets[key] = copy.deepcopy(lv)
    prov = staged.setdefault("provenance", {})
    if lprov is None:
        prov.pop(key, None)
    else:
        prov[key] = copy.deepcopy(lprov)
    _write_json(STAGED_JSON, staged)
    return {"reverted": key}


# --------------------------------------------------------------------------- #
# Whole-book reconciliation + diff
# --------------------------------------------------------------------------- #
def _mid(band: dict) -> float:
    lo, hi = band.get("low"), band.get("high")
    if isinstance(lo, (int, float)) and isinstance(hi, (int, float)):
        return (float(lo) + float(hi)) / 2.0
    return 0.0


def _reconcile(model: dict) -> dict:
    targets = model.get("targets") or {}
    sleeves = model.get("sleeves") or {}
    cash = float(model.get("cash_target_pct") or 0.0)
    targeted = sum(_mid(t) for t in targets.values()) + sum(_mid(s) for s in sleeves.values())
    available = 100.0 - targeted - cash
    out: dict = {
        "targeted_mid_pct": round(targeted, 2),
        "cash_target_pct": cash,
        "available_pct": round(available, 2),
        "over_allocated": available < -0.01,
        "funding_order": model.get("funding_order") or [],
    }
    holdings = _load(HOLDINGS_JSON)
    if isinstance(holdings, dict):
        weights = portfolio.holdings_weights(holdings)
        managed = set(targets) | {m for s in sleeves.values() for m in (s.get("members") or [])}
        untargeted = sorted(((s, w) for s, w in weights.items() if s not in managed),
                            key=lambda kv: -kv[1])
        out["untargeted"] = [{"symbol": s, "current_pct": round(w, 2)} for s, w in untargeted]
        out["untargeted_pct"] = round(sum(w for _, w in untargeted), 2)
    return out


def _overlap_findings(model: dict) -> list[dict]:
    holdings = _load(HOLDINGS_JSON)
    if not isinstance(holdings, dict):
        return []
    try:
        findings = rebalance.check_model(model, holdings)
    except Exception:  # noqa: BLE001 - a bad draft band shouldn't kill the diff
        return []
    return [{"severity": f.severity, "area": f.area, "message": f.message}
            for f in findings if f.severity in ("WARN", "ERROR")]


def _diff_section(live_map: dict, staged_map: dict, prov: dict, live_prov: dict,
                  kind: str) -> list[dict]:
    rows: list[dict] = []
    for key in sorted(set(live_map) | set(staged_map)):
        before = live_map.get(key)
        after = staged_map.get(key)
        if before == after:
            continue
        change = "added" if before is None else ("removed" if after is None else "modified")
        pkey = f"[{key}]" if kind == "sleeve" else key
        rows.append({
            "key": key,
            "kind": kind,
            "change": change,
            "before": before,
            "after": after,
            "provenance": prov.get(pkey),
            "locked": _is_pin(live_prov.get(pkey)),
        })
    return rows


def diff_staged_vs_live() -> dict:
    """Structured diff of the working draft against the live model, plus the
    whole-book reconciliation and overlap warnings. With no draft, reports the
    live reconciliation so the planner banner stays accurate."""
    live = load_live()
    staged = _load(STAGED_JSON)
    pins = load_pins()
    if not isinstance(staged, dict):
        return {
            "has_draft": False,
            "targets": [], "sleeves": [],
            "reconciliation": _reconcile(live),
            "overlaps": [], "runs": [], "pins": pins,
            "counts": {"targets": 0, "sleeves": 0, "total": 0},
        }
    sprov = staged.get("provenance") or {}
    lprov = live.get("provenance") or {}
    tdiff = _diff_section(live.get("targets") or {}, staged.get("targets") or {},
                          sprov, lprov, "target")
    sdiff = _diff_section(live.get("sleeves") or {}, staged.get("sleeves") or {},
                          sprov, lprov, "sleeve")
    return {
        "has_draft": True,
        "targets": tdiff,
        "sleeves": sdiff,
        "reconciliation": _reconcile(staged),
        "overlaps": _overlap_findings(staged),
        "runs": staged.get("_runs") or [],
        "pins": pins,
        "counts": {"targets": len(tdiff), "sleeves": len(sdiff),
                   "total": len(tdiff) + len(sdiff)},
    }


# --------------------------------------------------------------------------- #
# Discard / commit
# --------------------------------------------------------------------------- #
def discard_staged() -> dict:
    if STAGED_JSON.exists():
        try:
            STAGED_JSON.unlink()
        except OSError as exc:
            raise ValueError(f"could not discard working draft: {exc}")
        return {"discarded": True}
    return {"discarded": False}


# --------------------------------------------------------------------------- #
# Provenance timeline (append-only decision log)
# --------------------------------------------------------------------------- #
def _band_summary(band: dict | None) -> dict | None:
    """The attribution-relevant slice of a band: low/high/rule. None for an
    added/removed side. Keeps the log compact and stable even as the model
    schema grows other keys (note, structural, members...)."""
    if not isinstance(band, dict):
        return None
    return {k: band[k] for k in ("low", "high", "rule") if band.get(k) is not None}


def _provenance_entries(prior: dict, later: dict, *, at: str,
                        backfill: bool = False) -> list[dict]:
    """The band changes from *prior* -> *later*, one log entry each, pulling the
    source/run_id/segment/conviction from *later*'s provenance map. Pure: no disk,
    no clock (``at`` is injected), so it's the same routine the live commit and the
    backfill both diff through."""
    prov = later.get("provenance") or {}
    entries: list[dict] = []
    for kind, field in (("target", "targets"), ("sleeve", "sleeves")):
        bmap = prior.get(field) or {}
        amap = later.get(field) or {}
        for key in sorted(set(bmap) | set(amap)):
            before, after = bmap.get(key), amap.get(key)
            if before == after:
                continue
            change = "added" if before is None else ("removed" if after is None else "modified")
            prec = prov.get(f"[{key}]" if kind == "sleeve" else key)
            prec = prec if isinstance(prec, dict) else {}
            entry: dict = {
                "at": at, "key": key, "kind": kind, "change": change,
                "before": _band_summary(before), "after": _band_summary(after),
                "source": prec.get("source"),
            }
            for opt in ("run_id", "segment", "conviction"):
                if prec.get(opt):
                    entry[opt] = prec[opt]
            if backfill:
                entry["backfill"] = True
            entries.append(entry)
    return entries


def _dedupe_key(entry: dict) -> tuple:
    return (entry.get("at"), entry.get("kind"), entry.get("key"), entry.get("change"))


def read_provenance_log(path: Path | None = None) -> list[dict]:
    """Every logged entry (skipping any corrupt line). Empty when absent."""
    path = path or PROVENANCE_LOG  # resolve the module global at call time (patchable)
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
    except OSError:
        return []
    return out


def _append_provenance_log(entries: list[dict], *, path: Path | None = None) -> int:
    """Append entries as JSONL, skipping any whose (at, kind, key, change) is
    already present -- so a re-run (or an overlapping backfill) can't double-count.
    Returns the number actually written."""
    if not entries:
        return 0
    path = path or PROVENANCE_LOG
    with _LOG_LOCK:
        seen = {_dedupe_key(e) for e in read_provenance_log(path)}
        fresh = [e for e in entries if _dedupe_key(e) not in seen]
        if not fresh:
            return 0
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            for e in fresh:
                fh.write(json.dumps(e, sort_keys=True) + "\n")
    return len(fresh)


def _backup_timestamp(path: Path) -> str | None:
    """ISO instant a backup was taken, parsed from its ``target-model-<ts>.json``
    name (the snapshot's own ``as_of`` is a date, too coarse to order intraday
    commits). None if the name doesn't match."""
    stem = path.stem  # target-model-20260707T012233Z
    marker = "target-model-"
    if not stem.startswith(marker):
        return None
    raw = stem[len(marker):]
    try:
        return dt.datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(
            tzinfo=dt.timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        return None


def backfill_provenance_log(*, path: Path | None = None,
                            backup_dir: Path | None = None) -> dict:
    """Best-effort reconstruction of the timeline from the dated model backups.

    The backups are a sparse chain of whole-model snapshots (each taken *before*
    a mutating apply). Diffing consecutive snapshots, then the newest snapshot
    against the current live model, recovers the band changes that predate the
    log -- labelled ``backfill: true`` and dated from the *later* snapshot, so
    they're honestly distinguishable from the exact live-commit record. Idempotent
    via the same (at, kind, key, change) de-dupe as any append, so it's safe to
    re-run; it only ever fills gaps."""
    path = path or PROVENANCE_LOG
    backup_dir = backup_dir or TARGET_MODEL_BACKUP_DIR
    backups = sorted(
        (p for p in backup_dir.glob("target-model-*.json") if _backup_timestamp(p)),
        key=lambda p: _backup_timestamp(p) or "",
    )
    snapshots: list[tuple[str, dict]] = []
    for p in backups:
        model = _load(p)
        if isinstance(model, dict):
            snapshots.append((_backup_timestamp(p) or "", model))
    live = load_live()
    if live:
        snapshots.append((live.get("as_of") or now_iso(), live))

    entries: list[dict] = []
    for (_, prior), (at, later) in zip(snapshots, snapshots[1:]):
        entries.extend(_provenance_entries(prior, later, at=at, backfill=True))
    written = _append_provenance_log(entries, path=path)
    return {"snapshots": len(snapshots), "candidates": len(entries), "written": written}


def commit_staged(confirm: bool) -> dict:
    """Promote the working draft to the live model: backup, write, bump
    ``as_of``/``basis_snapshot``, refresh the holdings summary, clear the draft."""
    if not confirm:
        raise ValueError("confirm=true is required")
    staged = _load(STAGED_JSON)
    if not isinstance(staged, dict):
        raise ValueError("no working draft to commit")
    prior = load_live()  # snapshot BEFORE overwrite, for the provenance diff
    out = copy.deepcopy(staged)
    out.pop("_runs", None)
    out["as_of"] = _today()
    holdings = _load(HOLDINGS_JSON)
    if isinstance(holdings, dict) and holdings.get("generated_at"):
        out["basis_snapshot"] = holdings["generated_at"]
    # Never promote a draft that fails the model's own consistency checks (a band
    # with low>high, an unknown rule, a 'reduce' on something not held, sleeve
    # member overlaps, minimum targets + cash over 100%). WARN-level findings are
    # informational and ride along; an ERROR is a hard contradiction the live
    # model must not inherit, so refuse and name what to fix.
    if isinstance(holdings, dict):
        errors = [f for f in rebalance.check_model(out, holdings) if f.severity == "ERROR"]
        if errors:
            raise ValueError(
                f"cannot commit — the working draft has {len(errors)} blocking "
                "error(s): " + "; ".join(f"{f.area}: {f.message}" for f in errors)
            )
    backup = _backup_target_model()
    _write_json(TARGET_MODEL_JSON, out)
    # Record the band changes to the append-only timeline now that the live write
    # succeeded. Best-effort: a log-append failure must never roll back a commit
    # that already landed -- the model is the source of truth, the log is audit.
    logged = 0
    try:
        logged = _append_provenance_log(
            _provenance_entries(prior, out, at=now_iso()))
    except Exception:  # noqa: BLE001 - the timeline is audit, not a commit gate
        logged = 0
    try:
        STAGED_JSON.unlink()
    except OSError:
        pass
    site = _regenerate_site()
    return {"committed": True, "backup": backup, "as_of": out["as_of"],
            "provenance_logged": logged, "site": site}


# --------------------------------------------------------------------------- #
# Restore from a pre-apply backup (the visible undo for a committed apply)
# --------------------------------------------------------------------------- #
def _resolve_backup(rel: str) -> Path:
    """Validate a client-supplied backup path and return the resolved file. The
    path comes from a manifest/commit result, but it's still untrusted input, so
    it must resolve to a ``.json`` INSIDE the backups dir — never an arbitrary
    file we'd then promote to the live model."""
    if not rel:
        raise ValueError("no backup specified")
    root = TARGET_MODEL_BACKUP_DIR.resolve()
    target = (REPO_ROOT / rel).resolve()
    if root not in target.parents or target.suffix != ".json":
        raise ValueError("invalid backup path")
    if not target.is_file():
        raise ValueError("backup file not found (it may have been pruned)")
    return target


def diff_backup_vs_live(rel: str) -> dict:
    """Whole-book diff of what restoring ``rel`` would change, framed as
    live(before) -> backup(after) so it renders on the same band tracks as every
    other diff. Read-only: nothing is written."""
    backup = _load(_resolve_backup(rel))
    if not isinstance(backup, dict) or not backup.get("targets"):
        raise ValueError("backup is not a valid target model")
    live = load_live()
    tdiff = _diff_section(live.get("targets") or {}, backup.get("targets") or {}, {}, {}, "target")
    sdiff = _diff_section(live.get("sleeves") or {}, backup.get("sleeves") or {}, {}, {}, "sleeve")
    return {
        "backup": rel,
        "backup_as_of": backup.get("as_of"),
        "live_as_of": live.get("as_of"),
        "targets": tdiff,
        "sleeves": sdiff,
        "counts": {"targets": len(tdiff), "sleeves": len(sdiff), "total": len(tdiff) + len(sdiff)},
    }


def restore_backup(rel: str, confirm: bool) -> dict:
    """Restore the live target model from a pre-apply backup. Backs up the CURRENT
    live model first, so the restore is itself reversible, then writes the backup
    over live and refreshes the derived site. The working draft (if any) is left
    untouched — this is a live-model operation."""
    if not confirm:
        raise ValueError("confirm=true is required")
    backup = _load(_resolve_backup(rel))
    if not isinstance(backup, dict) or not backup.get("targets"):
        raise ValueError("backup is not a valid target model")
    safety = _backup_target_model()  # snapshot current live before overwriting it
    _write_json(TARGET_MODEL_JSON, backup)
    site = _regenerate_site()
    return {"restored": True, "backup": rel, "backup_of_current": safety,
            "as_of": backup.get("as_of"), "site": site}
