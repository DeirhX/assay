"""Tests for tools/symbols.py: alias filtering, input->provider resolution,
record annotation, alias persistence, and best-effort candidate validation /
company search. Offline -- the Yahoo provider, the alias source and the alias
store are all mocked, so nothing here touches the network or disk."""

from __future__ import annotations

import unittest
from unittest import mock

import _support  # noqa: F401
import symbols


class Aliases(unittest.TestCase):
    def test_filters_by_length_and_charset(self):
        raw = {
            "AAPL": "AAPL",                    # ok
            "BRK B": "BRK-B",                  # ok: space + dash are allowed
            "WAYTOOLONGSYMBOL1": "X",          # src > 16 chars -> dropped
            "GOOD": "WAYTOOLONGDESTINATION1",  # dst > 16 chars -> dropped
            "bad$": "X",                       # illegal char / lowercase -> dropped
            "OK2": "DST2",                     # ok
        }
        with mock.patch.object(symbols, "symbol_aliases", return_value=raw):
            self.assertEqual(symbols.aliases(),
                             {"AAPL": "AAPL", "BRK B": "BRK-B", "OK2": "DST2"})


class ResolveSymbol(unittest.TestCase):
    def test_uppercases_then_maps_through_aliases(self):
        with mock.patch.object(symbols, "symbol_aliases", return_value={"FB": "META"}), \
             mock.patch.object(symbols, "provider_symbol_for",
                               side_effect=lambda sym, al: al.get(sym, sym)):
            self.assertEqual(symbols.resolve_symbol("fb"), "META")   # aliased
            self.assertEqual(symbols.resolve_symbol("aapl"), "AAPL")  # passthrough


class AnnotateRecord(unittest.TestCase):
    def test_adds_symbol_pair_only_when_input_differs(self):
        with mock.patch.object(symbols.instruments, "classify", return_value="stock"):
            same = symbols.annotate_record({"quote_type": "EQUITY"}, "AAPL", "AAPL")
            self.assertNotIn("input_symbol", same)
            self.assertEqual(same["instrument_type"], "stock")

            diff = symbols.annotate_record({}, "FB", "META")
            self.assertEqual(diff["input_symbol"], "FB")
            self.assertEqual(diff["provider_symbol"], "META")
            self.assertEqual(diff["instrument_type"], "stock")

    def test_copies_caller_dict_when_symbols_differ(self):
        original = {"foo": 1}
        with mock.patch.object(symbols.instruments, "classify", return_value="etf"):
            out = symbols.annotate_record(original, "FB", "META")
        self.assertNotIn("input_symbol", original)  # original left untouched
        self.assertEqual(out["foo"], 1)
        self.assertEqual(out["instrument_type"], "etf")


class SaveAlias(unittest.TestCase):
    def test_adds_alias_and_persists(self):
        captured = {}
        with mock.patch.object(symbols, "symbol_aliases", return_value={}), \
             mock.patch.object(symbols, "write_json",
                               side_effect=lambda path, payload: captured.update(payload=payload)):
            out = symbols.save_alias({"input_symbol": "fb", "provider_symbol": "meta"})
        self.assertEqual(out["aliases"], {"FB": "META"})
        self.assertEqual(out["input_symbol"], "FB")
        self.assertEqual(out["provider_symbol"], "META")
        self.assertEqual(captured["payload"], {"FB": "META"})

    def test_self_alias_removes_existing_entry(self):
        with mock.patch.object(symbols, "symbol_aliases", return_value={"FB": "META"}), \
             mock.patch.object(symbols, "write_json") as write_json:
            out = symbols.save_alias({"input_symbol": "fb", "provider_symbol": "fb"})
        self.assertEqual(out["aliases"], {})            # FB -> FB collapses to nothing
        self.assertEqual(out["provider_symbol"], "FB")  # falls back to src
        write_json.assert_called_once()


class Candidates(unittest.TestCase):
    def test_validates_dedups_skips_self_and_flags_failures(self):
        def fake_chart(sym, rng=None, interval=None):
            if sym == "MSFT":
                return {"meta": {"exchangeName": "NMS", "currency": "USD"}}
            raise RuntimeError("no data for " + sym)

        with mock.patch.object(symbols.yahoo, "chart", side_effect=fake_chart):
            out = symbols.candidates({
                "input_symbol": "aapl",
                "candidates": ["msft", "MSFT", "aapl", "ZZZZ", "!!!"],
            })

        self.assertEqual(out["input_symbol"], "AAPL")
        # MSFT validated once (second is a dup, AAPL is the source itself).
        self.assertEqual([c["symbol"] for c in out["candidates"]], ["MSFT"])
        self.assertEqual(out["candidates"][0]["exchange"], "NMS")
        self.assertEqual(out["candidates"][0]["currency"], "USD")
        invalid = {c["symbol"] for c in out["invalid"]}
        self.assertIn("ZZZZ", invalid)  # provider raised
        self.assertIn("!!!", invalid)   # safe_symbol rejected before any call

    def test_non_list_candidates_is_rejected(self):
        with self.assertRaises(ValueError):
            symbols.candidates({"input_symbol": "AAPL", "candidates": "nope"})


class Search(unittest.TestCase):
    def test_blank_query_short_circuits_without_calling_provider(self):
        with mock.patch.object(symbols.yahoo, "search") as ys:
            self.assertEqual(symbols.search("   "), {"query": "", "results": []})
        ys.assert_not_called()

    def test_success_passes_query_and_limit_through(self):
        with mock.patch.object(symbols.yahoo, "search",
                               return_value=[{"symbol": "NVDA"}]) as ys:
            out = symbols.search("nvidia", limit=3)
        ys.assert_called_once_with("nvidia", limit=3)
        self.assertEqual(out, {"query": "nvidia", "results": [{"symbol": "NVDA"}]})

    def test_provider_failure_degrades_to_empty_with_error(self):
        with mock.patch.object(symbols.yahoo, "search", side_effect=RuntimeError("boom")):
            out = symbols.search("amd")
        self.assertEqual(out["query"], "amd")
        self.assertEqual(out["results"], [])
        self.assertIn("boom", out["error"])


if __name__ == "__main__":
    unittest.main()
