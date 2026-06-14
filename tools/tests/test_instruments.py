"""Tests for instrument classification: Yahoo quoteType wins, then symbol-shape
heuristics (^index, =F futures, =X fx, -USD crypto), then profile shape (sector
=> stock, summary-only => etf), else 'other'. Already-canonical values pass
through unchanged so re-annotating a stored record is idempotent."""

from __future__ import annotations

import unittest

import _support  # noqa: F401
import instruments


class Classify(unittest.TestCase):
    def test_yahoo_quote_type_wins(self):
        self.assertEqual(instruments.classify("NVDA", quote_type="EQUITY"), instruments.STOCK)
        self.assertEqual(instruments.classify("SOXX", quote_type="ETF"), instruments.ETF)
        self.assertEqual(instruments.classify("ES=F", quote_type="FUTURE"), instruments.FUTURES)
        self.assertEqual(instruments.classify("^GSPC", quote_type="INDEX"), instruments.INDEX)
        self.assertEqual(instruments.classify("VFIAX", quote_type="MUTUALFUND"), instruments.FUND)
        self.assertEqual(instruments.classify("BTC-USD", quote_type="CRYPTOCURRENCY"), instruments.CRYPTO)
        self.assertEqual(instruments.classify("EURUSD=X", quote_type="CURRENCY"), instruments.FX)

    def test_quote_type_is_case_insensitive(self):
        self.assertEqual(instruments.classify("NVDA", quote_type="equity"), instruments.STOCK)

    def test_canonical_value_passes_through(self):
        # Re-annotating a record that already stored a canonical type must be a
        # no-op rather than being re-heuristed into something else.
        for kind in instruments.CANONICAL:
            self.assertEqual(instruments.classify("WHATEVER", quote_type=kind), kind)

    def test_symbol_shape_fallback(self):
        self.assertEqual(instruments.classify("^GSPC"), instruments.INDEX)
        self.assertEqual(instruments.classify("CL=F"), instruments.FUTURES)
        self.assertEqual(instruments.classify("EURUSD=X"), instruments.FX)
        self.assertEqual(instruments.classify("ETH-USD"), instruments.CRYPTO)

    def test_unknown_quote_type_falls_through_to_symbol(self):
        self.assertEqual(instruments.classify("CL=F", quote_type="WEIRD"), instruments.FUTURES)

    def test_profile_sector_means_stock(self):
        self.assertEqual(
            instruments.classify("NVDA", profile={"sector": "Technology", "industry": "Semiconductors"}),
            instruments.STOCK,
        )

    def test_profile_summary_only_means_etf(self):
        self.assertEqual(
            instruments.classify("SOXX", profile={"summary": "The fund invests in..."}),
            instruments.ETF,
        )

    def test_no_signal_is_other(self):
        self.assertEqual(instruments.classify("ZZZ"), instruments.OTHER)
        self.assertEqual(instruments.classify("ZZZ", profile={}), instruments.OTHER)
        self.assertEqual(instruments.classify("ZZZ", profile=None), instruments.OTHER)

    def test_label(self):
        self.assertEqual(instruments.label(instruments.ETF), "ETF")
        self.assertEqual(instruments.label("stock"), "Stock")
        self.assertEqual(instruments.label("garbage"), "Other")


if __name__ == "__main__":
    unittest.main()
