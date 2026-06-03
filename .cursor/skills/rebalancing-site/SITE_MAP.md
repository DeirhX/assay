# Rebalancing Mini-Site Map

Root directory:

`E:/Projects/Active/Finance/rebalancing`

## Pages

| Page | Purpose |
| --- | --- |
| `next-steps.html` | Main broad rebalance execution checklist: sells, trims, buys, tax handling, ETF guidance, 1-3 month targets. |
| `loser-position-recovery.html` | Normalized comparison dashboard for `SOFI`, `PYPL`, and `EEFT`. Links to the broader plan and stock detail pages. |
| `sofi-detail.html` | SOFI detail page: keep most, trim only if cash/risk requires it. |
| `pypl-detail.html` | PYPL detail page: trim 50-75%, keep value-turnaround stub. |
| `eeft-detail.html` | EEFT detail page: sell first, keep at most a monitored stub. |

## Public File Links

- Overview plan: `file:///E:/Projects/Active/Finance/rebalancing/next-steps.html`
- Loser dashboard: `file:///E:/Projects/Active/Finance/rebalancing/loser-position-recovery.html`
- SOFI detail: `file:///E:/Projects/Active/Finance/rebalancing/sofi-detail.html`
- PYPL detail: `file:///E:/Projects/Active/Finance/rebalancing/pypl-detail.html`
- EEFT detail: `file:///E:/Projects/Active/Finance/rebalancing/eeft-detail.html`

## Navigation Expectations

- `next-steps.html` should link to the loser-position review and each stock detail page.
- `loser-position-recovery.html` should link to `next-steps.html` and each stock detail page.
- Each stock detail page should link back to `loser-position-recovery.html`. If the site grows, also add a link back to `next-steps.html`.

## Sensitive Data Rules

Do not commit:

- IBKR tokens or query IDs.
- Raw account identifiers.
- Raw Flex XML exports.
- Any `secrets.env` file.

Portfolio figures in these pages are summarized analytical outputs, not raw credentials or full account exports.
