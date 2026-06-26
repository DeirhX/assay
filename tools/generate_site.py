#!/usr/bin/env python3
"""Regenerate the human-readable holdings summary from the single source of truth.

Source of truth: ``data/current-holdings.json`` (produced by the IBKR Flex
reader). The markdown summary restates a slice of those numbers for quick,
no-tooling context, so it is derived from the JSON by this script and cannot
silently drift from it.

Target:
  * ``data/current-holdings-summary.md``         (fully regenerated)

The repo used to also keep a static HTML rebalance plan (``next-steps.html`` and
per-stock detail pages) in sync via ``<!--GEN:...-->`` markers. That static
surface was retired in favour of the SPA, which recommends next steps
dynamically from the same data + target model, so the HTML half is gone and only
the markdown snapshot remains.

Usage::

    py -3 tools/generate_site.py            # rewrite the summary if stale
    py -3 tools/generate_site.py --check    # exit 1 if the summary is stale (CI)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from portfolio import HOLDINGS_JSON as DATA_JSON, holdings_weights, provider_symbol_for  # noqa: E402
from config import REPO_ROOT  # noqa: E402

SUMMARY_MD = REPO_ROOT / "data" / "current-holdings-summary.md"

TOP_POSITIONS_IN_SUMMARY = 20


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


def regenerate(write: bool = True) -> dict:
    """Programmatic entry point (used by the CLI, holdings sync, and commit path).

    Recomputes the markdown summary from the data snapshot. When ``write`` is
    True a stale summary is rewritten and listed in ``written``; when False (a
    dry run / check) it is listed in ``stale`` instead. Returns a JSON-friendly
    summary so the web layer can report what changed without parsing stdout."""
    if not DATA_JSON.exists():
        return {
            "ok": False,
            "error": f"source of truth not found: {DATA_JSON.name}; "
                     "`data/` is a private submodule (git submodule update --init).",
            "written": [], "stale": [],
        }

    data = json.loads(DATA_JSON.read_text(encoding="utf-8"))
    new_content = render_markdown(data)
    on_disk = SUMMARY_MD.read_text(encoding="utf-8") if SUMMARY_MD.exists() else None

    written: list[str] = []
    stale: list[str] = []
    if on_disk != new_content:
        rel = str(SUMMARY_MD.relative_to(REPO_ROOT)).replace("\\", "/")
        if write:
            SUMMARY_MD.write_text(new_content, encoding="utf-8")
            written.append(rel)
        else:
            stale.append(rel)
    return {"ok": True, "error": None, "written": written, "stale": stale}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Do not write; exit non-zero if the summary is stale.",
    )
    args = parser.parse_args()

    res = regenerate(write=not args.check)
    if not res["ok"]:
        print(f"error: {res['error']}", file=sys.stderr)
        return 2

    if args.check:
        if res["stale"]:
            print("Stale generated artifacts (run tools/generate_site.py):")
            for rel in res["stale"]:
                print(f"  - {rel}")
            return 1
        print("The holdings summary is in sync with data/current-holdings.json.")
        return 0

    if res["written"]:
        print("Regenerated:")
        for rel in res["written"]:
            print(f"  - {rel}")
    else:
        print("Nothing to regenerate; the summary already matches the snapshot.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
