---
name: rebalancing-site
description: Maintain the static HTML rebalancing mini-site. Use when asked to update, add, reorganize, or summarize research pages in the rebalancing directory.
---

# Rebalancing Site

## Start Here

Before editing pages, read:

- [SITE_MAP.md](SITE_MAP.md)

If investment recommendations change, also read the `portfolio-rebalancing` skill and its `CURRENT_PLAN.md`.

## Site Rules

- Keep the site static: plain HTML and CSS, no build step.
- Use relative links between pages.
- Keep pages concise and decision-oriented.
- Preserve the dark visual style unless the user asks for a redesign.
- Include a visible sizing legend when pages use NAV percentages.
- Put source links on detail pages when specific stock claims are made.
- Do not add secrets, raw IBKR XML, account numbers, or credentials.

## Current Page Set

- `next-steps.html`: main execution checklist.
- `amd-detail.html`: AMD momentum, valuation, and action detail.
- `arm-detail.html`: ARM momentum, valuation, and action detail.
- `loser-position-recovery.html`: normalized dashboard for `EEFT`, `PYPL`, `SOFI`.
- `sofi-detail.html`: SOFI research detail.
- `pypl-detail.html`: PYPL research detail.
- `eeft-detail.html`: EEFT research detail.

## Editing Checklist

1. Update the relevant HTML page.
2. Add/update navigation links if a new page is introduced.
3. Keep `SITE_MAP.md` current.
4. Run lints/checks for edited files if available.
5. In the final response, provide the repo-relative path to the main updated page (e.g. `next-steps.html`). If an openable link is needed, build a `file://` URL from the current workspace's absolute path rather than hardcoding a drive.
