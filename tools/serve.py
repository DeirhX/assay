#!/usr/bin/env python3
"""Local, interactive research server for the rebalancing app.

Serves the single-page UI in ``web/`` and a small JSON API that runs the
on-demand deep-dive puller. Stdlib only -- no Flask, no FastAPI, no pip install,
no wheel roulette on bleeding-edge Python. Just::

    py -3 tools/serve.py
    # then open http://127.0.0.1:6060

Design notes / honest caveats:
* Binds to 127.0.0.1 only. This is a single-user local tool, not a web service.
  It happily runs code (network pulls) on request; do not expose it.
* Pulls are synchronous per request. A single ticker is a few seconds; a full
  segment is ~20 sequential pulls, so expect 30-60s. The server is threaded, so
  the UI stays responsive meanwhile.
* Secrets (FMP key) come from the environment / secrets.env, never from disk in
  this repo.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import (  # noqa: E402
    ANALYSIS_DIR, DEEP_DIR, HOLDINGS_JSON, REPO_ROOT, RESEARCH_DIR,
    SEGMENT_DEF_DIR, SEGMENT_OUT_DIR, TARGET_MODEL_JSON, WEB_DIR,
)

WEB_DIST = WEB_DIR / "dist"  # Vite build output; served in prod when present
# Must match pplx_deep_research.default_profile_dir(): the automation worker uses
# a dedicated profile so it never fights the MCP browser for the profile lock.
DEFAULT_PPLX_PROFILE_DIR = Path.home() / ".cursor" / "pplx-automation-profile"
ROOT_STATIC_SUFFIXES = {".html", ".css", ".js"}

from portfolio import holdings_payload, holdings_weights  # noqa: E402
from providers import yahoo  # noqa: E402
import instruments  # noqa: E402
import research_pull  # noqa: E402
import review_deep_research  # noqa: E402
import ticker_analysis  # noqa: E402
import rebalance  # noqa: E402
import risk  # noqa: E402
import tax_lots  # noqa: E402
import whatif  # noqa: E402
import journal  # noqa: E402
import jobs  # noqa: E402
import orchestrate  # noqa: E402  -- durable state machine for the guided strategy run
import hygiene  # noqa: E402  -- shared worst_severity for the research overlay
import errorlog  # noqa: E402
from peer_stats import _peer_stats  # noqa: E402  -- dossier peer-percentile math
import price_levels  # noqa: E402  -- locked per-symbol buy-below/trim-above triggers
from symbols import (  # noqa: E402  -- symbol resolve/alias/search (clean public names)
    aliases as _symbol_aliases, annotate_record as _annotate_symbol_record,
    candidates as _symbol_candidates, resolve_symbol as _resolve_symbol,
    save_alias as _save_symbol_alias, search as _symbol_search,
)
from segments_service import (  # noqa: E402  -- segment validate/prompt/list
    freshness_directive as _freshness_directive,
    segment_path as _segment_path, segment_prompt as _segment_prompt,
    segments_list as _segments_list, start_draft as _start_segment_draft,
    validate_definition as _validate_segment_definition,
)
from holdings_sync import (  # noqa: E402  -- read-only IBKR Flex sync (thin handlers below)
    _history_payload, _ibkr_status, _regenerate_site, _save_ibkr_secrets,
    _start_history_sync, _start_holdings_sync, _start_sectors_sync,
)
from target_model import _apply_target_proposal  # noqa: E402  -- target-model apply
from deep_runs import (  # noqa: E402  -- Deep Research run artifacts (list/save/delete)
    _delete_deep_run, _deep_runs, _save_deep_artifact,
)
from analysis_jobs import (  # noqa: E402  -- single-ticker analysis + the two Q&A thread families
    drop_qa_exchange as _drop_qa_exchange, latest_analysis as _latest_analysis,
    load_deep_qa as _load_deep_qa, load_qa as _load_qa,
    deep_qa_path as _deep_qa_path, qa_path as _qa_path,
    start_analysis as _start_analysis, start_deep_qa as _start_deep_qa,
    start_qa as _start_qa,
)
from browser_jobs import (  # noqa: E402  -- Perplexity auth + deep-research/login/import jobs
    get_auth_state as _get_auth_state, start_deep_research as _start_deep_research,
    start_import as _start_import, start_login as _start_login,
    verify_login as _verify_login,
)
from strategy_service import (  # noqa: E402  -- guided Direction->Rebalance run gates
    approve_strategy_proposal as _approve_strategy_proposal,
    approve_strategy_segment as _approve_strategy_segment,
    start_strategy as _start_strategy,
)
from ibkr_portfolio import load_env_file as _read_env_file  # noqa: E402  -- one KEY=VALUE parser
from trade_service import (  # noqa: E402  -- gated live-trading service (thin handlers below)
    _trade_cancel, _trade_orders, _trade_place, _trade_preview, _trade_status,
)
# Disk + identifier helpers and the job registry now live in their own modules;
# alias them so the rest of this file's call sites stay unchanged.
from store import (  # noqa: E402
    load as _load, write_json as _write_json,
    slugify as _slugify, safe_symbol as _safe_symbol,
)
from jobs import (  # noqa: E402
    any_active as _any_active_deep_job, active_count as _active_browser_count,
    max_slots as _max_browser_slots,
)


# HTTP error vocabulary (apierror): handlers and the services they call `raise`
# an outcome; _dispatch() maps each _HttpError to its .status. _BadRequest is
# used by _read_body; the Conflict/Forbidden/BadGateway outcomes are raised by
# the service modules that own that work, not by serve directly anymore.
from apierror import (  # noqa: E402
    BadRequest as _BadRequest, HttpError as _HttpError,
)


# Selectable chart windows -> (Yahoo range, interval). Short windows use intraday
# bars; longer windows step to a coarser interval so we don't ship thousands of
# daily points for a 10y view. (Yahoo has no "1w" range; 5d covers a trading week.)
PRICE_HISTORY_RANGES: dict[str, tuple[str, str]] = {
    "1d": ("1d", "5m"),
    "1w": ("5d", "30m"),
    "1mo": ("1mo", "1d"),
    "3mo": ("3mo", "1d"),
    "6mo": ("6mo", "1d"),
    "1y": ("1y", "1d"),
    "2y": ("2y", "1wk"),
    "5y": ("5y", "1wk"),
    "max": ("max", "1mo"),
}

from research_pull import PULL_LOCK as _PULL_LOCK  # noqa: E402  -- shared pull lock

# The deep-research / login / analysis job registry lives in jobs.py; concurrent
# browser runs are bounded by jobs.claim_active / jobs.release_active (a counting
# limit, default PPLX_MAX_CONCURRENT=3), each on its own cloned Chrome profile.
# The browser-backed services (browser_jobs.py) own the slot claims and the
# jobs.slots_busy_msg wording; serve only reports the live counts in /setup.
JOBS_LIST_LIMIT = 100  # cap the central Task Center feed (newest first)

# Dev live-reload. Off unless started with --reload. _BOOT_TOKEN is recomputed
# each time the process (re)starts, so the browser can tell an API restart apart
# from a no-op poll and reload itself even when no static asset changed.
_RELOAD = False
_BOOT_TOKEN = f"{time.time():.3f}"
# Let the strategy state machine recognize runs orphaned by a restart: any guided
# run still parked in a "running" state from a previous _BOOT_TOKEN had its worker
# thread killed by that restart and must be reaped instead of spun on forever.
orchestrate.set_boot_token(_BOOT_TOKEN)

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}


def _setup_status(*, run_checks: bool = False) -> dict:
    return {
        "llm": ticker_analysis.setup_status(run_checks=run_checks),
        "perplexity": _get_auth_state(),
        "ibkr": _ibkr_status(),
        "data": _data_status(),
        "environment": {
            "sec_user_agent": bool(os.environ.get("SEC_USER_AGENT")),
            "fmp_api_key": bool(os.environ.get("FMP_API_KEY")),
            "pplx_profile_dir": os.environ.get("PPLX_PROFILE_DIR") or str(DEFAULT_PPLX_PROFILE_DIR),
            "pplx_max_concurrent": _max_browser_slots(),
            "pplx_browsers_active": _active_browser_count(),
        },
    }


def _data_status() -> dict:
    holdings = _load(HOLDINGS_JSON)
    model = _load(TARGET_MODEL_JSON)
    positions = holdings.get("positions") if isinstance(holdings, dict) else []
    has_holdings = isinstance(positions, list) and len(positions) > 0
    has_model = isinstance(model, dict) and bool(model)
    return {
        "ready": bool(has_holdings and has_model),
        "holdings": {
            "exists": HOLDINGS_JSON.exists(),
            "positions": len(positions) if isinstance(positions, list) else 0,
        },
        "target_model": {
            "exists": TARGET_MODEL_JSON.exists(),
        },
        "empty": not (has_holdings and has_model),
    }


def _ticker_deep_prompt(symbol: str) -> dict:
    """Single-name Deep Research prompt: the expensive, on-demand counterpart to
    the cheap per-ticker CLI analysis. Subject is one company, not a segment, so
    the stem is namespaced ``ticker-<sym>`` and it reuses the same Perplexity
    run/save/Q&A machinery without polluting the segment list. The FORMAT block
    mirrors the segment prompt so the scraper's JSON guard and citation handling
    behave identically."""
    sym = (symbol or "").strip().upper()
    if not _TICKER_SHAPE.match(sym):
        raise ValueError(f"not a recognisable ticker: {symbol!r}")
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    weight = holdings_weights().get(sym)
    prompt = (
        f"Deep research on {sym} as a long-term investment, as of {today}.\n"
        "Cover, with evidence: what the business does and how it earns money; the "
        "most recent quarterly results and management guidance; valuation versus "
        "its own history and its closest peers; competitive positioning and moat; "
        "growth drivers and near-term catalysts; the main risks and red flags; and "
        "an explicit bull case and bear case over the next 6-24 months.\n"
        f"Identify {sym}'s closest public peers and compare them head-to-head; call "
        f"out any peer that looks more attractive than {sym} on its own merits.\n"
        "End with a clear portfolio stance \u2014 accumulate, hold, trim, or avoid "
        "\u2014 and the specific evidence that would flip that stance.\n"
        "Include source citations and distinguish facts from opinion. Call out which "
        "numeric claims need deterministic verification.\n"
        "On first mention of any public company, append its primary exchange ticker "
        "with a $ prefix, e.g. 'ServiceNow ($NOW)'. Include a peer comparison table "
        "with a 'Ticker' column.\n"
        "Do not ask clarifying questions; if anything is ambiguous, state your "
        "assumptions and proceed.\n"
        "FORMAT: write a prose report in Markdown with section headings, paragraphs, "
        "bullet lists, and Markdown tables. Do NOT return the answer as a JSON object "
        "or array, and do NOT wrap the whole response in a code block. Structured "
        "data belongs in Markdown tables, not JSON.\n"
    )
    prompt += _freshness_directive(today)
    if weight is not None:
        prompt += (
            f"\nFor context only (do not let it bias your conclusion), I currently "
            f"hold {sym} at {weight:.2f}% of my invested book.\n"
        )
    return {"segment": f"ticker-{sym.lower()}", "symbol": sym, "date": today, "prompt": prompt}


# Only these keys belong in a target-model entry; the synthesis engine carries
# extra metadata (conviction, sleeve, rationale) on a change that must never be
# written into the model itself.
# Research-overlay classification: how a thesis verdict leans. Used only to flag
# when the deterministic band action and the human thesis disagree -- never to
# size a trade. Thesis actions are free text from the dossier form, so the match
# is loose and lowercased.
_THESIS_ADD_LIKE = {"add", "accumulate", "buy", "build", "increase", "overweight"}
_THESIS_TRIM_LIKE = {"trim", "sell", "reduce", "exit", "avoid", "underweight", "do_not_add"}


def _research_conflict(row_action: str | None, thesis_action: str | None) -> bool:
    """True when the band's suggested action and the thesis verdict point opposite
    ways: trimming a name the thesis wants more of, or buying one it wants less
    of. Anything else (no thesis, agreement, a neutral hold/wait) is not a
    conflict."""
    ta = (thesis_action or "").lower().strip()
    if not ta:
        return False
    if row_action == "trim" and ta in _THESIS_ADD_LIKE:
        return True
    if row_action == "buy" and ta in _THESIS_TRIM_LIKE:
        return True
    return False


def _research_overlay(provider_sym: str) -> dict | None:
    """Compact, independent research context for one rebalance row, read from the
    per-ticker dossier. Returns None when there's no dossier so the row reads as
    'no signal'. Nothing here feeds the trade math -- it is decision support
    only. ``research_score`` is deliberately omitted (it is segment-only, not on
    per-ticker files)."""
    rec = _load(RESEARCH_DIR / f"{provider_sym}.json")
    if not isinstance(rec, dict):
        return None
    thesis = rec.get("thesis") if isinstance(rec.get("thesis"), dict) else {}
    momentum = rec.get("momentum") if isinstance(rec.get("momentum"), dict) else {}
    return {
        "as_of": rec.get("as_of"),
        "data_quality": hygiene.worst_severity(rec.get("cross_checks") or []),
        "decision": rec.get("decision"),
        "momentum_3m_pct": momentum.get("chg_3m_pct"),
        "thesis_action": (thesis.get("action") or "").strip() or None,
        "thesis_summary": (thesis.get("summary") or "").strip() or None,
        "thesis_as_of": thesis.get("as_of"),
    }


def _mark_price_map(holdings: dict | None) -> dict[str, dict]:
    """symbol -> {price, currency} from the holdings snapshot's marks. The same
    marks the plan was sized against, so the price gate compares apples to
    apples (instrument currency)."""
    out: dict[str, dict] = {}
    for p in (holdings or {}).get("positions") or []:
        sym = str(p.get("symbol") or "").strip().upper()
        price = p.get("mark_price")
        if sym and isinstance(price, (int, float)) and price:
            out[sym] = {"price": float(price), "currency": (p.get("currency") or "").upper()}
    return out


def _gate_current_price(name: str, provider_sym: str, price_map: dict[str, dict]) -> float | None:
    """Current instrument price for the gate: the holdings mark first (what the
    plan was sized on), else the dossier's last spot. None when neither knows."""
    held = price_map.get((name or "").upper())
    if held and isinstance(held.get("price"), (int, float)):
        return float(held["price"])
    rec = _load(RESEARCH_DIR / f"{provider_sym}.json")
    if isinstance(rec, dict):
        pv = rec.get("price")
        if isinstance(pv, dict) and isinstance(pv.get("value"), (int, float)):
            return float(pv["value"])
    return None


def _apply_price_gate(row: dict, provider_sym: str, price_map: dict[str, dict]) -> None:
    """Attach a ``price_gate`` to a target row from its locked level (if any) and
    the current price, and GRADE the suggested move by how much of the ladder the
    price currently unlocks:

    * fraction 0  -> nothing triggered yet: downgrade the action to ``"wait"``.
    * 0 < f < 1   -> some tranches live: keep the action but scale the band's
      suggested delta down to the live fraction (``full_*`` keeps the original).
    * fraction 1  -> fully unlocked: act on the whole band-implied delta.

    The weight band still sets the *target* delta; the ladder decides how much of
    it to act on now, and at what price. No level -> nothing changes."""
    level = price_levels.get(provider_sym)
    if not level:
        return
    current = _gate_current_price(row.get("name") or "", provider_sym, price_map)
    gate = price_levels.evaluate(level, current)
    if not gate:
        return
    action = row.get("action")
    side = "buy" if action == "buy" else ("trim" if action == "trim" else None)
    total = gate["buy_total"] if side == "buy" else (gate["trim_total"] if side == "trim" else 0)
    if side and gate["price_known"] and total:
        fraction = gate["buy_fraction"] if side == "buy" else gate["trim_fraction"]
        gate["applied_fraction"] = fraction
        if fraction <= 0:
            gate["blocked_action"] = side
            row["action"] = "wait"
        elif fraction < 1.0:
            gate["partial"] = True
            for key in ("suggest_delta_pct", "suggest_delta_czk"):
                val = row.get(key)
                if isinstance(val, (int, float)):
                    gate["full_" + key] = val
                    row[key] = round(val * fraction, 2) if key.endswith("pct") else round(val * fraction)
    row["price_gate"] = gate


def _attach_research_overlay(plan: dict, holdings: dict | None = None) -> None:
    """Enrich each held target row of a rebalance plan, in place, with a compact
    ``research`` object + a ``research_conflict`` flag, and a ``price_gate`` from
    any locked price level (downgrading the action to ``"wait"`` when the price
    isn't favorable yet). Best-effort: a missing/malformed dossier or level is
    skipped silently so the planner always renders."""
    price_map = _mark_price_map(holdings)
    for row in plan.get("rows") or []:
        if row.get("kind") != "target" or not row.get("held"):
            continue
        try:
            provider_sym = _resolve_symbol(row.get("name") or "")
        except Exception:  # noqa: BLE001 - a bad symbol shouldn't break the plan
            continue
        try:
            overlay = _research_overlay(provider_sym)
        except Exception:  # noqa: BLE001 - the overlay is optional; never break the plan
            overlay = None
        if overlay:
            row["research"] = overlay
            row["research_conflict"] = _research_conflict(row.get("action"), overlay["thesis_action"])
        try:
            _apply_price_gate(row, provider_sym, price_map)
        except Exception:  # noqa: BLE001 - gating is decision support, never fatal
            pass


_TICKER_SHAPE = re.compile(r"^[A-Z][A-Z0-9.]{0,5}$")


def _known_tickers() -> list[str]:
    """Curated universe of symbols we actually know about: pulled research
    dossiers, held positions, and segment members. The UI uses this to decide
    which bare uppercase tokens in a report are safe to turn into deep-dive
    links -- a small, relevant set beats the full US/EU universe, which collides
    badly with English words (NOW, ON, ALL, IT...)."""
    syms: set[str] = set()

    def add(value) -> None:
        if isinstance(value, dict):
            value = value.get("symbol")
        if not value:
            return
        s = str(value).strip().upper()
        if _TICKER_SHAPE.match(s):
            syms.add(s)

    for path in RESEARCH_DIR.glob("*.json"):
        add(path.stem)
    holdings = _load(HOLDINGS_JSON) or {}
    for pos in (holdings.get("positions") or []):
        add(pos)
    for src_dir in (SEGMENT_DEF_DIR, SEGMENT_OUT_DIR):
        for path in src_dir.glob("*.json"):
            data = _load(path) or {}
            for member in (data.get("members") or data.get("symbols") or []):
                add(member)
    return sorted(syms)


def _verdict_line(report: str) -> str | None:
    """The one-liner under an analysis '## Verdict' heading (stance + confidence
    + justification), stripped of markdown. Used as the recents-list summary."""
    if not report:
        return None
    lines = report.splitlines()
    for i, line in enumerate(lines):
        if re.match(r"^#{1,6}\s+verdict\b", line.strip(), re.I):
            buf: list[str] = []
            for nxt in lines[i + 1:]:
                s = nxt.strip()
                if not s:
                    if buf:
                        break
                    continue
                if re.match(r"^#{1,6}\s", s):
                    break
                buf.append(s)
            text = re.sub(r"\*\*?|`", "", " ".join(buf)).strip()
            return text or None
    return None


def _ticker_index() -> list[dict]:
    """Every ticker we have material on -- a pulled dossier and/or a saved CLI
    analysis -- with timestamps. The UI merges this with the browser's local
    view-history to offer a quick "jump back to a ticker" list. Server-side so a
    fresh browser still sees the tickers of interest, not an empty list."""
    out: dict[str, dict] = {}
    for path in RESEARCH_DIR.glob("*.json"):
        rec = _load(path) or {}
        sym = path.stem.upper()
        out[sym] = {
            "symbol": sym,
            "name": rec.get("name") or sym,
            "type": instruments.classify(
                sym,
                quote_type=rec.get("quote_type") or rec.get("instrument_type"),
                profile=rec.get("profile"),
            ),
            "as_of": rec.get("as_of"),
            "analyzed_at": None,
            "has_analysis": False,
        }
    for path in ANALYSIS_DIR.glob("*.meta.json"):
        meta = _load(path) or {}
        sym = (meta.get("symbol") or "").upper()
        if not sym:
            continue
        row = out.setdefault(sym, {
            "symbol": sym, "name": sym, "type": instruments.OTHER, "as_of": None,
            "analyzed_at": None, "has_analysis": False,
        })
        ts = meta.get("generated_at")
        if ts and (not row["analyzed_at"] or ts > row["analyzed_at"]):
            row["analyzed_at"] = ts
        row["has_analysis"] = True
    # Attach the verdict one-liner from each analyzed ticker's latest note so the
    # recents list summarizes the call (Accumulate/Hold/Trim/Avoid + why).
    for row in out.values():
        if row.get("has_analysis"):
            latest = _latest_analysis(row["symbol"])
            if latest:
                vl = _verdict_line(latest.get("report") or "")
                if vl:
                    row["verdict"] = vl
    return sorted(out.values(), key=lambda r: r["symbol"])


def _is_root_static_file(clean: str) -> bool:
    path = Path(clean)
    return (
        len(path.parts) == 1
        and path.suffix in ROOT_STATIC_SUFFIXES
        and (REPO_ROOT / clean).is_file()
    )


# ---- dev live-reload ------------------------------------------------------
def _assets_version() -> str:
    """Opaque token that changes whenever a served asset changes OR the server
    restarts. The browser reloads when this differs from what it last saw."""
    latest = 0.0
    for p in WEB_DIR.rglob("*"):
        if p.is_file():
            m = p.stat().st_mtime
            if m > latest:
                latest = m
    for p in REPO_ROOT.iterdir():  # root mini-site assets (site.css, *.html)
        if p.is_file() and p.suffix in ROOT_STATIC_SUFFIXES:
            m = p.stat().st_mtime
            if m > latest:
                latest = m
    return f"{latest:.3f}-{_BOOT_TOKEN}"


def _server_sources() -> list[Path]:
    """Python files whose edits warrant restarting the API process."""
    return sorted((REPO_ROOT / "tools").glob("*.py"))


def _reload_watcher() -> None:
    """Child-side watcher. When server code changes, exit with code 3 so the
    supervisor respawns a fresh process. Guards: never restart on code that fails
    to compile (keep serving the last good version), and never interrupt an
    in-flight Deep Research run (defer the exit until it ends)."""
    mtimes = {p: p.stat().st_mtime for p in _server_sources() if p.exists()}
    pending = False
    waited = False
    while True:
        time.sleep(1.0)
        for p in _server_sources():
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            if mtimes.get(p) == m:
                continue
            mtimes[p] = m
            try:
                compile(p.read_text(encoding="utf-8"), str(p), "exec")
            except SyntaxError as exc:
                sys.stderr.write(f"[reload] {p.name}: syntax error, staying on current code ({exc.msg} line {exc.lineno})\n")
                continue
            sys.stderr.write(f"[reload] {p.name} changed\n")
            pending = True
        if not pending:
            continue
        if _any_active_deep_job():
            if not waited:
                sys.stderr.write("[reload] change pending; holding restart until deep-research job(s) finish\n")
                waited = True
            continue
        sys.stderr.write("[reload] restarting to apply changes\n")
        sys.stderr.flush()
        os._exit(3)


def _run_reloader() -> int:
    """Supervisor (parent). Runs the server as a child and respawns it whenever
    the child exits with code 3 (a requested reload). Keeps a stable PID and the
    console, so Ctrl+C and stdout behave normally across reloads -- unlike execv,
    which on Windows detaches into a new, console-less process."""
    import subprocess

    child_env = dict(os.environ, _REBAL_RELOAD_CHILD="1")
    argv = [sys.executable, *sys.argv]
    print("[reload] supervisor watching tools/*.py — edits restart the API in place")
    while True:
        proc = subprocess.Popen(argv, env=child_env)
        try:
            code = proc.wait()
        except KeyboardInterrupt:
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
            return 0
        if code == 3:
            continue  # requested reload -> respawn with the new code
        return code  # clean exit or crash -> stop supervising


# Generous ceiling for JSON POST bodies (Deep Research reports run to a few
# hundred KB); anything bigger is a bug or abuse, not a legitimate request.
_MAX_BODY_BYTES = 5 * 1024 * 1024


# --------------------------------------------------------------------------- #
# Declarative API route tables. Each entry maps a path to a Handler method name;
# do_GET/do_POST resolve an exact match first, then the longest matching prefix.
# Adding an endpoint is a one-line table edit plus a handler method, instead of
# growing a 100-line if/elif chain. Prefix handlers read their own tail (symbol,
# segment, stem) off the path exactly as the old inline branches did.
# --------------------------------------------------------------------------- #
_GET_EXACT = {
    "/api/dev/livereload": "_get_livereload",
    "/api/holdings": "_get_holdings",
    "/api/portfolio-history": "_get_portfolio_history",
    "/api/ibkr/status": "_get_ibkr_status",
    "/api/rebalance": "_get_rebalance",
    "/api/risk": "_get_risk",
    "/api/journal": "_get_journal",
    "/api/segments": "_get_segments",
    "/api/peer-stats": "_get_peer_stats",
    "/api/price-levels": "_get_price_levels",
    "/api/deep-runs": "_get_deep_runs",
    "/api/error-log": "_get_error_log",
    "/api/tickers": "_get_tickers",
    "/api/ticker-index": "_get_ticker_index",
    "/api/analysis-config": "_get_analysis_config",
    "/api/setup/status": "_get_setup_status",
    "/api/analysis-models": "_get_analysis_models",
    "/api/trade/status": "_get_trade_status",
    "/api/trade/orders": "_get_trade_orders",
    "/api/deep-research/login-status": "_get_login_status",
    "/api/deep-job": "_get_deep_job",
    "/api/jobs": "_get_jobs",
    "/api/deep-prompt": "_get_deep_prompt",
    "/api/deep-qa": "_get_deep_qa",
    "/api/target-model": "_get_target_model",
    "/api/symbol-aliases": "_get_symbol_aliases",
    "/api/symbol-search": "_get_symbol_search",
    "/api/strategy/runs": "_get_strategy_runs",
}
_GET_PREFIX = [
    ("/api/strategy/", "_get_strategy"),
    ("/api/segment-def/", "_get_segment_def"),
    ("/api/deep-run/", "_get_deep_run"),
    ("/api/research/", "_get_research"),
    ("/api/analysis/", "_get_analysis"),
    ("/api/qa/", "_get_qa"),
    ("/api/history/", "_get_history"),
    ("/api/price-history/", "_get_price_history"),
    ("/api/segment/", "_get_segment"),
]
_POST_EXACT = {
    "/api/segment-draft": "_post_segment_draft",
    "/api/price-levels/lock": "_post_price_level_lock",
    "/api/price-levels/clear": "_post_price_level_clear",
    "/api/strategy/start": "_post_strategy_start",
    "/api/holdings/sync": "_post_holdings_sync",
    "/api/portfolio-history/sync": "_post_portfolio_history_sync",
    "/api/portfolio-history/sectors": "_post_portfolio_history_sectors",
    "/api/site/regenerate": "_post_site_regenerate",
    "/api/deep-job/cancel": "_post_deep_job_cancel",
    "/api/deep-run/delete": "_post_deep_run_delete",
    "/api/deep-qa": "_post_deep_qa",
    "/api/error-log": "_post_error_log",
    "/api/analysis-config": "_post_analysis_config",
    "/api/setup/check": "_post_setup_check",
    "/api/setup/ibkr": "_post_setup_ibkr",
    "/api/deep-research/save": "_post_deep_save",
    "/api/deep-research/run": "_post_deep_run",
    "/api/deep-research/login": "_post_deep_login",
    "/api/deep-research/import": "_post_deep_import",
    "/api/deep-research/verify-login": "_post_verify_login",
    "/api/deep-research/review": "_post_review",
    "/api/target-proposal/apply": "_post_proposal_apply",
    "/api/history/delete": "_post_history_delete",
    "/api/tax-plan": "_post_tax_plan",
    "/api/whatif": "_post_whatif",
    "/api/trade/preview": "_post_trade_preview",
    "/api/trade/place": "_post_trade_place",
    "/api/trade/cancel": "_post_trade_cancel",
    "/api/journal": "_post_journal",
    "/api/journal/outcome": "_post_journal_outcome",
    "/api/symbol-alias": "_post_symbol_alias",
    "/api/symbol-candidates": "_post_symbol_candidates",
}
_POST_PREFIX = [
    ("/api/strategy/", "_post_strategy_action"),
    ("/api/segment-def/", "_post_segment_def"),
    ("/api/analyze/", "_post_analyze"),
    ("/api/qa/", "_post_qa"),
    ("/api/pull-segment/", "_post_pull_segment"),
    ("/api/pull/", "_post_pull"),
    ("/api/thesis/", "_post_thesis"),
]
# Match the most specific (longest) prefix first so e.g. /api/pull-segment/ wins
# over /api/pull/ regardless of table order.
_GET_PREFIX.sort(key=lambda kv: -len(kv[0]))
_POST_PREFIX.sort(key=lambda kv: -len(kv[0]))


class Handler(BaseHTTPRequestHandler):
    server_version = "rebalancing-research/1.0"

    # ---- helpers -----------------------------------------------------------
    def _send_json(self, payload, status: int = 200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status: int, message: str):
        self._send_json({"error": message}, status=status)

    def _dispatch(self, run):
        """Invoke a route handler, mapping our HTTP-error vocabulary to a JSON
        error envelope so individual handlers don't each repeat the try/except.
        An _HttpError carries its own status; a bare ValueError is the common
        validation case (400). Anything else propagates to _handle_unexpected."""
        try:
            return run()
        except _HttpError as exc:
            return self._send_error_json(exc.status, str(exc))
        except ValueError as exc:
            return self._send_error_json(400, str(exc))

    def _handle_unexpected(self, exc: Exception):
        # Single funnel for unexpected handler failures (GET and POST): log the
        # full traceback to the terminal so we can actually debug, but hand the
        # browser a clean JSON envelope the frontend's error center understands.
        if isinstance(exc, research_pull.ProviderError):  # type: ignore[attr-defined]
            # An upstream data source (Yahoo/SEC/FMP) misbehaved -- that's an
            # expected external hiccup (and "ticker not found" lives here too),
            # not one of our incidents, so it stays out of the error log.
            return self._send_error_json(502, f"data source error: {exc}")
        sys.stderr.write(f"[serve] unhandled error on {self.command} {self.path}:\n")
        traceback.print_exc()
        errorlog.error("server", f"{type(exc).__name__}: {exc}",
                       request=f"{self.command} {self.path}")
        return self._send_error_json(500, f"{type(exc).__name__}: {exc}")

    def _serve_static(self, rel: str):
        if rel in ("", "/"):
            rel = "index.html"
        clean = rel.lstrip("/")
        if clean.startswith("web/"):
            target = (WEB_DIR / clean.removeprefix("web/")).resolve()
            allowed_root = WEB_DIR
        elif _is_root_static_file(clean):
            target = (REPO_ROOT / clean).resolve()
            allowed_root = REPO_ROOT
        else:
            # Prefer the Vite build (web/dist) when it exists; fall back to raw
            # web/ source otherwise. The entry is TypeScript (web/src/main.ts),
            # so the raw fallback only fully works via `npm run dev`; serving
            # the SPA directly from this server requires `npm run build`.
            base = WEB_DIST if (WEB_DIST / "index.html").is_file() else WEB_DIR
            target = (base / clean).resolve()
            allowed_root = base
        if allowed_root not in target.parents and target != allowed_root:
            return self._send_error_json(403, "forbidden")
        if not target.is_file():
            return self._send_error_json(404, f"not found: {rel}")
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", _CONTENT_TYPES.get(target.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        # Localhost dev tool: never cache static assets, so an edit + refresh (or
        # the live-reload) always shows the latest code instead of a stale copy.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > _MAX_BODY_BYTES:
            raise _BadRequest(f"request body too large ({length} bytes; max {_MAX_BODY_BYTES})")
        if not length:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            # A silent {} here used to turn client bugs into confusing downstream
            # "missing field" behavior; fail the request loudly instead.
            raise _BadRequest(f"malformed JSON body: {exc}") from exc
        if not isinstance(data, dict):
            raise _BadRequest("JSON body must be an object")
        return data

    def log_message(self, fmt, *args):  # quieter, single-line logs
        sys.stderr.write(f"  {self.address_string()} {fmt % args}\n")

    # ---- routing -----------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return None
        try:
            if not path.startswith("/api/"):
                return self._serve_static(path)
            return self._handle_get_api(path, parse_qs(parsed.query))
        except Exception as exc:  # noqa: BLE001
            return self._handle_unexpected(exc)

    def _handle_get_api(self, path: str, query: dict[str, list[str]]):
        name = _GET_EXACT.get(path)
        if name is None:
            for prefix, handler in _GET_PREFIX:
                if path.startswith(prefix):
                    name = handler
                    break
        if name is None:
            return self._send_error_json(404, "unknown endpoint")
        return self._dispatch(lambda: getattr(self, name)(path, query))

    # ---- GET handlers (one per _GET_EXACT / _GET_PREFIX entry) -------------
    def _get_livereload(self, path, query):
        return self._send_json({"enabled": _RELOAD, "version": _assets_version()})

    def _get_holdings(self, path, query):
        return self._send_json(holdings_payload())

    def _get_portfolio_history(self, path, query):
        payload = _history_payload()
        if not payload:
            return self._send_error_json(404, "no portfolio history yet — pull it from IBKR (History tab)")
        return self._send_json(payload)

    def _get_ibkr_status(self, path, query):
        # Token-free credential status (see _ibkr_status); the History tab reads
        # history_configured to guide setup before a pull is attempted.
        return self._send_json(_ibkr_status())

    def _get_error_log(self, path, query):
        try:
            limit = int((query.get("limit") or ["200"])[0])
        except ValueError:
            limit = 200
        return self._send_json({"entries": errorlog.recent(max(1, min(limit, errorlog.MAX_ENTRIES)))})

    def _get_deep_qa(self, path, query):
        stem = (query.get("stem") or [""])[0]
        return self._send_json(_load_deep_qa(stem))

    def _get_rebalance(self, path, query):
        model = _load(TARGET_MODEL_JSON)
        holdings = _load(HOLDINGS_JSON)
        if not model:
            return self._send_error_json(404, "no target model — data/target-model.json missing")
        if not holdings:
            return self._send_error_json(404, "no holdings snapshot — sync from IBKR first")
        plan = tax_lots.enrich_plan(rebalance.plan(model, holdings), holdings)
        _attach_research_overlay(plan, holdings)
        return self._send_json(plan)

    def _get_risk(self, path, query):
        holdings = _load(HOLDINGS_JSON)
        if not holdings:
            return self._send_error_json(404, "no holdings snapshot — sync from IBKR first")
        rng_key = (query.get("range") or ["1y"])[0].lower()
        rng = rng_key if rng_key in PRICE_HISTORY_RANGES else "1y"
        with _PULL_LOCK:
            return self._send_json(risk.risk_report(holdings, rng=rng))

    def _get_journal(self, path, query):
        entries = journal.load_entries()
        price_map = journal.price_map_from_holdings(_load(HOLDINGS_JSON))
        return self._send_json({
            "entries": list(reversed(entries)),  # newest first for the UI
            "calibration": journal.calibrate(entries, price_map),
            "actions": sorted(journal.ACTIONS),
        })

    def _get_segments(self, path, query):
        return self._send_json({"segments": _segments_list()})

    def _get_peer_stats(self, path, query):
        sym = (query.get("symbol") or [""])[0]
        return self._send_json(_peer_stats(_resolve_symbol(sym)))

    def _get_price_levels(self, path, query):
        return self._send_json({"levels": price_levels.load_all()})

    def _get_strategy_runs(self, path, query):
        return self._send_json({"runs": orchestrate.list_runs()})

    def _get_strategy(self, path, query):
        run_id = path.rsplit("/", 1)[-1]
        run = orchestrate.load_run(run_id)
        if not run:
            return self._send_error_json(404, f"unknown strategy run {run_id}")
        # A run left "running" by a previous server process has a dead worker;
        # fail it now so the client stops polling a spinner that will never move.
        run = orchestrate.reap_if_orphaned(run)
        job = jobs.get_public(run["job_id"]) if run.get("job_id") else None
        return self._send_json(orchestrate.public(run, job=job))

    def _get_segment_def(self, path, query):
        name = path.rsplit("/", 1)[-1].lower()
        rec = _load(_segment_path(name))
        return self._send_json(rec) if rec else self._send_error_json(404, f"unknown segment {name}")

    def _get_deep_runs(self, path, query):
        return self._send_json({"runs": _deep_runs()})

    def _get_tickers(self, path, query):
        return self._send_json({"tickers": _known_tickers()})

    def _get_ticker_index(self, path, query):
        return self._send_json({"tickers": _ticker_index()})

    def _get_analysis_config(self, path, query):
        return self._send_json({
            "config": ticker_analysis.load_config(),
            "available": ticker_analysis.available_backends(),
            "labels": ticker_analysis.PROVIDER_LABELS,
        })

    def _get_setup_status(self, path, query):
        return self._send_json(_setup_status())

    def _get_analysis_models(self, path, query):
        force = (query.get("refresh") or ["0"])[0] in ("1", "true")
        return self._send_json({"models": ticker_analysis.provider_models(force=force)})

    def _get_trade_status(self, path, query):
        # Never errors: a down/locked gateway is a normal state the UI renders.
        return self._send_json(_trade_status())

    def _get_trade_orders(self, path, query):
        return self._send_json(_trade_orders())

    def _get_login_status(self, path, query):
        return self._send_json(_get_auth_state())

    def _get_deep_job(self, path, query):
        job_id = (query.get("id") or [""])[0]
        pub = jobs.get_public(job_id)
        if not pub:
            return self._send_error_json(404, f"unknown job {job_id}")
        return self._send_json(pub)

    def _get_jobs(self, path, query):
        # Central Task Center feed: every known job, newest first. Capped so a
        # long-lived dev session can't make the payload unbounded -- finished
        # jobs are never evicted from the in-memory registry.
        return self._send_json({"jobs": jobs.list_public()[:JOBS_LIST_LIMIT]})

    def _get_deep_prompt(self, path, query):
        ticker = (query.get("ticker") or [""])[0].strip()
        name = (query.get("segment") or [""])[0]
        if ticker:
            return self._send_json(_ticker_deep_prompt(ticker))
        return self._send_json(_segment_prompt(name))

    def _get_deep_run(self, path, query):
        stem = _slugify(path.rsplit("/", 1)[-1])
        payload = {"stem": stem}
        for suffix, rel in {
            "report": DEEP_DIR / f"{stem}.md",
            "sources": DEEP_DIR / f"{stem}.sources.json",
            "review": DEEP_DIR / f"{stem}.review.md",
            "proposal": DEEP_DIR / f"{stem}.target-proposal.json",
        }.items():
            if rel.exists():
                payload[suffix] = (
                    _load(rel) if rel.suffix == ".json" else rel.read_text(encoding="utf-8")
                )
        return self._send_json(payload)

    def _get_target_model(self, path, query):
        rec = _load(TARGET_MODEL_JSON)
        return self._send_json(rec) if rec else self._send_error_json(404, "target model not found")

    def _get_symbol_aliases(self, path, query):
        return self._send_json({"aliases": _symbol_aliases()})

    def _get_research(self, path, query):
        sym = _safe_symbol(unquote(path.rsplit("/", 1)[-1]))
        provider_sym = _resolve_symbol(sym)
        rec = _load(RESEARCH_DIR / f"{provider_sym}.json")
        return self._send_json(_annotate_symbol_record(rec, sym, provider_sym)) if rec else self._send_error_json(404, f"no cached research for {sym}")

    def _get_analysis(self, path, query):
        sym = _safe_symbol(unquote(path.rsplit("/", 1)[-1]))
        rec = _latest_analysis(_resolve_symbol(sym))
        return self._send_json(rec) if rec else self._send_error_json(404, f"no analysis for {sym}")

    def _get_qa(self, path, query):
        sym = _safe_symbol(unquote(path.rsplit("/", 1)[-1]))
        return self._send_json(_load_qa(_resolve_symbol(sym)))

    def _get_history(self, path, query):
        sym = _safe_symbol(unquote(path.rsplit("/", 1)[-1]))
        provider_sym = _resolve_symbol(sym)
        return self._send_json({"symbol": sym, "provider_symbol": provider_sym, "history": research_pull.history_for(provider_sym)})

    def _get_price_history(self, path, query):
        sym = _safe_symbol(unquote(path.rsplit("/", 1)[-1]))
        provider_sym = _resolve_symbol(sym)
        rng_key = (query.get("range") or ["1y"])[0].lower()
        rng, interval = PRICE_HISTORY_RANGES.get(rng_key, PRICE_HISTORY_RANGES["1y"])
        try:
            result = yahoo.chart(provider_sym, rng=rng, interval=interval)
            ph = yahoo.price_history_from_chart(result, rng=rng, interval=interval)
        except Exception as exc:  # noqa: BLE001 - surface provider failure to UI
            return self._send_error_json(502, f"price history failed for {sym}: {exc}")
        if not ph:
            return self._send_error_json(404, f"no price history for {sym}")
        return self._send_json(ph)

    def _get_segment(self, path, query):
        name = path.rsplit("/", 1)[-1].lower()
        rec = _load(SEGMENT_OUT_DIR / f"{name}.json")
        return self._send_json(rec) if rec else self._send_error_json(404, f"no cached segment {name}")

    def _get_symbol_search(self, path, query):
        q = (query.get("q") or [""])[0]
        return self._send_json(_symbol_search(q))

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            return self._handle_post_api(path)
        except Exception as exc:  # noqa: BLE001
            return self._handle_unexpected(exc)

    def _handle_post_api(self, path: str):
        name = _POST_EXACT.get(path)
        if name is None:
            for prefix, handler in _POST_PREFIX:
                if path.startswith(prefix):
                    name = handler
                    break
        if name is None:
            return self._send_error_json(404, "unknown endpoint")
        return self._dispatch(lambda: getattr(self, name)(path))

    # ---- POST handlers (one per _POST_EXACT / _POST_PREFIX entry) ----------
    def _post_segment_draft(self, path):
        body = self._read_body()
        return self._send_json(_start_segment_draft(str(body.get("query") or "")))

    def _post_price_level_lock(self, path):
        # Keyed by provider symbol so the rebalance overlay and trade desk can
        # look it up the same way they resolve dossier/holdings symbols.
        body = self._read_body()
        sym = _resolve_symbol(_safe_symbol(str(body.get("symbol") or "")))
        entry = price_levels.lock(
            sym,
            fair_value=body.get("fair_value"),
            buy_ladder=body.get("buy_ladder"),
            trim_ladder=body.get("trim_ladder"),
            buy_below=body.get("buy_below"),
            trim_above=body.get("trim_above"),
            currency=str(body.get("currency") or ""),
            source=body.get("source") if isinstance(body.get("source"), dict) else None,
        )
        return self._send_json({"level": entry})

    def _post_price_level_clear(self, path):
        body = self._read_body()
        sym = _resolve_symbol(_safe_symbol(str(body.get("symbol") or "")))
        return self._send_json(price_levels.clear(sym))

    def _post_strategy_start(self, path):
        body = self._read_body()
        return self._send_json(_start_strategy(str(body.get("direction") or "")))

    def _post_strategy_action(self, path):
        # /api/strategy/{run_id}/{action}
        parts = path.strip("/").split("/")
        if len(parts) != 4:
            return self._send_error_json(404, "unknown strategy action")
        run_id, action = parts[2], parts[3]
        body = self._read_body()
        if action == "approve-segment":
            return self._send_json(_approve_strategy_segment(run_id, body.get("definition")))
        if action == "approve-proposal":
            return self._send_json(_approve_strategy_proposal(
                run_id, body.get("changes"), allow_blocked=bool(body.get("allow_blocked"))))
        return self._send_error_json(404, f"unknown strategy action {action}")

    def _post_segment_def(self, path):
        name = _slugify(path.rsplit("/", 1)[-1])
        body = self._read_body()
        definition = _validate_segment_definition(body.get("definition") or body)
        _write_json(SEGMENT_DEF_DIR / f"{name}.json", definition)
        return self._send_json({"name": name, "definition": definition, "segments": _segments_list()})

    def _post_holdings_sync(self, path):
        return self._send_json(_start_holdings_sync())

    def _post_portfolio_history_sync(self, path):
        full = bool(self._read_body().get("full"))
        return self._send_json(_start_history_sync(full=full))

    def _post_portfolio_history_sectors(self, path):
        return self._send_json(_start_sectors_sync())

    def _post_deep_qa(self, path):
        body = self._read_body()
        stem = _slugify(str(body.get("stem") or ""))
        if body.get("clear"):
            _write_json(_deep_qa_path(stem), {"stem": stem, "turns": []})
            return self._send_json(_load_deep_qa(stem))
        if "delete" in body:
            thread = _load_deep_qa(stem)
            if _drop_qa_exchange(thread, body.get("delete")):
                _write_json(_deep_qa_path(stem), thread)
            return self._send_json(_load_deep_qa(stem))
        return self._send_json(_start_deep_qa(stem, str(body.get("question") or "")))

    def _post_deep_run_delete(self, path):
        body = self._read_body()
        return self._send_json(_delete_deep_run(str(body.get("stem") or "")))

    def _post_error_log(self, path):
        body = self._read_body()
        if body.get("clear"):
            errorlog.clear()
        return self._send_json({"entries": errorlog.recent()})

    def _post_site_regenerate(self, path):
        res = _regenerate_site()
        if not res.get("ok"):
            return self._send_error_json(400, res.get("error") or "regeneration failed")
        return self._send_json(res)

    def _post_analyze(self, path):
        sym = _safe_symbol(unquote(path.rsplit("/", 1)[-1]))
        body = self._read_body()
        return self._send_json(_start_analysis(_resolve_symbol(sym), bool(body.get("refresh"))))

    def _post_qa(self, path):
        sym = _safe_symbol(unquote(path.rsplit("/", 1)[-1]))
        provider_sym = _resolve_symbol(sym)
        body = self._read_body()
        if body.get("clear"):
            _write_json(_qa_path(provider_sym), {"symbol": provider_sym, "turns": []})
            return self._send_json(_load_qa(provider_sym))
        if "delete" in body:
            thread = _load_qa(provider_sym)
            if _drop_qa_exchange(thread, body.get("delete")):
                _write_json(_qa_path(provider_sym), thread)
            return self._send_json(_load_qa(provider_sym))
        return self._send_json(_start_qa(provider_sym, str(body.get("question") or "")))

    def _post_deep_job_cancel(self, path):
        body = self._read_body()
        job_id = str(body.get("id") or "").strip()
        if not job_id:
            return self._send_error_json(400, "missing job id")
        ok = jobs.cancel_job(job_id)
        return self._send_json({"id": job_id, "cancelled": ok})

    def _post_analysis_config(self, path):
        body = self._read_body()
        return self._send_json({
            "config": ticker_analysis.save_config(body.get("config") or body),
            "available": ticker_analysis.available_backends(),
            "labels": ticker_analysis.PROVIDER_LABELS,
        })

    def _post_setup_check(self, path):
        return self._send_json(_setup_status(run_checks=True))

    def _post_setup_ibkr(self, path):
        body = self._read_body()
        try:
            return self._send_json(_save_ibkr_secrets(body))
        except ValueError as exc:
            return self._send_error_json(400, str(exc))
        except OSError as exc:
            return self._send_error_json(500, f"could not write secrets: {exc}")

    def _post_deep_save(self, path):
        body = self._read_body()
        return self._send_json(_save_deep_artifact(body))

    def _post_deep_run(self, path):
        body = self._read_body()
        return self._send_json(_start_deep_research(body))

    def _post_deep_login(self, path):
        return self._send_json(_start_login())

    def _post_deep_import(self, path):
        body = self._read_body()
        return self._send_json(_start_import(body))

    def _post_verify_login(self, path):
        return self._send_json(_verify_login())

    def _post_review(self, path):
        body = self._read_body()
        segment = str(body.get("segment") or "")
        date = str(body.get("date") or "")
        if not segment or not date:
            return self._send_error_json(400, "segment and date are required")
        # review() raises SystemExit (a BaseException) when the report or
        # segment definition is missing. That is NOT caught by do_POST's
        # `except Exception`, so it would kill the worker thread and return
        # nothing to the browser. Translate it into a clean 400 instead.
        try:
            return self._send_json(review_deep_research.review(segment, date))
        except SystemExit as exc:
            return self._send_error_json(400, str(exc) or "missing report for this segment + date")

    def _post_proposal_apply(self, path):
        body = self._read_body()
        return self._send_json(_apply_target_proposal(
            str(body.get("segment") or ""),
            str(body.get("date") or ""),
            bool(body.get("confirm")),
            allow_blocked=bool(body.get("allow_blocked")),
        ))

    def _post_history_delete(self, path):
        body = self._read_body()
        sym = _safe_symbol(str(body.get("symbol") or ""))
        provider_sym = _resolve_symbol(sym)
        removed = research_pull.delete_history(provider_sym, str(body.get("stamp") or ""))
        return self._send_json({
            "symbol": sym,
            "removed": removed,
            "history": research_pull.history_for(provider_sym),
        })

    def _post_tax_plan(self, path):
        body = self._read_body()
        holdings = _load(HOLDINGS_JSON)
        if not holdings:
            return self._send_error_json(404, "no holdings snapshot — sync from IBKR first")
        try:
            sym = _safe_symbol(str(body.get("symbol") or ""))
        except ValueError as exc:
            return self._send_error_json(400, str(exc))
        try:
            amount = float(body.get("amount_czk"))
        except (TypeError, ValueError):
            return self._send_error_json(400, "amount_czk must be a number")
        return self._send_json(tax_lots.breakdown_for_symbol(holdings, sym, amount))

    def _post_whatif(self, path):
        body = self._read_body()
        holdings = _load(HOLDINGS_JSON)
        model = _load(TARGET_MODEL_JSON)
        if not holdings or not model:
            return self._send_error_json(404, "need both a holdings snapshot and a target model")
        return self._send_json(whatif.simulate(holdings, model, body.get("trades")))

    def _post_trade_preview(self, path):
        return self._send_json(_trade_preview(self._read_body()))

    def _post_trade_place(self, path):
        return self._send_json(_trade_place(self._read_body()))

    def _post_trade_cancel(self, path):
        return self._send_json(_trade_cancel(self._read_body()))

    def _post_journal(self, path):
        body = self._read_body()
        journal.add_entry(body)
        entries = journal.load_entries()
        price_map = journal.price_map_from_holdings(_load(HOLDINGS_JSON))
        return self._send_json({
            "entries": list(reversed(entries)),
            "calibration": journal.calibrate(entries, price_map),
            "actions": sorted(journal.ACTIONS),
        })

    def _post_journal_outcome(self, path):
        body = self._read_body()
        journal.record_outcome(str(body.get("id") or ""), body.get("price"), str(body.get("note") or ""))
        entries = journal.load_entries()
        price_map = journal.price_map_from_holdings(_load(HOLDINGS_JSON))
        return self._send_json({
            "entries": list(reversed(entries)),
            "calibration": journal.calibrate(entries, price_map),
            "actions": sorted(journal.ACTIONS),
        })

    def _post_symbol_alias(self, path):
        body = self._read_body()
        return self._send_json(_save_symbol_alias(body))

    def _post_symbol_candidates(self, path):
        body = self._read_body()
        return self._send_json(_symbol_candidates(body))

    def _post_pull(self, path):
        try:
            sym = _safe_symbol(unquote(path.rsplit("/", 1)[-1]))
        except ValueError:
            return self._send_error_json(400, "bad symbol")
        provider_sym = _resolve_symbol(sym)
        with _PULL_LOCK:
            rec = research_pull.pull_ticker(provider_sym)
        return self._send_json(_annotate_symbol_record(rec, sym, provider_sym))

    def _post_pull_segment(self, path):
        name = path.rsplit("/", 1)[-1].lower()
        if not (SEGMENT_DEF_DIR / f"{name}.json").exists():
            return self._send_error_json(404, f"unknown segment {name}")
        with _PULL_LOCK:
            rec = research_pull.pull_segment(name)
        return self._send_json(rec)

    def _post_thesis(self, path):
        sym = _safe_symbol(unquote(path.rsplit("/", 1)[-1]))
        provider_sym = _resolve_symbol(sym)
        rec = _load(RESEARCH_DIR / f"{provider_sym}.json")
        if not rec:
            return self._send_error_json(404, f"pull {sym} before saving a thesis")
        body = self._read_body()
        import datetime as dt
        rec["thesis"] = {
            "summary": body.get("summary", ""),
            "action": body.get("action", ""),
            "drivers": body.get("drivers", []),
            "downside_triggers": body.get("downside_triggers", []),
            "source_confidence": body.get("source_confidence", ""),
            "review_after": body.get("review_after", ""),
            "source_artifact": body.get("source_artifact", ""),
            "as_of": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        }
        (RESEARCH_DIR / f"{provider_sym}.json").write_text(
            json.dumps(rec, indent=2) + "\n", encoding="utf-8"
        )
        return self._send_json(_annotate_symbol_record(rec, sym, provider_sym))


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address; loopback only (the API has no auth and can write credentials)")
    parser.add_argument("--port", type=int, default=6060)
    parser.add_argument("--reload", action="store_true",
                        help="dev: auto-restart on tools/*.py edits and live-reload the browser on asset changes")
    args = parser.parse_args()

    # In --reload mode the first invocation is the supervisor; it re-launches
    # itself as a child (marked via env) that actually serves and self-restarts.
    if args.reload and os.environ.get("_REBAL_RELOAD_CHILD") != "1":
        return _run_reloader()

    # Hard refusal, not a warning: every endpoint is unauthenticated, several
    # write to disk (target model, IBKR credentials) or spawn browser/CLI jobs.
    # Exposing that beyond loopback is indistinguishable from a remote shell.
    if args.host not in ("127.0.0.1", "::1", "localhost"):
        print(f"ERROR: refusing to bind non-loopback host {args.host!r}.", file=sys.stderr)
        print("  This server has no authentication and can write credentials and", file=sys.stderr)
        print("  portfolio targets. Run it on 127.0.0.1 and use a tunnel if remote", file=sys.stderr)
        print("  access is genuinely needed.", file=sys.stderr)
        return 2

    global _RELOAD
    _RELOAD = args.reload

    _load_secrets_env()

    from portfolio import DATA_MISSING_HINT, data_initialized
    if not data_initialized():
        print("  WARNING: " + DATA_MISSING_HINT.rstrip().replace("\n", "\n  "))
        print("  The UI will load but holdings/target views will be empty until then.")

    if not (WEB_DIST / "index.html").is_file():
        print("  WARNING: web/dist/ is missing -- the console UI will NOT load from this server.")
        print("  The SPA entry is TypeScript and needs a build. Either:")
        print("    npm run build   (then this server serves web/dist)")
        print("    npm run dev     (Vite dev server on :5173, proxying API calls here)")

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print(f"Rebalancing research server on {url}  (Ctrl+C to stop)")
    print("  Static UI + JSON API. Localhost only. Pulls run live data sources.")
    if _RELOAD:
        print("  Dev reload ON: editing tools/*.py restarts the API; web/ + site.css edits reload the browser.")
        threading.Thread(target=_reload_watcher, daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        httpd.server_close()
    return 0


def _load_secrets_env():
    """Best-effort load of repo-root secrets.env (gitignored) for FMP_API_KEY."""
    from config import ROOT_SECRETS
    for key, value in _read_env_file(ROOT_SECRETS).items():
        os.environ.setdefault(key, value)


if __name__ == "__main__":
    raise SystemExit(main())
