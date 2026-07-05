# Background freshness scheduler

Status: proposal · Scope: while `tools/serve.py` is running, keep the app's own
ground truth current — holdings snapshot, portfolio history, segment caches, and
prices for gated names — so the Today cockpit reports reality instead of asking
the user to go fetch it. **Strictly read-only automation.** Nothing here places,
stages, or sizes a trade, spends LLM/Perplexity quota, or mutates the target
model. It only runs work the UI already exposes as a button, on a schedule.

This is the first step of a broader "fewer requirements on the user" direction:
make the app self-maintaining (this doc) and later proactive (a notification
digest that reads the fresh data this produces).

## Why

Every recurring manual chore today is the user driving the machine: the app only
knows anything when you open it, only refreshes when you click, and only warns
you if you happen to be looking. The single most common Today nag —
"snapshot is N days old, resync" — is a read-only, idempotent, already-sanitized
Flex pull that runs as a background job with a task pill. There is no reason the
human must trigger it. Same for the incremental history top-up, stale segment
caches, and prices for names with a locked price gate.

## Non-goals (this PR)

- **No notifications / digest.** That is the next PR; this one produces the fresh
  data it will read.
- **No auto-refresh of LLM analyses or Deep Research.** Costs money/quota; stays
  a button. The scheduler may *observe* that N analyses are stale, never *run*
  them.
- **No risk-report precomputation.** On-demand is fine; it is heavy.
- **No change to any trading gate.** The preview→token→confirm→place path is
  untouched. A watched gate that triggers is surfaced, never acted on.

## Design overview

One new module, `tools/scheduler.py`, owns a single daemon thread started from
`serve.py`'s main path — the same placement and discipline as the existing
`_reload_watcher` (child process under the reload supervisor; guard against a
double-start so an edit-reload doesn't stack threads). The thread ticks every
60s, evaluates each task's pure `should_run(...)` predicate, and dispatches real
work through the existing `jobs.spawn(...)` registry so every automated action is
visible in the Task Center pill, cancellable, and logged exactly like a
user-initiated one.

**Persistence** — `data/cache/scheduler-state.json`:
`{task_name: {last_run, last_result}}`. Load-bearing: the reload supervisor
respawns the process on every `tools/*.py` edit, and without persisted stamps
each respawn would re-trigger pulls. Single writer (the thread); a
missing/corrupt file reads as "never ran".

**Config** — master switch `ASSAY_AUTO_REFRESH=1` (default **off**; opt-in for
the first release), resolved with the same env-then-`secrets.env` precedence as
the IBKR flags. Extract that resolver into `config.py` as
`config_value(key, default)` so `ibkr_trade`, `holdings_sync`, and `scheduler`
share one definition instead of each rolling its own. Optional granular
overrides under the master switch: `ASSAY_AUTO_RESYNC=0`, `ASSAY_AUTO_SEGMENTS=0`,
`ASSAY_GATE_WATCH=0`.

## The tasks

| Task | Condition (all must hold) | Cadence | Action | Reused seam |
|---|---|---|---|---|
| `holdings-resync` | `ibkr_status()["configured"]`; snapshot age > `overview.STALE_SNAPSHOT_DAYS` (7d); no `ibkr_sync` job running | check hourly, ≤ 1 run / 24h | `holdings_sync.start_holdings_sync()` (swallow `Conflict`) | the exact job the Resync button runs |
| `history-topup` | `ibkr_status()["history_configured"]`; last run > 7d | weekly | `start_history_sync(full=False)` | existing incremental history job |
| `segment-refresh` | any cached segment older than `overview.STALE_SEGMENT_DAYS` (45d) | 1 segment/sweep, ≤ 1 sweep / 24h | `research_pull.pull_segment(name)` under `PULL_LOCK`, oldest first | deterministic pull, rate-limit-safe |
| `gate-quotes` | `price_levels.load_all()` non-empty; weekday and inside `ASSAY_GATE_HOURS` (default 15:00–22:30 local, coarse US window) | hourly in-window | fetch latest close per gated symbol via new `yahoo.latest_price(sym)`; write `data/cache/quotes.json` `{sym: {price, at}}` | Yahoo session plumbing |

**Budgets.** Each sweep caps total network calls (e.g. ≤ 1 segment + ≤ 12
quotes) and staggers task starts by a few seconds. A ~90s delay after process
boot prevents an edit-reload storm from hammering providers during development.
Negative results (delisted symbol, empty chain) are cached so a bad name isn't
re-hit hourly.

## Making the gate watcher actually matter

Today `rebalance_overlay.gate_current_price()` resolves: holdings mark → cached
dossier price. Both go stale together, so a locked buy-below can be crossed for
days without the planner noticing. Change the precedence to:

1. **fresh quote** from `data/cache/quotes.json` (age ≤ 4h),
2. holdings mark,
3. dossier price.

That is a ~15-line change in `rebalance_overlay.py` (`mark_price_map` gains a
quote-overlay step) plus a small leaf `tools/quote_cache.py` for load/save/lookup
so the overlay never imports the scheduler. Effect: `/api/rebalance` and
`/api/overview` gate states (`gates_open` / `gates_waiting`) flip within an hour
of the market crossing a locked level — which is exactly what the Today cockpit's
"Act on triggered price levels" next-step needs to be trustworthy. Acting on the
trigger still requires the human to stage → preview → place.

## Surfacing

- `/api/overview` gains an `automation` block:
  `{enabled, tasks: [{name, last_run, last_result, next_eligible}]}`, built by a
  pure `overview.automation_summary(state, flags, now)`.
- **Today snapshot card**: with automation on, the stale-snapshot warning becomes
  "auto-resync armed — last synced X, next check by Y" instead of a nag; with it
  off, an unobtrusive "Tired of resyncing by hand? Enable auto-refresh in Setup."
- **Setup tab**: a checkbox writing `ASSAY_AUTO_REFRESH=1` to `tools/secrets.env`
  via the existing setup-save path (mirrors IBKR cred capture), plus a status line
  showing what the scheduler last did.

## File-by-file

| File | Change | Est. |
|---|---|---|
| `tools/scheduler.py` | **new** — task table, `should_run` predicates, tick loop, state persistence, budgets | ~220 |
| `tools/quote_cache.py` | **new** — load/save/lookup with age check (leaf, stdlib) | ~50 |
| `tools/config.py` | add shared `config_value()` (moved from `ibkr_trade._config_value`) | ~15 |
| `tools/ibkr_trade.py` | delegate `_config_value` to config (identical behavior) | ~5 |
| `tools/providers/yahoo.py` | `latest_price(symbol)` from the chart endpoint | ~20 |
| `tools/rebalance_overlay.py` | quote-overlay precedence in the price map | ~15 |
| `tools/serve.py` | start scheduler thread (behind flag); `automation` block in `_get_overview` | ~25 |
| `tools/overview.py` | pure `automation_summary(state, flags, now)` | ~40 |
| `web/src/overview.ts`, `setup.ts`, `index.html`, `style.css` | automation status on Today; Setup toggle | ~80 |
| tests | see below | ~250 |

## Testing strategy

Everything that decides *whether* to run is pure and injected — no thread, no
network, no sleep in tests.

- `test_scheduler.py`: each task's `should_run(state, now, status)` across stale
  vs fresh snapshot, missing creds, the 24h throttle, market-window edges, and
  master/granular flags; a corrupt state file reads as "never ran". One
  loop-tick test with fake tasks asserting dispatch order, budget enforcement,
  and that a raising task is logged (errorlog) without killing the tick.
- `test_quote_cache.py` + overlay tests: fresh quote beats holdings mark; stale
  quote loses to mark; a gate flips from blocked to open when the quote crosses
  the level (extends `test_research_overlay.py`).
- `overview` tests: `automation_summary` last/next-run rendering; the Today card
  copy switch (vitest).
- Existing suites stay green untouched — the scheduler is additive and off by
  default.

## Safety & scope guards (explicit)

- Master flag **off by default**; every action is one the UI already offers as a
  button; everything routes through `jobs` (visible, cancellable, logged).
- No task touches: order placement, the staged basket, the target model, LLM
  backends, or Perplexity.
- Provider courtesy: `PULL_LOCK` serialization, per-sweep budgets, ~90s startup
  delay, negative-result caching.
- The gate watcher *observes* prices; acting on a triggered gate still requires a
  human to stage/preview/place.

## Rollout (commit-by-commit within the PR)

1. `config_value` extraction + `scheduler.py` skeleton with `holdings-resync`
   only, plus tests — the highest-value 20%.
2. `history-topup` + `segment-refresh` (same predicate machinery).
3. `quote_cache` + yahoo `latest_price` + overlay precedence + `gate-quotes`.
4. Overview `automation` block + Today/Setup surfacing.

## Open questions (settle before implementing)

1. **Opt-in vs opt-out**: recommend opt-in (`ASSAY_AUTO_REFRESH=1`) for the first
   release, flipping to default-on once trusted.
2. **Cadences**: 24h resync / 7d history / 45d segments / hourly quotes — or tune
   (e.g. resync every market morning).
3. **Market window**: coarse 15:00–22:30 Prague weekday window for the gate
   watcher, or run hourly around the clock (simpler, slightly wasteful).

## Verification (every phase)

```powershell
py -3 -m pytest tools/tests -q
npm install; npm run lint; npm run typecheck; npm run test; npm run build
```
