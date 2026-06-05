# Tools

## serve.py + research_pull.py + providers/ (Interactive Research Console)

On-demand deep analysis for a single ticker or a whole industry segment. Stdlib
only -- no pip installs.

```powershell
$env:SEC_USER_AGENT = "assay research (you@example.com)"
py -3 tools/serve.py            # UI + API at http://127.0.0.1:8765 (localhost only)

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
  It preserves any human-authored `thesis` block across re-pulls.
- `serve.py` -- stdlib `http.server` app serving `web/` and a small JSON API
  (`/api/holdings`, `/api/segments`, `/api/research/<sym>`, `POST /api/pull/<sym>`,
  `/api/history/<sym>`, `POST /api/pull-segment/<name>`, `POST /api/thesis/<sym>`,
  the Deep Research pipeline endpoints for segment drafting, artifact saving,
  review, and target-proposal approval, plus the automated-run endpoints
  `POST /api/deep-research/run`, `POST /api/deep-research/login`,
  `POST /api/deep-research/verify-login`, `GET /api/deep-research/login-status`,
  and `GET /api/deep-job?id=<id>`). It serves root-level `.html`/`.css`/`.js`
  assets dynamically instead of maintaining a manual filename allowlist.
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
  `semiconductors.json` with sleeves matching `CURRENT_PLAN.md`. These are
  research lenses and may overlap; they are not target-model allocation sleeves.

### Segment and Deep Research workflow

The website is the normal control plane:

1. Open `http://127.0.0.1:8765` and use the Pipeline tab.
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
- **Dedicated profile.** The worker uses its own profile
  (`PPLX_PROFILE_DIR`, default `~/.cursor/pplx-automation-profile`), distinct
  from the agent's `user-playwright-pplx` MCP profile, to avoid a Chrome
  profile-lock fight and Chrome-version "downgrade" errors. Log in once via the
  button. Only one browser job (run or login) runs at a time.
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

## generate_site.py

Single source of truth for portfolio numbers is `data/current-holdings.json`
(produced by the IBKR Flex reader). This script rederives everything that
restates those numbers, so the markdown summary and HTML pages cannot silently
drift away from the snapshot.

### What it regenerates

- `data/current-holdings-summary.md` — fully rewritten from the JSON.
- `*.html` — only the values inside `<!--GEN:key-->...<!--/GEN:key-->` markers.

### Usage

```powershell
py -3 tools/generate_site.py          # rewrite stale artifacts in place
py -3 tools/generate_site.py --check  # exit 1 if anything is stale (CI/pre-commit)
```

Run it after every fresh IBKR pull (see the `ibkr-holdings` skill), then review
the diff before committing.

### Adding a generated value to a page

Wrap the literal in marker comments and add the key to `compute_fragments()`:

```html
<strong><!--GEN:nav.1pct-->«redacted»<!--/GEN:nav.1pct--></strong>
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
