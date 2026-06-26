#!/usr/bin/env python3
"""Whole-book portfolio optimizer: size the entire candidate pool at once.

``target_construct`` sizes ONE segment's names against a slice of the book. This
module generalizes that same deterministic logic to the *whole* portfolio: it
gathers a **candidate pool** (everything held, plus the basket's want/curious
picks, plus every name the standing model already targets, plus pinned names),
derives a conviction for each, then water-fills the invested budget across them
under constraints (per-name cap, sleeve ``member_caps``, a single-name
concentration limit, pin floors/ceilings, cash headroom).

The output is a ``schema_version: 2`` target-proposal with the exact ``changes[]``
shape ``target_staging.stage_changes(..., source="optimizer")`` and
``target_model.preview_plan_for_proposal`` already consume — so the whole back
half (working draft -> commit -> rebalance) is reused unchanged.

Design rules:
* Deterministic backbone. An optional LLM conviction pass (see ``llm-synthesis``)
  only enriches convictions; the sizing math here is pure and testable.
* Pins are hard anchors: a pinned name is never auto-dropped and its band is
  clamped to the pin's floor/ceiling.
* Sizing reuses ``target_construct``'s primitives (``_band_from_weight``,
  ``_POINTS``/``_RULE``, ``_member_caps``, ``_pins``) rather than reinventing
  them, so the two paths stay consistent.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Callable

import portfolio
import research_brief
import sleeve_aliases
import target_construct as tc
import ticker_analysis
from config import DATA_DIR, HOLDINGS_JSON, TARGET_MODEL_JSON
from store import load as _load, safe_symbol as _safe_symbol, write_json as _write_json

# Per-held-name convictions from the batch "analyze all holdings" review
# (analysis_jobs.run_portfolio_review_job). A pool-side input, read by build_pool.
PORTFOLIO_REVIEW_JSON = DATA_DIR / "portfolio-review.json"

# Whole-book defaults. The concentration limit is the single hard ceiling on any
# one name; per-name cap is the softer "no thesis is worth more than this" knob.
DEFAULT_CASH_TARGET_PCT = 5.0
DEFAULT_PER_NAME_CAP = tc.DEFAULT_PER_NAME_CAP  # 12%
DEFAULT_CONCENTRATION_PCT = 20.0
MIN_INVESTED_PCT = 50.0   # refuse to size a book that's almost all cash by mistake

# Conviction reads, in precedence order, all collapsing to high/medium/low/avoid.
_RULE_CONVICTION = {
    "accumulate": "high", "hold": "medium", "do_not_add": "medium", "wait": "low",
    "trim_only": "avoid", "reduce": "avoid", "avoid": "avoid",
}
_PIN_CONVICTION = {
    "accumulate": "high", "hold": "medium", "do_not_add": "medium", "wait": "low",
}
_TIER_CONVICTION = {"want": "medium", "curious": "low"}
# A basket pick can carry an explicit conviction snapshot; only these are trusted.
_EXPLICIT = {"high", "medium", "low", "avoid"}

_PER_NAME_EXCERPT = 800   # chars of each name's narrative fed to the synthesis LLM
_POOL_PROMPT_LIMIT = 16000  # total prompt body budget


def _round1(v: float) -> float:
    return round(float(v), 1)


def save_portfolio_review(holdings: dict, *, summary: str = "") -> dict:
    """Persist the batch portfolio review: ``{SYM: {conviction, rationale,
    stance}}`` plus a free-text rebalance summary. ``build_pool`` reads the
    convictions back as a held-name conviction source."""
    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "holdings": holdings or {},
        "summary": str(summary or ""),
    }
    _write_json(PORTFOLIO_REVIEW_JSON, payload)
    return payload


def load_review_convictions() -> dict[str, str]:
    """``{SYM: conviction}`` from the saved portfolio review (valid tiers only)."""
    data = _load(PORTFOLIO_REVIEW_JSON) or {}
    out: dict[str, str] = {}
    for sym, node in (data.get("holdings") or {}).items():
        conv = str((node or {}).get("conviction") or "").lower().strip()
        if conv in _EXPLICIT:
            out[str(sym).upper()] = conv
    return out


# --------------------------------------------------------------------------- #
# Pool construction
# --------------------------------------------------------------------------- #
def build_pool(*, model: dict | None = None, holdings: dict | None = None,
               basket_items: list[dict] | None = None,
               include_curious: bool = True,
               review_convictions: dict[str, str] | None = None) -> list[dict]:
    """The candidate pool: a de-duplicated union of held names, basket picks,
    existing-target names, and pinned names. Each entry carries everything the
    sizer and the UI need: current weight, current band, sleeve, pin, interest
    tier, the source(s) it came from, and a derived conviction.

    ``include_curious=False`` drops basket ``curious`` picks that aren't already
    held/targeted (a held curious name stays — it's in the book regardless)."""
    model = model if model is not None else (_load(TARGET_MODEL_JSON) or {})
    holdings = holdings if holdings is not None else (_load(HOLDINGS_JSON) or {})
    basket_items = basket_items or []
    reviews = review_convictions if review_convictions is not None else load_review_convictions()

    targets = model.get("targets") or {}
    weights = portfolio.holdings_weights(holdings) if isinstance(holdings, dict) else {}
    pins = tc._pins(model)
    member_of: dict[str, str] = {}
    for name, sleeve in (model.get("sleeves") or {}).items():
        for m in (sleeve.get("members") or []):
            member_of.setdefault(str(m).upper(), name)

    basket_by_sym = {str(b.get("symbol") or "").upper(): b for b in basket_items}

    # Gather the symbol universe, then build one entry per symbol so duplicate
    # sources (held + starred + targeted) merge into a single pool row.
    syms: set[str] = set()
    syms.update(weights.keys())
    syms.update(str(s).upper() for s in targets.keys())
    syms.update(pins.keys())
    syms.update(basket_by_sym.keys())

    pool: list[dict] = []
    for sym in sorted(syms):
        if not sym or sym.startswith("["):  # sleeve pins aren't pool names
            continue
        try:
            sym = _safe_symbol(sym)
        except ValueError:
            continue
        held_pct = round(weights[sym], 2) if sym in weights else None
        band = targets.get(sym) if isinstance(targets.get(sym), dict) else None
        pin = pins.get(sym)
        bk = basket_by_sym.get(sym)
        tier = (bk or {}).get("tier")
        sleeve = (bk or {}).get("sleeve") or member_of.get(sym) or (band or {}).get("sleeve") or ""
        sources = []
        if held_pct is not None:
            sources.append("held")
        if band:
            sources.append("model")
        if bk:
            sources.append(str(bk.get("source") or "basket"))
        if sym in pins:
            sources.append("pin")

        if include_curious is False and tier == "curious" and held_pct is None and not band:
            continue  # a parked curious pick we don't already own/target

        conviction, conv_source, rationale = _derive_conviction(
            band=band, pin=pin, basket=bk, held_pct=held_pct, review=reviews.get(sym))
        pool.append({
            "symbol": sym,
            "sleeve": sleeve,
            "held_pct": held_pct,
            "current_target": band,
            "pinned": bool(pin),
            "tier": tier,
            "sources": sorted(set(sources)),
            "conviction": conviction,
            "conviction_source": conv_source,
            "rationale": rationale,
            "segment": (bk or {}).get("segment"),
            "run": (bk or {}).get("run"),
        })
    return pool


def _derive_conviction(*, band, pin, basket, held_pct, review=None):
    """Conviction (+ where it came from) for a pool name, by precedence:
    explicit basket conviction > pin stance > portfolio-review conviction >
    existing target rule > basket tier > held-carry default. Returns
    ``(conviction, source, rationale)``."""
    if basket:
        explicit = str(basket.get("conviction") or "").lower().strip()
        if explicit in _EXPLICIT:
            return explicit, "basket", str(basket.get("note") or "")
    if pin:
        stance = str(pin.get("stance") or "").lower().strip()
        if stance in _PIN_CONVICTION:
            return _PIN_CONVICTION[stance], "pin", "Conviction pin (standing intent)."
    if review in _EXPLICIT:
        return review, "review", "From the latest portfolio review."
    if isinstance(band, dict):
        rule = str(band.get("rule") or "").lower().strip()
        if rule in _RULE_CONVICTION:
            return _RULE_CONVICTION[rule], "model", str(band.get("note") or "")
    if basket:
        tier = str(basket.get("tier") or "want").lower().strip()
        return _TIER_CONVICTION.get(tier, "medium"), "tier", ""
    if held_pct is not None:
        return "medium", "held", "Held position with no explicit signal (carry)."
    return "low", "default", ""


# --------------------------------------------------------------------------- #
# Sizing
# --------------------------------------------------------------------------- #
def _size_weights(points: dict[str, float], caps: dict[str, float], budget: float) -> dict[str, float]:
    """Conviction-proportional weights under per-name caps. Each name gets
    ``budget * points / total_points``, then is clamped to its cap. We do NOT
    redistribute a capped name's surplus into the others: pushing freed budget
    into lower-conviction names would inflate them to their caps purely because
    budget was available, which is conviction-dishonest. Leftover budget instead
    surfaces as ``available_pct`` for the user to deploy or hold as cash. This
    guarantees the sized total never exceeds the invested budget."""
    total = sum(points.values())
    if total <= 0:
        return {}
    return {sym: min(budget * pts / total, caps.get(sym, float("inf")))
            for sym, pts in points.items()}


def _clamp_to_pin(pins: dict, sym: str, low: float, high: float) -> tuple[float, float]:
    pin = pins.get(sym)
    if not pin:
        return low, high
    floor, ceil = pin.get("floor_pct"), pin.get("ceiling_pct")
    if isinstance(floor, (int, float)):
        low = max(low, float(floor))
    if isinstance(ceil, (int, float)):
        high = min(high, float(ceil)) if high else float(ceil)
    if high - low < tc.MIN_BAND_WIDTH:
        high = _round1(low + tc.MIN_BAND_WIDTH)
    return _round1(low), _round1(high)


def _change(entry: dict, *, low: float, high: float, rule: str, action: str,
            existing: dict | None, aliases: dict | None, challenges_pin: bool = False) -> dict:
    """An optimizer change record in the proposal ``changes[]`` shape that
    ``_apply_changes_to_model`` consumes. Mirrors ``target_construct._change`` but
    with an optimizer-honest provenance note."""
    sym = entry["symbol"]
    raw_sleeve = entry.get("sleeve") or ""
    sleeve, sleeve_known = sleeve_aliases.normalize(raw_sleeve, aliases=aliases)
    note_bits = []
    if entry.get("rationale"):
        note_bits.append(entry["rationale"])
    note_bits.append("Sized by the portfolio optimizer; verify before applying.")
    proposed: dict[str, Any] = {"low": low, "high": high, "rule": rule, "note": " ".join(note_bits)}
    if sleeve:
        proposed["sleeve"] = sleeve
    change: dict[str, Any] = {
        "symbol": sym,
        "action": action,
        "status": "draft",
        "conviction": entry.get("conviction"),
        "conviction_source": entry.get("conviction_source"),
        "sleeve": sleeve,
        "sleeve_unknown": bool(raw_sleeve) and not sleeve_known,
        "current_target": existing,
        "proposed_target": proposed,
        "rationale": entry.get("rationale") or "",
    }
    if challenges_pin:
        change["challenges_pin"] = True
    return change


def size_pool(pool: list[dict], model: dict, *, cash_target_pct: float,
              per_name_cap: float = DEFAULT_PER_NAME_CAP,
              concentration_pct: float = DEFAULT_CONCENTRATION_PCT,
              drop_avoid: bool = False) -> tuple[list[dict], dict]:
    """Pure sizing pass over the whole pool. Returns ``(changes, meta)``.

    Buy-rated names (high/medium/low) split the invested budget by conviction
    points under caps; avoid-rated *held* names get a trim band (or a drop when
    ``drop_avoid``); avoid-rated unheld names are skipped. Pins clamp bands and
    are never auto-dropped."""
    existing_targets = model.get("targets") or {}
    caps_model = tc._member_caps(model)
    pins = tc._pins(model)
    aliases = sleeve_aliases.load_aliases()

    cash = max(0.0, min(95.0, float(cash_target_pct)))
    invested = max(MIN_INVESTED_PCT, 100.0 - cash)
    hard_cap = min(float(per_name_cap), float(concentration_pct))

    def cap_for(sym: str) -> float:
        return min(hard_cap, caps_model.get(sym, hard_cap))

    by_sym = {e["symbol"]: e for e in pool}
    buy = {e["symbol"]: e["conviction"] for e in pool
           if e["conviction"] in tc._POINTS}
    points = {sym: tc._POINTS[conv] for sym, conv in buy.items()}
    caps = {sym: cap_for(sym) for sym in buy}
    weights = _size_weights(points, caps, invested)

    changes: list[dict] = []
    sized_total = 0.0
    for sym in sorted(buy):
        entry = by_sym[sym]
        low, high = tc._band_from_weight(weights.get(sym, 0.0))
        low, high = _clamp_to_pin(pins, sym, low, high)
        sized_total += (low + high) / 2.0
        existing = existing_targets.get(sym)
        action = "modify_target" if existing else "add_target"
        changes.append(_change(entry, low=low, high=high, rule=tc._RULE[buy[sym]],
                               action=action, existing=existing, aliases=aliases))

    # Avoid-rated names: trim held positions toward their existing band (or drop).
    trim_count = drop_count = 0
    for sym in sorted(by_sym):
        entry = by_sym[sym]
        if entry["conviction"] != "avoid":
            continue
        cur = entry.get("held_pct") or 0.0
        if cur <= 0:
            continue  # not held and not wanted -> no target needed
        pin = pins.get(sym)
        challenges = bool(pin and str(pin.get("stance") or "") in tc._OWN_STANCES)
        existing = existing_targets.get(sym)
        if drop_avoid and not pin:
            changes.append(_change(entry, low=0.0, high=0.0, rule="avoid",
                                   action="remove_target", existing=existing, aliases=aliases))
            drop_count += 1
            continue
        if isinstance(existing, dict) and isinstance(existing.get("high"), (int, float)):
            low = _round1(max(0.0, float(existing.get("low") or 0.0)))
            high = _round1(float(existing["high"]))
        else:
            low, high = 0.0, _round1(max(tc.MIN_BAND_WIDTH, cur * 0.5))
        if pin:
            low, high = _clamp_to_pin(pins, sym, low, high)
        changes.append(_change(entry, low=low, high=high, rule="trim_only",
                               action="modify_target" if existing else "add_target",
                               existing=existing, aliases=aliases, challenges_pin=challenges))
        trim_count += 1

    book = tc._book_reconciliation(model, changes)
    book["cash_target_pct"] = _round1(cash)
    book["available_pct"] = _round1(100.0 - book["targeted_mid_pct"] - cash)
    book["over_allocated"] = book["available_pct"] < -0.01
    meta = {
        "cash_target_pct": _round1(cash),
        "invested_budget_pct": _round1(invested),
        "per_name_cap": _round1(per_name_cap),
        "concentration_pct": _round1(concentration_pct),
        "sized_midpoint_total_pct": _round1(sized_total),
        "buy_count": len(buy),
        "trim_count": trim_count,
        "drop_count": drop_count,
        "pinned_count": sum(1 for c in changes if c["symbol"] in pins),
        "challenges_pins": sorted(c["symbol"] for c in changes if c.get("challenges_pin")),
        "book_reconciliation": book,
    }
    return changes, meta


# --------------------------------------------------------------------------- #
# Optional LLM synthesis (hybrid brain)
# --------------------------------------------------------------------------- #
def _build_pool_prompt(blocks: list[str]) -> str:
    body = "\n\n".join(blocks)
    if len(body) > _POOL_PROMPT_LIMIT:
        body = body[:_POOL_PROMPT_LIMIT] + "\n…[truncated]"
    return f"""You are sizing a whole investment book. Below is the candidate pool: every name, with how much we currently hold, its sleeve, and the latest research narrative we have on it (when any). For EACH ticker, judge how strongly the evidence supports OWNING it as a long-term position right now.

Return ONLY a JSON object mapping each ticker to an object with:
- "conviction": one of "high", "medium", "low", "avoid" ("avoid" = the evidence argues against owning / says trim or sell)
- "rationale": one short sentence grounded in the narrative
- "conflict": (optional) one short sentence ONLY if this name's thesis conflicts with another in the pool (e.g. two mutually-exclusive bets, or an overlap that double-counts a risk)

Do not invent tickers outside the pool. Do not add prose outside the JSON.

POOL:
{body}
"""


def llm_pool_convictions(pool: list[dict], *, cfg: dict | None = None,
                         progress: Callable[[str], None] | None = None,
                         cancel: Callable[[], bool] | None = None) -> dict[str, dict]:
    """Ask the configured backend for a whole-pool conviction read, grounded in
    each name's latest narrative. Returns ``{}`` on any failure (no backend, bad
    JSON, cancel) so the caller keeps the deterministic convictions. Mirrors
    ``target_construct.llm_convictions`` but pool-wide and narrative-grounded."""
    if not pool:
        return {}
    cfg = cfg or ticker_analysis.load_config()
    if not any(ticker_analysis.available_backends().values()):
        return {}
    if progress:
        progress("reading conviction across the pool…")
    blocks: list[str] = []
    for e in pool:
        sym = e["symbol"]
        nar = research_brief.latest_narrative(sym)
        excerpt = ""
        if nar and nar.get("text"):
            excerpt = " ".join(str(nar["text"]).split())[:_PER_NAME_EXCERPT]
        held = e.get("held_pct")
        header = f"{sym} (held {held if held is not None else 0}% · sleeve {e.get('sleeve') or '—'})"
        blocks.append(f"{header}:\n{excerpt or '(no local narrative — judge from general knowledge, conservatively)'}")
    prompt = _build_pool_prompt(blocks)
    res = ticker_analysis._run_with_fallback(prompt, cfg, progress, cancel, label="optimizer-synthesis")
    if not res.get("ok"):
        return {}
    parsed = ticker_analysis._extract_json_object(res.get("report") or "")
    if not isinstance(parsed, dict):
        return {}
    valid = {e["symbol"] for e in pool}
    out: dict[str, dict] = {}
    for sym, node in parsed.items():
        key = str(sym).upper().strip()
        if key not in valid:
            continue
        if isinstance(node, str):
            node = {"conviction": node}
        if not isinstance(node, dict):
            continue
        conviction = str(node.get("conviction") or "").lower().strip()
        if conviction not in _EXPLICIT:
            continue
        out[key] = {
            "conviction": conviction,
            "rationale": str(node.get("rationale") or "").strip(),
            "conflict": str(node.get("conflict") or "").strip(),
        }
    return out


def _apply_llm_convictions(pool: list[dict], llm: dict[str, dict]) -> None:
    """Overlay the LLM's conviction read onto the pool in place. The deterministic
    read stays as the floor for any name the LLM didn't rate."""
    for e in pool:
        node = llm.get(e["symbol"])
        if not node:
            continue
        e["conviction"] = node["conviction"]
        e["conviction_source"] = "llm"
        if node.get("rationale"):
            e["rationale"] = node["rationale"]
        if node.get("conflict"):
            e["conflict"] = node["conflict"]


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def optimize(*, cash_target_pct: float | None = None,
             per_name_cap: float = DEFAULT_PER_NAME_CAP,
             concentration_pct: float = DEFAULT_CONCENTRATION_PCT,
             include_curious: bool = True, drop_avoid: bool = False,
             exclude: set[str] | None = None, use_llm: bool = False,
             model: dict | None = None, holdings: dict | None = None,
             basket_items: list[dict] | None = None,
             cfg: dict | None = None,
             progress: Callable[[str], None] | None = None,
             cancel: Callable[[], bool] | None = None) -> dict:
    """Build the pool, size it, and return a ``schema_version: 2`` proposal ready
    for ``stage_changes``/``preview_plan_for_proposal``. ``exclude`` drops names
    from the pool before sizing (the UI's per-row "exclude" tier)."""
    model = model if model is not None else (_load(TARGET_MODEL_JSON) or {})
    if cash_target_pct is None:
        cash_target_pct = float(model.get("cash_target_pct") or DEFAULT_CASH_TARGET_PCT)
    exclude = {str(s).upper() for s in (exclude or set())}

    pool = build_pool(model=model, holdings=holdings, basket_items=basket_items,
                      include_curious=include_curious)
    pool = [e for e in pool if e["symbol"] not in exclude]

    synth_source = "deterministic"
    if use_llm:
        llm = llm_pool_convictions(pool, cfg=cfg, progress=progress, cancel=cancel)
        if llm:
            _apply_llm_convictions(pool, llm)
            synth_source = "llm"

    changes, meta = size_pool(pool, model, cash_target_pct=cash_target_pct,
                              per_name_cap=per_name_cap,
                              concentration_pct=concentration_pct, drop_avoid=drop_avoid)
    meta["included_curious"] = bool(include_curious)
    meta["pool_size"] = len(pool)
    meta["excluded"] = sorted(exclude)
    meta["synthesis"] = synth_source

    findings: list[dict] = []
    # Surface any cross-name conflicts the LLM flagged, so the user sees them at
    # the same gate as the over-allocation / pin warnings.
    for e in pool:
        if e.get("conflict"):
            findings.append({"level": "FYI", "symbol": e["symbol"],
                             "message": f"{e['symbol']}: {e['conflict']}"})
    for ch in changes:
        if ch.get("challenges_pin"):
            findings.append({"level": "WARN", "symbol": ch["symbol"],
                             "message": f"{ch['symbol']}: pinned (standing intent) but rated avoid — review before accepting."})
        if ch.get("sleeve_unknown"):
            findings.append({"level": "FYI", "symbol": ch["symbol"],
                             "message": f"{ch['symbol']}: sleeve '{ch.get('sleeve')}' is not in sleeve-aliases.json (kept as-is)."})
    book = meta["book_reconciliation"]
    if book.get("over_allocated"):
        findings.append({"level": "WARN", "symbol": None,
                         "message": (f"book over-allocated: midpoints would total {book.get('targeted_mid_pct')}% "
                                     f"+ {book.get('cash_target_pct')}% cash; raise cash, tighten caps, or drop names.")})

    return {
        "schema_version": 2,
        "kind": "optimizer",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "status": "draft",
        "message": ("Whole-book optimization. Every band is a proposal, not an "
                    "allocation change — review before staging."),
        "changes": changes,
        "optimizer_meta": meta,
        "findings": findings,
        "blocked_symbols": [],
        "pool": pool,
    }
