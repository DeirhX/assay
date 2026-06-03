---
name: portfolio-rebalancing
description: Continue the user's aggressive semiconductor/AI portfolio rebalancing work. Use when asked about next steps, target weights, trimming ARM/AMD, adding SOXX/XSD, Czech tax-aware rebalancing, or updating the current investment plan.
---

# Portfolio Rebalancing

## Start Here

Before answering or editing pages, read:

- [CURRENT_PLAN.md](CURRENT_PLAN.md)
- `data/current-holdings-summary.md` if current allocation context matters.
- `data/current-holdings.json` if exact positions, lots, cash, or NAV weights matter.

Use the `ibkr-holdings` skill when fresh portfolio data, NAV weights, or lot-level tax timing are needed.

## Current Rebalancing Thesis

The user's goal is aggressive upside, not conservative de-risking. The problem is not too much risk; it is badly concentrated risk, especially in `AMD` and `ARM`.

Default stance:

- Keep semiconductor/AI exposure high.
- Reduce single-name concentration.
- Prefer staged buys with limit orders after major weekly moves.
- Use weaker/loss-making non-core positions as first funding sources.
- Treat Czech tax drag as a cost, not a stop sign, when concentration risk is material.

## Output Style

Give concrete actions, not vague asset-allocation oatmeal:

- Say what to sell/trim/buy.
- Include approximate NAV percentages and CZK/USD sizing when possible.
- Identify what not to chase.
- Explain what would change the recommendation.
- Be explicit when fresh data is required before acting.

## Sizing Legend

Last discussed NAV basis:

- `1% NAV ~= «redacted»`
- `5% NAV ~= «redacted»`
- `10% NAV ~= «redacted»`

Refresh IBKR before relying on these exact amounts.

## Research Discipline

For individual stock research:

1. Separate business momentum from stock valuation.
2. Identify the real reason for recent price moves.
3. Compare current valuation to expected growth and margin trajectory.
4. Look for downside triggers, not only bullish catalysts.
5. Tie the answer back to portfolio action: add, hold, trim, sell, or wait.

Do not confuse "great company" with "good buy at this price." That is how portfolios get mugged by multiples.

## Claim Verification

Valuation claims (price, market cap, P/E, P/S) live as structured data in
`data/research-claims.json`, not as loose prose. After changing any of them, run
`py -3 tools/verify_claims.py` to confirm they are internally consistent and
agree with the broker snapshot. Anchor claims with an `asof` date so a moved
market is never mistaken for an error.
