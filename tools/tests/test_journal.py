"""Tests for the decision journal: entry validation, append-only persistence,
outcome recording, and the calibration that scores directional calls."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import _support  # noqa: F401
import journal


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
