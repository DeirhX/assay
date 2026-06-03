---
name: research-console
description: Run and extend the interactive Research Console for on-demand, live, multi-source deep dives on a ticker or industry segment. Use when asked to analyze a specific stock live, compare a peer/segment universe (e.g. semiconductors), launch/serve the console, add a data provider or segment, or work on tools/serve.py, tools/research_pull.py, or web/.
---

# Research Console

On-demand deep analysis launched from the rebalancing app. Live numbers from
multiple free sources, cross-checked, with human judgement kept separate.

## Run It

```powershell
$env:SEC_USER_AGENT = "finance-rebalancing research (you@example.com)"
py -3 tools/serve.py            # http://127.0.0.1:8765  (localhost only)
```

CLI equivalents (no server):

```powershell
py -3 tools/research_pull.py --ticker NVDA
py -3 tools/research_pull.py --segment semiconductors
```

## Architecture (stdlib only -- no pip installs)

- `tools/providers/yahoo.py` -- price/momentum + fundamentals (cookie+crumb).
- `tools/providers/sec_edgar.py` -- independent cross-check for US filers (XBRL
  companyfacts). Foreign ADRs are thin/absent; that gets flagged, not faked.
- `tools/providers/fmp.py` -- optional, only if `FMP_API_KEY` is set.
- `tools/research_pull.py` -- merges sources, runs cross-checks, writes
  `data/research/<SYM>.json`, ignored ticker history snapshots under
  `data/cache/research-history/<SYM>/`, and `data/research/segments/<name>.json`.
  It also attaches portfolio/target-model context and segment research scores.
- `tools/serve.py` -- stdlib `http.server`; serves `web/` + the JSON API.
- `web/` -- vanilla-JS single page (dossier-style deep dive / scored segment /
  holdings tabs).

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

## Common Tasks

- **Add a segment**: create `data/segments/<name>.json` with `title`, `sleeves`,
  and `members` (`symbol` + `sleeve`). Prefer US listings/ADRs so SEC can anchor.
- **Add a provider**: add `tools/providers/<x>.py` returning metric nodes
  (`{value, source, ...}`); wire it into `research_pull._collect(...)` and the
  preferred-source order in `METRIC_SPECS`. Add any new cross-checks in
  `_cross_checks`.
- **Add a metric**: extend the provider output, `METRIC_SPECS`, and the
  `METRIC_ROWS`/`SEG_COLS` arrays in `web/app.js`.

## Safety

- Server binds `127.0.0.1` only and runs live network pulls on request; never
  expose it.
- Secrets (`FMP_API_KEY`) come from `secrets.env` (gitignored). Never commit keys.
- `data/cache/` and `data/research/segments/` are gitignored; per-ticker research
  files may be committed because they carry the thesis.

## Caveats Worth Repeating to the User

- Yahoo is an unofficial feed; the crumb handshake is flaky and retried.
- SEC TTM is an approximation (summed quarters); FY values are exact.
- Foreign ADRs (TSM/ASML/ARM/KRX names) can show currency-mismatched ratios
  (e.g. TSM P/S in TWD vs USD market cap) -- these get a WARN.
- Recommendations are analysis, not financial or tax advice.
