# Assay

*Weigh the metal, not the hype — test every number before you trust it.*

Assay is a single-user portfolio research and rebalancing workbench. Like a
metallurgical assay tests whether a bar is really gold, it tests whether the
numbers behind a position — price, market cap, multiples, target weights — hold
up before you act on them.

Start here if you are new to the repo:

`ORIENTATION.md`

It has three layers:

1. **Static pages** (committed HTML) for the standing plan and per-stock detail.
2. **Interactive Research Console** (`web/` + `tools/serve.py`) for on-demand,
   live, multi-source deep dives on any ticker or whole industry segment.
3. **Deep Research pipeline** for website-managed segment creation, Perplexity
   report capture, source extraction, review gating, and target-model proposals.

Open the static main page (relative to this repo root):

`next-steps.html`

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

A local, single-user app for launching deep analysis on demand. Run it:

```powershell
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

`web/app.ts` is the entry. It is the old `app.js` ported verbatim to TypeScript
to stand up the toolchain; it carries `// @ts-nocheck` and gets typed as it is
carved into modules. `web/dist/` and `node_modules/` are gitignored — run
`npm run build` before serving in prod.

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
  visible browser opens against the persistent profile
  `~/.cursor/pplx-chrome-profile`; sign into Perplexity there. Future Deep
  Research pipeline runs reuse that session.

Typical Windows bootstrap:

```powershell
npm install
$env:SEC_USER_AGENT = "assay research (you@example.com)"
py -3 tools/serve.py
```

Then open the Setup tab, save the LLM provider preferences, run the CLI smoke
checks, and verify Perplexity login. Do not commit `secrets.env`, browser
profiles, API keys, or CLI tokens.

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
