# Tools

## serve.py + research_pull.py + providers/ (Interactive Research Console)

On-demand deep analysis for a single ticker or a whole industry segment. Stdlib
only -- no pip installs.

```powershell
$env:SEC_USER_AGENT = "finance-rebalancing research (you@example.com)"
py -3 tools/serve.py            # UI + API at http://127.0.0.1:8765 (localhost only)

py -3 tools/research_pull.py --ticker NVDA       # CLI: one deep dive
py -3 tools/research_pull.py --segment semiconductors   # CLI: whole peer set
```

### Pieces

- `providers/yahoo.py` -- price/momentum (chart endpoint) + fundamentals
  (quoteSummary, via the cookie+crumb handshake). Same source as `yfinance`,
  hit directly.
- `providers/sec_edgar.py` -- the free, authoritative cross-check for **US
  filers**: shares outstanding, revenue, net income from XBRL `companyfacts`.
  Foreign filers (ADRs) often have thin/absent data; the app flags that rather
  than pretending it verified anything.
- `providers/fmp.py` -- optional third opinion; enabled only if `FMP_API_KEY` is
  set (read from `secrets.env`, gitignored).
- `research_pull.py` -- pulls all sources, merges with a preferred-source order,
  and **cross-checks** them (price x shares vs market cap, Yahoo vs SEC share
  count, TTM revenue agreement, price freshness, single-source warnings). Writes
  `data/research/<SYMBOL>.json` and `data/research/segments/<name>.json`. It
  preserves any human-authored `thesis` block across re-pulls.
- `serve.py` -- stdlib `http.server` app serving `web/` and a small JSON API
  (`/api/holdings`, `/api/segments`, `/api/research/<sym>`, `POST /api/pull/<sym>`,
  `POST /api/pull-segment/<name>`, `POST /api/thesis/<sym>`).

### Data outputs

- `data/research/<SYMBOL>.json` -- per-ticker numbers + cross-checks + thesis.
  Carries human judgement, so it can be committed.
- `data/research/segments/<name>.json` -- derived peer dashboard (gitignored).
- `data/cache/sec_ticker_cik.json` -- weekly ticker->CIK cache (gitignored).
- `data/segments/<name>.json` -- **input** universe definition (committed), e.g.
  `semiconductors.json` with sleeves matching `CURRENT_PLAN.md`.

### Relationship to verify_claims.py

`verify_claims.py` stays the **offline** consistency check for the committed
claims in `research-claims.json`. The console is the **live** counterpart that
`tools/README` previously called "a later phase": it cross-checks fetched numbers
against an independent source at pull time.

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
- `snapshot.date`, `snapshot.report` — snapshot `generated_at` date and IBKR
  report date, shown in the staleness banner on the hub pages.

### Staleness: static banner vs run-time check

The site banner shows the snapshot's *absolute* date (deterministic, so `--check`
stays stable). It deliberately does **not** show a live "N days old" age, because
that would change every day and constantly invalidate the committed HTML. The
*age* check lives in `verify_claims.py` instead, where using the current time is
fine (it never writes files).

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
- **Snapshot age** (WARN/ERROR): `generated_at` older than 5 days warns, older
  than 30 days errors. Run-time check; uses the current date.

Note: a claim is verified against its `asof`, not "now" — a moved market is not a
lie. Live cross-checking against an independent source (yfinance is the chosen
provider) is a later phase; IBKR Flex cannot supply live quotes, so it stays the
snapshot refresher, not a quote feed.
