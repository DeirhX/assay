#!/usr/bin/env python3
"""Tests for the fresh-quote cache leaf: freshness windowing and load/save."""
import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import quote_cache

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 1, 5, 16, 0, tzinfo=UTC)


def _at(hours_ago):
    return (NOW - dt.timedelta(hours=hours_ago)).isoformat()


class FreshPrice(unittest.TestCase):
    def test_fresh_quote_is_returned(self):
        quotes = {"NVDA": {"price": 180.0, "currency": "USD", "at": _at(1)}}
        got = quote_cache.fresh_price("nvda", quotes=quotes, now=NOW)
        self.assertIsNotNone(got)
        self.assertEqual(got["price"], 180.0)
        self.assertEqual(got["currency"], "USD")

    def test_stale_quote_is_none(self):
        quotes = {"NVDA": {"price": 180.0, "at": _at(10)}}
        self.assertIsNone(quote_cache.fresh_price("NVDA", quotes=quotes, now=NOW))

    def test_edge_of_window_is_still_fresh(self):
        quotes = {"NVDA": {"price": 180.0, "at": _at(4)}}   # exactly 4h
        self.assertIsNotNone(quote_cache.fresh_price("NVDA", quotes=quotes, now=NOW))

    def test_missing_symbol_is_none(self):
        self.assertIsNone(quote_cache.fresh_price("AMD", quotes={}, now=NOW))

    def test_negative_or_absent_price_is_none(self):
        quotes = {"AMD": {"price": None, "error": "no data", "at": _at(1)}}
        self.assertIsNone(quote_cache.fresh_price("AMD", quotes=quotes, now=NOW))

    def test_unparsable_timestamp_is_none(self):
        quotes = {"AMD": {"price": 10.0, "at": "not-a-date"}}
        self.assertIsNone(quote_cache.fresh_price("AMD", quotes=quotes, now=NOW))


class LoadSave(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(
            quote_cache, "QUOTES_JSON", Path(self._tmp.name) / "quotes.json")
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_missing_file_loads_empty(self):
        self.assertEqual(quote_cache.load(), {})

    def test_round_trip(self):
        quote_cache.save({"NVDA": {"price": 180.0, "currency": "USD", "at": _at(0)}})
        self.assertEqual(quote_cache.load()["NVDA"]["price"], 180.0)

    def test_corrupt_file_loads_empty(self):
        quote_cache.QUOTES_JSON.write_text("{bad", encoding="utf-8")
        self.assertEqual(quote_cache.load(), {})


if __name__ == "__main__":
    unittest.main()
