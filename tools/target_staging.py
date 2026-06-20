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
  reversible backup), bumps ``as_of``/``basis_snapshot``, and regenerates the
  static plan.

Writes to the live model happen ONLY through ``commit_staged`` (and the pin
helpers, which edit standing human judgement directly). Everything else mutates
the draft. With no draft on disk, the system behaves exactly as before: the live
model is the single source of truth.
"""

from __future__ import annotations

import copy
import datetime as dt

import portfolio
import rebalance
from config import DATA_DIR, HOLDINGS_JSON, TARGET_MODEL_JSON
from holdings_sync import regenerate_site as _regenerate_site
from store import load as _load, safe_symbol as _safe_symbol, write_json as _write_json
from target_model import _apply_changes_to_model, _backup_target_model

# The working draft. Mirrors the live model schema plus a top-level ``provenance``
# map and a ``_runs`` audit list of contributing strategy runs.
STAGED_JSON = DATA_DIR / "target-model.staged.json"

_VALID_STANCES = {
    "accumulate", "hold", "trim_only", "do_not_add", "reduce", "avoid", "wait",
}


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


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
    masquerading as freshly research-derived. Returns the provenance map."""
    prov = model.setdefault("provenance", {})
    as_of = model.get("as_of")
    for sym in (model.get("targets") or {}):
        prov.setdefault(sym, {"source": "legacy-plan", "set_at": as_of})
    for name in (model.get("sleeves") or {}):
        prov.setdefault(f"[{name}]", {"source": "legacy-plan", "set_at": as_of})
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
    return bool(_is_pin(rec) and rec.get("locked"))


def set_pin(key: str, *, stance: str, floor_pct=None, ceiling_pct=None,
            rationale: str = "") -> dict:
    """Pin a conviction: durable human intent that anchors future runs. Writes
    the live model directly (with a backup) so it takes effect immediately, and
    mirrors into the working draft if one exists."""
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
    _backup_target_model()
    model.setdefault("provenance", {})[key] = rec
    _write_json(TARGET_MODEL_JSON, model)
    staged = _load(STAGED_JSON)
    if isinstance(staged, dict):
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


def _prov_record(change: dict, *, source: str, run_id, segment, now: str,
                 pin: dict | None) -> dict:
    rec: dict = {"source": source, "set_at": now}
    if run_id:
        rec["run_id"] = run_id
    if segment:
        rec["segment"] = segment
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
                  blocked=None, allow_drop_pinned: bool = False) -> dict:
    """Compose a set of proposal change records into the working draft, recording
    per-key provenance. Pinned names are guarded: a remove/drop of a pinned key
    is skipped unless ``allow_drop_pinned`` (an explicit user override)."""
    staged = load_staged(create=True)
    prov = staged.setdefault("provenance", {})
    pins = load_pins()
    blocked = set(blocked or [])

    guarded: list[dict] = []
    skipped_pre: list[dict] = []
    for ch in changes or []:
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
        guarded.append(ch)

    applied, skipped = _apply_changes_to_model(staged, guarded, blocked=blocked)
    skipped = skipped_pre + skipped
    applied_set = set(applied)
    now = _now_iso()

    for ch in guarded:
        act = ch.get("action")
        if act in ("add_target", "modify_target"):
            try:
                sym = _safe_symbol(ch.get("symbol", ""))
            except ValueError:
                continue
            if sym in applied_set:
                prov[sym] = _prov_record(ch, source=source, run_id=run_id,
                                         segment=segment, now=now, pin=pins.get(sym))
        elif act == "remove_target":
            try:
                sym = _safe_symbol(ch.get("symbol", ""))
            except ValueError:
                continue
            if f"-{sym}" in applied_set:
                prov.pop(sym, None)
        elif act in ("add_sleeve", "modify_sleeve", "set_sleeve", "zero_sleeve"):
            name = str(ch.get("sleeve") or ch.get("name") or "").strip()
            if f"[{name}]" in applied_set:
                prov[f"[{name}]"] = _prov_record(ch, source=source, run_id=run_id,
                                                 segment=segment, now=now, pin=None)
        elif act == "drop_sleeve":
            name = str(ch.get("sleeve") or ch.get("name") or "").strip()
            if f"[{name}]" in applied_set:
                prov.pop(f"[{name}]", None)

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


def commit_staged(confirm: bool) -> dict:
    """Promote the working draft to the live model: backup, write, bump
    ``as_of``/``basis_snapshot``, regenerate the static plan, clear the draft."""
    if not confirm:
        raise ValueError("confirm=true is required")
    staged = _load(STAGED_JSON)
    if not isinstance(staged, dict):
        raise ValueError("no working draft to commit")
    backup = _backup_target_model()
    out = copy.deepcopy(staged)
    out.pop("_runs", None)
    out["as_of"] = _today()
    holdings = _load(HOLDINGS_JSON)
    if isinstance(holdings, dict) and holdings.get("generated_at"):
        out["basis_snapshot"] = holdings["generated_at"]
    _write_json(TARGET_MODEL_JSON, out)
    try:
        STAGED_JSON.unlink()
    except OSError:
        pass
    site = _regenerate_site()
    return {"committed": True, "backup": backup, "as_of": out["as_of"], "site": site}
