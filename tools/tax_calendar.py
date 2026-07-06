#!/usr/bin/env python3
"""Czech 3-year exemption *calendar* -- the guaranteed-alpha lever, made proactive.

``tax_lots.py`` answers "which lots are cheapest to sell for *this* trim" --
reactively, only when a trim is being sized. This module inverts it: given the
whole book, it lays every not-yet-exempt lot on a forward calendar, so the
cheapest tax decision (often just "wait a few weeks") is visible *before* you
size a trade -- and can be pushed as a scheduler alert.

Two kinds of dated events, both derived from the same classification
(:func:`tax_lots.classify_lots`) so the calendar can never disagree with the
planner:

* **Exemption events** -- a taxable-*gain* lot approaching the 3-year mark.
  Selling it today realizes ``gain * rate`` of Czech tax; waiting until
  ``exempt_on`` makes that gain tax-free. *"TSM lot from 2023-08: tax-free in 41
  days; trimming today realizes X CZK."*
* **Harvest deadlines** -- a taxable-*loss* lot approaching the 3-year mark. A
  realized loss on a <3y lot offsets other taxable gains in the same tax period;
  once the lot turns exempt the loss becomes **unusable**. So exemption is a
  *deadline* for losses -- the mirror image of the gain case.

Plus a **year-end** rollup: the Czech tax period is the calendar year, so
harvestable losses are a use-it-or-lose-it decision each December.

Analysis only. Pure over lot dicts: no disk, no network, never trades. Money is
the account base currency (CZK); the 3-year clock is the lot's ``open_datetime``,
never IBKR's ST/LT code (that uses US rules). Not tax advice.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import portfolio  # noqa: E402
import tax_lots  # noqa: E402

# A lot this close to the 3-year mark is "soon" -- worth a flag on the calendar
# and a scheduler alert. Distinct from tax_lots.NEAR_EXEMPT_DAYS (the wider
# window at which the *planner* annotates a trim), because a standing alert should
# be tighter than a passive annotation.
SOON_DAYS = 60

# A year-end harvest nudge only fires inside this many days of Dec 31.
YEAR_END_WINDOW_DAYS = 30

# Don't re-alert the same lot within this window (scheduler dedup).
ALERT_TTL_DAYS = 25

CZ_TAX_RATE = tax_lots.CZ_TAX_RATE


# --------------------------------------------------------------------------- #
# Calendar
# --------------------------------------------------------------------------- #
def _symbols_with_lots(holdings: dict[str, Any]) -> list[str]:
    """Distinct symbols that have at least one tax lot, in first-seen order."""
    out: list[str] = []
    seen: set[str] = set()
    for lot in (holdings.get("lots") if isinstance(holdings, dict) else None) or []:
        sym = portfolio.clean_symbol(lot.get("symbol"))
        if sym and sym not in seen:
            seen.add(sym)
            out.append(sym)
    return out


def build_calendar(
    holdings: dict[str, Any],
    *,
    as_of: dt.datetime | None = None,
    soon_days: int = SOON_DAYS,
    tax_rate: float = CZ_TAX_RATE,
) -> dict[str, Any]:
    """Forward exemption calendar over the whole book.

    Returns ``{as_of, currency, exemptions[], harvest[], totals, year_end}``.
    ``exemptions`` are taxable-gain lots (soonest first); ``harvest`` are
    taxable-loss lots whose usable-loss window is closing (soonest deadline
    first). Exempt lots (already past the 3-year mark) carry no future event and
    are omitted.
    """
    as_of_dt = tax_lots._as_of(as_of)
    base_ccy = (holdings.get("base_currency") if isinstance(holdings, dict) else None) or "CZK"

    exemptions: list[dict[str, Any]] = []
    harvest: list[dict[str, Any]] = []
    for sym in _symbols_with_lots(holdings):
        for lot in tax_lots.classify_lots(holdings, sym, as_of=as_of_dt):
            bucket = lot.get("bucket")
            dte = lot.get("days_to_exempt")
            if not isinstance(dte, int):
                continue  # already exempt (or undated) -> no future event
            if bucket == "taxable_gain":
                exemptions.append({
                    "symbol": sym,
                    "open_datetime": lot.get("open_datetime"),
                    "exempt_on": lot.get("exempt_on"),
                    "days_to_exempt": dte,
                    "shares": lot.get("qty"),
                    "market_value": round(float(lot.get("mv") or 0.0), 2),
                    "gain": round(float(lot.get("gain") or 0.0), 2),
                    "tax_if_sold_now": round(max(0.0, float(lot.get("gain") or 0.0)) * tax_rate, 2),
                    "soon": dte <= soon_days,
                    "currency": base_ccy,
                })
            elif bucket == "taxable_loss":
                harvest.append({
                    "symbol": sym,
                    "open_datetime": lot.get("open_datetime"),
                    # When the lot turns exempt the loss stops being usable, so its
                    # exemption date is the harvest *deadline*.
                    "deadline": lot.get("exempt_on"),
                    "days_to_deadline": dte,
                    "shares": lot.get("qty"),
                    "market_value": round(float(lot.get("mv") or 0.0), 2),
                    "loss": round(-float(lot.get("gain") or 0.0), 2),   # positive number
                    "soon": dte <= soon_days,
                    "currency": base_ccy,
                })

    exemptions.sort(key=lambda e: e["days_to_exempt"])
    harvest.sort(key=lambda h: (h["days_to_deadline"], -h["loss"]))

    totals = {
        "n_exemptions": len(exemptions),
        "n_exemptions_soon": sum(1 for e in exemptions if e["soon"]),
        "n_harvest": len(harvest),
        "n_harvest_soon": sum(1 for h in harvest if h["soon"]),
        # Tax that becomes free as these lots cross the 3-year line.
        "tax_free_soon": round(sum(e["tax_if_sold_now"] for e in exemptions if e["soon"]), 2),
        "tax_free_total": round(sum(e["tax_if_sold_now"] for e in exemptions), 2),
        "harvestable_loss": round(sum(h["loss"] for h in harvest), 2),
        "harvestable_loss_soon": round(sum(h["loss"] for h in harvest if h["soon"]), 2),
    }

    return {
        "as_of": as_of_dt.date().isoformat(),
        "snapshot": holdings.get("generated_at") if isinstance(holdings, dict) else None,
        "currency": base_ccy,
        "soon_days": soon_days,
        "tax_rate": tax_rate,
        "exemptions": exemptions,
        "harvest": harvest,
        "totals": totals,
        "year_end": _year_end(as_of_dt.date(), harvest),
    }


def _year_end(today: dt.date, harvest: list[dict[str, Any]]) -> dict[str, Any]:
    """Days to Dec 31 plus the loss still harvestable this tax period.

    A loss lot whose exemption deadline lands *before* year-end must be harvested
    even sooner than Dec 31; ``harvestable_by_year_end`` counts only lots still
    usable on the 31st."""
    year_end = dt.date(today.year, 12, 31)
    days = (year_end - today).days
    year_end_iso = year_end.isoformat()
    usable = sum(
        h["loss"] for h in harvest
        if not h.get("deadline") or str(h["deadline"]) >= year_end_iso
    )
    return {
        "date": year_end_iso,
        "days_to_year_end": days,
        "harvestable_loss": round(sum(h["loss"] for h in harvest), 2),
        "harvestable_by_year_end": round(usable, 2),
    }


# --------------------------------------------------------------------------- #
# Scheduler alerts (pure -- the scheduler wires notify + persistence around this)
# --------------------------------------------------------------------------- #
def _recently_notified(notified: dict[str, Any], key: str, today: dt.date) -> bool:
    prev = notified.get(key)
    if not prev:
        return False
    try:
        prev_d = dt.date.fromisoformat(str(prev)[:10])
    except ValueError:
        return False
    return (today - prev_d).days < ALERT_TTL_DAYS


def pending_alerts(
    holdings: dict[str, Any],
    notified: dict[str, Any] | None = None,
    *,
    as_of: dt.datetime | None = None,
    soon_days: int = SOON_DAYS,
    tax_rate: float = CZ_TAX_RATE,
) -> list[dict[str, Any]]:
    """Alerts due right now, excluding lots re-notified inside ``ALERT_TTL_DAYS``.

    Pure over ``(holdings, notified)`` so it is trivially testable: each item is
    ``{key, title, body, tags, priority}``. The scheduler sends them and stamps
    ``key -> today`` only for the ones it actually delivered.
    """
    as_of_dt = tax_lots._as_of(as_of)
    today = as_of_dt.date()
    notified = notified or {}
    cal = build_calendar(holdings, as_of=as_of_dt, soon_days=soon_days, tax_rate=tax_rate)
    ccy = cal["currency"]
    out: list[dict[str, Any]] = []

    for e in cal["exemptions"]:
        if not e["soon"]:
            continue
        key = f"exempt:{e['symbol']}:{e['open_datetime']}"
        if _recently_notified(notified, key, today):
            continue
        out.append({
            "key": key,
            "title": f"{e['symbol']}: tax-free in {e['days_to_exempt']}d",
            "body": (f"{e['symbol']} lot opened {str(e['open_datetime'])[:10]} clears the 3-year "
                     f"exemption on {e['exempt_on']} ({e['days_to_exempt']}d). Trimming it today "
                     f"realizes ~{e['tax_if_sold_now']:,.0f} {ccy} of tax; waiting makes it free."),
            "tags": ("hourglass_flowing_sand",),
            "priority": "default",
        })

    for h in cal["harvest"]:
        if not h["soon"]:
            continue
        key = f"harvest:{h['symbol']}:{h['open_datetime']}"
        if _recently_notified(notified, key, today):
            continue
        out.append({
            "key": key,
            "title": f"{h['symbol']}: loss-harvest deadline in {h['days_to_deadline']}d",
            "body": (f"{h['symbol']} lot opened {str(h['open_datetime'])[:10]} carries a "
                     f"~{h['loss']:,.0f} {ccy} loss that stops offsetting gains once it turns "
                     f"exempt on {h['deadline']} ({h['days_to_deadline']}d). Harvest before then."),
            "tags": ("scissors",),
            "priority": "default",
        })

    ye = cal["year_end"]
    if 0 <= ye["days_to_year_end"] <= YEAR_END_WINDOW_DAYS and ye["harvestable_by_year_end"] > 0:
        key = f"year-end:{today.year}"
        if not _recently_notified(notified, key, today):
            out.append({
                "key": key,
                "title": f"Year-end: {ye['harvestable_by_year_end']:,.0f} {ccy} of losses to harvest",
                "body": (f"{ye['days_to_year_end']}d to {ye['date']}. Realizing up to "
                         f"~{ye['harvestable_by_year_end']:,.0f} {ccy} of losses this tax period "
                         f"offsets taxable gains. Unused losses don't carry the same way once lots turn exempt."),
                "tags": ("calendar",),
                "priority": "default",
            })

    return out


def _main() -> int:
    import argparse
    import json

    import store
    from config import HOLDINGS_JSON

    ap = argparse.ArgumentParser(description="Czech 3-year exemption calendar.")
    ap.add_argument("--soon-days", type=int, default=SOON_DAYS)
    ap.add_argument("--alerts", action="store_true", help="print due alerts instead of the calendar")
    args = ap.parse_args()

    holdings = store.load(HOLDINGS_JSON)
    if not holdings:
        raise SystemExit("no holdings snapshot -- sync from IBKR first")
    if args.alerts:
        print(json.dumps(pending_alerts(holdings, {}, soon_days=args.soon_days), indent=2))
    else:
        print(json.dumps(build_calendar(holdings, soon_days=args.soon_days), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
