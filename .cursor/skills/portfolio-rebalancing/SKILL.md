---
name: portfolio-rebalancing
description: Continue the user's aggressive semiconductor/AI portfolio rebalancing work. Use when asked about next steps, target weights, trimming ARM/AMD, adding SOXX/XSD, Czech tax-aware rebalancing, or updating the current investment plan.
---

# Portfolio Rebalancing

## Start Here

Before answering or editing pages, read:

- `data/target-model.json` (private submodule) — the machine-readable standing
  plan: target bands and per-name rules. Run `py -3 tools/rebalance.py` for the
  current drift view against it.
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

Position sizes are expressed as a percentage of NAV. The concrete `1% / 5% / 10% NAV`
amounts come from the current sanitized holdings snapshot in the private `data`
submodule (`data/current-holdings.json`); refresh IBKR before relying on exact figures.

## Live Deep Dives

For on-demand, live, multi-source analysis of a ticker or a whole segment (e.g.
semiconductors), use the `research-console` skill (`py -3 tools/serve.py`). It
pulls fresh numbers, cross-checks them across sources, and cross-joins a peer
universe against current NAV weights. Use it to pressure-test the targets below
before acting.

## Research Discipline

For individual stock research:

1. Separate business momentum from stock valuation.
2. Identify the real reason for recent price moves.
3. Compare current valuation to expected growth and margin trajectory.
4. Look for downside triggers, not only bullish catalysts.
5. Tie the answer back to portfolio action: add, hold, trim, sell, or wait.

Do not confuse "great company" with "good buy at this price." That is how portfolios get mugged by multiples.

## Target Model (where we want to be)

Target weights live as structured data in `data/target-model.json`, not as
prose. Each name/sleeve has a band (`low`/`high`) that is a no-trade zone and a
`rule` (`accumulate`, `trim_only`, `do_not_add`, `reduce`, `hold`, `wait`,
`avoid`). Edit targets there, then:

```powershell
py -3 tools/rebalance.py            # drift: current weight vs target band per name/sleeve
py -3 tools/rebalance.py --check    # validate the model against holdings (exit 1 on ERROR)
```

`--check` flags self-contradictions (e.g. a `do_not_add` name whose floor is
above its current weight, infeasible totals, double-listed sleeve members) and
holdings hygiene issues. Run it after any target edit or fresh IBKR pull.

## Claim Verification

Valuation claims (price, market cap, P/E, P/S) live as structured data in
`data/research-claims.json`, not as loose prose. After changing any of them, run
`py -3 tools/verify_claims.py` to confirm they are internally consistent and
agree with the broker snapshot. Anchor claims with an `asof` date so a moved
market is never mistaken for an error.
