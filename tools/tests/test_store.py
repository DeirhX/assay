"""Tests for the identifier validators that guard every file path the server
builds from user input (segment slugs, ticker symbols)."""

from __future__ import annotations

import unittest

import _support  # noqa: F401
import store


class Slugify(unittest.TestCase):
    def test_normalizes_to_kebab(self):
        self.assertEqual(store.slugify("Fintech & Payments!"), "fintech-payments")
        self.assertEqual(store.slugify("  AI  Compute  "), "ai-compute")

    def test_rejects_empty_and_overlong(self):
        with self.assertRaises(ValueError):
            store.slugify("   ")
        with self.assertRaises(ValueError):
            store.slugify("x" * 65)


class SafeSymbol(unittest.TestCase):
    def test_uppercases_and_accepts_valid(self):
        self.assertEqual(store.safe_symbol("amd"), "AMD")
        self.assertEqual(store.safe_symbol("brk.b"), "BRK.B")
        self.assertEqual(store.safe_symbol("eur=x"), "EUR=X")
        # Broker feeds (IBKR) use spaces for class shares, e.g. "BRK B" before
        # alias resolution to "BRK-B"; safe_symbol must accept them.
        self.assertEqual(store.safe_symbol("brk b"), "BRK B")

    def test_rejects_garbage(self):
        # Path separators, traversal, control punctuation, and over-long input
        # stay rejected (spaces are intentionally allowed -- see above).
        for bad in ["", "a/b", "a\\b", "a:b", "../etc", "x" * 17, "a;b"]:
            with self.assertRaises(ValueError):
                store.safe_symbol(bad)


if __name__ == "__main__":
    unittest.main()
