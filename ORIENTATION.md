# Assay — Repo Orientation

This repo is a portfolio research and rebalancing workspace. It is not an order
generator and it does not trade. It holds the live research tooling, generators,
and Cursor skills; the actual portfolio data (holdings, targets, research) lives
in a **private `data/` git submodule**, so the code can be public without leaking
positions. Run `git submodule update --init` after cloning (needs access to the
private data repo).

## What Lives Where

| Area | Path | Purpose |
| --- | --- | --- |
| Standing plan | generated `next-steps.html` (not committed); bands in `data/target-model.json` (private submodule) | Human-readable plan plus machine-readable target bands. |
| Stock pages | generated `*-detail.html` (not committed) | Per-name thesis, valuation, risks, action. |
| Holdings snapshot | `data/current-holdings.json`, `data/current-holdings-summary.md` (private `data` submodule) | Sanitized IBKR snapshot: no account id, no token, no raw XML. |
| Static generated values | `tools/generate_site.py` | Keeps NAV/position snippets in markdown and HTML synced to holdings JSON. |
| Rebalance validator | `tools/rebalance.py` | Compares current weights against `data/target-model.json`. |
| Risk lens | `tools/risk.py` | Correlation, volatility, effective-bets, and factor-shock stress over held names (`GET /api/risk`). |
| Tax-lot planner | `tools/tax_lots.py` | Czech 3-year-aware lot selection for a trim; enriches the rebalance plan (`POST /api/tax-plan`). |
| What-if simulator | `tools/whatif.py` | Recomputes post-trade weights/cash/realized-tax for a staged basket (`POST /api/whatif`). |
| Decision journal | `tools/journal.py`, `data/journal.json` (submodule) | Append-only decisions + outcome calibration (`GET/POST /api/journal`). |
| Portfolio history | `tools/ibkr_history.py`, `data/cache/ibkr/portfolio-history.json` (gitignored) | Full trade ledger + day-by-day NAV via windowed read-only Flex; persists once then tops up only new days (incremental by default, `--full`/`{"full":true}` to rebuild). NAV-over-time chart with trade markers in the History tab (`GET /api/portfolio-history`, `POST /api/portfolio-history/sync`). |
| Claim validator | `tools/verify_claims.py`, `data/research-claims.json` (submodule) | Checks valuation claims for arithmetic consistency and snapshot drift. |
| Research Console | `tools/serve.py`, `tools/research_pull.py`, `web/` | Local live research UI/API using Yahoo, SEC EDGAR, optional FMP. |
| Research segments | `data/segments/*.json` (submodule) | Website-managed research lenses. Overlap is allowed; these are not allocation sleeves. |
| Deep Research artifacts | `data/research/deep/` (submodule) | Perplexity reports, source sidecars, review-gate output, and target proposals. |
| Project skills | `.cursor/skills/` | Cursor instructions for future chats. |

## Standard Workflow

Use this when refreshing the repo after portfolio or market changes:

```powershell
# 1. Refresh IBKR using the vendored read-only reader (needs IBKR_FLEX_TOKEN /
#    IBKR_FLEX_QUERY_ID in the env or a gitignored tools\secrets.env). Easiest is
#    the "Resync from IBKR" button in the holdings UI; the CLI equivalent is:
py -3 "tools\ibkr_portfolio.py" --json --out "data\cache\ibkr\portfolio.json" --snapshot-dir "data\cache\ibkr\snapshots"

# 2. Update this repo's sanitized holdings snapshot.
# The "Resync from IBKR" button merges the pull into data\current-holdings.json
# without widening its sanitized shape. Otherwise regenerate it from the pull.

# 3. Regenerate/check derived static content.
py -3 "tools\generate_site.py"
py -3 "tools\generate_site.py" --check

# 4. Validate portfolio targets and claims.
py -3 "tools\rebalance.py" --check
py -3 "tools\verify_claims.py"
```

Expected validator behavior:

- `rebalance.py --check` may warn about option/notional rows if IBKR reports
  implausible weights. Treat those as data hygiene warnings, not trade commands.
- `verify_claims.py` verifies committed claims against their `asof` date and the
  broker snapshot. A moved market is not automatically a lie.

## Live Research Console

Run the local console when you need fresh numeric data for a ticker or segment.
The SPA is TypeScript and needs a one-time Vite build before `serve.py` can
serve it:

```powershell
npm install; npm run build   # once, and after pulling web/ changes
$env:SEC_USER_AGENT = "assay research (you@example.com)"
py -3 "tools\serve.py"
```

Then open:

`http://127.0.0.1:6060`

Use the website as the normal control surface. It can:

- draft a research segment from a freeform name,
- edit and approve segment membership,
- run deterministic pulls,
- generate a Perplexity Deep Research prompt,
- save a Deep Research report and Links-tab citation JSON,
- run the review gate,
- show target-model proposals for explicit approval.

Manual JSON editing is a debugging fallback, not the standard workflow. Future
you did not ask to become a config goblin.

CLI alternatives:

```powershell
py -3 "tools\research_pull.py" --ticker AMD
py -3 "tools\research_pull.py" --segment semiconductors
```

Research discipline:

- Numbers come from providers and are source-stamped.
- Human judgement goes in `thesis` blocks and is preserved across re-pulls.
- Research segments may overlap. Allocation sleeves in `data/target-model.json`
  should remain non-overlapping unless deliberately modeled.
- Disagreements are surfaced, not smoothed over. The whole damn point is to catch
  impossible market caps and stale valuation claims before they infect the plan.

## Perplexity Deep Research

Use Perplexity only through the browser workflow if the goal is to consume the
Pro subscription's included Deep Research quota.

Billing paths:

| Path | Billing | Use |
| --- | --- | --- |
| In-app Deep Research via `playwright-pplx` | Included in Pro quota | Narrative reports and synthesis. |
| Perplexity API / Sonar | Separate API billing | Programmatic paid API usage. |
| Perplexity Computer | Credit-billed | Avoid for ordinary research. |

Local setup:

1. `%USERPROFILE%\.cursor\mcp.json` should define `playwright-pplx` with a
   persistent profile at `%USERPROFILE%\.cursor\pplx-chrome-profile`.
2. Install the browser once:

   ```powershell
   npx -y @playwright/mcp@latest install-browser chrome-for-testing
   ```

3. Reload MCP servers or restart Cursor so `user-playwright-pplx` appears.
4. Log into `https://www.perplexity.ai` once in that launched browser profile.

Critical trap:

- Do **not** click the "Run deep research" card under "Put Computer to work".
  That routes to `/computer/...` and burns Computer credits.
- Use the composer Search dropdown's `Deep research` option. After submission,
  the URL must be `/search/...`.

See `.cursor/skills/perplexity-deep-research/SKILL.md` for the exact browser
automation workflow.

Pipeline discipline:

- Save committed Deep Research artifacts under `data/research/deep/<segment>-<date>.*`.
- Always extract sources from Perplexity's `Links` tab into a `.sources.json` sidecar.
- Run `tools/review_deep_research.py` or the website review action before changing
  ticker theses or target bands.
- Target-model proposals are drafts. Applying them requires explicit approval in
  the website and still does not create trades.

## What To Commit

**Code repo (this one, public):** code under `tools/`, the `web/` client, root
assets (`site.css`), `.cursor/skills/`, and docs. Generated HTML
(`*-detail.html`, `next-steps.html`) is **not** committed here — it is built
locally from private data and is gitignored.

**Private `data/` submodule:** all portfolio data lives there —
`current-holdings.json`/`.md` (after sanitization), `target-model.json`,
`research-claims.json`, `research/<SYMBOL>.json`, `segments/*.json`, and reviewed
`research/deep/*` artifacts.

Never commit anywhere:

- `secrets.env`, API keys, IBKR tokens, query IDs.
- Raw Flex XML or the full unsanitized `portfolio.json`.
- `data/cache/` (regenerable + live session auth).
- `.playwright-mcp/` or browser profile data.

A `pre-commit` hook (`tools/hooks/pre-commit`, enable with
`git config core.hooksPath tools/hooks`) blocks accidental commits of holdings
figures and sensitive files into the public code repo.

## Project Skills

When starting a new Cursor chat in this repo, ask it to use the relevant skills:

- `portfolio-rebalancing`: allocation thesis, targets, Czech tax context.
- `ibkr-holdings`: portfolio refresh and sanitized holdings context.
- `research-console`: local live numeric research.
- `run-web`: start both halves of the app (Python API + Vite frontend) locally.
- `perplexity-deep-research`: browser-based Perplexity Pro Deep Research.
- `rebalancing-site`: static page maintenance and navigation rules.

If recommendations change, update both the human-readable pages and the
machine-readable model/claims where applicable. Otherwise future-you gets to
debug two competing sources of truth, which is how civilization collapses.
