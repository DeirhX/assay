# Rebalancing Mini-Site Map

Root directory:

The repository root. All page paths below are relative to it.

## Pages

| Page | Purpose |
| --- | --- |
| `next-steps.html` | Main broad rebalance execution checklist: sells, trims, buys, tax handling, ETF guidance, 1-3 month targets. |
| `amd-detail.html` | AMD detail page: tangible AI GPU/CPU momentum, expensive valuation, hold core but trim concentration. |
| `arm-detail.html` | ARM detail page: real AI/data-center royalty momentum, extreme valuation, do not add and trim concentration. |
| `loser-position-recovery.html` | Normalized comparison dashboard for `SOFI`, `PYPL`, and `EEFT`. Links to the broader plan and stock detail pages. |
| `sofi-detail.html` | SOFI detail page: keep most, trim only if cash/risk requires it. |
| `pypl-detail.html` | PYPL detail page: trim 50-75%, keep value-turnaround stub. |
| `eeft-detail.html` | EEFT detail page: sell first, keep at most a monitored stub. |

## Data Files

| File | Purpose |
| --- | --- |
| `data/current-holdings.json` | Sanitized machine-readable IBKR holdings snapshot: positions, lots, cash, top positions, and tax-lot summary. |
| `data/current-holdings-summary.md` | Human-readable holdings snapshot summary for quick context. |

## Page Links

Paths are relative to the repository root. To produce an openable `file://` link,
build it from the current workspace's absolute path rather than hardcoding a drive.

- Overview plan: `next-steps.html`
- AMD detail: `amd-detail.html`
- ARM detail: `arm-detail.html`
- Loser dashboard: `loser-position-recovery.html`
- SOFI detail: `sofi-detail.html`
- PYPL detail: `pypl-detail.html`
- EEFT detail: `eeft-detail.html`

## Navigation Expectations

- `next-steps.html` should link to the loser-position review and each stock detail page.
- `loser-position-recovery.html` should link to `next-steps.html`, `amd-detail.html`, `arm-detail.html`, and each loser stock detail page.
- Each stock detail page should link back to `next-steps.html`; loser stock pages should also link back to `loser-position-recovery.html`.

## Sensitive Data Rules

Do not commit:

- IBKR tokens or query IDs.
- Raw account identifiers.
- Raw Flex XML exports.
- Any `secrets.env` file.

Portfolio figures in these pages are summarized analytical outputs, not raw credentials or full account exports.
