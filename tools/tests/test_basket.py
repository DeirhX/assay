"""Tests for the cross-surface basket store: add/remove/clear, symbol
normalization + junk rejection, idempotent re-add, and the read-side enrichment
that joins a pick with current holdings weight and the standing target band.
Offline: every disk path is redirected to a temp dir.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import _support  # noqa: F401
import basket
from store import write_json as _write_json


class _BasketCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.basket_json = root / "basket.json"
        self.holdings = root / "current-holdings.json"
        self.model = root / "target-model.json"
        self._orig = {
            "BASKET_JSON": basket.BASKET_JSON,
            "HOLDINGS_JSON": basket.HOLDINGS_JSON,
            "TARGET_MODEL_JSON": basket.TARGET_MODEL_JSON,
        }
        basket.BASKET_JSON = self.basket_json
        basket.HOLDINGS_JSON = self.holdings
        basket.TARGET_MODEL_JSON = self.model
        # portfolio.holdings_weights reads its own HOLDINGS_JSON default only when
        # passed None; basket always passes the loaded dict, so redirecting
        # basket.HOLDINGS_JSON is enough.

    def tearDown(self):
        for name, val in self._orig.items():
            setattr(basket, name, val)
        self.tmp.cleanup()


class AddRemove(_BasketCase):
    def test_add_normalizes_and_dedupes(self):
        basket.add_symbol("nvda", source="deepdive")
        view = basket.add_symbol("NVDA", source="rebalance", note="watch")  # dup
        self.assertEqual(view["count"], 1)
        item = view["items"][0]
        self.assertEqual(item["symbol"], "NVDA")
        # First source/added_at win; a non-empty note on re-add is folded in.
        self.assertEqual(item["source"], "deepdive")
        self.assertEqual(item["note"], "watch")

    def test_unknown_source_coerced_to_manual(self):
        view = basket.add_symbol("AMD", source="bogus")
        self.assertEqual(view["items"][0]["source"], "manual")

    def test_junk_symbol_rejected(self):
        with self.assertRaises(ValueError):
            basket.add_symbol("$5")
        with self.assertRaises(ValueError):
            basket.add_symbol("")

    def test_remove_is_idempotent(self):
        basket.add_symbol("INTC")
        self.assertEqual(basket.remove_symbol("INTC")["count"], 0)
        self.assertEqual(basket.remove_symbol("INTC")["count"], 0)  # no-op, no raise

    def test_clear(self):
        basket.add_symbol("AMD")
        basket.add_symbol("AVGO")
        self.assertEqual(basket.clear()["count"], 0)

    def test_symbols_list_for_client_toggle(self):
        basket.add_symbol("AMD")
        basket.add_symbol("NVDA")
        self.assertEqual(set(basket.view()["symbols"]), {"AMD", "NVDA"})


class Enrichment(_BasketCase):
    def _seed(self):
        _write_json(self.holdings, {
            "positions": [
                {"symbol": "NVDA", "base_market_value": 4000.0},
                {"symbol": "AMD", "base_market_value": 1000.0},
                {"symbol": "UNH", "base_market_value": 5000.0},
            ],
        })
        _write_json(self.model, {
            "targets": {"NVDA": {"low": 3, "high": 4, "rule": "accumulate", "note": "x"}},
            "sleeves": {"semis-equip": {"low": 5, "high": 7, "members": ["AMD"]}},
        })

    def test_join_held_weight_and_target_band(self):
        self._seed()
        basket.add_symbol("NVDA", source="deepdive")
        item = next(i for i in basket.enriched_items() if i["symbol"] == "NVDA")
        self.assertTrue(item["held_pct"] and item["held_pct"] > 0)
        self.assertTrue(item["targeted"])
        # Only band keys survive; the free-form note is not leaked as a band field.
        self.assertEqual(item["target_band"], {"low": 3, "high": 4, "rule": "accumulate"})
        self.assertIsNone(item["in_sleeve"])

    def test_sleeve_membership_counts_as_targeted(self):
        self._seed()
        basket.add_symbol("AMD")
        item = next(i for i in basket.enriched_items() if i["symbol"] == "AMD")
        self.assertTrue(item["targeted"])
        self.assertEqual(item["in_sleeve"], "semis-equip")
        self.assertIsNone(item["target_band"])

    def test_untargeted_unheld_pick(self):
        self._seed()
        basket.add_symbol("SOFI")
        item = next(i for i in basket.enriched_items() if i["symbol"] == "SOFI")
        self.assertIsNone(item["held_pct"])
        self.assertFalse(item["targeted"])


if __name__ == "__main__":
    unittest.main()
