# Tools

## generate_site.py

Single source of truth for portfolio numbers is `data/current-holdings.json`
(produced by the IBKR Flex reader). This script rederives everything that
restates those numbers, so the markdown summary and HTML pages cannot silently
drift away from the snapshot.

### What it regenerates

- `data/current-holdings-summary.md` — fully rewritten from the JSON.
- `*.html` — only the values inside `<!--GEN:key-->...<!--/GEN:key-->` markers.

### Usage

```powershell
py -3 tools/generate_site.py          # rewrite stale artifacts in place
py -3 tools/generate_site.py --check  # exit 1 if anything is stale (CI/pre-commit)
```

Run it after every fresh IBKR pull (see the `ibkr-holdings` skill), then review
the diff before committing.

### Adding a generated value to a page

Wrap the literal in marker comments and add the key to `compute_fragments()`:

```html
<strong><!--GEN:nav.1pct-->«redacted»<!--/GEN:nav.1pct--></strong>
```

The text between the markers is the seed value; the script overwrites it. The
markers survive regeneration, so the operation is idempotent.

### Available keys

- `nav.full`, `nav.1pct`, `nav.2pct`, `nav.5pct`, `nav.10pct` — NAV sizing legend.
- `pos.<SYMBOL>.shares|navpct|pnl|lots|cz3y` — per-position figures
  (`SYMBOL` currently limited to `LOSER_SYMBOLS` in the script).
- `claim.<SYMBOL>.price|mcap|pe_ttm|pe_fwd|ps` — valuation claims rendered from
  `data/research-claims.json` (see below).

## research-claims.json + verify_claims.py

`data/research-claims.json` holds the **structured valuation claims** that the
detail pages display (price, market cap, P/E, P/S). Each metric carries a numeric
`value` (or `low`/`high`) used for verification plus a `display` string used for
rendering, and an `asof` date that anchors the claim in time. Edit numbers here,
not in the HTML — `generate_site.py` pushes `display` into the `claim.*` markers.

`tools/verify_claims.py` is an **offline, deterministic** consistency check
(Phase 0). It does not fetch live quotes; it checks claims against each other and
against the broker marks in `current-holdings.json`:

```powershell
py -3 tools/verify_claims.py            # report findings
py -3 tools/verify_claims.py --strict   # also fail (exit 1) on warnings
```

Checks performed:

- **Identity** (ERROR): `price x shares_out ~= market_cap` within 5%. Catches the
  internally-impossible figures the "Data Hygiene" sections warn about in prose.
- **Snapshot price** (WARN): claimed price vs the broker `mark_price` within 3%.
- **Range edge** (INFO): broker mark falling outside a claimed price range.
- **Regression guard** (INFO/ERROR): values listed in `disproven_market_cap_usd_b`
  must stay arithmetically inconsistent; if one ever starts passing, that's an error.
- **Multiples** (ERROR): P/E and P/S must be positive.

Note: a claim is verified against its `asof`, not "now" — a moved market is not a
lie. Live cross-checking against an independent source (yfinance is the chosen
provider) is a later phase; IBKR Flex cannot supply live quotes, so it stays the
snapshot refresher, not a quote feed.
