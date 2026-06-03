# Rebalancing Research Site

Static research and execution checklist for the portfolio rebalancing work.

Open the main page:

`file:///E:/Projects/Active/Finance/rebalancing/next-steps.html`

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

In a new chat/workspace, ask Cursor to use the `portfolio-rebalancing` and `rebalancing-site` skills before changing recommendations or pages.

## Current Sizing Legend

Sanitized holdings snapshot: `data/current-holdings.json`.

Snapshot generated at `2026-06-03T10:23:04+00:00`, report date `20260602`, NAV about `«redacted»`:

- `1% NAV ~= «redacted»`
- `2% NAV ~= «redacted»`
- `5% NAV ~= «redacted»`
- `10% NAV ~= «redacted»`

Refresh IBKR before relying on exact amounts.

## Sensitive Data

Do not commit IBKR credentials, raw XML exports, full account snapshots, or `secrets.env`.

The pages contain summarized research and allocation guidance, not a full account export.
