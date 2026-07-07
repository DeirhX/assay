# Tools

## Tests

Stdlib `unittest`, no dependencies. They cover the parts where a silent bug
costs money: the trust engine (`research_pull` cross-checks, market-cap
reconciliation/quarantine, metric/profile merge, peer score), the target-band
decision logic (`portfolio`, `rebalance` model validation), the claim verifier
(`verify_claims` identity/staleness checks), the site generator
(`generate_site` GEN-marker replacement and `--check` semantics), the review
gate (`review_deep_research` conflicts, proposals, blocks), the IBKR Flex XML
parser (`ibkr_portfolio`) and the windowed trade/NAV history reconstructor
(`ibkr_history` window planning, trade/NAV parsing, dedupe, inception detection,
incremental top-up merge, anchor/rate-limit/1003 handling),
the server's request guards (`serve` malformed-JSON
400s, body-size cap, loopback-only host refusal), the identifier guards
(`store`), the job registry incl. cooperative cancel (`jobs`), the
analysis layer's web-tool wiring, timeouts, grounding-rule switch, config
validation, and cancellable subprocess (`ticker_analysis`), and the confidence
layer -- the portfolio risk math (`risk` correlation/vol/effective-bets/stress),
the Czech tax-lot selection (`tax_lots`), the what-if recompute (`whatif`), and
the decision-journal calibration (`journal`).

```powershell
py -3 -m unittest discover -s tools/tests -p "test_*.py" -t tools/tests
py -3 -m ruff check tools             # lint (CI runs both)
npm test                              # frontend unit tests (Vitest, web/tests/)
```

They run offline (no network, no live CLI) in ~1s. The config test sandboxes
`CONFIG_PATH` to a temp dir, so your real `analysis-config.json` is never
touched; the integration smoke test reuses a real committed `data/research/*.json`
dossier and skips cleanly if none is present.

## serve.py + research_pull.py + providers/ (Interactive Research Console)

On-demand deep analysis for a single ticker or a whole industry segment. Stdlib
only -- no pip installs.

```powershell
$env:SEC_USER_AGENT = "assay research (you@example.com)"
py -3 tools/serve.py            # UI + API at http://127.0.0.1:6060 (localhost only)

py -3 tools/research_pull.py --ticker NVDA       # CLI: one deep dive
py -3 tools/research_pull.py --segment semiconductors   # CLI: whole peer set
```

### Pieces

- `providers/yahoo.py` -- price/momentum/history (chart endpoint) + fundamentals
  (quoteSummary, via the cookie+crumb handshake). Same source as `yfinance`,
  hit directly.
- `providers/sec_edgar.py` -- the free, authoritative cross-check for **US
  filers**: shares outstanding, revenue, net income from XBRL `companyfacts`.
  Foreign filers (ADRs) often have thin/absent data; the app flags that rather
  than pretending it verified anything.
- `providers/fmp.py` -- optional third opinion; enabled only if `FMP_API_KEY` is
  set (read from `secrets.env`, gitignored).
- `portfolio.py` -- shared holdings/target-model helpers: computed position
  weights, target context, holdings payloads, and decision labels used by the
  server, puller, and generated-site checks.
- `research_pull.py` -- pulls all sources, merges with a preferred-source order,
  and **cross-checks** them (price x shares vs market cap, Yahoo vs SEC share
  count, TTM revenue agreement, price freshness, single-source warnings). Writes
  `data/research/<SYMBOL>.json`, ignored history snapshots under
  `data/cache/research-history/<SYMBOL>/`, and `data/research/segments/<name>.json`.
  It preserves any human-authored `thesis` block across re-pulls. A pull that
  retrieved no usable data (no price and no metrics) is **not** appended to
  history; `delete_history` removes a single snapshot (wired to the Deep Dive
  history table's delete button via `POST /api/history/delete`).
- `serve.py` -- stdlib `http.server` app serving `web/` and a small JSON API
  (`/api/holdings`, `/api/segments`, `/api/research/<sym>`, `POST /api/pull/<sym>`,
  `/api/history/<sym>`, `POST /api/history/delete`, `POST /api/pull-segment/<name>`,
  `POST /api/thesis/<sym>`,
  the Deep Research pipeline endpoints for segment drafting, artifact saving,
  review, and target-proposal approval, plus the automated-run endpoints
  `POST /api/deep-research/run`, `POST /api/deep-research/login`,
  `POST /api/deep-research/verify-login`, `GET /api/deep-research/login-status`,
  and `GET /api/deep-job?id=<id>`). `POST /api/holdings/sync` runs the IBKR Flex
  pull as a registered background job (kind `ibkr_sync`, polled via
  `/api/deep-job`) so it survives navigation, shows in the global task pill, and
  counts as active for the `--reload` watcher; one sync runs at a time. It serves
  root-level `.html`/`.css`/`.js` assets dynamically instead of maintaining a
  manual filename allowlist.
- `review_deep_research.py` -- offline review gate for saved Perplexity reports.
  It compares source quality, deterministic ticker data, holdings, and
  `target-model.json`, then writes a review markdown file and draft target-model
  proposal.
- `pplx_deep_research.py` -- **optional** Playwright worker that drives a
  logged-in Perplexity session to run in-app Deep Research and auto-save the
  artifacts. This is the only heavy dependency in the repo and is **not** imported
  by `serve.py` at load; the server imports it lazily inside a job thread, so if
  Playwright is absent the manual paste flow still works. See "Automated Deep
  Research" below.

### Data outputs

- `data/research/<SYMBOL>.json` -- per-ticker numbers + cross-checks + compact
  1Y Yahoo daily-close price history + thesis. Carries human judgement, so it
  can be committed.
- `data/cache/research-history/<SYMBOL>/*.json` -- ignored numeric snapshots from
  repeated pulls. Used by the dossier's "Recent pulls" table.
- `data/research/deep/<segment>-<date>.md` -- committed Perplexity Deep Research
  report text.
- `data/research/deep/<segment>-<date>.sources.json` -- committed Links-tab
  citation extraction.
- `data/research/deep/<segment>-<date>.review.md` -- review-gate output.
- `data/research/deep/<segment>-<date>.target-proposal.json` -- draft target
  changes for explicit human approval.
- `data/research/segments/<name>.json` -- derived peer dashboard (gitignored).
- `data/cache/sec_ticker_cik.json` -- weekly ticker->CIK cache (gitignored).
- `data/segments/<name>.json` -- **input** universe definition (committed), e.g.
  `semiconductors.json` with sleeves named after `data/target-model.json` ones.
  These are research lenses and may overlap; they are not target-model
  allocation sleeves.

### Segment and Deep Research workflow

The website is the normal control plane:

1. Open `http://127.0.0.1:6060` and use the Pipeline tab.
2. Draft or approve a segment. Segment JSON is stored in `data/segments/*.json`.
3. Run deterministic pulls for the segment.
4. Generate the Perplexity prompt, then either:
   - click **Run Deep Research** to automate it (see below), or
   - run it manually in Perplexity and paste the report + Links-tab citations.
5. Save the report and Links-tab citation JSON (the automated path does this for
   you).
6. Run the review gate.
7. Inspect target-model proposals and explicitly approve only if you want the
   target model changed.

### Automated Deep Research (optional, Playwright)

`Run Deep Research` puppets a **logged-in Perplexity web session** to spend the
Pro subscription's included quota (~20/day) instead of the metered API. It is
isolated in `pplx_deep_research.py` and only runs when you click the button.

Setup (once):

```powershell
py -3 -m pip install playwright
py -3 -m playwright install chromium
```

Then in the Pipeline tab click **Set up login** once (opens a visible window;
complete the Perplexity login). After that, **Run Deep Research** launches an
**off-screen** (invisible but not headless) browser, selects the Deep research
search mode, submits the prompt, polls until done, scrapes the report + Links-tab
citations, and saves the artifacts. The job is async; the UI polls
`/api/deep-job?id=...`.

Honest constraints:

- **Headless is blocked** -- Perplexity sits behind Cloudflare and a headless
  browser gets challenge-walled (no composer). The default is headed-off-screen
  for that reason; `window_mode: "headless"` exists only for experiments.
- **Dedicated automation profile.** The worker uses its own persistent profile
  (`~/.cursor/pplx-automation-profile`, override with `PPLX_PROFILE_DIR`) rather
  than sharing the `user-playwright-pplx` MCP browser's `pplx-chrome-profile`.
  Chromium profiles are single-writer locked, so sharing would make the worker
  and the MCP browser fight over the lock, and their Chrome versions could skew.
  This means you log in **twice** (once per profile): the Setup tab's "Set up
  Perplexity login" handles the automation profile. Only one browser job (run or
  login) runs at a time.
- **Quota is shared** with your manual Perplexity usage; don't smoke-test against
  it. Use `--dry-run` (selects the mode but never submits) to validate plumbing.
- Deep Research output is narrative synthesis -- the review gate still treats its
  numbers as claims to verify, not ground truth.

CLI (debugging the worker directly):

```powershell
py -3 tools/pplx_deep_research.py --login                 # visible login window
py -3 tools/pplx_deep_research.py --dry-run --prompt "x"  # mode select, no submit, no quota
py -3 tools/pplx_deep_research.py --prompt "deep research on ..."   # full run (spends quota)
```

The console encodes its current location in query parameters so browser
Back/Forward and shared links work for the main views, e.g.
`/?ticker=AMD`, `/?view=segment&segment=semiconductors`, and
`/?view=pipeline&run=fintech-payments-2026-06-03`.

CLI fallback (review gate):

```powershell
py -3 tools/review_deep_research.py --segment fintech-payments --date 2026-06-03
```

This writes:

```text
data/research/deep/fintech-payments-2026-06-03.review.md
data/research/deep/fintech-payments-2026-06-03.target-proposal.json
```

### Relationship to verify_claims.py

`verify_claims.py` stays the **offline** consistency check for the committed
claims in `research-claims.json`. The console is the **live** counterpart that
`tools/README` previously called "a later phase": it cross-checks fetched numbers
against an independent source at pull time.

## target-model.json + rebalance.py (Rebalancing Spine)

Where `current-holdings.json` answers *"where am I?"*, `data/target-model.json`
answers *"where do I want to be?"* — as **data, not prose**. It is the
machine-readable replacement for the target weights that used to live only in
`CURRENT_PLAN.md`. All weights are percent of NAV.

Structure:

- `cash_target_pct` — desired cash sleeve.
- `sleeves.<name>` — a combined target band (`low`/`high`) shared across
  `members` (e.g. `semis-equipment` = AMAT/LRCX/KLAC at 5-7%). Optional
  `member_caps` bound an individual member.
- `targets.<SYMBOL>` — a per-name band (`low`/`high`), a `rule`, an optional
  longer-horizon `structural` band, and a `note`. Bands are **no-trade zones**:
  only act when the current weight falls outside `[low, high]`.
- `rules_legend` documents the rules: `accumulate`, `trim_only`, `do_not_add`,
  `reduce`, `hold`, `wait`, `avoid`.

This is **judgement** (human-set). `tools/rebalance.py` is the **computed** side
— it never invents targets, it only measures against them:

```powershell
py -3 tools/rebalance.py            # drift preview + plain-language advice
py -3 tools/rebalance.py --check    # validate the model; exit 1 on ERROR
py -3 tools/rebalance.py --check --strict   # also exit 1 on WARN
```

Default mode prints a drift table (current weight vs band per name/sleeve) and a
short **advice** list — trim/add suggestions in %NAV, ordered by funding
priority. It is deliberately advice for a human, **not** an order generator:
no share counts, no netting, no execution. The user decides and trades.

`--check` is offline and deterministic (same severity/exit-code model as
`verify_claims.py`). It catches:

- **Invalid band / unknown rule** (ERROR): `low<=high` in `[0,100]`; rule must be
  one of the legend.
- **Rule vs current weight** (ERROR/WARN): a no-buy rule (`trim_only`,
  `do_not_add`, `reduce`, `avoid`) whose floor sits *above* the current weight is
  contradictory (you'd have to buy); `reduce` on an unheld position; `accumulate`
  already above its ceiling (should be `trim_only`); `hold` drifted outside band.
- **Double-listing** (ERROR/WARN): a symbol in two sleeves, or in both a sleeve
  and a top-level target.
- **Feasibility** (ERROR): minimum targets + cash exceeding 100% of NAV.
- **Coverage** (INFO): how much NAV the targets claim vs the untargeted "hold"
  bucket, and how much must be freed to fund the plan.
- **Holdings hygiene** (WARN): implausible weights (e.g. an option mislabelled at
  100% of NAV) or missing `percent_of_nav`.

The goal is to **advise**, not automate: surface drift and suggest trims/adds so
a human can decide. We are intentionally *not* building an order generator,
share-level netting, or execution. Targets are edited HERE, never in prose.

## risk.py / tax_lots.py / whatif.py / journal.py (confidence layer)

These four sit on top of the rebalance spine to make a trade plan something you
can act on with more than vibes. All are stdlib-only and split pure math/logic
from IO so the tests run offline.

- `risk.py` -- **portfolio-level risk lens**. Single-name bands are blind to the
  real risk in a concentrated book: the names move together. It pulls aligned
  daily closes (Yahoo, cached under `data/cache/risk/`), then computes a
  correlation matrix, per-name and portfolio volatility, an effective-number-of-
  bets metric (correlation-aware), the share of variance that is pure co-movement,
  and a factor-shock stress test (e.g. `SOXX -25%`) via each holding's beta.
  `analyze(...)` is pure; `risk_report(holdings, ...)` does the fetching. Loud
  caveats: correlation from free daily closes is regime-dependent and converges
  to 1.0 in a crash -- a decision aid, not a risk oracle. Also returns an `fx`
  block (from `fx_history`): non-base currency exposure and the window's FX move /
  estimated CZK contribution per currency. API: `GET /api/risk`.
- `fx_history.py` -- **daily FX-rate panel** (the FX-clean spine for currency
  attribution). A CZK-base book holding USD names earns two returns: the stock's
  and the exchange rate's; splitting them needs a *daily* rate series, not a
  point-in-time one. IBKR carries only per-trade rates and FRED has no clean daily
  USD/CZK, so the panel is built from Yahoo `<CCY><BASE>=X` pairs (behind a
  swappable `fetch` seam) and cached at `data/cache/fx-history.json`
  (`series["USDCZK"][date]` = base per 1 unit of foreign; incremental merge like
  `ibkr_history.extend_history`, stale-beats-nothing on a failed pull). API:
  `update_panel` / `load_panel` / `pair_series` / `rate_on` (as-of lookup tolerant
  of weekend/holiday gaps). Topped up alongside the IBKR history sync
  (`holdings_sync`), so attribution always has same-vintage rates. The **currency
  lens** on top -- `exposure_by_currency` (non-base share of the book) and
  `window_report` (each currency's FX move + estimated CZK contribution over the
  window) -- is read-only over the cached panel and feeds `risk.risk_report`'s
  `fx` block.
- `attribution.py` -- **process attribution** ("is this system earning its
  keep?"). Every other lens measures a position; this measures the *process*. Over
  a window it compares the actual **time-weighted return** against two skill-free
  baselines: **never-rebalanced** (freeze the book as it stood at the window start
  and let prices run) and the **benchmark** (put the same koruna -- starting NAV
  plus every deposit -- into SPY/QQQ). Honest only if three things hold, all
  enforced here: deposits/withdrawals are *neutralized* (`external_flows` +
  `time_weighted_return`, so a transfer never reads as alpha), every foreign price
  is converted *day-by-day* through the FX panel (`czk_price_series`, never a
  single point-in-time rate), and all curves are *seeded with the same starting
  NAV* so they diverge only on what the money bought. The frozen book is
  reconstructed by unwinding post-window trades from the current snapshot
  (`positions_at`); a ledger that starts after the window, or a name with no price
  history, becomes a loud caveat rather than a wrong number. Pure engine
  (`time_weighted_return` / `positions_at` / `hold_index` / `flow_curve`) is
  fully unit-tested; only `attribution_report` does IO (history/holdings/prices/
  FX, price fetch injectable). Read-only; never trades. API: `GET /api/attribution`
  (`range`, `benchmark`), surfaced in the **Attribution** sub-tab. A compact
  headline (`verdict_from_report` -> `cache_verdict`, `data/cache/attribution-verdict.json`)
  is warmed whenever the view is opened, so the **Today** cockpit can surface
  "beats doing nothing?" via `load_verdict` without a network hop of its own.
- `tax_lots.py` -- **Czech tax-lot-aware sell planner**. Given a symbol and an
  amount to raise, it picks specific lots to minimize tax: realize 3y-exempt gains
  first, then harvestable losses, then taxable gains. Uses `open_datetime` for the
  3-year test (never IBKR's ST/LT). `enrich_plan(...)` attaches a lot breakdown to
  each single-name trim in a rebalance plan. A trim that reaches a taxable-gain
  lot within `NEAR_EXEMPT_DAYS` of the 3-year mark also carries a `wait` block
  (proceeds/gain/tax_saved/exempt_on) so the planner can nudge "wait N days ~=
  save Y". API: `POST /api/tax-plan`; also folded into `GET /api/rebalance`.
- `tax_calendar.py` -- **forward 3-year-exemption calendar**. Inverts `tax_lots`
  from reactive (per-trim) to proactive (whole book): every not-yet-exempt lot on
  a timeline. `build_calendar(...)` returns `exemptions` (taxable-gain lots going
  tax-free -- wait), `harvest` (taxable-loss lots whose usable-loss window closes
  at exemption -- act before the deadline), totals, and a year-end rollup.
  `pending_alerts(holdings, notified, ...)` is a pure alert generator (per-lot
  dedup) the scheduler's `tax-alerts` task pushes to the notification channel.
  Pure, no network. API: `GET /api/tax-calendar` (Portfolio -> Tax).
- `whatif.py` -- **staged-trade simulator**. Recomputes the resulting portfolio for
  a basket of trades: post-trade weights/band status (reusing `rebalance`), cash,
  and realized tax (via `tax_lots`). Pure recompute; never writes holdings or
  trades. API: `POST /api/whatif`.
- `journal.py` -- **decision journal + calibration**. Append-only decision log in
  `data/journal.json` (judgement/context, committable). `calibrate(...)` scores
  directional calls against later prices (recorded outcome or live snapshot mark):
  a buy is "right" if it rose, a trim if it fell. API: `GET/POST /api/journal`,
  `POST /api/journal/outcome`.
- `ibkr_history.py` -- **full trade + NAV history** via read-only Flex. Where
  `ibkr_portfolio.py` answers "what do I hold now", this answers "how did I get
  here": the complete executed-trade ledger plus the day-by-day NAV series, back
  to account inception. Flex caps a request at 365 days, so it walks backward one
  window at a time, stopping after two empty windows (inception) or a safety cap;
  trades/cash dedupe by IBKR id, NAV by report date. It anchors windows to the
  latest *available* day (Flex 1003s a window ending today/an unsettled day), backs
  off on rate limits (1018), and binary-searches the largest servable window for
  sub-year-old accounts. Reuses the snapshot reader's HTTP/parse primitives
  (`fetch_report` gained optional `from_date`/`to_date`). **Incremental by
  default** (`extend_history`): once the cache exists, a re-run fetches only the
  days since it was last covered (7-day overlap to absorb restatements) and merges
  them — usually a single request; `--full` (or `{"full":true}` on the endpoint)
  forces a complete rebuild, and the payload carries a `summary.update` delta.
  Requires a Flex query that includes the **Trades** section and the **Net Asset
  Value (NAV) in Base** section (the UI label; it emits the `EquitySummaryInBase`
  daily-NAV rows — not the same as the "Change in NAV" period summary) —
  set `IBKR_FLEX_HISTORY_QUERY_ID` in `tools/secrets.env` (falls back
  to `IBKR_FLEX_QUERY_ID`). `POST /api/portfolio-history/sync` runs it as a
  registered background job (kind `ibkr_history`, polled via `/api/deep-job`); the
  normalized result is cached privately at `data/cache/ibkr/portfolio-history.json`
  (gitignored — it is the entire personal trade ledger) and read via
  `GET /api/portfolio-history`. Surfaced in the **History** tab as a NAV-over-time
  chart with every buy/sell marked.

All recommendations are analysis, not financial or tax advice.

## mcp_server.py (read-only MCP adapter)

Lets an MCP client (Claude Code, Claude Desktop, ...) act as a conversational
analyst over live portfolio state — "which underweights have the best
momentum?" — by exposing a hard-allowlisted, read-only subset of the local API
as MCP tools over stdio. Stdlib-only, no SDK: it speaks the stable MCP core
(initialize / tools/list / tools/call) as newline-delimited JSON-RPC.

- **Cannot trade by construction**: the tool registry is the only route table;
  nothing under `/api/trade/` exists in it and the sole POST (`/api/whatif`)
  is a pure recompute. The upstream base must be loopback.
- **Thin adapter**: every tool proxies to the running `serve.py`
  (`ASSAY_API_BASE`, default `http://127.0.0.1:6060`); if the console is down,
  tools return a clear "start serve.py" error instead of guessing.
- Tools: overview, holdings, rebalance plan, target model, risk report,
  portfolio history, journal, price levels, segments (list + peer tables),
  ticker research, price history, exit plan, and the what-if simulator.
- Register with Claude Code: `claude mcp add assay -- py -3 tools/mcp_server.py`
- Privacy: connecting an LLM client shares portfolio values with that model's
  provider — the same trust decision as the built-in LLM analysis jobs.

## generate_site.py

Single source of truth for portfolio numbers is `data/current-holdings.json`
(produced by the IBKR Flex reader). This script rederives the markdown holdings
summary from that snapshot, so it cannot silently drift away from the JSON. (The
old static HTML plan/detail pages were retired; the SPA now recommends next
steps dynamically from the same data.)

### What it regenerates

- `data/current-holdings-summary.md` — fully rewritten from the JSON.

### Usage

```powershell
py -3 tools/generate_site.py          # rewrite stale artifacts in place
py -3 tools/generate_site.py --check  # exit 1 if anything is stale (local gate;
                                      # CI can't run it without the private data submodule)
```

Run it after every fresh IBKR pull (see the `ibkr-holdings` skill), then review
the diff before committing.

### Adding a generated value to a page

Wrap the literal in marker comments and add the key to `compute_fragments()`:

```html
<strong><!--GEN:nav.1pct-->NNNk CZK<!--/GEN:nav.1pct--></strong>
```

The text between the markers is the seed value; the script overwrites it. The
markers survive regeneration, so the operation is idempotent.

### Available keys

- `nav.full`, `nav.1pct`, `nav.2pct`, `nav.5pct`, `nav.10pct` — NAV sizing legend.
- `pos.<SYMBOL>.shares|navpct|pnl|lots|cz3y` — per-position figures
  (`SYMBOL` currently limited to `LOSER_SYMBOLS` in the script).
- `claim.<SYMBOL>.price|mcap|pe_ttm|pe_fwd|ps` — valuation claims rendered from
  `data/research-claims.json` (see below).
- `snapshot.date`, `snapshot.report` — snapshot `generated_at` date and IBKR
  report date, shown in the staleness banner on the hub pages.

### Staleness: static banner vs run-time check

The site banner shows the snapshot's *absolute* date (deterministic, so `--check`
stays stable). It deliberately does **not** show a live "N days old" age, because
that would change every day and constantly invalidate the committed HTML. The
*age* check lives in `verify_claims.py` instead, where using the current time is
fine (it never writes files).

## research-claims.json + verify_claims.py

`data/research-claims.json` holds the **structured valuation claims** that the
detail pages display (price, market cap, P/E, P/S). Each metric carries a numeric
`value` (or `low`/`high`) used for verification plus a `display` string used for
rendering, and an `asof` date that anchors the claim in time. Edit numbers here,
not in the HTML — `generate_site.py` pushes `display` into the `claim.*` markers.

`tools/verify_claims.py` is an **offline, deterministic** consistency check
(Phase 0). It does not fetch live quotes; it checks claims against each other and
against the broker marks in `current-holdings.json`:

```powershell
py -3 tools/verify_claims.py            # report findings
py -3 tools/verify_claims.py --strict   # also fail (exit 1) on warnings
```

Checks performed:

- **Identity** (ERROR): `price x shares_out ~= market_cap` within 5%. Catches the
  internally-impossible figures the "Data Hygiene" sections warn about in prose.
- **Snapshot price** (WARN): claimed price vs the broker `mark_price` within 3%.
- **Range edge** (INFO): broker mark falling outside a claimed price range.
- **Regression guard** (INFO/ERROR): values listed in `disproven_market_cap_usd_b`
  must stay arithmetically inconsistent; if one ever starts passing, that's an error.
- **Multiples** (ERROR): P/E and P/S must be positive.
- **Snapshot age** (WARN/ERROR): `generated_at` older than 5 days warns, older
  than 30 days errors. Run-time check; uses the current date.

Note: a claim is verified against its `asof`, not "now" — a moved market is not a
lie. Live cross-checking against an independent source (yfinance is the chosen
provider) is a later phase; IBKR Flex cannot supply live quotes, so it stays the
snapshot refresher, not a quote feed.
