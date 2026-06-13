"""Tests for substring / company-name ticker search.

Covers the Yahoo provider parsing (yahoo.search, HTTP mocked) and serve's
_symbol_search envelope (provider mocked). No network is touched."""

from __future__ import annotations

import unittest
from unittest import mock

import _support  # noqa: F401  (sys.path side effect)
from providers import yahoo


def _quote(symbol, *, quoteType="EQUITY", shortname=None, longname=None,
           exchDisp="NASDAQ", typeDisp="Equity"):
    return {
        "symbol": symbol,
        "quoteType": quoteType,
        "shortname": shortname,
        "longname": longname,
        "exchDisp": exchDisp,
        "typeDisp": typeDisp,
    }


class YahooSearch(unittest.TestCase):
    def _run(self, payload):
        with mock.patch.object(yahoo, "get_json", return_value=payload) as gj:
            out = yahoo.search("appl", limit=5)
        return out, gj

    def test_blank_query_skips_network(self):
        with mock.patch.object(yahoo, "get_json",
                               side_effect=AssertionError("must not call out")):
            self.assertEqual(yahoo.search("   "), [])

    def test_parses_and_normalizes_quotes(self):
        payload = {"quotes": [
            _quote("AAPL", longname="Apple Inc.", shortname="Apple"),
            _quote("AMAT", shortname="Applied Materials, Inc."),
        ]}
        out, _ = self._run(payload)
        self.assertEqual(out[0], {
            "symbol": "AAPL", "name": "Apple Inc.",
            "exchange": "NASDAQ", "type": "Equity",
        })
        # shortname is used when longname is missing
        self.assertEqual(out[1]["name"], "Applied Materials, Inc.")

    def test_filters_non_equity_types_and_dedupes(self):
        payload = {"quotes": [
            _quote("AAPL", longname="Apple Inc."),
            _quote("BTC-USD", quoteType="CRYPTOCURRENCY", shortname="Bitcoin"),
            _quote("^GSPC", quoteType="INDEX", shortname="S&P 500"),
            _quote("SPY", quoteType="ETF", longname="SPDR S&P 500 ETF"),
            _quote("AAPL", longname="dupe"),  # duplicate symbol -> dropped
        ]}
        out, _ = self._run(payload)
        syms = [r["symbol"] for r in out]
        self.assertEqual(syms, ["AAPL", "SPY"])  # crypto + index filtered, dupe gone

    def test_respects_limit(self):
        payload = {"quotes": [_quote(f"SYM{i}") for i in range(20)]}
        with mock.patch.object(yahoo, "get_json", return_value=payload):
            out = yahoo.search("s", limit=3)
        self.assertEqual(len(out), 3)


class ServeSymbolSearch(unittest.TestCase):
    """serve._symbol_search composes the /api/symbol-search envelope."""

    @classmethod
    def setUpClass(cls):
        import serve
        cls.serve = serve

    def test_blank_query_returns_empty_envelope(self):
        out = self.serve._symbol_search("  ")
        self.assertEqual(out, {"query": "", "results": []})

    def test_results_passed_through(self):
        fake = [{"symbol": "AAPL", "name": "Apple Inc.", "exchange": "NASDAQ", "type": "Equity"}]
        with mock.patch.object(self.serve.yahoo, "search", return_value=fake):
            out = self.serve._symbol_search("appl")
        self.assertEqual(out["query"], "appl")
        self.assertEqual(out["results"], fake)

    def test_provider_failure_degrades_to_empty(self):
        with mock.patch.object(self.serve.yahoo, "search",
                               side_effect=RuntimeError("yahoo down")):
            out = self.serve._symbol_search("appl")
        self.assertEqual(out["results"], [])
        self.assertIn("error", out)


if __name__ == "__main__":
    unittest.main()
