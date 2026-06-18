# Rebalancing Mini-Site Map

Root directory:

The repository root. All page paths below are relative to it.

## Pages

| Page | Purpose |
| --- | --- |
| `next-steps.html` | Main broad rebalance execution checklist: sells, trims, buys, tax handling, ETF guidance, 1-3 month targets. The only remaining hand-authored landing page; symbol cells link into the SPA deep-dive. |
| `site.css` | Shared visual system for the root static pages: tokens, navigation pills, cards, legends, tables, notes, and responsive layout. |

The old per-stock `<sym>-detail.html` pages and `loser-position-recovery.html`
were **retired**: per-ticker narrative now lives in the SPA deep-dive (live data
+ on-demand CLI analysis + on-demand single-name Deep Research), and the loser
review became the `loser-position-recovery` research segment
(`data/segments/loser-position-recovery.json`), runnable as a thematic Deep
Research from the Pipeline tab.

## Data Files

| File | Purpose |
| --- | --- |
| `data/current-holdings.json` | Sanitized machine-readable IBKR holdings snapshot: positions, lots, cash, top positions, and tax-lot summary. |
| `data/current-holdings-summary.md` | Human-readable holdings snapshot summary for quick context. |
| `data/segments/*.json` | Website-managed research segment definitions. These are overlapping research lenses, not target-model allocation sleeves. |
| `data/research/<SYMBOL>.json` | Deterministic ticker pulls plus preserved thesis blocks. |
| `data/research/deep/*` | Committed Perplexity Deep Research reports, Links-tab source sidecars, review-gate output, and target proposals. |

## Documentation

| File | Purpose |
| --- | --- |
| `README.md` | Concise front door and common commands. |
| `ORIENTATION.md` | Cold-start guide covering repo organization, standard workflows, Perplexity setup, commit rules, and project skills. |
| `tools/README.md` | Detailed tool architecture and command reference. |

## Interactive Console

Run `py -3 tools\serve.py` and open `http://127.0.0.1:6060`.

Tabs:

- `Deep dive`: single ticker pull and thesis editor.
- `Segment`: deterministic peer dashboard for approved research segments.
- `Pipeline`: segment drafting/approval, Perplexity prompt/report/source capture,
  review gate, and target proposal approval.
- `Holdings`: sanitized portfolio snapshot and concentration view.

## Page Links

Paths are relative to the repository root. To produce an openable `file://` link,
build it from the current workspace's absolute path rather than hardcoding a drive.

- Overview plan: `next-steps.html`
- Per-ticker detail: the SPA deep-dive, `web/index.html?ticker=<SYM>` (replaces the retired `<sym>-detail.html` pages).
- Loser review: the `loser-position-recovery` research segment in the Pipeline/Reports tabs.

## Navigation Expectations

- `next-steps.html` symbol cells link into the SPA deep-dive (`web/index.html?ticker=<SYM>`), produced by `generate_site._name_cell`; the top nav also links to the deep-dive and Reports.
- Root static pages should link to `web/index.html`; when served through `tools/serve.py`, `/web/index.html` and root-level `.html`/`.css`/`.js` assets are served through the static-file guard.

## Sensitive Data Rules

Do not commit:

- IBKR tokens or query IDs.
- Raw account identifiers.
- Raw Flex XML exports.
- Any `secrets.env` file.

Portfolio figures in these pages are summarized analytical outputs, not raw credentials or full account exports.
