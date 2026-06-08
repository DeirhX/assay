#!/usr/bin/env python3
"""Regenerate derived site artifacts from the single source of truth.

Source of truth: ``data/current-holdings.json`` (produced by the IBKR Flex
reader). Everything else that restates those numbers -- the markdown summary
and the numeric fragments embedded in the HTML pages -- is derived from it by
this script, so the figures cannot silently drift apart.

Targets:
  * ``data/current-holdings-summary.md``         (fully regenerated)
  * ``*.html`` pages                             (only ``<!--GEN:key-->`` spans)

HTML pages opt in per value using marker comments::

    <strong><!--GEN:nav.1pct-->NNNk CZK<!--/GEN:nav.1pct--></strong>

The text between the markers is replaced with the freshly computed value. The
markers themselves are preserved, so regeneration is idempotent.

Usage::

    py -3 tools/generate_site.py            # rewrite stale artifacts in place
    py -3 tools/generate_site.py --check    # exit 1 if anything is stale (CI)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from portfolio import provider_symbol_for, holdings_weights  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_JSON = REPO_ROOT / "data" / "current-holdings.json"
CLAIMS_JSON = REPO_ROOT / "data" / "research-claims.json"
SUMMARY_MD = REPO_ROOT / "data" / "current-holdings-summary.md"

# Claim metrics surfaced on detail pages, mapped to their short fragment suffix.
CLAIM_METRICS = {
    "price_usd": "price",
    "market_cap_usd_b": "mcap",
    "pe_ttm": "pe_ttm",
    "pe_fwd": "pe_fwd",
    "ps": "ps",
}

# Positions surfaced individually on the loser dashboard and detail pages.
LOSER_SYMBOLS = ["SOFI", "PYPL", "EEFT"]
TOP_POSITIONS_IN_SUMMARY = 20


def money_czk_short(value: float) -> str:
    """Human-friendly CZK magnitude, e.g. 250000 -> '250k CZK'."""
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:.2f}m CZK"
    return f"{round(value / 1000)}k CZK"


def pnl_usd_short(value: float) -> str:
    """Unrealized P/L formatted in USD thousands (a negative renders as '-$X.Xk')."""
    thousands = value / 1000
    if thousands < 0:
        return f"-${abs(thousands):.1f}k"
    return f"${thousands:.1f}k"


def lot_range(lot_summary: dict) -> str:
    oldest = lot_summary["oldest_open_date"]
    newest = lot_summary["newest_open_date"]
    return oldest if oldest == newest else f"{oldest} to {newest}"


def index_by_symbol(rows: list[dict]) -> dict[str, dict]:
    return {row["symbol"]: row for row in rows}


def compute_fragments(data: dict, claims: dict | None) -> dict[str, str]:
    """Build the key -> rendered-value map injected into HTML markers."""
    legend = data["sizing_legend"]
    fragments: dict[str, str] = {
        "nav.full": money_czk_short(data["net_asset_value"]),
        "nav.1pct": money_czk_short(legend["one_percent_nav_czk"]),
        "nav.2pct": money_czk_short(legend["two_percent_nav_czk"]),
        "nav.5pct": money_czk_short(legend["five_percent_nav_czk"]),
        "nav.10pct": money_czk_short(legend["ten_percent_nav_czk"]),
    }

    positions = index_by_symbol(data["positions"])
    weights = holdings_weights(data)
    lots = index_by_symbol(data["tax_lot_summary"])
    for symbol in LOSER_SYMBOLS:
        pos = positions[symbol]
        lot = lots[symbol]
        fragments[f"pos.{symbol}.shares"] = f"{int(round(pos['quantity'])):,}"
        fragments[f"pos.{symbol}.navpct"] = f"{weights.get(symbol, 0.0):.2f}%"
        fragments[f"pos.{symbol}.pnl"] = pnl_usd_short(pos["unrealized_pnl"])
        fragments[f"pos.{symbol}.lots"] = lot_range(lot)
        fragments[f"pos.{symbol}.cz3y"] = str(int(round(lot["cz_three_year_eligible_quantity"])))

    generated_date = str(data.get("generated_at", ""))[:10]
    report_to = data.get("report_to_date", "")
    fragments["snapshot.date"] = generated_date
    fragments["snapshot.report"] = (
        f"{report_to[:4]}-{report_to[4:6]}-{report_to[6:8]}" if len(report_to) == 8 else report_to
    )

    if claims:
        for symbol, metrics in claims.get("symbols", {}).items():
            for field, suffix in CLAIM_METRICS.items():
                node = metrics.get(field)
                if isinstance(node, dict) and "display" in node:
                    fragments[f"claim.{symbol}.{suffix}"] = node["display"]
    return fragments


def render_markdown(data: dict) -> str:
    legend = data["sizing_legend"]
    weights = holdings_weights(data)
    lines = [
        "# Current Holdings Snapshot",
        "",
        f"- Generated at: `{data['generated_at']}`",
        f"- Report date: `{data['report_from_date']}` to `{data['report_to_date']}`",
        f"- NAV: `{data['net_asset_value']:,.0f} CZK`",
        f"- 1% NAV: `{legend['one_percent_nav_czk']:,.0f} CZK`",
        "",
        "## Top Positions",
        "",
        "| Symbol | Description | NAV % | Base Value CZK | Unrealized P/L |",
        "| --- | --- | ---: | ---: | ---: |",
    ]
    for pos in data["top_positions"][:TOP_POSITIONS_IN_SUMMARY]:
        weight = weights.get(pos["symbol"], 0.0)
        provider_symbol = provider_symbol_for(pos["symbol"])
        display_symbol = (
            f"{pos['symbol']} → {provider_symbol}"
            if provider_symbol != pos["symbol"].upper().strip()
            else pos["symbol"]
        )
        lines.append(
            f"| `{display_symbol}` | {pos['description']} | "
            f"{weight:.2f}% | "
            f"{pos['base_market_value']:,.0f} | "
            f"{pos['unrealized_pnl']:,.0f} |"
        )

    lines += [
        "",
        "## Focus Tax-Lot Summary",
        "",
        "| Symbol | Lots | Quantity | Czech 3Y Eligible Qty | Oldest Open | Newest Open |",
        "| --- | ---: | ---: | ---: | --- | --- |",
    ]
    for lot in data["tax_lot_summary"]:
        lines.append(
            f"| `{lot['symbol']}` | {lot['lot_count']} | "
            f"{lot['total_quantity']:,.4f} | "
            f"{lot['cz_three_year_eligible_quantity']:,.4f} | "
            f"{lot['oldest_open_date']} | {lot['newest_open_date']} |"
        )

    lines += [
        "",
        "## Notes",
        "",
        "- This is a sanitized snapshot for repo-local analysis.",
        "- Account identifiers, tokens, and raw Flex XML are intentionally omitted.",
        "- Use `data/current-holdings.json` for machine-readable positions and tax lots.",
        "- Refresh before acting; IBKR Flex is a generated snapshot, not live market data.",
        "",
    ]
    return "\n".join(lines)


def apply_fragments(text: str, fragments: dict[str, str]) -> tuple[str, int]:
    """Replace the body of every recognized ``<!--GEN:key-->...<!--/GEN:key-->``."""
    replaced = 0

    def repl(match: re.Match) -> str:
        nonlocal replaced
        key = match.group("key")
        if key not in fragments:
            return match.group(0)
        replaced += 1
        return f"<!--GEN:{key}-->{fragments[key]}<!--/GEN:{key}-->"

    pattern = re.compile(
        r"<!--GEN:(?P<key>[\w.]+)-->.*?<!--/GEN:(?P=key)-->",
        re.DOTALL,
    )
    return pattern.sub(repl, text), replaced


def iter_targets(data: dict, claims: dict | None):
    """Yield (path, new_content) for every generated artifact."""
    fragments = compute_fragments(data, claims)
    yield SUMMARY_MD, render_markdown(data)
    for html_path in sorted(REPO_ROOT.glob("*.html")):
        current = html_path.read_text(encoding="utf-8")
        updated, replaced = apply_fragments(current, fragments)
        if replaced:
            yield html_path, updated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Do not write; exit non-zero if any artifact is stale.",
    )
    args = parser.parse_args()

    if not DATA_JSON.exists():
        print(f"error: source of truth not found: {DATA_JSON}", file=sys.stderr)
        print("hint: `data/` is a private submodule; run `git submodule update --init`.", file=sys.stderr)
        return 2

    data = json.loads(DATA_JSON.read_text(encoding="utf-8"))
    claims = (
        json.loads(CLAIMS_JSON.read_text(encoding="utf-8"))
        if CLAIMS_JSON.exists()
        else None
    )

    stale: list[Path] = []
    written: list[Path] = []
    for path, new_content in iter_targets(data, claims):
        on_disk = path.read_text(encoding="utf-8") if path.exists() else None
        if on_disk == new_content:
            continue
        rel = path.relative_to(REPO_ROOT)
        if args.check:
            stale.append(rel)
        else:
            path.write_text(new_content, encoding="utf-8")
            written.append(rel)

    if args.check:
        if stale:
            print("Stale generated artifacts (run tools/generate_site.py):")
            for rel in stale:
                print(f"  - {rel}")
            return 1
        print("All generated artifacts are in sync with data/current-holdings.json.")
        return 0

    if written:
        print("Regenerated:")
        for rel in written:
            print(f"  - {rel}")
    else:
        print("Nothing to regenerate; artifacts already match the snapshot.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
