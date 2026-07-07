# Process attribution — "is this system earning its keep?"

Status: **in progress** (updated 2026-07-07) · Scope: turn the data Assay already
stores (the full trade ledger + daily NAV, per-band **provenance**,
sleeves/segments, the journal) into a verdict on the *process itself* — not "is
this stock up" but "did rebalancing, this optimizer, these overrides, add value
over doing nothing." Deterministic over stored data; **read-only, never trades**.

> **Progress (reconciled against `main`).** Phase 1 — the **FX spine** — has
> shipped in two parts: the panel (`tools/fx_history.py` + a `holdings_sync`
> top-up fold-in, PR #175) and its first consumer, a **currency-exposure +
> FX-effect tile on the Risk view** (`fx_history.exposure_by_currency` /
> `window_report` → `/api/risk → fx`). Phase 2 — the **provenance timeline** —
> shipped (`provenance-log.jsonl` writer in `commit_staged` +
> `backfill_provenance_log()`; PR #179). Phase 3 — the **attribution engine** —
> ships in this PR: `tools/attribution.py` (pure `time_weighted_return` /
> `positions_at` / `hold_index` / `flow_curve` + the `attribution_report`
> assembler), the `never-rebalanced` and `benchmark` counterfactuals, `GET
> /api/attribution`, and the **Attribution** sub-tab (a growth-of-100 chart +
> actual-vs-baseline deltas). Phase 3 was then extended (follow-up PR): the
> headline verdict is **cached and surfaced on the Today cockpit**
> (`verdict_from_report` / `cache_verdict` / `load_verdict` →
> `overview.attribution_summary`), warmed when the view is opened so the cockpit
> reads it network-free. What remains: the `follow_all` counterfactual over
> the provenance log and the full NAV `decompose_fx` local/FX/residual split
> (phase 4), and by-source / by-group attribution tables (the rest of phase 5). The
> `cash_transactions[].type` question is settled operationally — the classifier
> substring-matches deposit/withdraw against IBKR's one external type
> ("Deposits/Withdrawals"); everything else stays internal book return.

Currency attribution (recommendation #3) is **folded into this data model from
day one**: a CZK-base investor holding mostly USD assets sees a chunk of every
month's move that is just USDCZK/EURCZK noise. If the counterfactuals and the
by-source numbers aren't FX-clean, they're lying by a few points, so FX is not a
later bolt-on — it's a column in the same panel.

This is the natural extension of the planned journal-calibration work: that
scores individual *calls*; this scores the *system*.

## Why

We have every ingredient and have never assembled them:

- **Actual NAV curve + cash flows** — `data/cache/ibkr/portfolio-history.json`:
  `nav_series[{date, nav, cash, stock}]` (already base CZK), `trades[]` (each with
  `date`, `symbol`, `side`, `fx_rate_to_base`, `base_cash_flow`, `base_value`,
  `realized_pnl`), and `cash_transactions[{date, type, currency, amount,
  base_amount}]` (deposits, dividends, fees).
- **Provenance** — `target-model.json → provenance[key]` records the `source`
  (`strategy` / `optimizer` / `pipeline` / `manual` / `user-pin` / `legacy-plan`),
  `run_id`, `segment`, `conviction`, `set_at` for every band.
- **Groupings** — `sleeves.<name>.members` (symbol→sleeve) and
  `data/segments/*.json` (symbol→segment, many-to-many).
- **Judgement** — `data/journal.json` decisions/outcomes.

Nobody with this data should have to *wonder* whether the process pays. Today
they do, because nothing joins the ledger to the intent behind it.

## What it answers

1. **Counterfactuals** (same external cash flows, so the comparison is honest):
   - *Actual* NAV curve vs
   - *Never rebalanced* — freeze the book as of date X, let prices run.
   - *Just bought SPY / QQQ* — every koruna (including each deposit) into the
     benchmark at the prevailing price.
   - *Followed every suggestion immediately* — replay the provenance-dated band
     changes as trades at their `set_at` and mark forward.
2. **Attribution by decision source** — did `strategy`-driven band changes beat
   `optimizer`-driven ones? Did `manual` overrides and `user-pin`s help or hurt?
3. **Attribution by sleeve / segment** — which theses actually paid.

## Non-goals

- **Not a tax/performance report.** No IRR-for-the-accountant, no per-lot
  realized-gain statement (that's `tax_lots` / the new tax calendar).
- **No predictions.** It scores the past deterministically; it never forecasts.
- **No trading, no model writes.** Pure read-side, like `risk`/`overview`.
- **Not real-time.** It runs over the stored ledger + cached price/FX history;
  a stale snapshot just means a stale end-point, surfaced as such.

## The load-bearing gaps (must close first)

Two things we store are too thin to attribute against, and the plan's first
commits fix them:

1. ~~**No daily FX panel.**~~ **CLOSED (phase 1, PR #175 + this PR).** History
   stores per-*trade* `fx_rate_to_base` at execution and base-converted NAV — but
   no `USDCZK`/`EURCZK` *time series*. You cannot split a NAV move into local-price
   vs FX, nor convert a benchmark's USD curve into CZK correctly, without one. →
   `tools/fx_history.py` builds an incremental daily panel at
   `data/cache/fx-history.json` (`series["USDCZK"][date]` = base per 1 unit of the
   foreign currency), topped up alongside the IBKR history sync in `holdings_sync`
   (the plan's "no new task"), with `rate_on` as-of lookup; this PR adds the first
   consumer (`exposure_by_currency` + `window_report` → `/api/risk`). **Source
   note:** IBKR only carries point-in-time trade rates (no daily series) and FRED
   has no clean daily USD/CZK, so the panel is sourced from **Yahoo `<CCY><BASE>=X`
   pairs** — behind the `fetch` seam so the provider swaps without touching
   consumers; IBKR trade rates can later *anchor/validate* the panel on trade dates.
2. **Provenance has no history.** `provenance[key]` is point-in-time (the *current*
   source per band); `commit_staged` strips the staging `_runs` audit and only
   drops a sparse dated `target-model-*.json` backup. "Followed every suggestion"
   and by-source attribution need a *timeline of band changes*. → append-only
   `provenance-log.jsonl`, written on every commit going forward (and best-effort
   backfilled from the existing dated backups).

## Design overview

Two thin data collectors feeding one pure attribution engine and one view.

```
ibkr history (NAV+trades+flows) ─┐
fx_history.json (daily FX panel) ─┼─> attribution.py (pure) ─> /api/attribution ─> Portfolio→Attribution
provenance-log.jsonl (timeline) ─┘        price-history cache (per-name, CZK via FX)
```

### Data model (new)

| Artifact | Where | Shape | Notes |
|---|---|---|---|
| Daily FX panel | `data/cache/fx-history.json` (gitignored) | `{ "base": "CZK", "series": { "USDCZK": {date: rate}, "EURCZK": {…} } }` | Incremental top-up like history; one provider round-trip per missing window. |
| Provenance timeline | `data/provenance-log.jsonl` (**private submodule**) | one JSON object/line: `{at, key, kind, change, before:{low,high,rule}, after:{…}, source, run_id, segment, conviction}` | Appended by `commit_staged`; the durable decision lineage backups can't give. |
| (reused) Per-name price series | `data/cache/prices/<SYM>.json` | existing Yahoo chart cache (native currency) | Converted to CZK via the FX panel at read time, so a name's return is FX-clean. |

`fx-history.json` is regenerable (cache); `provenance-log.jsonl` is precious
history and lives in the private `data/` submodule — which is exactly why the
**backup/restore drill** (the operational note) becomes load-bearing once this
ships.

### The computations (`tools/attribution.py`, all pure)

- **`time_weighted_return(nav_series, external_flows)`** — chain daily sub-period
  returns, neutralizing external cash flows (deposits/withdrawals from
  `cash_transactions`, *not* dividends/trades, which are internal to the book) so
  a big deposit isn't mistaken for performance. This is the honest spine every
  curve shares.
- **`counterfactual_hold(book_at_X, prices_czk, flows)`** — freeze weights at X,
  apply each holding's CZK total-return, re-inject flows pro-rata. "What if I'd
  never touched it."
- **`counterfactual_benchmark(flows, spy_czk)`** — every external flow buys the
  benchmark at that day's price; mark forward. SPY/QQQ priced in USD then
  converted through the FX panel — the FX fold-in in action.
- **`counterfactual_follow_all(provenance_log, prices_czk, flows)`** — replay each
  logged band change as an immediate rebalance to the new band midpoint; the gap
  vs *actual* is your execution/hesitation drag.
- **`decompose_fx(nav_series, positions_native, fx_panel)`** — split each period's
  CZK move into **local-price effect** (native return × start weight) + **FX
  effect** (FX return × start weight) + **flow**. Answers "did the strategy work
  or did the koruna move." Its residual is a data-quality check, surfaced not
  hidden.
- **`attribute_by_source(trades, provenance_log, prices_czk)`** — bucket each
  name's contribution by the provenance `source` in force while it was held;
  report contribution and hit-rate per source.
- **`attribute_by_group(contributions, sleeves, segments)`** — roll the same
  per-name contributions up by sleeve and by segment (segments overlap, so this
  is a lens, not a partition — labelled as such).

Every function takes plain dicts/lists and returns plain dicts — no disk, no
network — so the whole thing is unit-tested with fixtures, exactly like
`overview.py` and `risk.analyze`.

### FX-clean by construction

The single rule that makes #1 trustworthy: **every cross-currency number goes
through `fx_panel`, never through a single point-in-time rate.** A name's daily
return is computed in its native currency then converted; the benchmark's USD
curve is converted daily; `decompose_fx` reports the FX slice explicitly. There
is no "convert at today's rate and hope" path.

## API

`GET /api/attribution?from=<date>&benchmark=SPY` → one payload:
`{as_of, base, range, twr, curves:{actual, hold, benchmark, follow_all}, fx:{local_pct, fx_pct, residual_pct}, by_source:[…], by_sleeve:[…], by_segment:[…], caveats:[]}`.
Read-only; `Cache-Control: no-store`; heavy price fetches fan out under the
existing `_PULL_LOCK` + thread pool (mirroring `exit_plan._prewarm_caches`).

## Surfacing

- **New Portfolio sub-tab: Attribution** (`web/src/attribution.ts`, registered in
  `shell.ts` beside Risk/Tax/Journal). A NAV-curve chart (actual vs the three
  counterfactuals), an FX-decomposition strip (local vs FX vs residual), and two
  tables (by source, by sleeve/segment). Same chrome as `risk.ts`
  (`apiLoad`/`statTile`/`simpleTable`), loud caveats first.
- **Optional Risk tile**: "FX exposure: X% USD" — makes the hedging decision
  *visible* even before anyone acts on it.
- **Today cockpit**: a one-line verdict when the sample is long enough
  ("process +N.N% vs never-rebalanced over the window").

## File-by-file

| File | Change | Est. |
|---|---|---|
| `tools/fx_history.py` | ✅ **shipped (#175)** — incremental daily FX panel (Yahoo pairs + cache, `rate_on`); this PR adds `exposure_by_currency` + `window_report` | ~350 |
| `tools/holdings_sync.py` | ✅ **shipped (#175)** — FX top-up folded into the history sync job (no new task) | ~11 |
| `tools/attribution.py` | **new** — the pure engine (TWR, 3 counterfactuals, full FX split, 2 attributions) | ~300 |
| `tools/target_staging.py` | append to `provenance-log.jsonl` in `commit_staged`; a `backfill_provenance_log()` over existing backups | ~40 |
| `tools/serve.py` | `GET /api/attribution` handler + route; import | ~30 |
| `web/src/attribution.ts` + `index.html` + `style.css` + `shell.ts` | the view + nav wiring | ~260 |
| `tools/risk.py` + `web/src/risk.ts` + `style.css` | ✅ **shipped (this PR)** — `fx` block on `/api/risk` + currency exposure/FX-effect tile & table | ~90 |
| `tools/tests/test_attribution.py`, `test_fx_history.py`, `test_target_staging.py` (log) | fixtures for TWR, each counterfactual, FX split, source/group attribution, log append | ~350 |
| docs (`ORIENTATION`, `ARCHITECTURE`, `tools/README`) | new modules + endpoint + view | ~30 |

## Testing strategy

- **TWR**: a hand-computed 3-period series with a mid-window deposit — assert the
  deposit doesn't leak into return.
- **Counterfactuals**: synthetic 2-name book + fixed price/FX fixtures; assert
  *never-rebalanced* drifts to the winner, *benchmark* tracks SPY-in-CZK, and
  *follow-all* replays a logged band change.
- **FX split**: a USD-only book with flat native prices and a moving USDCZK →
  the entire NAV move must land in the FX slice, residual ≈ 0.
- **Attribution**: two names, two provenance sources, opposite outcomes → the
  right source gets credited/blamed; overlapping-segment double-count is handled.
- **Provenance log**: `commit_staged` appends the expected lines; backfill is
  idempotent; a missing/corrupt log degrades to "no timeline" (curves that need
  it are omitted with a caveat, not an error).
- Existing suites stay green; everything additive.

## Rollout (commit-by-commit)

1. ✅ **Shipped.** `fx_history.py` + `data/cache/fx-history.json` + the top-up
   folded into the `holdings_sync` history job + tests (PR #175) — the FX spine.
   Then pulled forward from the original step 4: the **currency-exposure +
   FX-effect tile on Risk** (`exposure_by_currency` / `window_report` → `/api/risk
   → fx`), which surfaces "% non-base" and a window FX-contribution
   estimate on the *current* book (labelled an approximation, not a realized-return
   statement). *(No new scheduler task: FX rides the existing history top-up, so
   attribution always sees same-vintage rates.)*
2. ✅ **Shipped (this PR).** `provenance-log.jsonl` writer in `commit_staged` +
   `backfill_provenance_log()` + tests — the decision timeline (nothing consumes
   it yet). Best-effort append: a log failure never rolls back a landed commit.
3. ✅ **Shipped (this PR).** `attribution.py`: flow-neutralized TWR +
   `never-rebalanced` + `benchmark` counterfactuals (pure `time_weighted_return` /
   `positions_at` / `hold_index` / `flow_curve` + the `attribution_report`
   assembler) + `/api/attribution` + the **Attribution** sub-tab (growth-of-100
   chart, actual-vs-baseline deltas), reading the cached history dict and pricing
   through the FX panel. **The 80% that answers "beats doing nothing?"** The
   frozen book is reconstructed by unwinding post-window trades; a ledger that
   starts after the window, or an unpriceable name, degrades to a caveat.
4. `decompose_fx` — the *full* per-period NAV split into local-price + FX +
   residual over the whole `nav_series` (the phase-1 tile is the current-holdings
   FX-effect shortcut; this is the exact, flow-aware version) + the FX strip on the
   Attribution view.
5. `attribute_by_source` + `attribute_by_group` + the tables. ✅ **Today verdict
   shipped** (follow-up PR): the cached headline (`cache_verdict`/`load_verdict`)
   feeds `overview.attribution_summary`, surfaced as a Process-attribution card on
   the Today cockpit (read-only over the cache — no network on the Today path).

## Open questions

1. ~~**FX source.**~~ **Settled: Yahoo `=X` pairs.** Ground truth (see the
   phase-1 gap note above): IBKR Flex stores only per-trade point-in-time rates —
   no daily series — and FRED has no clean daily USD/CZK. Yahoo `<CCY><BASE>=X` is
   the only source here that yields a continuous daily panel, so it is the default,
   kept behind the `fx_history` seam. IBKR's trade rates remain the authority on
   trade dates and are the natural later cross-check.
2. **Dividends/fees in TWR.** Decision: dividends are **internal** return (part of
   the book), only deposits/withdrawals are external flows. `cash_transactions[]`
   in `portfolio-history.json` carries a `type` and `base_amount` per row — the TWR
   flow filter keys off `type` (deposit/withdrawal vs dividend/fee). Confirm the
   exact `type` strings against a live report before phase 3.
3. **Benchmark set.** SPY + QQQ to start (converted to CZK through the FX panel —
   the fold-in in action). ACWI/VT remains a candidate for a home-bias-honest
   whole-market baseline.
4. **Provenance backfill depth.** The log is truthful only from ship-date forward;
   dated backups give a sparse, best-effort past. Lean: accept the partial history
   and label pre-log periods rather than reconstructing from strategy run
   manifests (`data/research/strategy/<run_id>.json`).
5. **"Follow every suggestion" semantics.** Rebalance to band *midpoint* at
   `set_at`. Midpoint is simplest and neutral.

## Reconciliation with `main` (reuse, don't rebuild)

- **Trade ledger reading is solved.** `tools/reconcile.py` already reads the
  `portfolio-history.json → trades[]` ledger (Z-tolerant parse via `timeutil`,
  `portfolio.clean_symbol`) for snapshot-vs-ledger drift. The attribution engine
  reads the *same* `trades[]`/`nav_series`/`cash_transactions` — no new Flex reader
  is needed; phase 3 consumes the cached history dict directly.
- **Journal auto-scoring already exists.** The `journal-score` scheduler task
  (`journal.score_outcomes`) stamps fixed-horizon outcomes on individual *calls*.
  Attribution scores the *system*; `attribute_by_source` should **complement** the
  journal's per-decision calibration, not re-derive it — link the two in the view
  rather than duplicating horizon logic.
- **Refactors landed.** `rebalance-model.ts`, `ticker_directory.py`, and the
  analysis prompt/report split reshaped files this plan references; re-grep before
  editing rather than trusting old line-level assumptions.

## Verification (every phase)

```powershell
py -3 -m pytest tools/tests -q
npm install; npm run lint; npm run typecheck; npm run test; npm run build
```
