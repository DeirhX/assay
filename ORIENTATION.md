# Assay — Repo Orientation

This repo is a portfolio research and rebalancing workspace. It is not an order
generator and it does not trade. It keeps the standing plan, current sanitized
holdings, live research tooling, and Cursor skills in one place so another chat
can continue without transcript archaeology.

## What Lives Where

| Area | Path | Purpose |
| --- | --- | --- |
| Standing plan | `next-steps.html`, `data/target-model.json` | Human-readable plan plus machine-readable target bands. |
| Stock pages | `amd-detail.html`, `arm-detail.html`, `sofi-detail.html`, `pypl-detail.html`, `eeft-detail.html` | Per-name thesis, valuation, risks, action. |
| Holdings snapshot | `data/current-holdings.json`, `data/current-holdings-summary.md` | Sanitized IBKR snapshot: no account id, no token, no raw XML. |
| Static generated values | `tools/generate_site.py` | Keeps NAV/position snippets in markdown and HTML synced to holdings JSON. |
| Rebalance validator | `tools/rebalance.py` | Compares current weights against `data/target-model.json`. |
| Claim validator | `tools/verify_claims.py`, `data/research-claims.json` | Checks valuation claims for arithmetic consistency and snapshot drift. |
| Research Console | `tools/serve.py`, `tools/research_pull.py`, `web/` | Local live research UI/API using Yahoo, SEC EDGAR, optional FMP. |
| Research segments | `data/segments/*.json` | Website-managed research lenses. Overlap is allowed; these are not allocation sleeves. |
| Deep Research artifacts | `data/research/deep/` | Committed Perplexity reports, source sidecars, review-gate output, and target proposals. |
| Project skills | `.cursor/skills/` | Cursor instructions for future chats. |

## Standard Workflow

Use this when refreshing the repo after portfolio or market changes:

```powershell
# 1. Refresh IBKR in the separate reader repo.
Set-Location "E:\Projects\Active\Stash\ibkr-portfolio"
py -3 "ibkr_portfolio.py" --json --out "portfolio.json" --snapshot-dir "snapshots"

# 2. Update this repo's sanitized holdings snapshot.
# Use the repo's ibkr-holdings skill instructions, or regenerate the sanitized
# snapshot from the refreshed portfolio.json if the helper script is available.

# 3. Return to this repo and regenerate/check derived static content.
Set-Location "E:\Projects\Active\Finance\rebalancing"
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

Run the local console when you need fresh numeric data for a ticker or segment:

```powershell
$env:SEC_USER_AGENT = "assay research (you@example.com)"
py -3 "tools\serve.py"
```

Then open:

`http://127.0.0.1:8765`

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

1. `C:\Users\doome\.cursor\mcp.json` should define `playwright-pplx` with a
   persistent profile at `C:\Users\doome\.cursor\pplx-chrome-profile`.
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

Commit:

- Static HTML pages.
- `data/current-holdings.json` and `data/current-holdings-summary.md` after
  sanitization.
- `data/target-model.json`.
- `data/research-claims.json`.
- `data/research/<SYMBOL>.json` when it contains useful research and thesis.
- `data/segments/*.json` for approved research segment definitions.
- `data/research/deep/*.md`, `*.sources.json`, `*.review.md`, and
  `*.target-proposal.json` when they are reviewed research artifacts.
- Project skills under `.cursor/skills/`.

Do not commit:

- `secrets.env`, API keys, IBKR tokens, query IDs.
- Raw Flex XML or full unsanitized `portfolio.json`.
- `data/cache/`.
- `data/research/segments/`.
- `.playwright-mcp/` or browser profile data.

## Project Skills

When starting a new Cursor chat in this repo, ask it to use the relevant skills:

- `portfolio-rebalancing`: allocation thesis, targets, Czech tax context.
- `ibkr-holdings`: portfolio refresh and sanitized holdings context.
- `research-console`: local live numeric research.
- `perplexity-deep-research`: browser-based Perplexity Pro Deep Research.
- `rebalancing-site`: static page maintenance and navigation rules.

If recommendations change, update both the human-readable pages and the
machine-readable model/claims where applicable. Otherwise future-you gets to
debug two competing sources of truth, which is how civilization collapses.
