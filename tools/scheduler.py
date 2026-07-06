#!/usr/bin/env python3
"""Background freshness scheduler -- read-only self-maintenance.

While ``serve.py`` runs, keep the app's own ground truth current (holdings
snapshot, portfolio history, segment caches, and quotes for gated names) by
running the exact jobs the UI already exposes as buttons, on a schedule -- so the
Today cockpit reports reality instead of nagging the user to go fetch it.

STRICTLY read-only. Nothing here places, stages, or sizes a trade, spends
LLM/Perplexity quota, or mutates the target model. Every action routes through
``jobs.spawn`` so it is visible in the Task Center, cancellable, and logged
exactly like a user-initiated one.

Off by default; opt in with ``ASSAY_AUTO_REFRESH=1`` (env or tools/secrets.env).
Granular kill switches under the master: ``ASSAY_AUTO_RESYNC``,
``ASSAY_AUTO_SEGMENTS``, ``ASSAY_GATE_WATCH`` (each default on when the master is
on).

Design: one daemon thread started from serve.py's main path (same discipline as
``_reload_watcher``). The thread ticks every 60s, evaluates each task's pure
``should_run(last_run, obs)`` predicate against freshly gathered observations,
and dispatches real work. Persisted stamps in ``data/cache/scheduler-state.json``
survive the reload supervisor's respawns so an edit-reload doesn't re-trigger
pulls.
"""
from __future__ import annotations

import datetime as dt
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import apierror
import config
import holdings_sync
import jobs
import overview
import price_levels
import quote_cache
import research_pull
import segments_service
import store
import timeutil
from providers import yahoo

STATE_FILE = config.DATA_DIR / "cache" / "scheduler-state.json"

TICK_SECONDS = 60
STARTUP_DELAY_SECONDS = 90       # let an edit-reload storm settle before touching providers
STAGGER_SECONDS = 3             # space task starts within one sweep

# Per-sweep budgets (provider courtesy).
MAX_QUOTES_PER_SWEEP = 12

# Throttles: the real gate against re-running work every 60s tick.
RESYNC_MIN_INTERVAL = dt.timedelta(hours=24)
HISTORY_MIN_INTERVAL = dt.timedelta(days=7)
SEGMENT_MIN_INTERVAL = dt.timedelta(hours=24)
QUOTES_MIN_INTERVAL = dt.timedelta(hours=1)

# Negative-quote suppression: a symbol that just failed to price is not re-hit
# until this lapses, so a delisted/renamed name doesn't burn a provider call
# every sweep.
QUOTE_NEG_TTL_SECONDS = 24 * 3600

DEFAULT_GATE_HOURS = "15:00-22:30"   # coarse US session in Prague local time


# --------------------------------------------------------------------------- #
# Flags
# --------------------------------------------------------------------------- #
def enabled() -> bool:
    """Master switch. Everything is off unless this is explicitly set."""
    return config.flag_enabled("ASSAY_AUTO_REFRESH", "0")


def _task_enabled(task: "Task") -> bool:
    if not enabled():
        return False
    return task.flag is None or config.flag_enabled(task.flag, "1")


# --------------------------------------------------------------------------- #
# Time helpers (pure)
# --------------------------------------------------------------------------- #
def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _parse_iso(stamp: Any) -> dt.datetime | None:
    return timeutil.parse_iso_utc(stamp)


def _throttle_ok(last_run: dt.datetime | None, now: dt.datetime, interval: dt.timedelta) -> bool:
    """True when the task has never run or the throttle window has elapsed."""
    return last_run is None or (now - last_run) >= interval


def parse_gate_hours(spec: str) -> tuple[int, int]:
    """``"15:00-22:30"`` -> ``(900, 1350)`` minutes-of-day. Falls back to the
    default window on any malformed input rather than raising."""
    try:
        lo, hi = spec.split("-", 1)
        lh, lm = (int(x) for x in lo.split(":", 1))
        hh, hm = (int(x) for x in hi.split(":", 1))
        return lh * 60 + lm, hh * 60 + hm
    except (ValueError, AttributeError):
        lo, hi = DEFAULT_GATE_HOURS.split("-", 1)
        lh, lm = (int(x) for x in lo.split(":", 1))
        hh, hm = (int(x) for x in hi.split(":", 1))
        return lh * 60 + lm, hh * 60 + hm


def _window_open(weekday: int, minutes_of_day: int, spec: str) -> bool:
    """Pure: weekday (Mon=0) inside Mon-Fri and time-of-day inside ``spec``."""
    if weekday >= 5:
        return False
    start, end = parse_gate_hours(spec)
    return start <= minutes_of_day <= end


def in_market_window(now: dt.datetime, spec: str | None = None) -> bool:
    """Whether ``now`` (aware UTC) falls in the coarse weekday market window,
    evaluated in the server's local time."""
    spec = spec or config.config_value("ASSAY_GATE_HOURS", DEFAULT_GATE_HOURS)
    local = now.astimezone()
    return _window_open(local.weekday(), local.hour * 60 + local.minute, spec)


# --------------------------------------------------------------------------- #
# State persistence  ({task_name: {last_run, last_result}})
# --------------------------------------------------------------------------- #
def load_state() -> dict[str, dict]:
    """Persisted run stamps. A missing/corrupt file reads as "never ran"."""
    data = store.load(STATE_FILE, {})
    return data if isinstance(data, dict) else {}


def save_state(state: dict[str, dict]) -> None:
    store.write_json(STATE_FILE, state)


# --------------------------------------------------------------------------- #
# Observations (gathered once per tick; injected into the pure predicates)
# --------------------------------------------------------------------------- #
@dataclass
class Obs:
    now: dt.datetime
    ibkr_configured: bool = False
    ibkr_history_configured: bool = False
    snapshot_age_days: int | None = None
    stale_segments: list[str] = field(default_factory=list)   # names, oldest first
    gated_symbols: list[str] = field(default_factory=list)
    market_open: bool = False
    is_running: Callable[[str], bool] = jobs.running


def _load_holdings() -> dict | None:
    return store.load(config.HOLDINGS_JSON)


def observe(now: dt.datetime | None = None) -> Obs:
    now = now or _now()
    status = holdings_sync.ibkr_status()
    snap = overview.snapshot_summary(_load_holdings(), now=now)

    stale = []
    for s in segments_service.segments_list():
        if not s.get("cached"):
            continue
        age = overview.age_days(s.get("cached_at"), now)
        if age is not None and age > overview.STALE_SEGMENT_DAYS:
            stale.append((age, s.get("name")))
    stale.sort(reverse=True)   # oldest first
    stale_names = [name for _age, name in stale if name]

    gated = sorted(str(k).upper() for k in price_levels.load_all().keys())

    return Obs(
        now=now,
        ibkr_configured=bool(status.get("configured")),
        ibkr_history_configured=bool(status.get("history_configured")),
        snapshot_age_days=snap.get("age_days"),
        stale_segments=stale_names,
        gated_symbols=gated,
        market_open=in_market_window(now),
        is_running=jobs.running,
    )


# --------------------------------------------------------------------------- #
# should_run predicates (pure)
# --------------------------------------------------------------------------- #
def _should_resync(last_run: dt.datetime | None, obs: Obs) -> bool:
    return (
        obs.ibkr_configured
        and obs.snapshot_age_days is not None
        and obs.snapshot_age_days > overview.STALE_SNAPSHOT_DAYS
        and not obs.is_running("ibkr_sync")
        and _throttle_ok(last_run, obs.now, RESYNC_MIN_INTERVAL)
    )


def _should_history(last_run: dt.datetime | None, obs: Obs) -> bool:
    return (
        obs.ibkr_history_configured
        and not obs.is_running("ibkr_history")
        and _throttle_ok(last_run, obs.now, HISTORY_MIN_INTERVAL)
    )


def _should_segments(last_run: dt.datetime | None, obs: Obs) -> bool:
    return bool(obs.stale_segments) and _throttle_ok(last_run, obs.now, SEGMENT_MIN_INTERVAL)


def _should_quotes(last_run: dt.datetime | None, obs: Obs) -> bool:
    return (
        bool(obs.gated_symbols)
        and obs.market_open
        and _throttle_ok(last_run, obs.now, QUOTES_MIN_INTERVAL)
    )


# --------------------------------------------------------------------------- #
# Actions (dispatch real work; return a short result string for the state file)
# --------------------------------------------------------------------------- #
def _run_resync(_obs: Obs) -> str:
    try:
        job = holdings_sync.start_holdings_sync()
        return f"resync spawned #{job.get('id', '?')}"
    except apierror.Conflict:
        return "skipped: a resync was already running"


def _run_history(_obs: Obs) -> str:
    try:
        job = holdings_sync.start_history_sync(full=False)
        return f"history top-up spawned #{job.get('id', '?')}"
    except apierror.Conflict:
        return "skipped: a history pull was already running"


def _run_segments(obs: Obs) -> str:
    name = obs.stale_segments[0]
    if jobs.running("segment_refresh"):
        return "skipped: a segment refresh was already running"
    job = jobs.spawn("segment_refresh", _segment_worker, name, segment=name)
    return f"segment '{name}' refresh spawned #{job.get('id', '?')}"


def _segment_worker(job_id: str, name: str) -> None:
    jobs.update_job(job_id, state=jobs.JobState.RUNNING, message=f"refreshing segment {name}\u2026")
    try:
        with research_pull.PULL_LOCK:
            research_pull.pull_segment(name)
        jobs.update_job(job_id, state=jobs.JobState.DONE, message=f"segment {name} refreshed")
    except Exception as exc:  # noqa: BLE001 -- surface any pull failure on the job, not the tick
        jobs.update_job(job_id, state=jobs.JobState.ERROR, message=str(exc), error=str(exc))


def _run_quotes(obs: Obs) -> str:
    if jobs.running("gate_quotes"):
        return "skipped: a quote sweep was already running"
    quotes = quote_cache.load()
    # Drop names still inside their negative-result cooldown.
    fresh_syms = [s for s in obs.gated_symbols if not _neg_suppressed(quotes.get(s), obs.now)]
    batch = fresh_syms[:MAX_QUOTES_PER_SWEEP]
    if not batch:
        return "no gated names due for a quote"
    job = jobs.spawn("gate_quotes", _quotes_worker, batch)
    return f"quote sweep spawned for {len(batch)} name(s) #{job.get('id', '?')}"


def _neg_suppressed(entry: Any, now: dt.datetime) -> bool:
    """True if ``entry`` is a recent negative (errored) quote still in cooldown."""
    if not isinstance(entry, dict) or not entry.get("error"):
        return False
    age = quote_cache.age_seconds(entry.get("at"), now)
    return age is not None and age < QUOTE_NEG_TTL_SECONDS


def _quotes_worker(job_id: str, syms: list[str]) -> None:
    jobs.update_job(job_id, state=jobs.JobState.RUNNING, message=f"fetching {len(syms)} quote(s)\u2026")
    quotes = quote_cache.load()
    ok = 0
    for sym in syms:
        key = sym.upper()
        try:
            with research_pull.PULL_LOCK:
                px = yahoo.latest_price(sym)
            quotes[key] = {"price": px["price"], "currency": px.get("currency"), "at": px["at"]}
            ok += 1
        except Exception as exc:  # noqa: BLE001 -- negative-cache and keep going
            quotes[key] = {
                "price": None,
                "currency": None,
                "at": _now().isoformat(timespec="seconds"),
                "error": str(exc)[:160],
            }
        time.sleep(0.3)   # be polite between provider calls
    quote_cache.save(quotes)
    jobs.update_job(job_id, state=jobs.JobState.DONE, message=f"{ok}/{len(syms)} quotes updated")


# --------------------------------------------------------------------------- #
# Task table
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Task:
    name: str
    flag: str | None
    should_run: Callable[[dt.datetime | None, Obs], bool]
    run: Callable[[Obs], str]
    label: str = ""
    interval: dt.timedelta = RESYNC_MIN_INTERVAL   # informational: drives next_eligible surfacing


TASKS: list[Task] = [
    Task("holdings-resync", "ASSAY_AUTO_RESYNC", _should_resync, _run_resync,
         label="Holdings resync", interval=RESYNC_MIN_INTERVAL),
    Task("history-topup", "ASSAY_AUTO_RESYNC", _should_history, _run_history,
         label="History top-up", interval=HISTORY_MIN_INTERVAL),
    Task("segment-refresh", "ASSAY_AUTO_SEGMENTS", _should_segments, _run_segments,
         label="Segment refresh", interval=SEGMENT_MIN_INTERVAL),
    Task("gate-quotes", "ASSAY_GATE_WATCH", _should_quotes, _run_quotes,
         label="Gate quotes", interval=QUOTES_MIN_INTERVAL),
]


def task_status() -> list[dict]:
    """Descriptor list for the Today cockpit / ``overview.automation_summary``:
    each task's name, label, whether it's currently enabled, and its throttle
    interval in seconds. Read-only; no scheduler internals leak."""
    return [
        {
            "name": t.name,
            "label": t.label or t.name,
            "enabled": _task_enabled(t),
            "interval_s": int(t.interval.total_seconds()),
        }
        for t in TASKS
    ]


# --------------------------------------------------------------------------- #
# Tick loop
# --------------------------------------------------------------------------- #
def tick(now: dt.datetime | None = None, *, obs: Obs | None = None) -> list[str]:
    """Evaluate every enabled task once. Returns the names that fired. A task
    that raises is logged and stamped (so it isn't retried before its throttle
    lapses) without aborting the sweep. ``obs`` is injectable for tests."""
    now = now or _now()
    obs = obs or observe(now)
    state = load_state()
    fired: list[str] = []
    for task in TASKS:
        if not _task_enabled(task):
            continue
        rec = state.get(task.name) or {}
        last_run = _parse_iso(rec.get("last_run"))
        try:
            if not task.should_run(last_run, obs):
                continue
            result = task.run(obs)
        except Exception as exc:  # noqa: BLE001 -- one bad task must not kill the tick
            sys.stderr.write(f"[scheduler] task {task.name} failed: {exc}\n")
            result = f"error: {exc}"
        state[task.name] = {"last_run": now.isoformat(timespec="seconds"), "last_result": result}
        save_state(state)
        fired.append(task.name)
        time.sleep(STAGGER_SECONDS)
    return fired


def run_loop(stop: threading.Event | None = None) -> None:
    """The daemon body: settle, then tick every ``TICK_SECONDS`` until stopped."""
    if stop is None:
        stop = threading.Event()
    if stop.wait(STARTUP_DELAY_SECONDS):
        return
    while not stop.is_set():
        try:
            tick()
        except Exception as exc:  # noqa: BLE001 -- never let the loop die
            sys.stderr.write(f"[scheduler] tick failed: {exc}\n")
        stop.wait(TICK_SECONDS)


_STARTED = False
_START_LOCK = threading.Lock()


def start() -> bool:
    """Launch the scheduler daemon once per process, only when enabled. Returns
    True if it started, False if disabled or already running."""
    global _STARTED
    if not enabled():
        return False
    with _START_LOCK:
        if _STARTED:
            return False
        _STARTED = True
    threading.Thread(target=run_loop, name="assay-scheduler", daemon=True).start()
    return True
