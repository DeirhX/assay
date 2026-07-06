# Segment-promise UX roadmap

Status: **draft** (written 2026-07-06) · Scope: making the website answer
"which segments are showing the most promise, and am I positioned for them?"
at a glance, and giving every buy/trim decision visible trend context. This is
a planning document, not a commitment; nothing here touches order placement —
it is all read-only visualization over data the app already collects (plus two
small provider additions).

Origin: a 2026-07-06 visual walkthrough of the live app (Plan, Today,
Rebalance, Segments, Deep dive, Target state, Trade, History, Risk, Watchlist).
Verdict: the visual foundation (dark theme, band tracks, flow bar) is strong,
but the app's surfaces are organized around *maintaining the target model*;
the segment-promise question has no surface at all — one segment at a time,
behind a dropdown, as a numbers-only table.

## Design constraints (carry through every phase)

- **No chart library.** All new visuals are hand-rolled SVG, same as
  `web/src/band-viz.ts`. Keeps the no-framework SPA stance and the bundle tiny.
- **Backend stays stdlib-only.** New endpoints aggregate *cached* files; a
  render never triggers a network fetch. Freshness stays the scheduler's job.
- **Privacy mode covers everything new.** Any NAV-, exposure-, or CZK-derived
  value renders through `sensitive()`. Exposure *percentages* are sensitive;
  segment momentum/breadth are public market data and are not.
- **Every pure builder gets a unit test** (vitest for TS builders, unittest for
  Python aggregators), mirroring `flowbar.ts` / `overview.py` discipline.
- **Each phase lands as 1–2 reviewable PRs** behind no flags — these are
  read-only UI features; dark-launch complexity isn't warranted.

---

## Phase 0 — bugs found during the walkthrough (immediate, one PR)

1. **Privacy-mode leak: IBKR account id.** With privacy mode ON, the Trade
   desk banner renders the live account id in clear text. Wrap every account-id
   render in `sensitive(...)`: the connection banner and preview-card title in
   `web/src/trade.ts` (`renderConnection`, `renderPreview`), the place-confirm
   modal, and `placeResultHtml`. Audit the rest with a grep for
   `account` across `web/src/*.ts`; add a vitest asserting the banner builder
   emits `data-sensitive` around the id.
2. **Deep-dive lands mid-page.** Navigating to
   `?view=deepdive&ticker=SYM` can render with the topbar scrolled into the
   middle of the viewport. Likely `$("#ticker-input").focus()` in
   `shell.ts:restoreNav` scrolling mid-layout. Fix:
   `focus({ preventScroll: true })` plus an explicit `window.scrollTo(0, 0)`
   on every `setActiveView` view *switch* (not on popstate back/forward, which
   should restore scroll). Add an e2e assertion: after deep-link load,
   `window.scrollY === 0`.

Exit criteria: no unblurred account id anywhere under privacy mode; deep links
land at the top of the page.

---

## Phase 1 — the segment leaderboard (the money view)

**Goal:** one screen ranking all segments by "promise," with your exposure
overlaid. Becomes the Research group's landing view.

### 1a. Backend: `GET /api/segments/leaderboard` (`tools/segments_service.py`)

Aggregate over every cached segment pull (`SEGMENT_OUT_DIR/*.json`) — no
fetching. Per segment compute:

- `momentum_3m_med`, `momentum_12m_med` — median member momentum (fields
  already in the cached peer rows).
- `breadth_3m` — share of members with positive 3M momentum.
- `val_growth_med` — median of (fwd P/E ÷ rev growth) over members with both;
  null-safe (many rows are n/a — report coverage count alongside).
- `exposure_pct` — sum of held weights of members, from
  `portfolio.holdings_weights` (the one weight definition; invariant §11.1).
  Overlapping segments mean exposures don't sum to 100% — the UI must label
  this ("segments overlap").
- `score` — a transparent composite for the default sort. Deliberately dumb
  and documented: `rank(momentum_3m_med) + rank(breadth_3m)` — no magic
  weights, shown on hover. The point is ordering, not prediction.
- Meta: `cached_at`, `member_count`, `stale` (reuse the segment-staleness rule
  the scheduler already applies).

Unit tests: fixture segment caches → deterministic medians/breadth/exposure;
overlap case; empty/missing-field rows.

### 1b. Frontend: `web/src/leaderboard.ts` + view wiring

- New view `leaderboard`, group `research`, becomes `GROUP_DEFAULT.research`
  (Ticker/Reports/Segments stay as sub-tabs; deep links unchanged).
- Layout: **ranked tile grid** (not a treemap in v1 — treemaps need size
  normalization decisions that fight overlapping exposures; revisit later).
  Each tile: segment title, 3M/12M momentum (tone-colored), breadth as a tiny
  fraction bar, exposure as a `sensitive()` chip, staleness dot reusing the
  existing freshness idiom.
- Sort toggles: promise (default) · momentum · breadth · exposure gap.
- **Exposure-gap callouts** pinned above the grid: "hot but underweight" (top
  quartile score, exposure < X%) and "cold but overweight" (bottom quartile,
  exposure > Y%). These are the two actionable states.
- Click-through: tile → existing `?view=segment&segment=<slug>` table.
- **Plan-flow hook:** each callout carries "Explore in Plan →" which navigates
  to the strategy view with the direction input pre-filled (e.g. "add
  exposure to <segment title>") via a nav param — turning the leaderboard
  finding into a guided run in one click. No new backend; it's prefill only.

Pure builders (`leaderboardTileHtml`, `rankSegments`, `exposureGaps`) exported
for vitest. E2e: leaderboard renders from fixture payload; tile click lands on
the segment table.

Exit criteria: from a cold open, "which segment is hottest and am I in it?"
is answered in one screen and zero clicks.

---

## Phase 2 — trend context in every symbol row

**Goal:** no naked tickers at decision points — every row where you size or
triage a name shows the shape of its recent move plus decision-relevant chips.

### 2a. Spark data: `GET /api/spark?symbols=A,B,C` (`tools/quote_cache.py`)

- Serves ~90 daily closes downsampled to ≤30 points per symbol, from cached
  data only: dossier `price_history` when present, else the quote cache.
  Missing symbol → omitted from the response (the UI degrades to no
  sparkline); the endpoint never fetches.
- Add a scheduler note (not in scope here): the existing gate-quote sweep
  already keeps gated names fresh; plan-row names piggyback on the dossier
  refresh cadence. Good enough for a 90-day shape.

### 2b. `web/src/spark.ts` — one sparkline component

- `sparkSvg(points, { tone })`: ~40 lines of SVG polyline, tone from
  first→last sign, no axes, fixed 96×24 box, `<title>` = 3M change. Pure,
  vitest-covered.
- Batch hydration helper: views render tables instantly, then one `/api/spark`
  call per view fills placeholders (`data-spark="SYM"`). No per-row requests.

### 2c. Where it lands

| Surface | Adds |
| --- | --- |
| Segment peer table (`segment.ts`) | sparkline column after SYMBOL |
| Rebalance planner rows (`rebalance.ts`) | sparkline + `3M` + `vs 52WH` chips beside the band track |
| Watchlist (`basket.ts`) | sparkline + score + momentum columns (fills the starved table; NOTE column earns its place) |
| Deep-dive header | unchanged (already has the full chart) |

### 2d. Decision chips: RS and earnings date

- **Relative strength:** `rs_3m = momentum_3m − SPY.momentum_3m`. Cache SPY's
  momentum in the quote cache (one symbol, same refresh path). Where a segment
  contains an ETF row (e.g. SOXX in AI Semiconductors), the segment table also
  shows RS vs that ETF. Rendered as a signed chip (`RS +12%`), tone-colored.
- **Earnings proximity:** extend `tools/providers/yahoo.py` to capture the
  next earnings date (calendarEvents) into the dossier during normal pulls
  (new metric node, source-stamped like everything else). Chip `ER 6d` when
  within 14 days, on: peer rows, watchlist, rebalance rows, and — as a
  guardrail — **trade preview order lines** ("earnings in 6d" warning chip,
  server-side in `trade_service.preview` warnings so it also reaches the
  pre-flight list).

Exit criteria: planner, peer table, and watchlist each show shape + RS at row
level; staging a name into earnings produces a visible warning at preview.

---

## Phase 3 — peer table upgrades (picking within a segment)

All client-side over the existing cached payload; no backend work.

1. **Median summary row** pinned under the header: segment medians for each
   numeric column. Every value then reads against its baseline.
2. **Relative shading:** color numeric cells by z-score vs the segment median
   (two tone steps each way), replacing raw green-positive/red-negative for
   valuation columns where "positive" ≠ "good".
3. **Scatter toggle:** table ↔ scatter of rev growth (x) vs fwd P/E (y), dot
   size = market cap, held names ringed, quadrant hint labels
   ("cheap & growing" corner). Hand-rolled SVG (~120 lines), hover = row
   tooltip, click = deep dive. Names missing either metric are listed under
   the plot rather than silently dropped (the app's "disagreements are
   surfaced" discipline).
4. **Held% as a mini-bar** instead of a faint numeral, `sensitive()`-wrapped.

Pure builders + vitest for the z-scoring and scatter scaling; e2e toggles the
scatter and clicks through to a deep dive.

---

## Phase 4 — cold-start and cockpit visuals

1. **Segments view auto-loads** the last-viewed (else freshest) cached
   segment instead of the "No segment loaded" instruction card. URL behavior
   unchanged; `Run live pull` stays the explicit refresh.
2. **Plan view context cards** above the direction input, fed by the Phase 1
   endpoint: hottest-underweight, coldest-overweight, biggest breadth mover.
   Each pre-fills the direction prompt on click. Kills the dead space and
   seeds the guided run with the user's own data.
3. **Today gets one visual lane:** NAV sparkline (from the history cache — the
   `/api/portfolio-history` payload already has daily NAV; add a `?tail=90`
   cheap mode), a sleeve-exposure donut (SVG, `sensitive()` labels), and a
   5-tile mini-leaderboard strip linking to Phase 1's view. Keep the
   next-step banner primacy — visuals sit below it.
4. **Rebalance cash tiles → one flow bar:** trims-funding-buys as a single
   horizontal stacked bar (freed / floor headroom / still-needed), replacing
   four stat tiles with one glanceable figure. Keep "Fund this plan" button
   placement.

---

## Phase 5 — polish (cutting-edge feel, cheap wins)

- **Skeleton loaders** for table views (CSS shimmer on placeholder rows)
  replacing spinner-then-pop; one shared `.skeleton` class.
- **Stat-tile unification:** one `statTile` component already exists in
  `core.ts` — migrate Risk/History/Target-state local variants to it; single
  place for label size, value scale, tone borders.
- **Command palette (Ctrl+K):** extend the existing header ticker autocomplete
  into a two-mode palette — symbols (current behavior) + actions ("go to
  risk", "resync holdings", "load segment: <slug>"). Actions are a static
  registry mapping to `setActiveView`/existing buttons; no new backend.
- **Reduced-motion + focus-visible audit** across the new SVG/hover surfaces.

---

## Sequencing and sizing

| Order | Item | Size | Depends on |
| --- | --- | --- | --- |
| 1 | Phase 0 bug PR | S | — |
| 2 | Phase 1a endpoint + tests | M | — |
| 3 | Phase 1b leaderboard view | M | 1a |
| 4 | Phase 2a/2b spark endpoint + component | M | — |
| 5 | Phase 2c row integration | M | 2a/2b |
| 6 | Phase 2d RS + earnings chips (provider touch + preview warning) | M | 2a |
| 7 | Phase 3 peer table (median row, shading, scatter) | M | — |
| 8 | Phase 4 cold-start + cockpit | M | 1a (for cards/strip) |
| 9 | Phase 5 polish | S–M | — |

Phases 1, 2, and 3 are independent tracks after Phase 0; the leaderboard
(items 2–3) is the highest-value start. Suggested first three PRs:
**(1) Phase 0 bugs, (2) leaderboard endpoint, (3) leaderboard view.**

## Non-goals (explicit)

- No new data providers beyond the yahoo earnings-date field; no news feeds,
  no social sentiment, no intraday charts.
- No score-driven automation: the leaderboard ranks and links, it never
  stages, sizes, or proposes trades. Advice ≠ orders (invariant §11.4).
- No treemap/heatmap layout in v1 (overlapping segments make area encoding
  misleading); reconsider only if the tile grid proves insufficient.
- No mobile layout work in this roadmap.
