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
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import (  # noqa: E402
    DEEP_DIR, HOLDINGS_JSON, REPO_ROOT, RESEARCH_DIR, ROOT_STATIC_SUFFIXES,
    SEGMENT_DEF_DIR, SEGMENT_OUT_DIR, TARGET_MODEL_JSON, WEB_DIR,
    set_secret as _set_secret,
)

WEB_DIST = WEB_DIR / "dist"  # Vite build output; served in prod when present
# Must match pplx_deep_research.default_profile_dir(): the automation worker uses
# a dedicated profile so it never fights the MCP browser for the profile lock.
DEFAULT_PPLX_PROFILE_DIR = Path.home() / ".cursor" / "pplx-automation-profile"

from portfolio import decision_label, holdings_payload, portfolio_context  # noqa: E402
from providers import yahoo  # noqa: E402
import research_pull  # noqa: E402
import review_deep_research  # noqa: E402
import ticker_analysis  # noqa: E402
import rebalance  # noqa: E402
import rebalance_routes  # noqa: E402
import execution_plan  # noqa: E402
import risk  # noqa: E402
import attribution  # noqa: E402  -- process attribution: actual TWR vs never-rebalanced / benchmark
import regime  # noqa: E402  -- descriptive macro strip over the segment leaderboard
import tax_lots  # noqa: E402
import tax_calendar  # noqa: E402  -- forward 3-year-exemption calendar (proactive tax lever)
import exit_plan  # noqa: E402  -- advisory graceful-exit planner (tax-timed scale-out)
import option_market  # noqa: E402  -- instrument-wide option quote refresh
import whatif  # noqa: E402
import journal  # noqa: E402
import jobs  # noqa: E402
import activity  # noqa: E402  -- durable JSONL feed of tickers viewed + tasks finished
import scheduler  # noqa: E402  -- read-only background freshness scheduler (off by default)
import orchestrate  # noqa: E402  -- durable state machine for the guided strategy run
import errorlog  # noqa: E402
from peer_stats import _peer_stats  # noqa: E402  -- dossier peer-percentile math
import price_levels  # noqa: E402  -- locked per-symbol buy-below/trim-above triggers
import quote_cache  # noqa: E402  -- fresh-quote + cached sparkline series (never fetches)
from symbols import (  # noqa: E402  -- symbol resolve/alias/search (clean public names)
    aliases as _symbol_aliases, annotate_record as _annotate_symbol_record,
    candidates as _symbol_candidates, resolve_symbol as _resolve_symbol,
    save_alias as _save_symbol_alias, search as _symbol_search,
)
from segments_service import (  # noqa: E402  -- segment validate/prompt/list
    leaderboard as _segments_leaderboard,
    segment_path as _segment_path, segment_prompt as _segment_prompt,
    segments_list as _segments_list, start_draft as _start_segment_draft,
    validate_definition as _validate_segment_definition,
)
from holdings_sync import (  # noqa: E402  -- read-only IBKR Flex sync (thin handlers below)
    history_payload as _history_payload, ibkr_status as _ibkr_status,
    save_ibkr_secrets as _save_ibkr_secrets,
    start_history_sync as _start_history_sync, start_holdings_sync as _start_holdings_sync,
    start_sectors_sync as _start_sectors_sync,
)
import holdings_live  # noqa: E402  -- read-only live-mark overlay (CPAPI) for the holdings view
import target_staging  # noqa: E402  -- staging layer: working draft + provenance + pins
import basket  # noqa: E402  -- cross-surface ticker shortlist (upstream of the working draft)
import overview  # noqa: E402  -- "Today" cockpit: pure lane summaries + next-step pick
import reconcile  # noqa: E402  -- ledger-vs-snapshot drift (pure)
import optimizer  # noqa: E402  -- whole-book global sizer over the candidate pool
from target_model import preview_plan_for_proposal as _preview_plan  # noqa: E402
from deep_runs import (  # noqa: E402  -- Deep Research run artifacts (list/save/delete)
    delete_deep_run as _delete_deep_run, deep_runs as _deep_runs,
    discovered_for as _discovered_for,
    save_deep_artifact as _save_deep_artifact,
)
from analysis_jobs import (  # noqa: E402  -- single-ticker analysis + the two Q&A thread families
    drop_qa_exchange as _drop_qa_exchange, latest_analysis as _latest_analysis,
    load_deep_qa as _load_deep_qa, load_qa as _load_qa,
    deep_qa_path as _deep_qa_path, qa_path as _qa_path,
    start_analysis as _start_analysis, start_deep_qa as _start_deep_qa,
    start_portfolio_review as _start_portfolio_review,
    start_qa as _start_qa,
)
from browser_jobs import (  # noqa: E402  -- Perplexity auth + deep-research/login/import jobs
    forget_integration as _forget_pplx,
    get_auth_state as _get_auth_state, start_deep_research as _start_deep_research,
    start_import as _start_import, start_login as _start_login,
    verify_login as _verify_login,
)
from strategy_service import (  # noqa: E402  -- guided Direction->Rebalance run gates
    approve_strategy_proposal as _approve_strategy_proposal,
    approve_strategy_segment as _approve_strategy_segment,
    start_basket_plan as _start_basket_plan,
    start_strategy as _start_strategy,
)
from rebalance_overlay import (  # noqa: E402  -- research overlay + price gate on plan rows
    attach_research_overlay as _attach_research_overlay,
)
from ibkr_portfolio import load_env_file as _read_env_file  # noqa: E402  -- one KEY=VALUE parser
import devreload  # noqa: E402  -- opt-in --reload supervisor / watcher / asset token
from ticker_directory import (  # noqa: E402  -- known-symbol universe, recents index, deep prompt
    known_tickers as _known_tickers, ticker_deep_prompt as _ticker_deep_prompt,
    ticker_index as _ticker_index,
)
from trade_service import (  # noqa: E402  -- gated live-trading service (thin handlers below)
    _trade_cancel, _trade_orders, _trade_peg_start, _trade_peg_stop,
    _trade_place, _trade_preview, _trade_quotes, _trade_reconnect, _trade_status,
    _trade_tickle, basket_state as _basket_state, load_basket as _load_basket,
    queue_working_conflicts as _queue_working_conflicts,
    remove_basket_leg as _remove_basket_leg,
    replace_stock_basket as _replace_stock_basket,
    review_basket as _review_basket, save_basket as _save_basket,
    set_basket_leg_included as _set_basket_leg_included,
    set_only_basket_legs_included as _set_only_basket_legs_included,
)
# Disk + identifier helpers and the job registry now live in their own modules;
# alias them so the rest of this file's call sites stay unchanged.
from store import (  # noqa: E402
    load as _load, write_json as _write_json,
    slugify as _slugify, safe_symbol as _safe_symbol,
)
from jobs import (  # noqa: E402
    active_count as _active_browser_count, max_slots as _max_browser_slots,
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
        "automation": {"enabled": scheduler.enabled(), "tasks": scheduler.task_status()},
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


def _is_root_static_file(clean: str) -> bool:
    path = Path(clean)
    return (
        len(path.parts) == 1
        and path.suffix in ROOT_STATIC_SUFFIXES
        and (REPO_ROOT / clean).is_file()
    )


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
def _exit_cfg_from_query(query: dict) -> dict:
    """Pull the exit-planner knobs out of a GET query string, ignoring anything
    non-numeric so a fat-fingered param falls back to the module defaults."""
    cfg: dict[str, float | int] = {}
    for key, cast in (("horizon_days", int), ("adv_slice_pct", float),
                      ("near_exempt_days", int), ("tax_rate", float),
                      ("default_tranches", int)):
        raw = (query.get(key) or [None])[0]
        if raw is None:
            continue
        try:
            cfg[key] = cast(raw)
        except (TypeError, ValueError):
            pass
    return cfg


_GET_EXACT = {
    "/api/dev/livereload": "_get_livereload",
    "/api/holdings": "_get_holdings",
    "/api/holdings/live": "_get_holdings_live",
    "/api/overview": "_get_overview",
    "/api/portfolio-history": "_get_portfolio_history",
    "/api/ibkr/status": "_get_ibkr_status",
    "/api/rebalance": "_get_rebalance",
    "/api/rebalance/route": "_get_rebalance_route",
    "/api/execution-plan": "_get_execution_plan",
    "/api/exit-plan": "_get_exit_plan",
    "/api/risk": "_get_risk",
    "/api/attribution": "_get_attribution",
    "/api/regime": "_get_regime",
    "/api/tax-calendar": "_get_tax_calendar",
    "/api/journal": "_get_journal",
    "/api/segments": "_get_segments",
    "/api/segments/leaderboard": "_get_segments_leaderboard",
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
    "/api/trade/tickle": "_get_trade_tickle",
    "/api/trade/orders": "_get_trade_orders",
    "/api/trade/quotes": "_get_trade_quotes",
    "/api/trade/basket": "_get_trade_basket",
    "/api/trade/queue-conflicts": "_get_trade_queue_conflicts",
    "/api/deep-research/login-status": "_get_login_status",
    "/api/deep-job": "_get_deep_job",
    "/api/jobs": "_get_jobs",
    "/api/activity": "_get_activity",
    "/api/deep-prompt": "_get_deep_prompt",
    "/api/deep-qa": "_get_deep_qa",
    "/api/target-model": "_get_target_model",
    "/api/target-model/restore-preview": "_get_restore_preview",
    "/api/symbol-aliases": "_get_symbol_aliases",
    "/api/symbol-search": "_get_symbol_search",
    "/api/strategy/runs": "_get_strategy_runs",
    "/api/staging": "_get_staging",
    "/api/basket": "_get_basket",
    "/api/optimizer": "_get_optimizer",
    "/api/spark": "_get_spark",
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
    "/api/deep-job/cancel": "_post_deep_job_cancel",
    "/api/activity/view": "_post_activity_view",
    "/api/deep-run/delete": "_post_deep_run_delete",
    "/api/deep-qa": "_post_deep_qa",
    "/api/error-log": "_post_error_log",
    "/api/analysis-config": "_post_analysis_config",
    "/api/setup/check": "_post_setup_check",
    "/api/setup/ibkr": "_post_setup_ibkr",
    "/api/setup/automation": "_post_setup_automation",
    "/api/setup/perplexity/forget": "_post_forget_perplexity",
    "/api/deep-research/save": "_post_deep_save",
    "/api/deep-research/run": "_post_deep_run",
    "/api/deep-research/login": "_post_deep_login",
    "/api/deep-research/import": "_post_deep_import",
    "/api/deep-research/verify-login": "_post_verify_login",
    "/api/deep-research/review": "_post_review",
    "/api/target-proposal/apply": "_post_proposal_apply",
    "/api/staging/commit": "_post_staging_commit",
    "/api/staging/discard": "_post_staging_discard",
    "/api/target-model/restore": "_post_restore_target",
    "/api/staging/edit": "_post_staging_edit",
    "/api/basket/add": "_post_basket_add",
    "/api/basket/tier": "_post_basket_tier",
    "/api/basket/remove": "_post_basket_remove",
    "/api/basket/clear": "_post_basket_clear",
    "/api/basket/draft-plan": "_post_basket_draft_plan",
    "/api/optimizer/run": "_post_optimizer_run",
    "/api/optimizer/stage": "_post_optimizer_stage",
    "/api/portfolio-review": "_post_portfolio_review",
    "/api/history/delete": "_post_history_delete",
    "/api/rebalance/funding": "_post_rebalance_funding",
    "/api/rebalance/stage": "_post_rebalance_stage_routes",
    "/api/execution-plan": "_post_execution_plan",
    "/api/tax-plan": "_post_tax_plan",
    "/api/exit-plan/refresh-options": "_post_exit_plan_refresh_options",
    "/api/exit-plan/stage": "_post_exit_plan_stage",
    "/api/whatif": "_post_whatif",
    "/api/trade/reconnect": "_post_trade_reconnect",
    "/api/trade/preview": "_post_trade_preview",
    "/api/trade/place": "_post_trade_place",
    "/api/trade/cancel": "_post_trade_cancel",
    "/api/trade/peg": "_post_trade_peg",
    "/api/trade/peg/stop": "_post_trade_peg_stop",
    "/api/trade/basket": "_post_trade_basket",
    "/api/trade/basket/review": "_post_trade_basket_review",
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
        if isinstance(exc, research_pull.ProviderError):
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
        return self._send_json({"enabled": _RELOAD, "version": devreload.assets_version(_BOOT_TOKEN)})

    def _get_holdings(self, path, query):
        return self._send_json(holdings_payload())

    def _get_holdings_live(self, path, query):
        # Best-effort live-mark overlay: the delayed Flex snapshot stays the base
        # (tax lots, cost, country); this refreshes equity marks from the CPAPI
        # gateway when it's authenticated. Never an error — {available:false} just
        # tells the UI to keep showing the delayed snapshot.
        snapshot = _load(HOLDINGS_JSON)
        if not snapshot:
            return self._send_json({"available": False, "reason": "no snapshot yet"})
        try:
            res = holdings_live.refresh_marks(snapshot)
        except Exception as exc:  # noqa: BLE001 -- overlay must never break the view
            return self._send_json({"available": False, "reason": f"{type(exc).__name__}"})
        if not res:
            return self._send_json({"available": False, "reason": "live gateway unavailable"})
        return self._send_json(res)

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
        # Plan against the working draft when one exists so the drift, suggested
        # trades, and what-if reflect what the user is editing (clearly labelled
        # in the UI); otherwise the live model, exactly as before.
        has_draft = target_staging.has_draft()
        model = target_staging.active_model()
        holdings = _load(HOLDINGS_JSON)
        if not model:
            return self._send_error_json(404, "no target model — data/target-model.json missing")
        if not holdings:
            return self._send_error_json(404, "no holdings snapshot — sync from IBKR first")
        plan = tax_lots.enrich_plan(rebalance.plan(model, holdings), holdings)
        _attach_research_overlay(plan, holdings)
        # Provenance + working-draft flag so the planner can badge each band's
        # source (legacy/stale vs research-derived vs pinned) and show a banner
        # when uncommitted changes are sitting in the working draft.
        plan["provenance"] = model.get("provenance") or {}
        plan["staged"] = {
            "has_draft": has_draft,
            "previewing_draft": has_draft,
            "pending": target_staging.diff_staged_vs_live()["counts"]["total"] if has_draft else 0,
        }
        execution_plan.reconcile_queue(_basket_state().get("trades") or [])
        plan["execution_plan"] = execution_plan.state_for_plan(plan)
        return self._send_json(plan)

    def _get_execution_plan(self, path, query):
        execution_plan.reconcile_queue(_basket_state().get("trades") or [])
        model = target_staging.active_model()
        holdings = _load(HOLDINGS_JSON)
        if model and holdings:
            plan = tax_lots.enrich_plan(rebalance.plan(model, holdings), holdings)
            return self._send_json(execution_plan.state_for_plan(plan))
        return self._send_json(execution_plan.load_plan())

    def _get_rebalance_route(self, path, query):
        holdings = _load(HOLDINGS_JSON)
        if not holdings:
            return self._send_error_json(404, "no holdings snapshot — sync from IBKR first")
        symbol = str((query.get("symbol") or [""])[0]).strip()
        try:
            delta_czk = float((query.get("delta_czk") or [""])[0])
            route = rebalance_routes.build_route(holdings, symbol, delta_czk)
        except (TypeError, ValueError) as exc:
            return self._send_error_json(400, str(exc))
        return self._send_json(route)

    def _get_overview(self, path, query):
        # The "Today" cockpit: lane summaries + one next-step recommendation,
        # composed from the same stores every other view reads. Tolerant of a
        # first run (no holdings / no model) — sections degrade, never 404.
        now = dt.datetime.now(dt.timezone.utc)
        holdings = _load(HOLDINGS_JSON)
        model = target_staging.active_model()
        snap = overview.snapshot_summary(holdings, now=now)

        plan_sum = None
        if model and snap["exists"]:
            plan = rebalance.plan(model, holdings)
            _attach_research_overlay(plan, holdings)
            plan_sum = overview.plan_summary(plan)

        has_draft = target_staging.has_draft()
        draft = {
            "has_draft": has_draft,
            "pending": target_staging.diff_staged_vs_live()["counts"]["total"] if has_draft else 0,
        }
        segs = _segments_list()
        seg_records = [rec for s in segs if s.get("cached")
                       and (rec := _load(SEGMENT_OUT_DIR / f"{s['name']}.json"))]
        # Ledger drift: does the execution ledger contain trades the snapshot
        # predates? Cheap read of the already-cached history; degrades to
        # "not checked" when history has never been pulled.
        drift = reconcile.drift_report(holdings, _history_payload())
        payload = {
            "generated_at": now.isoformat(timespec="seconds"),
            "snapshot": snap,
            "drift": drift,
            "plan": plan_sum,
            "draft": draft,
            "staged_basket": overview.staged_basket_summary(_load_basket()),
            "journal": overview.journal_summary(journal.load_entries(), now=now),
            # Read-only over the cached verdict (warmed when Attribution is opened) --
            # no network on the Today path. Absent/thin cache degrades to a nudge.
            "attribution": overview.attribution_summary(attribution.load_verdict(), now=now),
            "research": overview.research_summary(
                basket.enriched_items(), _ticker_index(), segs, seg_records, now=now),
            "automation": overview.automation_summary(
                scheduler.load_state(), scheduler.task_status(),
                enabled=scheduler.enabled(), now=now),
        }
        payload["next_step"] = overview.next_step(payload)
        return self._send_json(payload)

    def _get_exit_plan(self, path, query):
        # Advisory scale-out for the trim set (plans against the working draft when
        # one exists, exactly like the rebalance planner). Optional query params:
        # include=SYM,SYM (also exit these untargeted names), full=SYM,SYM (force
        # to zero), and the config knobs horizon_days/adv_slice_pct/near_exempt_days/
        # tax_rate. with_options=0 serves the fast tax/sale plan first; the UI
        # follows with the enriched option-route request.
        model = target_staging.active_model()
        holdings = _load(HOLDINGS_JSON)
        if not model:
            return self._send_error_json(404, "no target model — data/target-model.json missing")
        if not holdings:
            return self._send_error_json(404, "no holdings snapshot — sync from IBKR first")
        include = [s for s in (query.get("include") or [""])[0].split(",") if s.strip()]
        full = [s for s in (query.get("full") or [""])[0].split(",") if s.strip()]
        cfg = _exit_cfg_from_query(query)
        with_options = (
            str((query.get("with_options") or ["1"])[0]).strip().lower()
            not in {"0", "false", "no", "off"}
        )
        with _PULL_LOCK:
            plan = exit_plan.build_exit_plan(
                model, holdings, include=include, full_exit=full, cfg=cfg,
                with_options=with_options,
            )
        return self._send_json(plan)

    def _get_risk(self, path, query):
        holdings = _load(HOLDINGS_JSON)
        if not holdings:
            return self._send_error_json(404, "no holdings snapshot — sync from IBKR first")
        rng_key = (query.get("range") or ["1y"])[0].lower()
        rng = rng_key if rng_key in PRICE_HISTORY_RANGES else "1y"
        with _PULL_LOCK:
            return self._send_json(risk.risk_report(holdings, rng=rng))

    def _get_attribution(self, path, query):
        # Did the process earn its keep? Actual time-weighted return vs the
        # never-rebalanced and benchmark counterfactuals over the window --
        # flow-neutralized and FX-clean. Fetches benchmark/held-name prices on a
        # cold cache, so guard it with the shared pull lock.
        holdings = _load(HOLDINGS_JSON)
        history = _history_payload()
        rng_key = (query.get("range") or [attribution.DEFAULT_RANGE])[0].lower()
        rng = rng_key if rng_key in attribution.RANGE_DAYS else attribution.DEFAULT_RANGE
        benchmark = (query.get("benchmark") or [attribution.DEFAULT_BENCHMARK])[0]
        with _PULL_LOCK:
            report = attribution.attribution_report(history, holdings, rng=rng, benchmark=benchmark)
        # Warm the headline cache so the Today cockpit can show the verdict without
        # a network hop of its own. Best-effort; never fails the request.
        attribution.cache_verdict(report)
        return self._send_json(report)

    def _get_regime(self, path, query):
        # Descriptive macro backdrop (rates/credit/USD/vol) for the segment view.
        # Cached 6h in regime.py; network only on a cold cache, so guard it.
        with _PULL_LOCK:
            return self._send_json(regime.build_regime())

    def _get_tax_calendar(self, path, query):
        # Forward view of every lot's Czech 3-year exemption date: gain lots going
        # tax-free (wait) and loss lots whose harvest window is closing (act).
        # Pure over the snapshot's tax lots -- no network, so no _PULL_LOCK.
        holdings = _load(HOLDINGS_JSON)
        if not holdings:
            return self._send_error_json(404, "no holdings snapshot — sync from IBKR first")
        try:
            soon = int((query.get("soon_days") or ["60"])[0])
        except (TypeError, ValueError):
            soon = tax_calendar.SOON_DAYS
        return self._send_json(tax_calendar.build_calendar(holdings, soon_days=soon))

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

    def _get_segments_leaderboard(self, path, query):
        return self._send_json(_segments_leaderboard())

    def _get_peer_stats(self, path, query):
        sym = (query.get("symbol") or [""])[0]
        return self._send_json(_peer_stats(_resolve_symbol(sym)))

    def _get_price_levels(self, path, query):
        return self._send_json({"levels": price_levels.load_all()})

    def _get_strategy_runs(self, path, query):
        return self._send_json({"runs": orchestrate.list_runs()})

    def _get_staging(self, path, query):
        return self._send_json(target_staging.diff_staged_vs_live())

    def _get_basket(self, path, query):
        return self._send_json(basket.view())

    def _get_spark(self, path, query):
        # Batch sparkline series for a row-level trend cue. Cached-only: reads
        # dossier price history, never fetches. Symbols are resolved through the
        # alias map (display -> provider) and the response is keyed back by the
        # display symbol the caller asked for; unknown/thin names are omitted.
        raw = (query.get("symbols") or [""])[0]
        pairs: list[tuple[str, str]] = []
        for token in raw.split(","):
            display = _safe_symbol(token.strip())
            if display:
                pairs.append((display, _resolve_symbol(display)))
        series = quote_cache.spark_series([prov for _, prov in pairs])
        out = {display: series[prov] for display, prov in pairs if prov in series}
        return self._send_json({"spark": out})

    def _get_optimizer(self, path, query):
        # The candidate pool + the default constraints, with no sizing yet. The
        # client renders the pool table and the constraints panel from this, then
        # POSTs /api/optimizer/run to size it.
        include_curious = (query.get("include_curious") or ["1"])[0] not in ("0", "false", "")
        pool = optimizer.build_pool(basket_items=basket.pool_candidates(include_curious=True),
                                    include_curious=include_curious)
        model = _load(TARGET_MODEL_JSON) or {}
        return self._send_json({
            "pool": pool,
            "constraints": {
                "cash_target_pct": float(model.get("cash_target_pct") or optimizer.DEFAULT_CASH_TARGET_PCT),
                "per_name_cap": optimizer.DEFAULT_PER_NAME_CAP,
                "concentration_pct": optimizer.DEFAULT_CONCENTRATION_PCT,
                "min_position_pct": optimizer.DEFAULT_MIN_POSITION_PCT,
                "max_names": optimizer.DEFAULT_MAX_NAMES,
                "conviction_curve": optimizer.DEFAULT_CONVICTION_CURVE,
                "include_curious": include_curious,
            },
        })

    def _get_strategy(self, path, query):
        run_id = path.rsplit("/", 1)[-1]
        run = orchestrate.load_run(run_id)
        if not run:
            return self._send_error_json(404, f"unknown strategy run {run_id}")
        # A run left "running" by a previous server process has a dead worker;
        # fail it now so the client stops polling a spinner that will never move.
        run = orchestrate.reap_if_orphaned(run) or run
        jid = run.get("job_id")
        job = jobs.get_public(jid) if jid else None
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

    def _get_trade_tickle(self, path, query):
        # Keepalive for an open Trade view; returns live session booleans and
        # never errors (a dropped session is a normal state the UI reflects).
        return self._send_json(_trade_tickle())

    def _get_trade_orders(self, path, query):
        return self._send_json(_trade_orders())

    def _get_trade_quotes(self, path, query):
        # Live {last,bid,ask} per conid for the working-orders market cells,
        # fetched separately from the (already slow) orders list so that list can
        # paint before this ~2s market-snapshot round-trip returns.
        raw = (query.get("conids") or [""])[0]
        conids: list[int] = []
        for tok in raw.split(","):
            tok = tok.strip()
            if tok:
                try:
                    conids.append(int(tok))
                except ValueError:
                    pass
        return self._send_json({"quotes": _trade_quotes(conids)})

    def _get_trade_basket(self, path, query):
        # Include the content revision and projection-review status so the Trade
        # desk can fail closed when the queue changed after Target-state review.
        return self._send_json(_basket_state())

    def _get_trade_queue_conflicts(self, path, query):
        return self._send_json(_queue_working_conflicts())

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

    def _get_activity(self, path, query):
        # Durable, cross-restart feed for the Activity view: tickers viewed +
        # tasks finished, newest-first. Capped so the payload stays bounded.
        try:
            limit = int((query.get("limit") or ["400"])[0])
        except (TypeError, ValueError):
            limit = 400
        return self._send_json({"events": activity.recent(max(1, min(limit, 1000)))})

    def _get_deep_prompt(self, path, query):
        ticker = (query.get("ticker") or [""])[0].strip()
        name = (query.get("segment") or [""])[0]
        if ticker:
            return self._send_json(_ticker_deep_prompt(ticker))
        return self._send_json(_segment_prompt(name))

    def _get_deep_run(self, path, query):
        stem = _slugify(path.rsplit("/", 1)[-1])
        payload: dict[str, object] = {"stem": stem}
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
        # Names the report discusses beyond the segment's members -- starrable
        # into the optimizer pool. Best-effort; an empty list on a ticker run.
        payload["discovered_candidates"] = _discovered_for(stem)
        return self._send_json(payload)

    def _get_target_model(self, path, query):
        rec = _load(TARGET_MODEL_JSON)
        return self._send_json(rec) if rec else self._send_error_json(404, "target model not found")

    def _get_restore_preview(self, path, query):
        # What restoring a pre-apply backup would change, before the user commits
        # to it. ValueError (bad/missing backup) becomes a 400 via _dispatch.
        rel = (query.get("backup") or [""])[0]
        return self._send_json(target_staging.diff_backup_vs_live(rel))

    def _get_symbol_aliases(self, path, query):
        return self._send_json({"aliases": _symbol_aliases()})

    def _get_research(self, path, query):
        sym = _safe_symbol(unquote(path.rsplit("/", 1)[-1]))
        provider_sym = _resolve_symbol(sym)
        rec = _load(RESEARCH_DIR / f"{provider_sym}.json")
        if not rec:
            return self._send_error_json(404, f"no cached research for {sym}")
        # Holdings change much more often than the cached dossier. Refresh this
        # slice on every read so the detail header never shows a stale position.
        rec = dict(rec)
        rec["portfolio"] = portfolio_context(provider_sym)
        rec["decision"] = decision_label(rec["portfolio"])
        return self._send_json(_annotate_symbol_record(rec, sym, provider_sym))

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

    def _post_activity_view(self, path):
        # Client pings this when a dossier opens, so "tickers visited" survives a
        # restart and crosses devices (it used to be browser-local only). The
        # feed debounces repeat views, so a chatty client is harmless.
        body = self._read_body()
        sym = _safe_symbol(str(body.get("symbol") or ""))
        logged = activity.record_view(sym, str(body.get("name") or "")) if sym else False
        return self._send_json({"ok": True, "logged": bool(logged)})

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

    def _post_setup_automation(self, path):
        # Toggle the read-only background scheduler (ASSAY_AUTO_REFRESH). Persists
        # to tools/secrets.env and takes effect live: enabling starts the daemon
        # without a restart; disabling makes every task a no-op on the next tick.
        body = self._read_body()
        on = bool(body.get("enabled"))
        try:
            _set_secret("ASSAY_AUTO_REFRESH", "1" if on else "0")
        except OSError as exc:
            return self._send_error_json(500, f"could not write secrets: {exc}")
        os.environ["ASSAY_AUTO_REFRESH"] = "1" if on else "0"
        if on:
            scheduler.start()
        return self._send_json({"enabled": scheduler.enabled(), "tasks": scheduler.task_status()})

    def _post_deep_save(self, path):
        body = self._read_body()
        return self._send_json(_save_deep_artifact(body))

    def _post_forget_perplexity(self, path):
        return self._send_json(_forget_pplx())

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
        # Now stages into the working draft instead of writing the live model;
        # the user reviews and commits the draft once. The endpoint name is kept
        # for the Pipeline UI's existing call site.
        body = self._read_body()
        staged = target_staging.stage_proposal(
            str(body.get("segment") or ""),
            str(body.get("date") or ""),
            source="pipeline",
            allow_blocked=bool(body.get("allow_blocked")),
        )
        return self._send_json({**staged, "staging": target_staging.diff_staged_vs_live()})

    def _post_staging_commit(self, path):
        body = self._read_body()
        return self._send_json(target_staging.commit_staged(bool(body.get("confirm"))))

    def _post_staging_discard(self, path):
        return self._send_json(target_staging.discard_staged())

    def _post_restore_target(self, path):
        body = self._read_body()
        return self._send_json(
            target_staging.restore_backup(str(body.get("backup") or ""), bool(body.get("confirm"))))

    def _post_basket_add(self, path):
        body = self._read_body()
        try:
            return self._send_json(basket.add_symbol(
                str(body.get("symbol") or ""),
                source=str(body.get("source") or "manual"),
                note=str(body.get("note") or ""),
                tier=str(body.get("tier") or "want"),
                segment=(str(body.get("segment")) if body.get("segment") else None),
                run=(str(body.get("run")) if body.get("run") else None),
                conviction=(str(body.get("conviction")) if body.get("conviction") else None)))
        except ValueError as exc:
            return self._send_error_json(400, str(exc))

    def _post_basket_tier(self, path):
        body = self._read_body()
        try:
            return self._send_json(basket.set_tier(
                str(body.get("symbol") or ""), str(body.get("tier") or "want")))
        except ValueError as exc:
            return self._send_error_json(400, str(exc))

    def _post_basket_remove(self, path):
        body = self._read_body()
        try:
            return self._send_json(basket.remove_symbol(str(body.get("symbol") or "")))
        except ValueError as exc:
            return self._send_error_json(400, str(exc))

    def _post_basket_clear(self, path):
        return self._send_json(basket.clear())

    def _post_basket_draft_plan(self, path):
        # Turn the basket into a guided strategy run (segment -> synthesis ->
        # proposal gate -> staging). Returns the run manifest so the client can
        # hand off to the strategy view.
        try:
            return self._send_json(_start_basket_plan(basket.basket_members()))
        except ValueError as exc:
            return self._send_error_json(400, str(exc))

    def _post_optimizer_run(self, path):
        # Size the whole candidate pool under the posted constraints and return
        # the proposal + the rebalance plan it WOULD produce (throwaway preview;
        # nothing is written until /api/optimizer/stage).
        body = self._read_body()

        def _f(key, default):
            val = body.get(key)
            try:
                return float(val) if val is not None else default
            except (TypeError, ValueError):
                return default

        # max_names: absent -> product default; explicit null/0 -> off (no cap).
        if "max_names" in body:
            try:
                mn = int(body["max_names"]) if body["max_names"] else None
            except (TypeError, ValueError):
                mn = optimizer.DEFAULT_MAX_NAMES
        else:
            mn = optimizer.DEFAULT_MAX_NAMES

        exclude = body.get("exclude") if isinstance(body.get("exclude"), list) else []
        try:
            proposal = optimizer.optimize(
                cash_target_pct=_f("cash_target_pct", None),
                per_name_cap=_f("per_name_cap", optimizer.DEFAULT_PER_NAME_CAP),
                concentration_pct=_f("concentration_pct", optimizer.DEFAULT_CONCENTRATION_PCT),
                min_position_pct=_f("min_position_pct", optimizer.DEFAULT_MIN_POSITION_PCT),
                max_names=mn,
                conviction_curve=str(body.get("conviction_curve") or optimizer.DEFAULT_CONVICTION_CURVE),
                include_curious=bool(body.get("include_curious", True)),
                drop_avoid=bool(body.get("drop_avoid", False)),
                use_llm=bool(body.get("use_llm", False)),
                exclude={str(s) for s in exclude},
                basket_items=basket.pool_candidates(include_curious=True),
            )
        except Exception as exc:  # noqa: BLE001 - a bad constraint shouldn't 500
            return self._send_error_json(400, str(exc))
        return self._send_json({"proposal": proposal, "preview": _preview_plan(proposal)})

    def _post_optimizer_stage(self, path):
        # Stage the (reviewed) optimizer changes into the working draft, reusing
        # the same provenance-aware path every other proposal flows through.
        body = self._read_body()
        changes = body.get("changes")
        if not isinstance(changes, list) or not changes:
            return self._send_error_json(400, "changes[] required")
        res = target_staging.stage_changes(
            changes, source="optimizer",
            allow_drop_pinned=bool(body.get("allow_drop_pinned")))
        return self._send_json({**res, "staging": target_staging.diff_staged_vs_live()})

    def _post_portfolio_review(self, path):
        # Kick off the batch "analyse all holdings" job. It runs in the Task
        # Center and writes per-held conviction the optimizer pool reads back.
        body = self._read_body()
        return self._send_json(_start_portfolio_review(bool(body.get("refresh"))))

    def _post_staging_edit(self, path):
        """Manual edits to the working draft and pin management. ``op`` selects:
        edit (stage one change), revert (restore a key to live), pin / unpin
        (standing conviction on the live model)."""
        body = self._read_body()
        op = str(body.get("op") or "edit")
        if op == "revert":
            return self._send_json(target_staging.revert_key(str(body.get("key") or "")))
        if op == "pin":
            return self._send_json({"pin": target_staging.set_pin(
                str(body.get("key") or ""),
                stance=str(body.get("stance") or "hold"),
                floor_pct=body.get("floor_pct"),
                ceiling_pct=body.get("ceiling_pct"),
                rationale=str(body.get("rationale") or ""),
            )})
        if op == "unpin":
            return self._send_json(target_staging.clear_pin(str(body.get("key") or "")))
        change = body.get("change")
        if not isinstance(change, dict):
            return self._send_error_json(400, "edit requires a 'change' object")
        return self._send_json(target_staging.stage_changes(
            [change], source="manual",
            allow_drop_pinned=bool(body.get("allow_drop_pinned"))))

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

    def _post_rebalance_funding(self, path):
        # Deterministic funding suggestions for a plan whose buys outrun its
        # trims: funding_order first, then untargeted names, each capped at its
        # headroom and tax-annotated. Advice only — lands as editable inputs.
        body = self._read_body()
        holdings = _load(HOLDINGS_JSON)
        model = target_staging.active_model()
        if not holdings or not model:
            return self._send_error_json(404, "need both a holdings snapshot and a target model")
        try:
            needed = float(body.get("needed_czk"))
        except (TypeError, ValueError):
            return self._send_error_json(400, "needed_czk must be a number")
        exclude = body.get("exclude") if isinstance(body.get("exclude"), list) else []
        out = rebalance.funding_candidates(model, holdings, needed, exclude=exclude)
        # Tax view per suggestion: what selling that slice would realize (Czech
        # 3-year lot selection), so a "cheap" funding source is visible at once.
        for c in out["candidates"]:
            try:
                bd = tax_lots.breakdown_for_symbol(holdings, c["symbol"], float(c["suggest_czk"]))
                tot = bd.get("totals") or {}
                c["tax"] = {
                    "taxable_gain": tot.get("taxable_gain"),
                    "exempt_proceeds": tot.get("exempt_proceeds"),
                    "harvestable_loss": tot.get("harvestable_loss"),
                    "has_lots": bool(bd.get("has_lots")),
                }
            except Exception:  # noqa: BLE001 -- tax annotation is best-effort decoration
                c["tax"] = None
        return self._send_json(out)

    def _post_execution_plan(self, path):
        body = self._read_body()
        action = str(body.get("action") or "patch")
        if action == "patch":
            return self._send_json(execution_plan.patch_item(
                str(body.get("item_id") or ""),
                body.get("changes") if isinstance(body.get("changes"), dict) else {},
                expected_version=body.get("version"),
            ))
        if action == "manual":
            state, item = execution_plan.add_manual(body.get("item") or {})
            return self._send_json({"state": state, "item": item})
        if action == "replace_rebalance":
            model = target_staging.active_model()
            holdings = _load(HOLDINGS_JSON)
            if not model or not holdings:
                raise ValueError("target model and holdings snapshot are required")
            plan = tax_lots.enrich_plan(rebalance.plan(model, holdings), holdings)
            _attach_research_overlay(plan, holdings)
            return self._send_json(execution_plan.replace_rebalance(plan))
        if action == "queue_selected":
            holdings = _load(HOLDINGS_JSON)
            if not holdings:
                raise ValueError("holdings snapshot is required")
            return self._send_json(execution_plan.queue_selected(holdings))
        raise ValueError("unsupported execution plan action")

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

    def _post_exit_plan_refresh_options(self, path):
        """Refresh every cached option quote for one exit-plan instrument."""
        body = self._read_body()
        model = target_staging.active_model()
        holdings = _load(HOLDINGS_JSON)
        if not model or not holdings:
            return self._send_error_json(404, "need both a holdings snapshot and a target model")
        try:
            symbol = _safe_symbol(str(body.get("symbol") or ""))
        except ValueError as exc:
            return self._send_error_json(400, str(exc))
        include = body.get("include") if isinstance(body.get("include"), list) else []
        full = body.get("full_exit") if isinstance(body.get("full_exit"), list) else []
        cfg = body.get("cfg") if isinstance(body.get("cfg"), dict) else None
        with _PULL_LOCK:
            option_market.cached_option_chain(symbol, force_quotes=True)
            plan = exit_plan.build_exit_plan(
                model,
                holdings,
                include=include,
                full_exit=full,
                cfg=cfg,
                with_options=True,
            )
        return self._send_json(plan)

    def _post_exit_plan_stage(self, path):
        # Rebuild the plan server-side (it's ephemeral) with the same params the
        # client used, then merge the requested tranche into the staged basket so
        # the trade desk picks it up. Never trusts a client-supplied share/CZK size.
        body = self._read_body()
        model = target_staging.active_model()
        holdings = _load(HOLDINGS_JSON)
        if not model or not holdings:
            return self._send_error_json(404, "need both a holdings snapshot and a target model")
        symbol = str(body.get("symbol") or "").strip()
        if not symbol:
            return self._send_error_json(400, "symbol is required")
        route = str(body.get("route") or "sell_shares").strip().lower()
        if route not in {"sell_shares", "covered_call"}:
            return self._send_error_json(400, "route must be sell_shares or covered_call")
        include = body.get("include") if isinstance(body.get("include"), list) else []
        full = body.get("full_exit") if isinstance(body.get("full_exit"), list) else []
        cfg = body.get("cfg") if isinstance(body.get("cfg"), dict) else None
        with _PULL_LOCK:
            plan = exit_plan.build_exit_plan(model, holdings, include=include, full_exit=full, cfg=cfg)
        try:
            if route == "sell_shares":
                try:
                    index = int(body.get("index"))
                except (TypeError, ValueError):
                    return self._send_error_json(400, "index must be an integer tranche number")
                result = exit_plan.stage_tranche(plan, symbol, index)
            else:
                try:
                    conid = int(body.get("conid"))
                    strike = float(body.get("strike"))
                    contracts = int(body.get("contracts"))
                except (TypeError, ValueError):
                    return self._send_error_json(
                        400, "covered call needs numeric conid, strike, and contracts",
                    )
                result = exit_plan.stage_covered_call(
                    plan,
                    symbol,
                    conid=conid,
                    expiry=str(body.get("expiry") or ""),
                    strike=strike,
                    contracts=contracts,
                )
            return self._send_json(result)
        except (ValueError, TypeError) as exc:
            return self._send_error_json(400, str(exc))

    def _post_whatif(self, path):
        body = self._read_body()
        holdings = _load(HOLDINGS_JSON)
        # Simulate against the working draft when one exists, matching the planner.
        model = target_staging.active_model()
        if not holdings or not model:
            return self._send_error_json(404, "need both a holdings snapshot and a target model")
        return self._send_json(whatif.simulate(holdings, model, body.get("trades")))

    def _post_rebalance_stage_routes(self, path):
        body = self._read_body()
        holdings = _load(HOLDINGS_JSON)
        if not holdings:
            return self._send_error_json(404, "no holdings snapshot — sync from IBKR first")
        try:
            result = rebalance_routes.stage_routes(
                holdings, body.get("trades"), body.get("selections"),
                mode=body.get("mode") or "replace",
                source=body.get("source") or "rebalance_routes",
            )
        except (TypeError, ValueError) as exc:
            return self._send_error_json(400, str(exc))
        item_ids = [
            str(selection.get("execution_item_id") or "")
            for selection in body.get("selections") or []
            if isinstance(selection, dict) and selection.get("execution_item_id")
        ]
        if item_ids:
            execution_plan.mark_queued(item_ids, result.get("basket") or [])
        return self._send_json({**result, **_basket_state()})

    def _post_trade_reconnect(self, path):
        # Re-establish the brokerage session (ssodh/init) without a browser
        # login; returns the refreshed status so the banner updates in one call.
        return self._send_json(_trade_reconnect())

    def _post_trade_preview(self, path):
        return self._send_json(_trade_preview(self._read_body()))

    def _post_trade_place(self, path):
        result = _trade_place(self._read_body())
        execution_plan.mark_submitted()
        return self._send_json(result)

    def _post_trade_cancel(self, path):
        return self._send_json(_trade_cancel(self._read_body()))

    def _post_trade_peg(self, path):
        return self._send_json(_trade_peg_start(self._read_body()))

    def _post_trade_peg_stop(self, path):
        return self._send_json(_trade_peg_stop(self._read_body()))

    def _post_trade_basket(self, path):
        # Planner writes replace stock legs but cannot forge covered calls.
        # Queue controls mutate server-known legs by id so stale clients fail
        # closed instead of replacing a newer queue snapshot.
        body = self._read_body()
        if body.get("clear") is True:
            _save_basket([])
        elif body.get("only_leg_ids") is not None:
            _set_only_basket_legs_included(body.get("only_leg_ids"))
        elif body.get("toggle_leg_id") is not None:
            _set_basket_leg_included(
                body.get("toggle_leg_id"),
                body.get("included"),
            )
        elif body.get("remove_leg_id") is not None:
            _remove_basket_leg(body.get("remove_leg_id"))
        else:
            _replace_stock_basket(body.get("trades"))
        return self._send_json(_basket_state())

    def _post_trade_basket_review(self, path):
        body = self._read_body()
        return self._send_json(_review_basket(body.get("revision")))

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
        # Match research_pull's dossier writer: atomic + locked (Windows-safe),
        # and deliberately unsorted so re-saving a thesis doesn't churn key order.
        _write_json(RESEARCH_DIR / f"{provider_sym}.json", rec, sort_keys=False)
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
    if args.reload and os.environ.get(devreload.RELOAD_CHILD_ENV) != "1":
        return devreload.run_reloader()

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
        threading.Thread(target=devreload.reload_watcher, daemon=True).start()
    # Read-only background freshness scheduler. No-op unless ASSAY_AUTO_REFRESH=1;
    # every action it takes is one the UI already exposes as a button.
    if scheduler.start():
        print("  Auto-refresh ON: background scheduler keeps holdings/segments/quotes current.")
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
