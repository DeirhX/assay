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
import html
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rebalance  # noqa: E402  -- same drift/action engine the web planner uses
from portfolio import HOLDINGS_JSON as DATA_JSON, TARGET_MODEL_JSON, holdings_weights, provider_symbol_for  # noqa: E402
from config import REPO_ROOT  # noqa: E402

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


# --------------------------------------------------------------------------- #
# Region (block) generation: rebalance.plan() -> whole HTML table bodies
# --------------------------------------------------------------------------- #
def _esc(text: object) -> str:
    return html.escape(str(text or ""))


def _name_cell(name: str, kind: str) -> str:
    """Link a symbol to its systematic deep-dive in the SPA; sleeves render plain.

    The old hand-authored ``<sym>-detail.html`` pages were retired in favour of
    the data-backed deep-dive view, so every symbol now points at that instead
    (it works for any ticker, not just the five that once had a static page)."""
    if kind == "sleeve":
        return _esc(name)
    sym = (name or "").strip().upper()
    if not sym:
        return _esc(name)
    return f'<a href="web/index.html?ticker={_esc(sym)}">{_esc(name)}</a>'


def _price_per_share(holdings: dict) -> dict[str, float]:
    """symbol -> base-currency price per share, for converting CZK deltas to
    share counts. Derived from the snapshot, so it shares the snapshot's basis."""
    out: dict[str, float] = {}
    for pos in holdings.get("positions", []):
        qty = pos.get("quantity")
        bmv = pos.get("base_market_value")
        if isinstance(qty, (int, float)) and qty and isinstance(bmv, (int, float)):
            out[pos.get("symbol", "")] = bmv / qty
    return out


def _sensitive(text: str) -> str:
    """Wrap a value the privacy toggle should be able to mask."""
    return f'<span data-sensitive>{text}</span>'


def render_targets_block(plan: dict) -> str:
    """Body of the '1-3 Month Targets' table, straight from target-model bands +
    notes, annotated with the live status the planner computed."""
    lines: list[str] = []
    for r in plan["rows"]:
        band = f"{r['low']:g}-{r['high']:g}%"
        status = r["status"]
        if status == "IN":
            hint = "in band"
        else:
            now = _sensitive(f"{r['current_pct']:g}%")
            hint = f"now {now}, <strong>{status.lower()}</strong> band"
        note = _esc(r.get("note") or "")
        path = f"{note} ({hint})." if note else f"{hint.capitalize()}."
        lines.append(
            f"      <tr><td>{_name_cell(r['name'], r['kind'])}</td>"
            f'<td class="num">{band}</td><td>{path}</td></tr>'
        )
    return "\n".join(lines)


def render_actions_block(plan: dict, prices: dict[str, float]) -> str:
    """Body of the 'This Week' table: only out-of-band names that need a trade,
    trims first (in funding order), then buys by size. Mirrors rebalance.advice()."""
    funding_order: list[str] = plan.get("funding_order", [])

    def fpri(name: str) -> int:
        return funding_order.index(name) if name in funding_order else len(funding_order)

    trims, buys, reviews = [], [], []
    for r in plan["rows"]:
        action = r.get("action")
        if action == "trim":
            trims.append(r)
        elif action == "buy":
            buys.append(r)
        elif action == "review":
            reviews.append(r)
    trims.sort(key=lambda r: (fpri(r["name"]), -abs(r["suggest_delta_pct"])))
    buys.sort(key=lambda r: -abs(r["suggest_delta_pct"]))

    def move_cell(r: dict) -> str:
        pct = abs(r["suggest_delta_pct"])
        bits = [f"~{pct:g}% book"]
        czk = r.get("suggest_delta_czk")
        if isinstance(czk, (int, float)):
            bits.append(_sensitive(f"~{abs(czk):,.0f} CZK"))
        price = prices.get(r["name"])
        if r["kind"] == "target" and price:
            shares = abs(czk) / price if isinstance(czk, (int, float)) else 0
            if shares >= 1:
                bits.append(_sensitive(f"~{round(shares):,} sh"))
        return ", ".join(bits)

    rows = []

    def emit(label: str, items: list[dict]) -> None:
        for r in items:
            rows.append(
                f"      <tr><td>{label}</td><td>{_name_cell(r['name'], r['kind'])}</td>"
                f"<td>{move_cell(r)}</td><td>{_esc(r.get('note') or '')}</td></tr>"
            )

    emit("Trim / sell", trims)
    emit("Add / buy", buys)
    emit("Review", reviews)
    if not rows:
        rows.append('      <tr><td colspan="4">Everything is within its band — no trades needed.</td></tr>')
    return "\n".join(rows)


# Names you should not be buying, by rule, with a short human stance.
DO_NOT_CHASE_RULES = {
    "trim_only": "Trim only, never add",
    "do_not_add": "Hold, do not add",
    "wait": "Wait for a catalyst",
    "avoid": "Avoid / do not own",
}


def render_donotchase_block(plan: dict) -> str:
    """Body of the 'Do Not Chase' table: every name on a no-buy rule, with the
    live status appended so an over-band name reads as an active trim."""
    lines: list[str] = []
    for r in plan["rows"]:
        stance = DO_NOT_CHASE_RULES.get(r["rule"])
        if not stance:
            continue
        if r["status"] == "ABOVE":
            stance += " &mdash; over band, trim"
        why = _esc(r.get("note") or "")
        lines.append(
            f"      <tr><td>{_name_cell(r['name'], r['kind'])}</td>"
            f"<td>{stance}</td><td>{why}</td></tr>"
        )
    return "\n".join(lines)


def compute_blocks(holdings: dict, model: dict | None) -> dict[str, str]:
    """key -> generated HTML for every ``<!--GENBLOCK:key-->`` region."""
    if not model:
        return {}
    plan = rebalance.plan(model, holdings)
    prices = _price_per_share(holdings)
    return {
        "rebalance.targets": render_targets_block(plan),
        "rebalance.actions": render_actions_block(plan, prices),
        "rebalance.donotchase": render_donotchase_block(plan),
    }


def apply_blocks(text: str, blocks: dict[str, str]) -> tuple[str, int]:
    """Replace the body of every recognized ``<!--GENBLOCK:key-->...<!--/...-->``."""
    replaced = 0

    def repl(match: re.Match) -> str:
        nonlocal replaced
        key = match.group("key")
        if key not in blocks:
            return match.group(0)
        replaced += 1
        return f"<!--GENBLOCK:{key}-->\n{blocks[key]}\n      <!--/GENBLOCK:{key}-->"

    pattern = re.compile(
        r"<!--GENBLOCK:(?P<key>[\w.]+)-->.*?<!--/GENBLOCK:(?P=key)-->",
        re.DOTALL,
    )
    return pattern.sub(repl, text), replaced


def iter_targets(data: dict, claims: dict | None, model: dict | None = None):
    """Yield (path, new_content) for every generated artifact."""
    fragments = compute_fragments(data, claims)
    blocks = compute_blocks(data, model)
    yield SUMMARY_MD, render_markdown(data)
    for html_path in sorted(REPO_ROOT.glob("*.html")):
        current = html_path.read_text(encoding="utf-8")
        updated, n_frag = apply_fragments(current, fragments)
        updated, n_block = apply_blocks(updated, blocks)
        if n_frag or n_block:
            yield html_path, updated


def regenerate(write: bool = True) -> dict:
    """Programmatic entry point (used by both the CLI and serve.py).

    Recomputes every derived artifact from the data snapshot. When ``write`` is
    True, stale files are rewritten and listed in ``written``; when False (a dry
    run / check) they are listed in ``stale`` instead. Returns a JSON-friendly
    summary so the web layer can report what changed without parsing stdout."""
    if not DATA_JSON.exists():
        return {
            "ok": False,
            "error": f"source of truth not found: {DATA_JSON.name}; "
                     "`data/` is a private submodule (git submodule update --init).",
            "written": [], "stale": [],
        }

    data = json.loads(DATA_JSON.read_text(encoding="utf-8"))
    claims = json.loads(CLAIMS_JSON.read_text(encoding="utf-8")) if CLAIMS_JSON.exists() else None
    model = json.loads(TARGET_MODEL_JSON.read_text(encoding="utf-8")) if TARGET_MODEL_JSON.exists() else None

    written: list[str] = []
    stale: list[str] = []
    for path, new_content in iter_targets(data, claims, model):
        on_disk = path.read_text(encoding="utf-8") if path.exists() else None
        if on_disk == new_content:
            continue
        rel = str(path.relative_to(REPO_ROOT)).replace("\\", "/")
        if write:
            path.write_text(new_content, encoding="utf-8")
            written.append(rel)
        else:
            stale.append(rel)
    return {"ok": True, "error": None, "written": written, "stale": stale,
            "has_model": model is not None}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Do not write; exit non-zero if any artifact is stale.",
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
        print("All generated artifacts are in sync with data/current-holdings.json.")
        return 0

    if res["written"]:
        print("Regenerated:")
        for rel in res["written"]:
            print(f"  - {rel}")
    else:
        print("Nothing to regenerate; artifacts already match the snapshot.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
