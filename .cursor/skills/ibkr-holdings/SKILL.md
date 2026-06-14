---
name: ibkr-holdings
description: Pull and analyze the user's Interactive Brokers portfolio from the local IBKR Flex reader. Use when asked to refresh holdings, inspect positions, compute NAV weights, check tax lots, or make portfolio/rebalancing recommendations.
---

# IBKR Holdings

## Source

The read-only IBKR Flex reader is **vendored in this repo** at
`tools/ibkr_portfolio.py` (standard library only). Credentials are never
committed: the reader resolves `IBKR_FLEX_TOKEN` / `IBKR_FLEX_QUERY_ID` from the
environment or a gitignored `tools/secrets.env` containing two lines,
`IBKR_FLEX_TOKEN=...` and `IBKR_FLEX_QUERY_ID=...` (no example file is shipped --
`.gitignore` deliberately blocks `secrets.*`). The Settings tab's IBKR pane can
write this file for you. Never print, copy, echo, summarize, or commit tokens.
If credentials are missing or invalid, ask the user to update `tools/secrets.env`.

Note there are **two** gitignored secrets files: `tools/secrets.env` (IBKR Flex
credentials, used by the reader and written by the Settings tab) and repo-root
`secrets.env` (`FMP_API_KEY` for the optional FMP data provider, loaded by
`tools/serve.py` at startup).

## Pull Current Holdings

Easiest: click **Resync from IBKR** in the holdings UI, which runs the vendored
reader via `serve.py` and refreshes `data/current-holdings.json`. Or run it
directly from the repo root:

```powershell
py -3 "tools/ibkr_portfolio.py" --json --out "data/cache/ibkr/portfolio.json" --snapshot-dir "data/cache/ibkr/snapshots"
```

Raw pulls land under `data/cache/ibkr/` — gitignored, inside the private `data`
submodule, never in the public tree. The writes are:

- `portfolio.json`: latest parsed portfolio snapshot.
- `snapshots/portfolio-YYYYMMDD-HHMMSS.json`: timestamped copy.

The Flex query is read-only and cannot trade. It returns a generated snapshot, not streaming live prices. Always report `generated_at`, report dates, and whether prices are stale.

## Full Trade & NAV History

The snapshot reader answers "what do I hold now". To reconstruct the **entire
trading and portfolio-value history** (every executed trade + the day-by-day NAV
series, back to inception), use `tools/ibkr_history.py`. It walks the account
backward one ≤365-day Flex window at a time (the Flex per-request cap), stopping
after two empty windows or a safety cap, and de-duplicates across overlapping
windows. Real-world Flex friction is handled: it anchors windows to the latest
*available* day (Flex 1003s a window ending today/an unsettled day), backs off on
rate limits (1018), and binary-searches the largest servable window for accounts
younger than a year.

**Incremental by default.** Once a cache exists, a re-run fetches only the days
since it was last covered (with a 7-day overlap to absorb Flex restatements) and
merges them in — typically a single Flex request. The first run, or `--full`,
rebuilds the whole thing back to inception. The result carries a
`summary.update` block (`new_trades`, `new_nav_points`, …) describing the delta.

This needs a Flex query that includes the **Trades** section (tick Executions)
and the **Net Asset Value (NAV) in Base** section (CashTransactions optional).
Use the exact UI labels: there is no checkbox called "EquitySummaryInBase" — that
is only the XML tag the "Net Asset Value (NAV) in Base" section emits. Do not pick
"Change in NAV" instead; that is a single per-period summary, not the daily NAV
series. The snapshot query (OpenPositions only) will not work. Create a dedicated
Activity Flex query for history and set `IBKR_FLEX_HISTORY_QUERY_ID`. The Settings
tab's IBKR pane has a **History Flex Query ID** field that writes this to
`tools/secrets.env` for you (and shows a "History query: ready / not set" badge);
you can also edit the file directly. It falls back to `IBKR_FLEX_QUERY_ID` if you
put everything in one query, but the positions snapshot query lacks the required
sections, so a dedicated history query is what actually works.

Easiest: click **Update from IBKR** in the History tab (or **Rebuild full** to
force a complete rebuild), which runs this as a background job (kind
`ibkr_history`, POST `/api/portfolio-history/sync` with optional `{"full":true}`)
and renders a NAV-over-time chart with buy/sell markers. CLI equivalent:

```powershell
# Incremental top-up when the cache exists; full build on the first run.
py -3 "tools/ibkr_history.py" --out "data/cache/ibkr/portfolio-history.json" -v
# Force a complete rebuild back to inception:
py -3 "tools/ibkr_history.py" --out "data/cache/ibkr/portfolio-history.json" --full -v
```

The normalized payload (`nav_series`, `trades`, `cash_transactions`, `summary`)
is cached at `data/cache/ibkr/portfolio-history.json` — **gitignored**, since it
is the entire personal trade ledger. Read-only, like the snapshot reader. For
Czech 3-year eligibility still use the open-lot `open_datetime` from the snapshot,
not the trade ledger's realized rows.

## Repo-Local Cached Snapshot

This repo also stores a sanitized holdings snapshot for continuity:

- `data/current-holdings.json`: machine-readable positions, cash, lots, top positions, and tax-lot summary.
- `data/current-holdings-summary.md`: human-readable summary.

Use the cached snapshot when the user asks to continue prior analysis without refreshing. Refresh IBKR first when making trade recommendations or when the user asks for current holdings.

## JSON Shape

Use `positions` for allocation weights and `lots` for tax-lot timing:

```json
{
  "net_asset_value": 0.0,
  "positions": [
    {
      "symbol": "AMD",
      "quantity": 0.0,
      "market_value": 0.0,
      "base_market_value": 0.0,
      "percent_of_nav": 0.0,
      "unrealized_pnl": 0.0
    }
  ],
  "lots": [
    {
      "symbol": "AMD",
      "quantity": 0.0,
      "open_datetime": "ISO-8601 UTC",
      "cost_basis_money": 0.0,
      "base_market_value": 0.0,
      "unrealized_pnl": 0.0
    }
  ],
  "cash": [
    {"currency": "BASE_SUMMARY", "ending_cash": 0.0}
  ]
}
```

Important: `market_value` may be in the instrument's trading currency. Use `base_market_value` or `percent_of_nav` for portfolio weights.

## Czech Tax Context

Default assumption: the user is a Czech tax-resident individual holding listed shares/ETFs/securities as private assets, not business assets.

- Do not apply U.S. wash-sale or U.S. capital-gains rules unless explicitly requested.
- Czech securities sales are tested on gross sale proceeds, not only profit.
- The CZK 100,000 annual gross-proceeds exemption is usually irrelevant for serious rebalancing.
- The key holding-period test for listed securities is 3 years.
- Use `lots[].open_datetime` for Czech 3-year eligibility. Do not use IBKR's `ST`/`LT` code.
- Taxable securities losses can generally offset taxable securities gains in the same tax period, but not exempt sales, dividends, unrelated income, or future years. Verify with a tax professional before acting.

## Analysis Checklist

1. Refresh unless the user explicitly says to use cached data.
2. Compute top weights from `base_market_value / net_asset_value`.
3. Classify semiconductor/AI exposure separately from broad tech and unrelated holdings.
4. For proposed sells, inspect `lots[].open_datetime`, unrealized gain/loss, and position concentration.
5. Treat tax drag as a cost, not an automatic veto, when concentration risk is high.
6. State that recommendations are analysis, not financial or tax advice.
