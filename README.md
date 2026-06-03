# Rebalancing Research Site

Static research and execution checklist for the portfolio rebalancing work.

Open the main page:

`file:///E:/Projects/Active/Finance/rebalancing/next-steps.html`

## Pages

- `next-steps.html` — broad rebalance checklist and target weights.
- `loser-position-recovery.html` — normalized dashboard for `SOFI`, `PYPL`, and `EEFT`.
- `sofi-detail.html` — SOFI detail page.
- `pypl-detail.html` — PYPL detail page.
- `eeft-detail.html` — EEFT detail page.

## Project Skills

This directory includes project-scoped Cursor skills under `.cursor/skills/`:

- `ibkr-holdings` — refresh and parse the local IBKR Flex portfolio reader.
- `portfolio-rebalancing` — continue the portfolio allocation and stock research workflow.
- `rebalancing-site` — maintain the static HTML mini-site.

In a new chat/workspace, ask Cursor to use the `portfolio-rebalancing` and `rebalancing-site` skills before changing recommendations or pages.

## Current Sizing Legend

Last discussed NAV was about `31.6m CZK`:

- `1% NAV ~= 316k CZK ~= $15.1k`
- `2% NAV ~= 632k CZK`
- `5% NAV ~= 1.58m CZK`
- `10% NAV ~= 3.16m CZK`

Refresh IBKR before relying on exact amounts.

## Sensitive Data

Do not commit IBKR credentials, raw XML exports, full account snapshots, or `secrets.env`.

The pages contain summarized research and allocation guidance, not a full account export.
