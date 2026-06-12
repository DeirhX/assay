#!/usr/bin/env python3
"""Read your IBKR portfolio via the Flex Web Service (read-only).

The IBKR Flex Web Service is a two-step, read-only report API:
  1) SendRequest  -> returns a ReferenceCode for a freshly generated report
  2) GetStatement -> returns the report XML (generation takes a few seconds;
                     we poll until it's ready)

It is read-only by construction: the service only returns reports and can never
place trades. Data is a snapshot generated on request (positions/marks as-of
generation time), not a live tick stream.

Credentials: pass --token/--query-id, set IBKR_FLEX_TOKEN / IBKR_FLEX_QUERY_ID
in the environment, or (easiest) paste them into the local `secrets.env` file
next to this script. secrets.env is gitignored.

Zero third-party dependencies — standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"
SCRIPT_DIR = Path(__file__).resolve().parent
SECRETS_FILE = SCRIPT_DIR / "secrets.env"
USER_AGENT = "ibkr_portfolio/1.0 (+stdlib)"


# --------------------------------------------------------------------------- #
# Config / secrets
# --------------------------------------------------------------------------- #
def load_env_file(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE parser (no python-dotenv dependency)."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        out[key.strip()] = val.strip().strip('"').strip("'")
    return out


def resolve_credentials(args: argparse.Namespace) -> tuple[str, str]:
    file_vals = load_env_file(SECRETS_FILE)
    token = (
        args.token
        or os.environ.get("IBKR_FLEX_TOKEN")
        or file_vals.get("IBKR_FLEX_TOKEN")
    )
    query_id = (
        args.query_id
        or os.environ.get("IBKR_FLEX_QUERY_ID")
        or file_vals.get("IBKR_FLEX_QUERY_ID")
    )
    token = (token or "").strip()
    query_id = (query_id or "").strip()
    if not token or token.startswith("<"):
        sys.exit(
            f"ERROR: no Flex token. Paste it into {SECRETS_FILE.name}, set "
            "IBKR_FLEX_TOKEN, or pass --token."
        )
    if not query_id or query_id.startswith("<"):
        sys.exit(
            f"ERROR: no Query ID. Paste it into {SECRETS_FILE.name}, set "
            "IBKR_FLEX_QUERY_ID, or pass --query-id."
        )
    return token, query_id


# --------------------------------------------------------------------------- #
# HTTP / Flex flow
# --------------------------------------------------------------------------- #
def http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (trusted host)
        return resp.read()


def fetch_report(token: str, query_id: str, *, max_tries: int = 12,
                 delay: float = 3.0, verbose: bool = False) -> ET.Element:
    """Run the two-step Flex flow and return the parsed report root element."""
    # Step 1 — SendRequest
    q = urllib.parse.urlencode({"t": token, "q": query_id, "v": "3"})
    if verbose:
        print(f"-> SendRequest (queryId={query_id})", file=sys.stderr)
    root = ET.fromstring(http_get(f"{BASE}/SendRequest?{q}"))

    if root.tag != "FlexStatementResponse" or _text(root, "Status") != "Success":
        raise RuntimeError(
            f"SendRequest failed: Status={_text(root, 'Status')} "
            f"Code={_text(root, 'ErrorCode')} {_text(root, 'ErrorMessage')}"
        )
    ref_code = _text(root, "ReferenceCode")
    stmt_url = _text(root, "Url") or f"{BASE}/GetStatement"

    # Step 2 — GetStatement (poll)
    for attempt in range(1, max_tries + 1):
        q2 = urllib.parse.urlencode({"t": token, "q": ref_code, "v": "3"})
        doc = ET.fromstring(http_get(f"{stmt_url}?{q2}"))

        if doc.tag == "FlexQueryResponse":
            return doc
        if doc.tag == "FlexStatementResponse":
            code = _text(doc, "ErrorCode")
            msg = _text(doc, "ErrorMessage")
            if code == "1019" or "in progress" in (msg or "").lower():
                if verbose:
                    print(f"   not ready (try {attempt}/{max_tries}); "
                          f"waiting {delay}s...", file=sys.stderr)
                time.sleep(delay)
                continue
            raise RuntimeError(f"GetStatement failed: Code={code} {msg}")
        raise RuntimeError(f"Unexpected response root <{doc.tag}>")

    raise TimeoutError(
        f"Statement not ready after {int(max_tries * delay)}s. Try again shortly."
    )


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def _text(el: ET.Element, child: str) -> str | None:
    node = el.find(child)
    return node.text if node is not None else None


def _dec(val: str | None) -> float:
    try:
        return float(val) if val not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


def _optional_dec(val: str | None) -> float | None:
    if val in (None, ""):
        return None
    return _dec(val)


def _ibkr_datetime(val: str | None) -> str:
    """Convert IBKR's YYYYMMDD;HHMMSS timestamps to ISO-ish UTC strings."""
    if not val:
        return ""
    try:
        dt = datetime.strptime(val, "%Y%m%d;%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return val
    return dt.isoformat(timespec="seconds")


def _base_value(position_value: float, fx_rate_to_base: float) -> float:
    return position_value * (fx_rate_to_base or 1.0)


@dataclass
class Position:
    symbol: str
    asset_class: str
    sub_category: str
    description: str
    currency: str
    fx_rate_to_base: float
    quantity: float
    cost_price: float
    mark_price: float
    market_value: float
    base_market_value: float
    unrealized_pnl: float
    percent_of_nav: float | None
    issuer_country_code: str
    listing_exchange: str


@dataclass
class Lot:
    symbol: str
    asset_class: str
    sub_category: str
    description: str
    currency: str
    fx_rate_to_base: float
    quantity: float
    cost_price: float
    cost_basis_money: float
    mark_price: float
    market_value: float
    base_market_value: float
    unrealized_pnl: float
    open_datetime: str
    holding_period_datetime: str
    ib_holding_code: str
    originating_order_id: str
    originating_transaction_id: str
    issuer_country_code: str
    listing_exchange: str


@dataclass
class Portfolio:
    account: str
    generated_at: str
    report_from_date: str
    report_to_date: str
    report_when_generated: str
    net_asset_value: float | None
    positions: list[Position] = field(default_factory=list)
    lots: list[Lot] = field(default_factory=list)
    cash: list[dict] = field(default_factory=list)


def _parse_position(p: ET.Element) -> Position:
    position_value = _dec(p.get("positionValue"))
    fx_rate = _dec(p.get("fxRateToBase"))
    return Position(
        symbol=p.get("symbol", ""),
        asset_class=p.get("assetCategory", ""),
        sub_category=p.get("subCategory", ""),
        description=p.get("description", ""),
        currency=p.get("currency", ""),
        fx_rate_to_base=fx_rate,
        quantity=_dec(p.get("position")),
        cost_price=_dec(p.get("costBasisPrice")),
        mark_price=_dec(p.get("markPrice")),
        market_value=position_value,
        base_market_value=_base_value(position_value, fx_rate),
        unrealized_pnl=_dec(p.get("fifoPnlUnrealized")),
        percent_of_nav=_optional_dec(p.get("percentOfNAV")),
        issuer_country_code=p.get("issuerCountryCode", ""),
        listing_exchange=p.get("listingExchange", ""),
    )


def _parse_lot(p: ET.Element) -> Lot:
    position_value = _dec(p.get("positionValue"))
    fx_rate = _dec(p.get("fxRateToBase"))
    return Lot(
        symbol=p.get("symbol", ""),
        asset_class=p.get("assetCategory", ""),
        sub_category=p.get("subCategory", ""),
        description=p.get("description", ""),
        currency=p.get("currency", ""),
        fx_rate_to_base=fx_rate,
        quantity=_dec(p.get("position")),
        cost_price=_dec(p.get("costBasisPrice")),
        cost_basis_money=_dec(p.get("costBasisMoney")),
        mark_price=_dec(p.get("markPrice")),
        market_value=position_value,
        base_market_value=_base_value(position_value, fx_rate),
        unrealized_pnl=_dec(p.get("fifoPnlUnrealized")),
        open_datetime=_ibkr_datetime(p.get("openDateTime")),
        holding_period_datetime=_ibkr_datetime(p.get("holdingPeriodDateTime")),
        ib_holding_code=p.get("code", ""),
        originating_order_id=p.get("originatingOrderID", ""),
        originating_transaction_id=p.get("originatingTransactionID", ""),
        issuer_country_code=p.get("issuerCountryCode", ""),
        listing_exchange=p.get("listingExchange", ""),
    )


def _positions_from_lots(lots: list[Lot], nav: float | None) -> list[Position]:
    """Fallback for Flex queries configured with lot rows but no summary rows."""
    by_symbol: dict[tuple[str, str, str], list[Lot]] = {}
    for lot in lots:
        key = (lot.symbol, lot.asset_class, lot.currency)
        by_symbol.setdefault(key, []).append(lot)

    positions: list[Position] = []
    for lot_group in by_symbol.values():
        first = lot_group[0]
        quantity = sum(lot.quantity for lot in lot_group)
        market_value = sum(lot.market_value for lot in lot_group)
        base_market_value = sum(lot.base_market_value for lot in lot_group)
        cost_basis_money = sum(lot.cost_basis_money for lot in lot_group)
        unrealized_pnl = sum(lot.unrealized_pnl for lot in lot_group)
        percent_of_nav = (
            (base_market_value / nav) * 100 if nav not in (None, 0) else None
        )
        positions.append(Position(
            symbol=first.symbol,
            asset_class=first.asset_class,
            sub_category=first.sub_category,
            description=first.description,
            currency=first.currency,
            fx_rate_to_base=first.fx_rate_to_base,
            quantity=quantity,
            cost_price=(cost_basis_money / quantity if quantity else 0.0),
            mark_price=first.mark_price,
            market_value=market_value,
            base_market_value=base_market_value,
            unrealized_pnl=unrealized_pnl,
            percent_of_nav=percent_of_nav,
            issuer_country_code=first.issuer_country_code,
            listing_exchange=first.listing_exchange,
        ))
    return positions


def parse_portfolio(doc: ET.Element) -> Portfolio:
    stmt = doc.find("./FlexStatements/FlexStatement")
    if stmt is None:
        raise RuntimeError("No FlexStatement in report — check the query period.")
    account = stmt.get("accountId", "")

    cash: list[dict] = []
    for c in stmt.findall("./CashReport/CashReportCurrency"):
        cash.append({
            "currency": c.get("currency", ""),
            "ending_cash": _dec(c.get("endingCash")),
        })

    nav = None
    nav_rows = stmt.findall("./EquitySummaryInBase/EquitySummaryByReportDateInBase")
    if nav_rows:
        nav = _dec(nav_rows[-1].get("total"))

    positions: list[Position] = []
    lots: list[Lot] = []
    for p in stmt.findall("./OpenPositions/OpenPosition"):
        level = (p.get("levelOfDetail") or "").upper()
        if level == "LOT":
            lots.append(_parse_lot(p))
        else:
            positions.append(_parse_position(p))

    if not positions and lots:
        positions = _positions_from_lots(lots, nav)

    return Portfolio(
        account=account,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        report_from_date=stmt.get("fromDate", ""),
        report_to_date=stmt.get("toDate", ""),
        report_when_generated=stmt.get("whenGenerated", ""),
        net_asset_value=nav,
        positions=positions,
        lots=lots,
        cash=cash,
    )


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def print_table(pf: Portfolio) -> None:
    print(f"\nIBKR Portfolio - account {pf.account}")
    if pf.net_asset_value is not None:
        print(f"Net Asset Value: {pf.net_asset_value:,.2f}")
    print()
    if pf.positions:
        cols = (
            "SYMBOL", "CLASS", "CCY", "QTY", "COST", "MARK", "MKT VALUE",
            "BASE VALUE", "NAV %", "UNRLZD PnL",
        )
        rows = [
            (
                p.symbol, p.asset_class, p.currency,
                f"{p.quantity:,.2f}", f"{p.cost_price:,.4f}",
                f"{p.mark_price:,.4f}", f"{p.market_value:,.2f}",
                f"{p.base_market_value:,.2f}",
                "" if p.percent_of_nav is None else f"{p.percent_of_nav:,.2f}",
                f"{p.unrealized_pnl:,.2f}",
            )
            for p in sorted(pf.positions, key=lambda x: x.base_market_value, reverse=True)
        ]
        widths = [max(len(c), *(len(r[i]) for r in rows)) for i, c in enumerate(cols)]
        line = "  ".join(c.ljust(widths[i]) for i, c in enumerate(cols))
        print(line)
        print("  ".join("-" * widths[i] for i in range(len(cols))))
        for r in rows:
            print("  ".join(r[i].rjust(widths[i]) if i >= 3 else r[i].ljust(widths[i])
                            for i in range(len(cols))))
    else:
        print("No open positions in report.")
    if pf.cash:
        print("\nCash:")
        for c in pf.cash:
            print(f"  {c['currency']:<14} {c['ending_cash']:,.2f}")
    print()


def to_json(pf: Portfolio) -> str:
    d = asdict(pf)
    return json.dumps(d, indent=2)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def run_once(token: str, query_id: str, args: argparse.Namespace) -> int:
    """Fetch + render a single pull. Returns process-style exit code."""
    try:
        doc = fetch_report(token, query_id, verbose=args.verbose)
    except Exception as e:  # noqa: BLE001 — surface a clean message to the user
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    quiet = bool(args.watch)  # in watch mode, don't spam the full table
    if args.raw:
        output = ET.tostring(doc, encoding="unicode")
        if not quiet:
            print(output)
    else:
        pf = parse_portfolio(doc)
        output = to_json(pf)
        if quiet:
            nav = f"{pf.net_asset_value:,.2f}" if pf.net_asset_value is not None else "n/a"
            print(f"[{datetime.now().strftime('%H:%M:%S')}] {pf.account}  "
                  f"NAV={nav}  positions={len(pf.positions)}  lots={len(pf.lots)}")
        elif args.json:
            print(output)
        else:
            print_table(pf)

    if args.out:
        Path(args.out).write_text(output, encoding="utf-8")
        if not quiet:
            print(f"(written to {args.out})", file=sys.stderr)
    if args.snapshot_dir:
        snap_dir = Path(args.snapshot_dir)
        snap_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        ext = "xml" if args.raw else "json"
        (snap_dir / f"portfolio-{stamp}.{ext}").write_text(output, encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Read IBKR portfolio via Flex Web Service.")
    ap.add_argument("--token", help="Flex Web Service token (else env/secrets.env).")
    ap.add_argument("--query-id", help="Flex Query ID (else env/secrets.env).")
    ap.add_argument("--json", action="store_true", help="Emit parsed JSON.")
    ap.add_argument("--raw", action="store_true", help="Emit raw report XML.")
    ap.add_argument("--out", help="Write output to this file as well as stdout.")
    ap.add_argument("--snapshot-dir", help="Also write a timestamped copy into this dir.")
    ap.add_argument("--watch", type=int, metavar="SECONDS",
                    help="Re-pull every N seconds (min 60). Flex reports refresh "
                         "slowly, so keep this generous; Ctrl+C to stop.")
    ap.add_argument("-v", "--verbose", action="store_true", help="Show poll progress.")
    args = ap.parse_args(argv)

    token, query_id = resolve_credentials(args)

    if not args.watch:
        return run_once(token, query_id, args)

    interval = max(60, args.watch)
    if interval != args.watch:
        print(f"(watch interval raised to {interval}s — Flex is rate-limited and "
              "data refreshes slowly)", file=sys.stderr)
    print(f"Watching every {interval}s. Ctrl+C to stop.", file=sys.stderr)
    try:
        while True:
            run_once(token, query_id, args)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
