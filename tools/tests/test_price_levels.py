"""Tests for the locked price-trigger store and the pure gate evaluation
(price_levels.py): round-trip lock/get/clear against a temp store, input
validation, and the buy_below/trim_above blocking math."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import _support  # noqa: F401  -- puts tools/ on sys.path
import price_levels


class StoreRoundTrip(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._orig = price_levels.LEVELS_JSON
        price_levels.LEVELS_JSON = Path(self._tmp.name) / "price-levels.json"
        self.addCleanup(lambda: setattr(price_levels, "LEVELS_JSON", self._orig))

    def test_lock_get_clear(self):
        self.assertEqual(price_levels.load_all(), {})
        self.assertIsNone(price_levels.get("AMD"))

        entry = price_levels.lock("amd", buy_below="92", trim_above=145, currency="usd",
                                  source={"kind": "ticker_analysis", "stem": "amd-2026"})
        self.assertEqual(entry["symbol"], "AMD")
        self.assertEqual(entry["buy_below"], 92.0)
        self.assertEqual(entry["trim_above"], 145.0)
        self.assertEqual(entry["currency"], "USD")
        self.assertEqual(entry["status"], "active")
        self.assertIn("locked_at", entry)
        self.assertEqual(entry["source"]["stem"], "amd-2026")

        self.assertEqual(price_levels.get("AMD"), entry)
        self.assertEqual(set(price_levels.load_all()), {"AMD"})

        cleared = price_levels.clear("amd")
        self.assertTrue(cleared["cleared"])
        self.assertIsNone(price_levels.get("AMD"))
        # Clearing an absent level is idempotent, not an error.
        self.assertFalse(price_levels.clear("AMD")["cleared"])

    def test_lock_requires_a_level(self):
        with self.assertRaises(ValueError):
            price_levels.lock("AMD")

    def test_lock_rejects_inverted_band(self):
        with self.assertRaises(ValueError):
            price_levels.lock("AMD", buy_below=150, trim_above=100)

    def test_lock_rejects_nonpositive_price(self):
        with self.assertRaises(ValueError):
            price_levels.lock("AMD", buy_below=0)
        with self.assertRaises(ValueError):
            price_levels.lock("AMD", trim_above=-5)

    def test_lock_only_one_side(self):
        entry = price_levels.lock("NVDA", buy_below=100)
        self.assertEqual(entry["buy_below"], 100.0)
        self.assertIsNone(entry["trim_above"])


class Evaluate(unittest.TestCase):
    def test_none_level_is_none(self):
        self.assertIsNone(price_levels.evaluate(None, 100))

    def test_buy_below_blocks_when_too_dear(self):
        lvl = {"buy_below": 92.0, "trim_above": None, "currency": "USD"}
        # Price above buy_below -> not yet favorable, blocks the buy.
        res = price_levels.evaluate(lvl, 100.0)
        self.assertTrue(res["blocks_buy"])
        self.assertFalse(res["blocks_trim"])
        # Price at/below the level clears the buy gate.
        self.assertFalse(price_levels.evaluate(lvl, 92.0)["blocks_buy"])
        self.assertFalse(price_levels.evaluate(lvl, 80.0)["blocks_buy"])

    def test_trim_above_blocks_when_too_cheap(self):
        lvl = {"buy_below": None, "trim_above": 145.0, "currency": "USD"}
        res = price_levels.evaluate(lvl, 100.0)
        self.assertTrue(res["blocks_trim"])
        self.assertFalse(res["blocks_buy"])
        self.assertFalse(price_levels.evaluate(lvl, 145.0)["blocks_trim"])
        self.assertFalse(price_levels.evaluate(lvl, 200.0)["blocks_trim"])

    def test_unknown_price_never_blocks_but_flags(self):
        lvl = {"buy_below": 92.0, "trim_above": 145.0, "currency": "USD"}
        res = price_levels.evaluate(lvl, None)
        self.assertFalse(res["blocks_buy"])
        self.assertFalse(res["blocks_trim"])
        self.assertFalse(res["price_known"])
        self.assertIsNone(res["current"])


class LimitPriceFor(unittest.TestCase):
    def test_sides(self):
        lvl = {"buy_below": 92.0, "trim_above": 145.0}
        self.assertEqual(price_levels.limit_price_for(lvl, "BUY"), 92.0)
        self.assertEqual(price_levels.limit_price_for(lvl, "SELL"), 145.0)
        self.assertIsNone(price_levels.limit_price_for(None, "BUY"))
        self.assertIsNone(price_levels.limit_price_for({"buy_below": None}, "BUY"))


if __name__ == "__main__":
    unittest.main()
