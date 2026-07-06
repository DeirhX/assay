"""Tests for ledger-vs-snapshot drift. Pure over two dicts: the holdings snapshot
and the Flex execution ledger. The signal is "trades dated after the snapshot",
NOT absolute-position reconstruction (which a rolling ledger can't do honestly)."""

from __future__ import annotations

import unittest

import _support  # noqa: F401
import reconcile

SNAP_AT = "2026-06-10T16:00:00+00:00"


def _holdings(generated_at=SNAP_AT):
    return {"generated_at": generated_at,
            "positions": [{"symbol": "NVDA", "quantity": 100, "asset_class": "STK"}]}


def _trade(dt_, sym="NVDA", side="SELL", qty=-10):
    return {"datetime": dt_, "date": dt_[:10], "symbol": sym, "side": side, "quantity": qty}


class NotChecked(unittest.TestCase):
    def test_no_snapshot(self):
        r = reconcile.drift_report(None, {"trades": []})
        self.assertFalse(r["checked"])
        self.assertFalse(r["stale_vs_ledger"])
        self.assertIn("snapshot", r["reason"])

    def test_no_ledger(self):
        r = reconcile.drift_report(_holdings(), None)
        self.assertFalse(r["checked"])
        self.assertIn("ledger", r["reason"])

    def test_unparseable_snapshot_time(self):
        r = reconcile.drift_report(_holdings("not-a-date"), {"trades": [_trade("2026-06-11T10:00:00+00:00")]})
        self.assertFalse(r["checked"])


class Drift(unittest.TestCase):
    def test_no_trades_after_snapshot_is_clean(self):
        # Every trade predates the snapshot -> the book is current w.r.t. the ledger.
        hist = {"trades": [_trade("2026-06-01T10:00:00+00:00"), _trade("2026-06-09T10:00:00+00:00")]}
        r = reconcile.drift_report(_holdings(), hist)
        self.assertTrue(r["checked"])
        self.assertFalse(r["stale_vs_ledger"])
        self.assertEqual(r["n_trades_after"], 0)

    def test_flags_trades_after_snapshot(self):
        hist = {"trades": [
            _trade("2026-06-05T10:00:00+00:00"),                       # before -> ignored
            _trade("2026-06-11T14:30:00+00:00", side="SELL", qty=-10),  # after
            _trade("2026-06-12T09:00:00+00:00", sym="AMD", side="BUY", qty=5),  # after
        ]}
        r = reconcile.drift_report(_holdings(), hist)
        self.assertTrue(r["stale_vs_ledger"])
        self.assertEqual(r["n_trades_after"], 2)
        self.assertEqual(r["last_trade_at"], "2026-06-12T09:00:00+00:00")
        syms = {row["symbol"]: row for row in r["by_symbol"]}
        self.assertEqual(syms["NVDA"]["net_qty"], -10.0)
        self.assertEqual(syms["NVDA"]["sells"], 1)
        self.assertEqual(syms["AMD"]["net_qty"], 5.0)
        self.assertEqual(syms["AMD"]["buys"], 1)

    def test_nets_multiple_trades_per_symbol(self):
        hist = {"trades": [
            _trade("2026-06-11T10:00:00+00:00", side="BUY", qty=8),
            _trade("2026-06-11T11:00:00+00:00", side="SELL", qty=-3),
        ]}
        r = reconcile.drift_report(_holdings(), hist)
        nvda = r["by_symbol"][0]
        self.assertEqual(nvda["net_qty"], 5.0)
        self.assertEqual(nvda["buys"], 1)
        self.assertEqual(nvda["sells"], 1)

    def test_trades_after_list_is_capped_and_ordered(self):
        trades = [_trade(f"2026-06-{11 + i:02d}T10:00:00+00:00") for i in range(12)]
        r = reconcile.drift_report(_holdings(), {"trades": trades}, list_limit=5)
        self.assertEqual(r["n_trades_after"], 12)
        self.assertEqual(len(r["trades_after"]), 5)          # capped
        # Kept the most recent, still time-ascending.
        dts = [t["datetime"] for t in r["trades_after"]]
        self.assertEqual(dts, sorted(dts))
        self.assertEqual(dts[-1], "2026-06-22T10:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
