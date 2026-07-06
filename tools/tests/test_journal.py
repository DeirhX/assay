"""Tests for the decision journal: entry validation, append-only persistence,
outcome recording, and the calibration that scores directional calls."""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

import _support  # noqa: F401
import journal

UTC = dt.timezone.utc


class AddEntry(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "journal.json"

    def test_requires_symbol_action_thesis(self):
        with self.assertRaises(ValueError):
            journal.add_entry({"action": "buy", "thesis": "x"}, path=self.tmp)
        with self.assertRaises(ValueError):
            journal.add_entry({"symbol": "AMD", "action": "yolo", "thesis": "x"}, path=self.tmp)
        with self.assertRaises(ValueError):
            journal.add_entry({"symbol": "AMD", "action": "buy"}, path=self.tmp)

    def test_append_assigns_id_and_persists(self):
        e1 = journal.add_entry({"symbol": "amd", "action": "buy", "thesis": "cheap", "price": 100}, path=self.tmp)
        e2 = journal.add_entry({"symbol": "NVDA", "action": "trim", "thesis": "rich"}, path=self.tmp)
        self.assertEqual(e1["symbol"], "AMD")           # normalized
        self.assertNotEqual(e1["id"], e2["id"])
        entries = journal.load_entries(self.tmp)
        self.assertEqual(len(entries), 2)               # append-only
        self.assertEqual([e["symbol"] for e in entries], ["AMD", "NVDA"])

    def test_record_outcome_then_calibrates_from_it(self):
        e = journal.add_entry({"symbol": "AMD", "action": "buy", "thesis": "x", "price": 100}, path=self.tmp)
        journal.record_outcome(e["id"], 120.0, "rallied", path=self.tmp)
        entries = journal.load_entries(self.tmp)
        cal = journal.calibrate(entries)               # no price_map; uses recorded outcome
        self.assertEqual(cal["n_scored"], 1)
        self.assertEqual(cal["n_correct"], 1)
        self.assertAlmostEqual(cal["scored"][0]["move_pct"], 20.0)

    def test_record_outcome_unknown_id_raises(self):
        with self.assertRaises(ValueError):
            journal.record_outcome("nope", 1.0, path=self.tmp)


class Calibrate(unittest.TestCase):
    def _entry(self, symbol, action, price):
        return {"id": symbol + action, "symbol": symbol, "action": action, "price": price, "outcome": None}

    def test_directional_scoring(self):
        entries = [
            self._entry("WIN", "buy", 100.0),     # rises to 120 -> correct
            self._entry("LOSE", "buy", 100.0),    # falls to 90 -> wrong
            self._entry("TRIM", "trim", 100.0),   # falls to 80 -> trim was correct
            self._entry("HOLD", "hold", 100.0),   # not directional -> excluded
        ]
        price_map = {"WIN": 120.0, "LOSE": 90.0, "TRIM": 80.0, "HOLD": 110.0}
        cal = journal.calibrate(entries, price_map)
        scored = {s["symbol"]: s for s in cal["scored"]}
        self.assertTrue(scored["WIN"]["correct"])
        self.assertFalse(scored["LOSE"]["correct"])
        self.assertTrue(scored["TRIM"]["correct"])      # -20% move on a trim
        self.assertIsNone(scored["HOLD"]["correct"])    # holds are context only
        self.assertEqual(cal["n_scored"], 3)
        self.assertEqual(cal["n_correct"], 2)
        self.assertAlmostEqual(cal["hit_rate_pct"], 66.7)

    def test_no_price_means_not_scored(self):
        cal = journal.calibrate([{"id": "a", "symbol": "X", "action": "buy", "price": None, "outcome": None}])
        self.assertEqual(cal["n_scored"], 0)
        self.assertIsNone(cal["hit_rate_pct"])

    def test_recorded_outcome_beats_price_map(self):
        entries = [{"id": "a", "symbol": "X", "action": "buy", "price": 100.0,
                    "outcome": {"price": 150.0}}]
        cal = journal.calibrate(entries, {"X": 90.0})   # live mark says down...
        self.assertAlmostEqual(cal["scored"][0]["move_pct"], 50.0)  # ...but the recorded outcome wins


class HorizonScoring(unittest.TestCase):
    """Fixed-horizon (30/90/365d) outcome accrual from historical closes."""

    NOW = dt.datetime(2026, 6, 1, tzinfo=UTC)

    def _entry(self, created, action="buy", price=100.0, horizons=None):
        e = {"id": "e1", "symbol": "AMD", "action": action, "price": price,
             "created_at": created, "outcome": None}
        if horizons is not None:
            e["horizons"] = horizons
        return e

    def test_due_horizons_only_returns_matured_unscored(self):
        # Created 100 days before NOW: 30 and 90 are due, 365 is not.
        e = self._entry((self.NOW - dt.timedelta(days=100)).isoformat())
        self.assertEqual(journal.due_horizons(e, self.NOW), [30, 90])

    def test_due_horizons_skips_non_directional_and_priceless(self):
        old = (self.NOW - dt.timedelta(days=400)).isoformat()
        self.assertEqual(journal.due_horizons(self._entry(old, action="hold"), self.NOW), [])
        self.assertEqual(journal.due_horizons(self._entry(old, price=None), self.NOW), [])

    def test_already_scored_horizon_is_not_due(self):
        e = self._entry((self.NOW - dt.timedelta(days=100)).isoformat(),
                        horizons={"30": {"days": 30}})
        self.assertEqual(journal.due_horizons(e, self.NOW), [90])

    def test_score_stamps_move_and_correctness(self):
        e = self._entry((self.NOW - dt.timedelta(days=100)).isoformat(), action="buy", price=100.0)
        # Close on/after the 30d target is 120 (+20%, buy correct); 90d is 90 (-10%, wrong).
        series = {
            (self.NOW - dt.timedelta(days=70)).date().isoformat(): 120.0,   # ~30d after entry
            (self.NOW - dt.timedelta(days=10)).date().isoformat(): 90.0,    # ~90d after entry
        }
        n = journal.score_entry_horizons(e, series, now=self.NOW)
        self.assertEqual(n, 2)
        self.assertAlmostEqual(e["horizons"]["30"]["move_pct"], 20.0)
        self.assertTrue(e["horizons"]["30"]["correct"])
        self.assertAlmostEqual(e["horizons"]["90"]["move_pct"], -10.0)
        self.assertFalse(e["horizons"]["90"]["correct"])

    def test_score_is_idempotent_and_skips_uncovered_horizons(self):
        e = self._entry((self.NOW - dt.timedelta(days=100)).isoformat())
        series = {(self.NOW - dt.timedelta(days=70)).date().isoformat(): 110.0}  # only reaches 30d
        self.assertEqual(journal.score_entry_horizons(e, series, now=self.NOW), 1)   # 30d only
        self.assertEqual(journal.score_entry_horizons(e, series, now=self.NOW), 0)   # re-run: no-op
        self.assertNotIn("90", e["horizons"])   # 90d target not covered by the series

    def test_score_outcomes_orchestrates_with_injected_loader(self):
        tmp = Path(tempfile.mkdtemp()) / "journal.json"
        journal.add_entry({"symbol": "AMD", "action": "buy", "thesis": "x", "price": 100.0},
                          path=tmp, now=self.NOW - dt.timedelta(days=100))
        loaded = []

        def loader(sym):
            loaded.append(sym)
            return {(self.NOW - dt.timedelta(days=70)).date().isoformat(): 130.0}  # reaches 30d

        res = journal.score_outcomes(path=tmp, now=self.NOW, load_series=loader)
        self.assertEqual(res["stamped"], 1)
        self.assertEqual(res["entries_touched"], 1)
        self.assertEqual(loaded, ["AMD"])                       # fetched once
        stored = journal.load_entries(tmp)[0]
        self.assertAlmostEqual(stored["horizons"]["30"]["move_pct"], 30.0)

    def test_score_outcomes_noop_when_nothing_due(self):
        tmp = Path(tempfile.mkdtemp()) / "journal.json"
        journal.add_entry({"symbol": "AMD", "action": "buy", "thesis": "x", "price": 100.0},
                          path=tmp, now=self.NOW)   # created "now" -> no horizon matured
        res = journal.score_outcomes(path=tmp, now=self.NOW,
                                     load_series=lambda s: {})
        self.assertEqual(res, {"stamped": 0, "entries_touched": 0, "symbols": 0})


class PriceMap(unittest.TestCase):
    def test_from_mark_then_from_market_value(self):
        holdings = {"positions": [
            {"symbol": "A", "mark_price": 12.5},
            {"symbol": "B", "base_market_value": 200.0, "quantity": 10},
            {"symbol": "C"},  # no price info -> skipped
        ]}
        pm = journal.price_map_from_holdings(holdings)
        self.assertAlmostEqual(pm["A"], 12.5)
        self.assertAlmostEqual(pm["B"], 20.0)
        self.assertNotIn("C", pm)


if __name__ == "__main__":
    unittest.main()
