# Assay — Feature Guide

A tour of everything Assay does, told from the surface the user actually touches.
Where `ARCHITECTURE.md` explains *how* the app is built and *why*, this file is
the **feature catalog**: every view, every button, and what it triggers.

- **User-facing** features are grouped by the app's five navigation groups.
- Each view lists what you see, what you can do, and the API endpoints it calls
  (so a maintainer can trace a button to its handler in `tools/serve.py`).
- A condensed **backend feature map** and an **endpoint index** close the doc.

> Assay is a **single-user, local-first portfolio research and rebalancing
> workbench**. The research/rebalance halves never trade; the opt-in **Trade
> desk** is the sole order-placing path, and it stays gated + paper-first.

---

## Navigation model

The header exposes **five workflow-ordered groups**:
**Today → Plan → Research → Orders → Portfolio**. Watchlist and Activity are
utility destinations; the Settings gear sits outside the groups.

| Group | Lands on | Sub-tabs |
| --- | --- | --- |
| **Today** | Daily command center | *(single view)* |
| **Plan** | Direction → Rebalance (`strategy`) | Guided plan · Optimizer · Pending model changes |
| **Research** | Segment leaderboard | Explore · Ticker · Deep Research *(+ Pipeline/Segment sub-pages)* |
| **Orders** | Order pipeline | Flowbar: Build orders · Review impact · Preview & place |
| **Portfolio** | Positions | Positions · History · Analytics |
| **Watchlist / Activity** | Their respective utility views | Activity also contains Decisions |

Behaviors that apply everywhere:

- **Group memory** — re-clicking most group headers returns you to the *last view
  visited* in that group. Orders is deliberately stable: its header always opens
  the Order pipeline index rather than a remembered placement step.
- **The URL is the persistence layer** — `?view=`, `?ticker=`, `?segment=`,
  `?run=` round-trip so any state is deep-linkable and back/forward works. A bare
  `/` means Today. Mangled `%3D`-encoded query strings (from chat/markdown
  renderers) self-heal on boot.
- **First-run redirect** — if the data dir is empty, boot lands on Settings.

---

## Cross-cutting UI

These are present regardless of which view is open.

### Header ticker search
A persistent search box autocompletes over tickers **you already have locally**
(the server's cached ticker index, 60s TTL) folded with your browser-local
*recently viewed* list. Prefix-on-symbol beats prefix-on-name beats substring;
empty query shows recents. Enter / ↑ / ↓ / Esc all work. Picking a ticker opens
the deep dive (live-pulling on a cache miss). It deliberately does **not** hit
live symbol search — that's the deep-dive's job for symbols you don't own yet.
`GET /api/ticker-index`

### Watchlist star (★)
A ★ appears on ticker rows across the app (deep dive, rebalance rows, strategy
table, segment table, discovered candidates…). Toggling it adds/removes the name
from the Watchlist basket, tagged `want` or `curious`, carrying provenance (which
segment/run it came from). A live count badge rides the Watchlist group header.
`POST /api/basket/add|remove`

### Task Center
A header indicator + floating pill surface background work (IBKR syncs, Deep
Research, LLM analysis, strategy runs, sector backfills). A panel splits *In
progress* vs *Recent* and deep-links a finished job to its result view. Polls
fast when active (~3.5s), slow when idle (~20s). Cancellable jobs (Q&A, segment
draft) get a cancel button. The registry is in-memory, so it **resets on server
restart** (the UI says so).
`GET /api/jobs` · `POST /api/deep-job/cancel`

### Error center
A badge (hidden at zero) opens a panel listing in-session errors by source
(Server / Network / Task / App), with per-row dismiss and Clear-all. Fed by the
`api()` helper (5xx + network failures), failed jobs, and global
`unhandledrejection`/`error` handlers. Same source+message within 5s collapses
with a count. (The Settings tab shows the *durable* server-side operational log
separately.)

### Privacy mode
A header toggle blurs every `[data-sensitive]` value (NAV, CZK amounts, account
ids, exposures). Persisted in `localStorage`. Public market data (momentum,
prices, multiples) stays visible.

### Select-to-analyze chip
Highlight a ticker-shaped token (1–7 chars) inside any rendered report/summary
prose and an "Analyze {SYM} →" chip pops up — the escape hatch for symbols that
were never auto-linked.

### In-report ticker links
Symbols inside rendered Deep Research reports and summaries are linkified against
the known-ticker set and routed to the deep dive without a full page reload.
`GET /api/tickers`

---

## Plan

### Direction → Rebalance (guided flow) — `strategy.ts`
The dominant path and the app's landing view. You type an investment **direction**
in plain language and the app runs a durable, gated state machine to turn it into
a staged rebalance proposal.

- **Start screen:** direction textarea, **Start guided run**, and a list of recent
  runs (resume any).
- **Stage stepper:** Draft → Segment → Research → Synthesize → Review →
  Recommendation. Completed stages are clickable for a read-only revisit.
- **Gate 1 (segment approval):** review/edit the LLM-drafted segment definition
  JSON, then **Approve segment & synthesize →**.
- **Gate 2 (proposal approval):** a band-shift table (★ per row) with an optional
  JSON edit; **Add to working draft →**, with an "apply blocked names anyway"
  escape hatch.
- **States handled inline:** running spinner; a Perplexity login gate (with *Open
  settings* / *Resume run*); a staged confirmation linking to the Working draft;
  and a done state showing a rebalance preview table.

Endpoints: `POST /api/strategy/start`, `GET /api/strategy/{run_id}` (polled 3s
while running), `POST /api/strategy/{run_id}/approve-segment`,
`POST /api/strategy/{run_id}/approve-proposal`, `GET /api/strategy/runs`,
`GET /api/staging`.

---

## Research

### Segment leaderboard — `leaderboard.ts`
Ranks every cached segment by a "promise" score so you can see *which universe is
hottest, and whether you own it*.

- **Tiles** per segment: promise score, 3M/12M momentum, a breadth bar, your
  exposure %, and a valuation-vs-growth figure. Stale segments get a warning dot.
- **Sort toolbar:** Promise · Momentum · Breadth · Exposure gap (client-side,
  with a FLIP re-order animation).
- **Actionable callouts:** *Hot but underweight* and *Cold but overweight*, each
  with *Open segment* and *Explore in Plan →* (prefills a strategy direction).

Endpoint: `GET /api/segments/leaderboard`.

### Ticker / Deep dive — `deepdive.ts` (+ `deepdive/*`)
The per-name research cockpit, opened from search, links, or a live pull.

- **Entry:** type a symbol → **Analyze** (live pull) or load from cache; a no-data
  empty state offers *Pull live*. Fast ticker switches drop stale renders.
- **Overview:** header (price, decision pill, held %, source badges), decision
  strip, decision-context grid, and a price chart with range selector.
- **Fundamentals:** business summary, valuation/momentum grids, and a **data-trust
  cross-checks** panel (identity/consistency severities).
- **Analysis:** an LLM analyst report (run/refresh, background job), a price-level
  **ladder editor + lock** (buy-below/trim-above), pin-to-model, a backends
  config modal, and Markdown export.
- **Deep Research card:** prior per-ticker runs, prompt builder, run/login.
- **Q&A card:** ask/clear/delete grounded questions against the latest analysis.
- **History:** the pull change-log and the **thesis editor** (human judgement,
  preserved across re-pulls).
- **No-market-data flow:** symbol search, candidate validation, alias mapping.

Endpoints: `GET /api/research/{sym}`, `POST /api/pull/{sym}`,
`GET /api/price-history/{sym}`, `GET /api/peer-stats`, `POST /api/analyze/{sym}`,
`GET /api/analysis/{sym}`, `GET|POST /api/qa/{sym}`, `GET /api/history/{sym}`,
`POST /api/history/delete`, `POST /api/thesis/{sym}`, `GET /api/price-levels`,
`POST /api/price-levels/lock|clear`, `POST /api/staging/edit`,
`GET|POST /api/analysis-config`, `GET /api/analysis-models`,
`GET /api/deep-runs`, `GET /api/deep-prompt`, `POST /api/deep-research/run`,
`GET /api/symbol-search`, `POST /api/symbol-candidates|/api/symbol-alias`.

### Reports / Analyses — `analyses.ts`
The reader + library for Deep Research runs.

- **+ New run** launches the Pipeline wizard (below).
- **Left list:** segments (grouped), the latest run per segment, sub-runs, and
  orphan runs; a **+ run** on a segment row starts the pipeline pre-selected;
  delete × per row.
- **Reader:** Markdown report with a TOC (structured-JSON fallback), the review
  gate, discovered candidates (★), citations, a collapsible prompt, **Delete
  analysis**, and a report Q&A card.

Endpoints: `GET /api/deep-runs`, `GET /api/deep-run/{stem}`,
`POST /api/deep-run/delete`, `GET /api/segments`, `GET /api/deep-prompt`,
`GET|POST /api/deep-qa`, `GET /api/tickers`.

### Segments — `segment.ts` (+ macro strip `regime.ts`)
A peer-comparison universe you can pull and rank.

- Dropdown segment picker, **Run live pull**, **Load cached**.
- A **sortable peer table** (score desc by default): ★, symbol, trend sparkline,
  decision pill, metric columns (price, mkt cap, fwd P/E, P/S, rev growth, gross
  margin, 3M/12M, vs-52w-high), held %. Row click → deep dive.
- A **macro regime strip** (rates/credit/USD/vol) above the table.

Endpoints: `GET /api/segments`, `POST /api/pull-segment/{name}`,
`GET /api/segment/{name}`, `GET /api/regime`, `GET /api/spark`.

### Pipeline wizard — `pipeline.ts` *(sub-page of Reports)*
A 4-step flow to run and apply Deep Research. Steps lock until the prior one has a
saved artifact on disk. **← Back to reports** exits.

1. **Segment** — pick existing, or create (LLM draft or manual JSON).
2. **Deep Research** — build prompt, run via Perplexity (or import a finished
   URL, or fall back to a deterministic pull), with an in-place login gate.
3. **Report** — save the report + citations, or import/pick an existing run.
4. **Review & apply** — run the review gate, then stage the proposal into the
   working draft.

Endpoints: `POST /api/segment-draft`, `POST /api/segment-def/{slug}`,
`GET /api/deep-prompt`, `POST /api/deep-research/run|import|save|review`,
`GET /api/deep-research/login-status`, `POST /api/deep-research/login|verify-login`,
`POST /api/pull-segment/{name}`, `GET /api/deep-runs`, `GET /api/deep-run/{stem}`,
`POST /api/target-proposal/apply`.

---

## Orders and rebalancing

The Orders group lands on a stable **Order pipeline** index (`orders.ts`) that
shows three distinct sources without conflating them:

- **Planned trades** — selected/deferred durable intent from
  `data/cache/execution-plan.json`.
- **Order queue** — exact local legs and projection-approval state from
  `data/cache/staged-basket.json`.
- **Working at IBKR** — live broker orders from Client Portal API.

The **flow bar** (`flowbar.ts`) remains the guarded action path:
*Current book → Build orders → Review impact → Preview & place*. Completed fills
remain under Portfolio History. (`GET /api/overview`,
`GET /api/execution-plan`, `GET /api/trade/basket`,
`GET /api/trade/orders`.)

The Optimizer and Working draft are documented nearby because they feed this
path, but their navigation home is **Plan**, not Orders.

### Rebalance planner — `rebalance.ts`
The core advice view: target-vs-holdings drift and band-closing suggestions.

- **Summary:** NAV, invested, snapshot freshness, cash target, funding-order hint.
- **Live stats:** cash freed/needed/net, cash after plan, bands closed — recompute
  on every edit.
- **Table:** targets + sleeves (with expandable members) + untargeted funding
  rows. Each row: a band track (ghost current / projected tick), an editable
  **Plan %**, a Czech tax-lot drawer, a research line (data-quality dot, thesis
  chip, 3M momentum, conflict→planner escalation), and a price-gate line from
  locked ladders.
- **Omnifilter:** ticker search + status/action/confidence facet pills.
- **Simulate basket** / **Reset to suggested**; a what-if panel (bands, cash, net
  cash, taxable gain, concentration deltas, trade table, **Log to journal**).
- A working-draft banner appears when previewing against a staged model.

Endpoints: `GET /api/rebalance`, `POST /api/whatif`, `POST /api/trade/basket`,
`POST /api/rebalance/funding`, `GET /api/trade/orders`, `GET /api/spark`.

### Optimizer — `optimizer.ts`
Whole-book sizing under explicit constraints.

- **Constraints:** cash target, per-name cap, max concentration, min position, max
  names, conviction curve, include-curious, drop-avoid, AI conviction synthesis.
- **Candidate pool** table with exclude checkboxes, source chips, conviction.
- **Analyze the pool** (background review job) · **Optimize** · **Stage to working
  draft →**.

Endpoints: `GET /api/optimizer`, `POST /api/optimizer/run|/api/optimizer/stage`,
`POST /api/portfolio-review`.

### Working draft — `staging.ts`
The staged target model you review before committing to live.

- Reconciliation tiles (budget / over-allocation warnings), overlap advisories,
  pinned convictions.
- Changes grouped **New / Adjusted / Removed** with band-shift viz and a **Keep
  current** per row.
- Post-commit: confirmation, **Go to Trade →**, and an optional **Revert this
  commit** with a diff preview.

Endpoints: `GET /api/staging`, `POST /api/staging/commit|discard|edit`,
`GET /api/target-model/restore-preview`, `POST /api/target-model/restore`.

### Target state — `targetstate.ts`
A read-only projection of where the book lands.

- A source banner (staged basket vs plan suggestions vs at-rest), summary tiles
  (bands in-band, cash, net cash, realized taxable gain), per-name band tracks
  (ghost "now" vs solid "after", unchanged names collapsed), and an expandable
  trade list.

Endpoints: `GET /api/rebalance`, `GET /api/trade/basket`, `POST /api/whatif`.

### Exit planner — `exit.ts`
Tax-timed, liquidity-aware scale-out ladders.

- **Config header:** horizon, ADV slice %, near-exempt days, tax rate (rebuilds on
  change).
- **Summary strip** + per-position cards: recommendation CTA, tax-layering bar,
  defer-lot list, a scale-out schedule table with **Stage →** per tranche, and an
  **options overlay** (covered call / protective put — analysis only).

Endpoints: `GET /api/exit-plan`, `POST /api/exit-plan/stage`.

### Trade desk — `trade.ts` *(the one gated exception)*
The only view that places real orders, and only when `IBKR_TRADING_ENABLED`.

- **Banners:** trading disabled, gateway disconnected, paper vs LIVE, live-lock.
- Staged basket → **Preview through IBKR** (margin/commission grid, per-order
  confirm checkboxes, band-effect rows, collision warnings vs working orders, a
  risk-delta panel, and a stale-snapshot gate).
- A preview **TTL countdown** on Place; **Confirm all** (paper only); a LIVE
  placement modal that requires typing the account id or `PLACE`.
- Result card (per-order outcomes, **Resync holdings**, **Log to journal**) and a
  working-orders card (refresh, cancel, **Keep at top** peg loop).

Endpoints: `GET /api/trade/basket|status|orders|tickle`,
`POST /api/trade/reconnect|preview|place|cancel|peg|peg/stop|basket`,
`POST /api/holdings/sync`.

---

## Portfolio

### Today — `overview.ts`
The cockpit and front door: one prioritized **Next step** CTA plus lane cards.

- **Portfolio lane:** holdings snapshot, standing plan, working draft, staged
  basket, decision journal, process-attribution verdict.
- **Research lane:** shortlist triage, research queue, segment universes (stale
  list). Each card links to its owning view.

Endpoint: `GET /api/overview` (+ `POST /api/holdings/sync` from the snapshot card).

### Positions — `holdings.ts`
- **Resync from IBKR** (background job + summary refresh).
- Hero (NAV, invested, uninvested, Top-2/5/10 concentration chips), a position
  list with tiered weight bars (options show exercise-% notional), a **Show asset
  values** toggle, and row → deep dive.

Endpoints: `GET /api/holdings`, `POST /api/holdings/sync`.

### History — `history.ts`
- A **NAV chart** with buy/sell markers (scroll-zoom, drag-pan), stat cards
  (latest NAV, change, realized P&L, trade count), and collapsibles (by sector,
  activity by name, trade ledger).
- **Update from IBKR** / **Rebuild full** / **Fetch sectors**; a friendly empty
  state + setup guidance when the history Flex query is missing.

Endpoints: `GET /api/portfolio-history`, `POST /api/portfolio-history/sync|sectors`,
`GET /api/ibkr/status`.

### Risk — `risk.ts`
Portfolio-level correlation risk (not per-name bands).

- Caveat banner, headline (effective bets, co-movement share, portfolio vol, avg
  pairwise correlation, FX exposure), a currency-exposure table, factor-shock
  stress cards, a correlation heatmap, and a per-name volatility table. Window
  selector (3mo–5y).

Endpoint: `GET /api/risk`.

### Attribution — `attribution.ts`
Did the *process* earn its keep? Actual time-weighted return vs two skill-free
baselines.

- Caveats + meta (window, benchmark, net flows), TWR stats vs never-rebalanced and
  vs benchmark (pp deltas), and a growth-of-100 chart of all three series.
  Benchmark (SPY/QQQ) + window selectors.

Endpoint: `GET /api/attribution`.

### Tax — `tax.ts`
The forward Czech 3-year exemption calendar.

- Headline tiles (tax-free soon, exemptions upcoming, harvestable loss, days to
  year-end), a year-end harvest nudge, an **Exemptions** table (gain lots nearing
  the 3-year mark), and a **Harvest deadlines** table (loss lots). Soon-window
  selector.

Endpoint: `GET /api/tax-calendar`.

### Journal — `journal.ts`
A decision journal with outcome calibration.

- A logging form (symbol, action, size CZK, price, review date, thesis, expected
  outcome), a calibration headline (hit rate, count, avg move after buys/trims),
  and entry cards with **Record outcome**. Prefilled from rebalance-simulate and
  trade-place.

Endpoints: `GET|POST /api/journal`, `POST /api/journal/outcome`.

---

## Watchlist

### Watchlist basket — `basket.ts`
The cross-surface shortlist that feeds the guided plan and optimizer.

- A table (ticker, trend spark, Want/Curious toggle, source, held %, in-plan band,
  note, ★), **Clear basket**, **Draft a plan from these picks →** (guided run),
  and **Optimize portfolio →**.

Endpoints: `GET /api/basket`, `POST /api/basket/add|remove|tier|clear|draft-plan`.

---

## Settings

### Setup wizard — `setup.ts`
Collapsible steps with a progress header that points at the next incomplete step.

1. **LLM CLI** — Claude/Cursor install/auth badges, enable toggles, model pickers,
   smoke checks, web-research toggle, timeout.
2. **Perplexity** — login setup/verify, profile path.
3. **IBKR** — Flex token, positions + history query IDs, save, sync.
4. **Environment** — SEC user-agent, FMP key (advanced).
5. **Automation** — background auto-refresh toggle + recent task activity.
6. **Error log** — the durable server operational log (refresh / clear).

Endpoints: `GET /api/setup/status`, `POST /api/setup/check|ibkr|automation`,
`GET|POST /api/analysis-config`, `GET /api/analysis-models`,
`POST /api/deep-research/login|verify-login`, `POST /api/holdings/sync`,
`GET|POST /api/error-log`.

---

## Backend feature map

The Python backend (`tools/`, stdlib-only threaded `http.server` on
`127.0.0.1:6060`) behind the views above. Modules grouped by role:

**Serving & infra** — `serve.py` (routing + dispatch), `apierror.py` (error
vocabulary), `devreload.py` (dev live-reload), `mcp_server.py` (read-only MCP
adapter), `config.py` (paths + secrets), `store.py` (atomic JSON IO),
`errorlog.py` (durable op log), `timeutil.py` (ISO/TTL), `hygiene.py` (severity
ranking).

**Providers** — `providers/{yahoo,sec_edgar,fmp,fred,common}.py` (data-source
adapters returning metric nodes).

**Research / facts** — `research_pull.py` (dossier puller + cross-checks),
`portfolio.py` (weights + rule semantics), `symbols.py` (display↔provider),
`ticker_directory.py` (known universe + prompts), `peer_stats.py`,
`quote_cache.py`, `sectors.py`, `instruments.py`, `segments_service.py`,
`verify_claims.py`, `generate_site.py`.

**Deep Research / narrative** — `pplx_deep_research.py` (Perplexity Playwright),
`browser_jobs.py`, `deep_runs.py`, `review_deep_research.py` (the gate),
`report_tickers.py`, `ticker_analysis.py` + `analysis_prompts.py` +
`analysis_report.py` (LLM analyst tier), `analysis_jobs.py`, `research_brief.py`.

**Target model + sizing** — `target_model.py`, `target_staging.py` (the sole path
to live), `target_construct.py` (per-segment), `optimizer.py` (whole-book),
`basket.py`, `sleeve_aliases.py`, `price_levels.py`.

**Advice layer (never orders)** — `rebalance.py` + `rebalance_overlay.py`,
`whatif.py`, `exit_plan.py` + `options_overlay.py` + `options_math.py`,
`overview.py` (Today), `reconcile.py`, `strategy_service.py` + `orchestrate.py`
(guided flow).

**Risk / tax / attribution** — `risk.py`, `risk_delta.py`, `fx_history.py`,
`attribution.py`, `tax_lots.py`, `tax_calendar.py`, `regime.py`.

**IBKR / trade** — `ibkr_portfolio.py`, `ibkr_history.py`, `holdings_sync.py`
(read-only), `ibkr_trade.py` + `trade_service.py` + `order_peg.py` +
`order_watch.py` (gated).

**Jobs / scheduler / notify** — `jobs.py`, `scheduler.py`, `notify.py`.

**Journal** — `journal.py`.

---

## Endpoint index

A flat reference of the ~107 API routes (parameterized paths shown with `{…}`):

```
Holdings / portfolio
  GET  /api/holdings                 POST /api/holdings/sync
  GET  /api/overview
  GET  /api/portfolio-history        POST /api/portfolio-history/sync|sectors
  GET  /api/ibkr/status

Research / dossier
  GET  /api/research/{sym}           POST /api/pull/{sym}
  GET  /api/history/{sym}            POST /api/history/delete
  POST /api/thesis/{sym}
  GET  /api/price-history/{sym}      GET  /api/spark
  GET  /api/peer-stats               GET  /api/tickers   GET /api/ticker-index
  GET  /api/analysis/{sym}           POST /api/analyze/{sym}
  GET  /api/qa/{sym}                 POST /api/qa/{sym}
  GET  /api/analysis-config          POST /api/analysis-config
  GET  /api/analysis-models          POST /api/portfolio-review

Deep research pipeline
  GET  /api/deep-runs                GET  /api/deep-run/{stem}
  POST /api/deep-run/delete          POST /api/deep-research/save|run|login|import|verify-login|review
  GET  /api/deep-research/login-status
  GET  /api/deep-prompt              POST /api/target-proposal/apply
  GET  /api/deep-qa                  POST /api/deep-qa

Segments
  GET  /api/segments                 GET  /api/segments/leaderboard
  GET  /api/segment/{name}           GET|POST /api/segment-def/{name}
  POST /api/segment-draft            POST /api/pull-segment/{name}
  GET  /api/regime

Target model / staging
  GET  /api/target-model             GET  /api/staging
  POST /api/staging/commit|discard|edit
  GET  /api/target-model/restore-preview   POST /api/target-model/restore
  GET  /api/basket                   POST /api/basket/add|tier|remove|clear|draft-plan
  GET  /api/optimizer                POST /api/optimizer/run|stage
  GET  /api/price-levels             POST /api/price-levels/lock|clear

Rebalance / advice
  GET  /api/rebalance                POST /api/rebalance/funding
  GET  /api/exit-plan                POST /api/exit-plan/stage
  POST /api/whatif
  GET  /api/strategy/runs            GET  /api/strategy/{run_id}
  POST /api/strategy/start|.../approve-segment|.../approve-proposal

Risk / attribution / tax
  GET  /api/risk                     GET  /api/attribution
  GET  /api/tax-calendar             POST /api/tax-plan

Trade / IBKR
  GET  /api/trade/status|tickle|orders|basket
  POST /api/trade/reconnect|preview|place|cancel|peg|peg/stop|basket

Jobs / tasks
  GET  /api/jobs                     GET  /api/deep-job     POST /api/deep-job/cancel

Journal
  GET  /api/journal                  POST /api/journal|/api/journal/outcome

Setup / config / misc
  GET  /api/setup/status             POST /api/setup/check|ibkr|automation
  GET  /api/symbol-aliases           POST /api/symbol-alias
  GET  /api/symbol-search            POST /api/symbol-candidates
  GET  /api/error-log                POST /api/error-log
  GET  /api/dev/livereload           (dev only)
```

---

*Generated from source inspection of `web/src/` and `tools/serve.py`. When a
button and this doc disagree, the code wins — update this file.*
