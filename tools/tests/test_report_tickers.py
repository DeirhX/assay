"""Tests for the server-side report ticker harvest (the Python mirror of the
frontend linkify harvest) and the discovered-candidate extraction it feeds."""

from __future__ import annotations

import unittest

import _support  # noqa: F401
import report_tickers as rt


class Harvest(unittest.TestCase):
    def test_dollar_and_exchange_and_paren(self):
        text = "We hold $NVDA and like (NASDAQ: AMD). Cameco (CCJ) is interesting."
        syms = rt.harvest_symbols(text)
        self.assertIn("NVDA", syms)
        self.assertIn("AMD", syms)
        self.assertIn("CCJ", syms)

    def test_stoplist_blocks_bare_parenthetical(self):
        # "(AI)" is a stoplisted acronym, not a ticker; "$AI" would override but
        # a bare parenthetical respects the stoplist.
        self.assertNotIn("AI", rt.harvest_symbols("the (AI) wave"))

    def test_dollar_overrides_stoplist(self):
        self.assertIn("NOW", rt.harvest_symbols("ServiceNow $NOW is a buy"))

    def test_bare_dollar_amount_is_not_a_ticker(self):
        self.assertEqual(rt.harvest_symbols("it fell to $5 and $1000"), set())

    def test_foreign_numeric_needs_suffix(self):
        self.assertIn("000660.KS", rt.harvest_symbols("SK Hynix $000660.KS"))
        self.assertNotIn("000660", rt.harvest_symbols("(KRX: 000660) listed"))


class Discovered(unittest.TestCase):
    def test_excludes_members_and_keeps_new_names(self):
        text = "We hold $NVDA. A peer (NASDAQ: AMD) and newcomer $ASML look strong."
        cands = rt.discovered_candidates(text, exclude={"NVDA"})
        syms = {c["symbol"] for c in cands}
        self.assertNotIn("NVDA", syms)       # excluded as a member
        self.assertEqual(syms, {"AMD", "ASML"})

    def test_action_inferred_from_nearby_verbs(self):
        text = "We should trim $AMD aggressively given the reduce thesis."
        cands = rt.discovered_candidates(text)
        amd = next(c for c in cands if c["symbol"] == "AMD")
        self.assertEqual(amd["action"], "trim")

    def test_context_snippet_present(self):
        cands = rt.discovered_candidates("Consider adding $ASML to the book.")
        self.assertTrue(cands[0]["context"])


if __name__ == "__main__":
    unittest.main()
