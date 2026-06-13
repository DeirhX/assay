---
name: research-console
description: Run and extend the interactive Research Console for on-demand, live, multi-source deep dives on a ticker or industry segment, plus the website-controlled Deep Research pipeline. Use when asked to analyze a stock live, compare a segment universe, draft/approve segments, launch/serve the console, add a data provider, or work on tools/serve.py, tools/research_pull.py, tools/review_deep_research.py, or web/.
---

# Research Console

On-demand deep analysis launched from the rebalancing app. Live numbers from
multiple free sources, cross-checked, with human judgement kept separate.

## Run It

The SPA is TypeScript; it must be built before `serve.py` can serve it.

```powershell
npm install && npm run build   # once, and after pulling web/ changes
$env:SEC_USER_AGENT = "assay research (you@example.com)"
py -3 tools/serve.py            # http://127.0.0.1:6060  (localhost only)
```

For frontend development use `npm run dev` (Vite on `http://localhost:5173`
with HMR, proxying `/api` to the Python server) alongside `serve.py`. To start
both halves together, use the `run-web` skill
(`pwsh .cursor/skills/run-web/scripts/run-web.ps1`).

CLI equivalents (no server):

```powershell
py -3 tools/research_pull.py --ticker NVDA
py -3 tools/research_pull.py --segment semiconductors
```

## Architecture (Python backend is stdlib only -- no pip installs)

- `tools/providers/yahoo.py` -- price/momentum/history + fundamentals (cookie+crumb).
- `tools/providers/sec_edgar.py` -- independent cross-check for US filers (XBRL
  companyfacts). Foreign ADRs are thin/absent; that gets flagged, not faked.
- `tools/providers/fmp.py` -- optional, only if `FMP_API_KEY` is set.
- `tools/portfolio.py` -- shared portfolio/target-model helper layer for
  computed weights, target context, holdings payloads, and decision labels.
- `tools/research_pull.py` -- merges sources, runs cross-checks, writes
  `data/research/<SYM>.json`, ignored ticker history snapshots under
  `data/cache/research-history/<SYM>/`, and `data/research/segments/<name>.json`.
  It also attaches portfolio/target-model context, segment research scores, and
  compact Yahoo daily-close price history for the Deep Dive chart. A pull with no
  usable data (no price and no metrics) is not written to history; `delete_history`
  drops a single snapshot via `POST /api/history/delete` (the Deep Dive history
  table's delete button).
- `tools/review_deep_research.py` -- reviews saved Perplexity Deep Research
  artifacts against sources, deterministic ticker data, holdings, and target rules.
- `tools/serve.py` -- stdlib `http.server`; serves the built SPA (`web/dist/`)
  + the JSON API.
- `web/` -- TypeScript SPA built with Vite; entry `web/src/main.ts`, per-view
  modules under `web/src/` (deep dive / segment / pipeline / analyses /
  rebalance / holdings / setup tabs).

## Non-Negotiable Discipline

This repo exists to not trust unverified numbers. Keep it that way:

1. **Numbers and judgement stay separate.** The puller only writes the numeric
   block; the `thesis` block (human/LLM authored, dated) is preserved across
   re-pulls. Never have the model write numbers into prose.
2. **Multi-source or say so.** Every metric carries its source. When only Yahoo
   is available (most foreign ADRs), the UI flags "no independent cross-check".
3. **Surface disagreement, don't smooth it.** The Data Trust panel and the
   per-name dots reflect real cross-check findings (ERROR/WARN/INFO). Don't
   suppress them to make output look clean.
4. **Stamp `as_of` and source.** A moved market is not a lie; staleness is shown.
5. **Use the website as the control plane.** Segment creation, Deep Research
   artifact saving, review-gate runs, thesis drafts, and target proposals should
   flow through the local UI. Manual JSON editing is a debugging fallback.

## Common Tasks

- **Add a segment**: use the Pipeline tab to draft, edit, validate, and approve
  it. The saved file is `data/segments/<name>.json` with `title`, `sleeves`, and
  `members` (`symbol` + `sleeve`). Research segments may overlap; target-model
  allocation sleeves should not double-count unless explicitly modeled.
- **Run Deep Research pipeline**: Pipeline tab builds the prompt, saves
  `data/research/deep/<segment>-<date>.md` and `.sources.json`, then runs the
  review gate to create `.review.md` and `.target-proposal.json`.
- **Add a provider**: add `tools/providers/<x>.py` returning metric nodes
  (`{value, source, ...}`); wire it into `research_pull._collect(...)` and the
  preferred-source order in `METRIC_SPECS`. Add any new cross-checks in
  `_cross_checks`.
- **Add a metric**: extend the provider output, `METRIC_SPECS`, and the
  `METRIC_ROWS` array in `web/src/deepdive.ts` / `SEG_COLS` in
  `web/src/segment.ts`.

## Safety

- Server binds `127.0.0.1` only and runs live network pulls on request; never
  expose it.
- Secrets (`FMP_API_KEY`) come from `secrets.env` (gitignored). Never commit keys.
- `data/cache/` and `data/research/segments/` are gitignored; per-ticker research,
  approved segment definitions, and reviewed `data/research/deep/` artifacts may
  be committed because they carry thesis/context.

## Caveats Worth Repeating to the User

- Yahoo is an unofficial feed; the crumb handshake is flaky and retried.
- SEC TTM is an approximation (summed quarters); FY values are exact.
- Foreign ADRs (TSM/ASML/ARM/KRX names) can show currency-mismatched ratios
  (e.g. TSM P/S in TWD vs USD market cap) -- these get a WARN.
- Recommendations are analysis, not financial or tax advice.
