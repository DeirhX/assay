---
name: ibkr-holdings
description: Pull and analyze the user's Interactive Brokers portfolio from the local IBKR Flex reader. Use when asked to refresh holdings, inspect positions, compute NAV weights, check tax lots, or make portfolio/rebalancing recommendations.
---

# IBKR Holdings

## Source

Use the existing read-only IBKR Flex reader:

`E:/Projects/Active/Stash/ibkr-portfolio`

It uses `secrets.env` in that folder. Never print, copy, echo, summarize, or commit tokens. If credentials are missing or invalid, ask the user to update `secrets.env`.

## Pull Current Holdings

Run from `E:/Projects/Active/Stash/ibkr-portfolio`:

```powershell
py -3 "ibkr_portfolio.py" --json --out "portfolio.json" --snapshot-dir "snapshots"
```

This writes:

- `portfolio.json`: latest parsed portfolio snapshot.
- `snapshots/portfolio-YYYYMMDD-HHMMSS.json`: timestamped copy.

The Flex query is read-only and cannot trade. It returns a generated snapshot, not streaming live prices. Always report `generated_at`, report dates, and whether prices are stale.

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
