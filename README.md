# Assay

*Weigh the metal, not the hype — test every number before you trust it.*

Assay is a single-user portfolio research and rebalancing workbench. Like a
metallurgical assay tests whether a bar is really gold, it tests whether the
numbers behind a position — price, market cap, multiples, target weights — hold
up before you act on them.

## Introduction

### What it is for

Assay is a personal, local-first **research and decision-support** tool for one
investor managing one portfolio. Its whole reason to exist is distrust: market
data is messy, and a single bad number (an impossible market cap, a stale price,
a foreign filer with no independent anchor) can quietly poison a rebalancing
decision. Assay pulls the same figures from multiple sources, cross-checks them,
and surfaces the disagreements **before** you act.

### What it gives you

- **Verified numbers.** Live deep dives on any ticker — price, momentum, market
  cap, P/E (ttm/fwd), P/S, revenue, margins, share count — pulled from Yahoo and
  cross-checked against SEC EDGAR (and FMP if a key is set), with mismatches
  flagged rather than smoothed over.
- **A standing rebalance plan.** Current NAV weights compared against your
  target bands, with risk, tax-lot, and what-if tooling to pressure-test a trade
  basket before you place it.
- **Segment and peer dashboards.** A whole peer universe (e.g. semiconductors)
  ranked and cross-joined against what you already own.
- **A Deep Research pipeline.** Perplexity report capture, source extraction, a
  local review gate, and draft target-model proposals — none of which ever
  create trades on their own.
- **A separation of facts from judgement.** Fetched numbers and your written
  thesis are kept strictly apart; re-pulling data never clobbers your reasoning.

### Intended use case

You are a hands-on individual investor who wants to validate the data and
allocations behind your own positions, run on-demand research, and keep a
defensible standing plan. You run it locally, against your own private holdings,
on your own machine. It is explicitly **not** multi-user, not a hosted service,
and not a robo-advisor. Research never trades. There is one — and only one —
surface that can place orders: the opt-in, paper-first **Trade desk** (see
below), which is disabled by default and places only orders you confirm
one-by-one.

### Prerequisites

- **Python 3** (stdlib only — no `pip install` needed for the backend).
- **Node.js + npm** to build the TypeScript SPA once (`npm install && npm run build`).
- **A `SEC_USER_AGENT`** environment variable identifying yourself to the SEC
  fair-access API.
- **Access to the private `data/` submodule** for your own holdings/targets;
  without it the app still starts, just with empty portfolio views.
- *Optional:* an `FMP_API_KEY` (third market-data opinion), one local LLM CLI
  (`cursor-agent` or `claude`) for analyst notes, and a Perplexity Pro login for
  the Deep Research pipeline.

### How to use it (quickstart)

```powershell
git submodule update --init                        # private holdings (optional)
npm install && npm run build                        # build the SPA once
$env:SEC_USER_AGENT = "assay research (you@example.com)"
py -3 tools/serve.py                                # then open http://127.0.0.1:6060
```

See **Setup**, **Interactive Research Console**, and **First-Time Setup** below
for the full details.

### Disclaimer

**This tool is for personal research and informational purposes only. It is not
financial, investment, tax, or legal advice, and nothing it produces is a
recommendation to buy or sell any security.** Market data is pulled from
third-party sources that may be delayed, incomplete, or wrong — verify
everything independently before acting. Any tax handling (including the Czech
3-year lot logic) is a convenience estimate, not professional tax guidance.

The optional **Trade desk** can place real orders through Interactive Brokers,
but only when you explicitly enable it (`IBKR_TRADING_ENABLED`), only after you
preview the basket, and only for orders you confirm individually; live (non-paper)
placement requires a second flag (`IBKR_ALLOW_LIVE`) on top. Order execution is
inherently risky: prices move, orders can partially fill or be rejected, and a
mistaken basket can lose real money. **You alone are responsible for every order
and its outcome**; the authors accept no liability for any loss arising from use
of this software. Validate on a paper account first. Use at your own risk.

Start here if you are new to the repo:

`ORIENTATION.md`

It has three layers:

1. **Static pages** (generated HTML, built locally from private data) for the
   standing plan and per-stock detail.
2. **Interactive Research Console** (`web/` + `tools/serve.py`) for on-demand,
   live, multi-source deep dives on any ticker or whole industry segment.
3. **Deep Research pipeline** for website-managed segment creation, Perplexity
   report capture, source extraction, review gating, and target-model proposals.

The static pages are generated from your private holdings (see **Setup**) and are
not committed. Build them with `py -3 tools/generate_site.py`, then open
`next-steps.html` (repo root).

## Setup

Portfolio data lives in a **private `data/` git submodule** — the public code repo
contains no holdings, NAV, or P/L. After cloning:

```powershell
git submodule update --init   # requires access to the private data repo
```

Without it, `data/` stays empty: `tools/serve.py` still starts (holdings and target
views are simply empty), and the CLI tools exit with a clear "data not initialized"
message instead of a stack trace.

## Core Processes

For the full workflow, see `ORIENTATION.md`.

Short version:

```powershell
# Serve the local Research Console.
$env:SEC_USER_AGENT = "assay research (you@example.com)"
py -3 tools/serve.py

# Validate target model and committed valuation claims.
py -3 tools/rebalance.py --check
py -3 tools/verify_claims.py

# Check generated holdings/page snippets are synchronized.
py -3 tools/generate_site.py --check
```

Use `tools/generate_site.py` after refreshing `data/current-holdings.json`.

## Interactive Research Console

A local, single-user app for launching deep analysis on demand. The SPA is
TypeScript and **must be built once** before the Python server can serve it:

```powershell
npm install && npm run build   # once (and after pulling web/ changes)
# optional but polite: identify yourself to the SEC fair-access API
$env:SEC_USER_AGENT = "assay research (you@example.com)"
py -3 tools/serve.py
# then open http://127.0.0.1:6060
```

### Frontend (Vite + TypeScript)

The client (`web/`) is built with Vite + TypeScript. The Python server still owns
the API, jobs, auth, and the static report pages; Vite only builds the SPA.

```powershell
npm install          # once

# Day-to-day development (hot reload):
npm run dev          # Vite dev server on http://localhost:5173 (HMR),
                     # proxying /api -> the Python server. Keep `py -3 tools/serve.py`
                     # running alongside it for the API.
                     # Note: Vite binds to localhost (IPv6); use localhost, not 127.0.0.1.

# Production / serving via Python:
npm run build        # emits web/dist/ ; tools/serve.py serves it automatically
py -3 tools/serve.py # then open http://127.0.0.1:6060

npm run typecheck    # tsc --noEmit (loose baseline today; tightened as code is
                     # split into typed modules)
```

The entry is `web/src/main.ts`, which boots a set of per-view modules under
`web/src/` (shell, deepdive, segment, pipeline, analyses, rebalance, holdings,
setup). Most modules still carry `// @ts-nocheck` and get typed incrementally.
`web/dist/` and `node_modules/` are gitignored — run `npm run build` before
serving via the Python server; without it `serve.py` warns at startup and the
UI will not load.

What it does:

- **Deep dive** any ticker: live price, momentum, market cap, P/E (ttm/fwd),
  P/S, revenue, net income, margins, share count -- pulled from **Yahoo** and
  cross-checked against **SEC EDGAR** filing data (and **FMP** if a key is set).
- **Data trust** panel: every pull is verified across sources. The same
  discipline as `verify_claims.py`, but live -- it flags share-count mismatches,
  internally-impossible market caps, stale prices, and foreign filers with no
  independent anchor (the `$349B`/`$161B` class of bug).
- **Segment** dashboard: pull a whole peer universe (e.g. `semiconductors`),
  ranked and sortable, cross-joined against your current NAV weights so you can
  see owned vs. cheaper-peer at a glance.
- **Pipeline** tab: draft or approve research segments, generate Deep Research
  prompts, save Perplexity report/source artifacts, run the review gate, and
  inspect target-model proposals without hand-editing config files.
- **Thesis editor**: your judgement (summary / action / drivers / downside
  triggers) is saved per ticker, kept strictly separate from the fetched
  numbers, and never clobbered on re-pull.

Backend stack: **Python stdlib only**. No Flask/FastAPI, no yfinance/pandas,
nothing to `pip install` -- it talks to the same Yahoo and SEC sources directly
via `urllib`, and serves on `http.server`. Binds to `127.0.0.1` only; it runs
live network pulls on request, so do not expose it.

Frontend stack: **Vite + TypeScript** (see *Frontend* above). The "no build
step" rule applied while this was a static page; it now has a real client with
an async job model, so a build step earns its keep (HMR, types, modules).

Optional FMP third opinion: put `FMP_API_KEY=...` in `secrets.env` (gitignored).

> Note: the standing static pages (`next-steps.html`, per-stock detail) still
> work as plain files. The Research Console is the built/served app.

## First-Time Setup

Open `http://127.0.0.1:6060/?view=setup` after starting the server. The Setup
tab checks the local pieces that cannot be safely committed to the repo:

- **Environment**: set `SEC_USER_AGENT` before starting the server. `FMP_API_KEY`
  is optional and adds a third market-data opinion when available.
- **LLM CLIs**: install and authorize at least one local analysis CLI. Assay uses
  `claude` first and `cursor-agent` as fallback for single-ticker analyst notes.
  Run each CLI once in a terminal and complete its login flow, then use the Setup
  tab's smoke check. A binary being on `PATH` is not enough; the check verifies
  that authorization actually works.
- **Perplexity login**: click **Set up Perplexity login** in the Setup tab. A
  visible browser opens against the persistent automation profile
  `~/.cursor/pplx-automation-profile`; sign into Perplexity there. Future Deep
  Research pipeline runs reuse that session. (This is separate from the
  `pplx-chrome-profile` used by the `user-playwright-pplx` MCP browser.)

Typical Windows bootstrap:

```powershell
npm install
npm run build
$env:SEC_USER_AGENT = "assay research (you@example.com)"
py -3 tools/serve.py
```

Then open the Setup tab, save the LLM provider preferences, run the CLI smoke
checks, and verify Perplexity login. Do not commit `secrets.env`, browser
profiles, API keys, or CLI tokens.

## Live Trading (Trade desk)

The **Trade desk** (Portfolio → Trade) is the only part of Assay that can place
real orders. Everything else is read-only research. It is **off by default** and
**paper-first** by design.

It uses the IBKR **Client Portal Web API** over a local **Client Portal Gateway**
(a Java program you run yourself), reached with stdlib `urllib` — no new Python
dependencies. This is a supervised path: you log into the gateway (with 2FA) once
per session, and you confirm every order.

How it flows:

1. Stage a basket in the **Rebalance** planner and press *Simulate basket*.
2. Switch to **Trade**, which reuses that basket and shows the gateway/account
   status (a loud banner distinguishes **paper** from **LIVE**).
3. *Preview through IBKR* sizes the CZK basket into share orders and fetches
   IBKR's margin/commission impact — nothing is placed.
4. Tick each order to confirm, then *Place*. IBKR's confirmation prompts are
   answered automatically; order status and a cancel control appear below.

Enabling it (all gitignored in `tools/secrets.env`, or as env vars):

```powershell
# Run the Client Portal Gateway, then log in at https://localhost:5000 (2FA).
$env:IBKR_TRADING_ENABLED = "1"          # master switch; off by default
# $env:IBKR_ALLOW_LIVE   = "1"           # ONLY after validating on paper
# $env:IBKR_GATEWAY_BASE = "https://localhost:5000/v1/api"   # default
# $env:IBKR_TRADE_ACCOUNT_ID = "DU1234567"  # optional; default prefers paper
```

Safety model: trading is refused unless `IBKR_TRADING_ENABLED` is set; a basket
must be previewed before it can be placed (the preview returns a token the place
step must echo, so a mutated basket is rejected); orders are re-derived
server-side from that token-bound basket, never trusted from the browser; and
**live (non-paper) placement stays locked** until `IBKR_ALLOW_LIVE` is also set.
Paper accounts are detected by their `DU` prefix.

Paper validation checklist (do this before ever setting `IBKR_ALLOW_LIVE`):

- [ ] Gateway running and logged in; Trade tab shows *Paper account DU…*.
- [ ] Stage a small basket, *Preview*, and confirm the sized share counts and
      IBKR margin/commission look sane.
- [ ] Place it on paper; verify the orders appear in IBKR and in the live-orders
      list, then cancel/let them fill.
- [ ] Re-preview after the fill and confirm positions moved as expected.

## Pages

- `next-steps.html` — broad rebalance checklist and target weights.
- `amd-detail.html` — AMD momentum/valuation/action detail page.
- `arm-detail.html` — ARM momentum/valuation/action detail page.
- `loser-position-recovery.html` — normalized dashboard for `SOFI`, `PYPL`, and `EEFT`.
- `sofi-detail.html` — SOFI detail page.
- `pypl-detail.html` — PYPL detail page.
- `eeft-detail.html` — EEFT detail page.

## Project Skills

This directory includes project-scoped Cursor skills under `.cursor/skills/`:

- `ibkr-holdings` — refresh and parse the local IBKR Flex portfolio reader.
- `portfolio-rebalancing` — continue the portfolio allocation and stock research workflow.
- `rebalancing-site` — maintain the static HTML mini-site.
- `research-console` — run/extend the local live research console.
- `run-web` — start the Python API server and the Vite frontend together locally.
- `perplexity-deep-research` — run in-app Perplexity Deep Research through browser automation using Pro included quota.

In a new chat/workspace, ask Cursor to read `ORIENTATION.md` and use the relevant project skills before changing recommendations, pages, target models, or research data.

## Perplexity Research

Perplexity Pro quota is used through browser automation, not the API.

- Browser in-app Deep Research: included Pro quota.
- Perplexity API / Sonar: separately metered API billing.
- Perplexity Computer: credit-billed; avoid for normal research.

The dedicated MCP server is `playwright-pplx` / `user-playwright-pplx`. After MCP reload/restart, log into Perplexity once in the persistent profile. Use the Search dropdown's **Deep research** option and verify the URL is `/search/...`, not `/computer/...`.

See `.cursor/skills/perplexity-deep-research/SKILL.md` for exact steps.

Saved Deep Research artifacts live under `data/research/deep/<segment>-<date>.*`:

- `.md` — report text.
- `.sources.json` — Perplexity Links-tab citation URLs.
- `.review.md` — local review-gate output.
- `.target-proposal.json` — draft target-model changes, never trades.

## Current Sizing Legend

Sanitized holdings snapshot: `data/current-holdings.json` (private `data` submodule).

Position sizes are expressed as a percentage of NAV. The concrete `1% / 2% / 5% / 10%`
amounts are computed from the snapshot — refresh IBKR before relying on exact figures.

## Sensitive Data

Commit sanitized analytical data only. Do not commit IBKR credentials, raw XML exports, full unsanitized account snapshots, browser profiles, API keys, or `secrets.env`.

The pages contain summarized research and allocation guidance, not a full account export.
