"""Tests for the Czech 3-year exemption calendar: which lots become events, the
gain-vs-loss split, the "soon" flag, totals, the year-end rollup, and the pure
alert generator's dedup."""

from __future__ import annotations

import datetime as dt
import unittest

import _support  # noqa: F401
import tax_calendar as tc

AS_OF = dt.datetime(2026, 6, 13, tzinfo=dt.timezone.utc)


def lot(symbol, qty, mv, cost, opened):
    return {"symbol": symbol, "quantity": qty, "base_market_value": mv,
            "cost_basis_money": cost, "open_datetime": opened}


def _holdings():
    return {
        "base_currency": "CZK",
        "lots": [
            # taxable gain, ~49d to exempt (2026-08-01): gain 40k, tax 6k.
            lot("NEAR", 10, 100_000.0, 60_000.0, "2023-08-01T00:00:00Z"),
            # taxable gain, years out: gain 30k, tax 4.5k, not soon.
            lot("FAR", 10, 100_000.0, 70_000.0, "2025-06-01T00:00:00Z"),
            # taxable loss, ~18d to the exemption deadline (2026-07-01): loss 30k.
            lot("LOSSY", 10, 50_000.0, 80_000.0, "2023-07-01T00:00:00Z"),
            # already exempt: no future event either way.
            lot("OLD", 10, 100_000.0, 60_000.0, "2020-01-01T00:00:00Z"),
            lot("OLDLOSS", 10, 50_000.0, 90_000.0, "2021-01-01T00:00:00Z"),
        ],
    }


class BuildCalendar(unittest.TestCase):
    def test_only_not_yet_exempt_lots_become_events(self):
        cal = tc.build_calendar(_holdings(), as_of=AS_OF, soon_days=60)
        ex_syms = {e["symbol"] for e in cal["exemptions"]}
        hv_syms = {h["symbol"] for h in cal["harvest"]}
        self.assertEqual(ex_syms, {"NEAR", "FAR"})   # gain lots only
        self.assertEqual(hv_syms, {"LOSSY"})          # loss lot only
        self.assertNotIn("OLD", ex_syms)              # exempt -> no event
        self.assertNotIn("OLDLOSS", hv_syms)

    def test_exemptions_sorted_soonest_first_and_flagged(self):
        cal = tc.build_calendar(_holdings(), as_of=AS_OF, soon_days=60)
        self.assertEqual([e["symbol"] for e in cal["exemptions"]], ["NEAR", "FAR"])
        near, far = cal["exemptions"]
        self.assertTrue(near["soon"])
        self.assertFalse(far["soon"])
        self.assertEqual(near["tax_if_sold_now"], 6000.0)   # 40k gain * 15%
        self.assertEqual(far["tax_if_sold_now"], 4500.0)

    def test_totals(self):
        t = tc.build_calendar(_holdings(), as_of=AS_OF, soon_days=60)["totals"]
        self.assertEqual(t["n_exemptions"], 2)
        self.assertEqual(t["n_exemptions_soon"], 1)
        self.assertEqual(t["tax_free_soon"], 6000.0)
        self.assertEqual(t["tax_free_total"], 10500.0)
        self.assertEqual(t["harvestable_loss"], 30000.0)
        self.assertEqual(t["harvestable_loss_soon"], 30000.0)

    def test_harvest_carries_a_deadline(self):
        cal = tc.build_calendar(_holdings(), as_of=AS_OF, soon_days=60)
        h = cal["harvest"][0]
        self.assertEqual(h["symbol"], "LOSSY")
        self.assertEqual(h["loss"], 30000.0)
        self.assertEqual(h["deadline"], "2026-07-01")
        self.assertTrue(h["soon"])

    def test_soon_window_is_honoured(self):
        cal = tc.build_calendar(_holdings(), as_of=AS_OF, soon_days=10)
        # With a 10-day window, the ~49d NEAR gain is no longer "soon".
        self.assertFalse(next(e for e in cal["exemptions"] if e["symbol"] == "NEAR")["soon"])

    def test_empty_holdings_do_not_raise(self):
        cal = tc.build_calendar({"lots": []}, as_of=AS_OF)
        self.assertEqual(cal["exemptions"], [])
        self.assertEqual(cal["harvest"], [])
        self.assertEqual(cal["totals"]["n_exemptions"], 0)


class YearEnd(unittest.TestCase):
    def test_days_to_year_end(self):
        ye = tc.build_calendar(_holdings(), as_of=AS_OF)["year_end"]
        self.assertEqual(ye["date"], "2026-12-31")
        self.assertEqual(ye["days_to_year_end"], (dt.date(2026, 12, 31) - dt.date(2026, 6, 13)).days)

    def test_loss_clearing_before_year_end_is_excluded_from_usable(self):
        # LOSSY's deadline (2026-07-01) is before year-end, so it can't be
        # harvested "by year-end" even though it's in the total harvestable pool.
        ye = tc.build_calendar(_holdings(), as_of=AS_OF)["year_end"]
        self.assertEqual(ye["harvestable_loss"], 30000.0)
        self.assertEqual(ye["harvestable_by_year_end"], 0.0)


class PendingAlerts(unittest.TestCase):
    def test_soon_events_generate_alerts(self):
        alerts = tc.pending_alerts(_holdings(), {}, as_of=AS_OF, soon_days=60)
        keys = {a["key"] for a in alerts}
        self.assertIn("exempt:NEAR:2023-08-01T00:00:00Z", keys)
        self.assertIn("harvest:LOSSY:2023-07-01T00:00:00Z", keys)
        # FAR is not soon; no year-end alert this far out (201 days).
        self.assertFalse(any("FAR" in k for k in keys))
        self.assertFalse(any(k.startswith("year-end") for k in keys))

    def test_recently_notified_lots_are_suppressed(self):
        today = AS_OF.date().isoformat()
        notified = {
            "exempt:NEAR:2023-08-01T00:00:00Z": today,
            "harvest:LOSSY:2023-07-01T00:00:00Z": today,
        }
        self.assertEqual(tc.pending_alerts(_holdings(), notified, as_of=AS_OF), [])

    def test_stale_notification_no_longer_suppresses(self):
        old = (AS_OF.date() - dt.timedelta(days=tc.ALERT_TTL_DAYS + 1)).isoformat()
        notified = {"exempt:NEAR:2023-08-01T00:00:00Z": old}
        keys = {a["key"] for a in tc.pending_alerts(_holdings(), notified, as_of=AS_OF)}
        self.assertIn("exempt:NEAR:2023-08-01T00:00:00Z", keys)

    def test_year_end_alert_fires_inside_the_window(self):
        # In December, with a loss lot whose deadline is after year-end, the
        # year-end harvest nudge fires.
        holdings = {
            "base_currency": "CZK",
            "lots": [lot("LOSSY", 10, 50_000.0, 80_000.0, "2024-06-01T00:00:00Z")],
        }
        dec = dt.datetime(2026, 12, 10, tzinfo=dt.timezone.utc)
        keys = {a["key"] for a in tc.pending_alerts(holdings, {}, as_of=dec)}
        self.assertIn("year-end:2026", keys)


if __name__ == "__main__":
    unittest.main()
