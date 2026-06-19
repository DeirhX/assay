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
        # A single legacy level becomes a single full-size buy tranche.
        self.assertEqual(len(entry["buy_ladder"]), 1)
        self.assertEqual(entry["buy_ladder"][0]["size_pct"], 1.0)
        self.assertEqual(entry["trim_ladder"], [])

    def test_lock_ladder_with_fair_value_derives_prices_and_sorts(self):
        entry = price_levels.lock(
            "AVGO", fair_value=400,
            buy_ladder=[{"discount_pct": 0.20, "size_pct": 0.5},
                        {"discount_pct": 0.10, "size_pct": 0.5}],
            trim_ladder=[{"premium_pct": 0.25, "size_pct": 1.0}],
            currency="usd")
        self.assertEqual(entry["fair_value"], 400.0)
        # Buy ladder sorted by price descending (shallowest discount first).
        self.assertEqual([t["price"] for t in entry["buy_ladder"]], [360.0, 320.0])
        # Mirrored outermost levels for back-compat consumers.
        self.assertEqual(entry["buy_below"], 360.0)
        self.assertEqual(entry["trim_above"], 500.0)

    def test_lock_normalizes_sizes_to_one(self):
        entry = price_levels.lock(
            "AMD", buy_ladder=[{"price": 90, "size_pct": 2}, {"price": 80, "size_pct": 2}])
        self.assertAlmostEqual(sum(t["size_pct"] for t in entry["buy_ladder"]), 1.0)
        self.assertAlmostEqual(entry["buy_ladder"][0]["size_pct"], 0.5)

    def test_lock_fills_missing_sizes_equally(self):
        entry = price_levels.lock(
            "AMD", buy_ladder=[{"price": 90}, {"price": 80}, {"price": 70}])
        sizes = [t["size_pct"] for t in entry["buy_ladder"]]
        self.assertAlmostEqual(sum(sizes), 1.0)
        for s in sizes:
            self.assertAlmostEqual(s, 1 / 3)

    def test_lock_rejects_buy_above_trim_across_ladders(self):
        with self.assertRaises(ValueError):
            price_levels.lock("AMD", buy_ladder=[{"price": 150}],
                              trim_ladder=[{"price": 100}])

    def test_lock_rejects_buy_above_fair_value(self):
        with self.assertRaises(ValueError):
            price_levels.lock("AMD", fair_value=100, buy_ladder=[{"price": 120}])

    def test_lock_rejects_trim_below_fair_value(self):
        with self.assertRaises(ValueError):
            price_levels.lock("AMD", fair_value=100, trim_ladder=[{"price": 80}])


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

    def test_legacy_single_level_is_binary_fraction(self):
        lvl = {"buy_below": 92.0, "trim_above": None, "currency": "USD"}
        self.assertEqual(price_levels.evaluate(lvl, 100.0)["buy_fraction"], 0.0)
        self.assertEqual(price_levels.evaluate(lvl, 90.0)["buy_fraction"], 1.0)

    def test_graded_buy_fraction_accumulates_as_price_falls(self):
        lvl = {
            "fair_value": 400.0,
            "buy_ladder": [
                {"price": 360.0, "size_pct": 0.5},
                {"price": 320.0, "size_pct": 0.3},
                {"price": 280.0, "size_pct": 0.2},
            ],
        }
        # Above all tranches: nothing live, buy blocked.
        hi = price_levels.evaluate(lvl, 380.0)
        self.assertTrue(hi["blocks_buy"])
        self.assertEqual(hi["buy_fraction"], 0.0)
        self.assertEqual(hi["buy_live"], 0)
        self.assertEqual(hi["buy_total"], 3)
        self.assertEqual(hi["next_buy"]["price"], 360.0)
        self.assertAlmostEqual(hi["next_buy"]["distance"], 20.0)
        # Between first and second tranche: first tranche live (50%).
        mid = price_levels.evaluate(lvl, 340.0)
        self.assertFalse(mid["blocks_buy"])
        self.assertAlmostEqual(mid["buy_fraction"], 0.5)
        self.assertEqual(mid["buy_live"], 1)
        self.assertEqual(mid["next_buy"]["price"], 320.0)
        # Below the second tranche: two live (80%).
        low = price_levels.evaluate(lvl, 300.0)
        self.assertAlmostEqual(low["buy_fraction"], 0.8)
        self.assertEqual(low["buy_live"], 2)
        # Below everything: fully unlocked.
        deep = price_levels.evaluate(lvl, 100.0)
        self.assertAlmostEqual(deep["buy_fraction"], 1.0)
        self.assertEqual(deep["buy_live"], 3)
        self.assertIsNone(deep["next_buy"])

    def test_graded_trim_fraction_accumulates_as_price_rises(self):
        lvl = {
            "fair_value": 400.0,
            "trim_ladder": [
                {"price": 500.0, "size_pct": 0.6},
                {"price": 600.0, "size_pct": 0.4},
            ],
        }
        self.assertTrue(price_levels.evaluate(lvl, 450.0)["blocks_trim"])
        self.assertAlmostEqual(price_levels.evaluate(lvl, 550.0)["trim_fraction"], 0.6)
        self.assertAlmostEqual(price_levels.evaluate(lvl, 650.0)["trim_fraction"], 1.0)


class Normalize(unittest.TestCase):
    def test_legacy_record_upgrades_to_ladders(self):
        norm = price_levels._normalize({"buy_below": 92.0, "trim_above": 145.0, "currency": "USD"})
        self.assertEqual(len(norm["buy_ladder"]), 1)
        self.assertEqual(norm["buy_ladder"][0]["price"], 92.0)
        self.assertEqual(norm["buy_ladder"][0]["size_pct"], 1.0)
        self.assertEqual(norm["buy_below"], 92.0)
        self.assertEqual(norm["trim_above"], 145.0)

    def test_normalize_is_idempotent(self):
        once = price_levels._normalize({"fair_value": 400, "buy_ladder": [{"discount_pct": 0.1, "size_pct": 1.0}]})
        twice = price_levels._normalize(once)
        self.assertEqual(once["buy_ladder"], twice["buy_ladder"])
        self.assertEqual(once["buy_below"], twice["buy_below"])


class LimitPriceFor(unittest.TestCase):
    def test_sides(self):
        lvl = {"buy_below": 92.0, "trim_above": 145.0}
        self.assertEqual(price_levels.limit_price_for(lvl, "BUY"), 92.0)
        self.assertEqual(price_levels.limit_price_for(lvl, "SELL"), 145.0)
        self.assertIsNone(price_levels.limit_price_for(None, "BUY"))
        self.assertIsNone(price_levels.limit_price_for({"buy_below": None}, "BUY"))

    def test_ladder_uses_outermost_tranche(self):
        lvl = {
            "fair_value": 400.0,
            "buy_ladder": [{"price": 360.0, "size_pct": 0.5}, {"price": 320.0, "size_pct": 0.5}],
            "trim_ladder": [{"price": 500.0, "size_pct": 0.5}, {"price": 600.0, "size_pct": 0.5}],
        }
        # Buy limit = highest buy tranche; sell limit = lowest trim tranche.
        self.assertEqual(price_levels.limit_price_for(lvl, "BUY"), 360.0)
        self.assertEqual(price_levels.limit_price_for(lvl, "SELL"), 500.0)


if __name__ == "__main__":
    unittest.main()
