#!/usr/bin/env python3
"""Tests for the fresh-quote cache leaf: freshness windowing and load/save."""
import datetime as dt
import json
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


class Downsample(unittest.TestCase):
    def test_short_series_is_returned_verbatim(self):
        self.assertEqual(quote_cache._downsample([1.0, 2.0, 3.0], 30), [1.0, 2.0, 3.0])

    def test_long_series_is_capped_with_endpoints_kept(self):
        vals = [float(i) for i in range(100)]
        out = quote_cache._downsample(vals, 30)
        self.assertLessEqual(len(out), 30)
        self.assertEqual(out[0], 0.0)      # first kept
        self.assertEqual(out[-1], 99.0)    # last kept


class SparkSeries(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.rd = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, symbol, closes, currency="USD"):
        points = [{"date": f"2026-01-{i + 1:02d}", "close": c} for i, c in enumerate(closes)]
        (self.rd / f"{symbol}.json").write_text(
            json.dumps({"price_history": {"currency": currency, "points": points}}),
            encoding="utf-8")

    def test_series_downsamples_and_computes_window_change(self):
        self._write("NVDA", [float(i) for i in range(100, 200)])  # 100 -> 199
        out = quote_cache.spark_series(["NVDA"], research_dir=self.rd, tail_days=90, max_points=30)
        self.assertIn("NVDA", out)
        self.assertLessEqual(len(out["NVDA"]["points"]), 30)
        # window is the last 90 closes: 110..199, so change = (199-110)/110.
        self.assertAlmostEqual(out["NVDA"]["change"], (199 - 110) / 110, places=4)
        self.assertEqual(out["NVDA"]["currency"], "USD")

    def test_missing_dossier_is_omitted(self):
        self.assertEqual(quote_cache.spark_series(["GHOST"], research_dir=self.rd), {})

    def test_single_point_cannot_draw_a_line_and_is_omitted(self):
        self._write("ONE", [42.0])
        self.assertEqual(quote_cache.spark_series(["ONE"], research_dir=self.rd), {})

    def test_symbols_are_deduped_and_uppercased(self):
        self._write("AMD", [10.0, 11.0, 12.0])
        out = quote_cache.spark_series(["amd", "AMD", " amd "], research_dir=self.rd)
        self.assertEqual(list(out.keys()), ["AMD"])

    def test_non_numeric_closes_are_skipped(self):
        self._write("MIX", [10.0, "n/a", 12.0, None, 14.0])
        out = quote_cache.spark_series(["MIX"], research_dir=self.rd)
        self.assertEqual(out["MIX"]["points"], [10.0, 12.0, 14.0])


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
