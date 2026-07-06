"""Tests for analysis_report: the pure parsers that turn a backend's free-text
report back into structured data -- the price-level ladder and the token/cache
usage normalizer."""

from __future__ import annotations

import unittest

import _support  # noqa: F401
import analysis_report as ar


class Usage(unittest.TestCase):
    def test_keeps_known_counters_as_ints(self):
        u = ar._norm_usage({"input_tokens": 10, "output_tokens": 5.0,
                            "cache_read_input_tokens": 3, "junk": "x"})
        self.assertEqual(u, {"input_tokens": 10, "output_tokens": 5,
                             "cache_read_input_tokens": 3})

    def test_non_dict_is_empty(self):
        self.assertEqual(ar._norm_usage(None), {})


class ParsePriceLevels(unittest.TestCase):
    # parse_price_levels now returns the canonical ladder shape (fair_value +
    # buy_ladder/trim_ladder) with the legacy buy_below/trim_above mirrored as
    # the outermost tranche of each side. The legacy two-liner still parses via
    # the fallback path into single-tranche ladders.
    def test_strict_bullets_fallback(self):
        report = ("## Price levels\n- Buy below: $92\n- Trim above: $145\n")
        out = ar.parse_price_levels(report, "USD")
        self.assertEqual(out["buy_below"], 92.0)
        self.assertEqual(out["trim_above"], 145.0)
        self.assertEqual(out["currency"], "USD")
        self.assertIsNone(out["fair_value"])
        self.assertEqual(len(out["buy_ladder"]), 1)
        self.assertEqual(out["buy_ladder"][0]["price"], 92.0)
        self.assertEqual(out["buy_ladder"][0]["size_pct"], 1.0)
        self.assertEqual(out["trim_ladder"][0]["price"], 145.0)

    def test_none_and_na(self):
        report = "- Buy below: none\n- Trim above: N/A\n"
        out = ar.parse_price_levels(report, "usd")
        self.assertIsNone(out["buy_below"])
        self.assertIsNone(out["trim_above"])
        self.assertEqual(out["buy_ladder"], [])
        self.assertEqual(out["trim_ladder"], [])
        self.assertEqual(out["currency"], "USD")

    def test_thousands_and_decimals(self):
        report = "Buy below: $1,234.50\nTrim above: 2,000\n"
        out = ar.parse_price_levels(report)
        self.assertEqual(out["buy_below"], 1234.50)
        self.assertEqual(out["trim_above"], 2000.0)

    def test_trailing_parenthetical_and_em_dash(self):
        report = "- Buy below — $80 (about 12% under spot)\n- Trim above: none\n"
        out = ar.parse_price_levels(report)
        self.assertEqual(out["buy_below"], 80.0)
        self.assertIsNone(out["trim_above"])

    def test_none_word_before_a_later_number_still_reads_none(self):
        report = "- Buy below: none, though $50 was the old floor\n"
        self.assertIsNone(ar.parse_price_levels(report)["buy_below"])

    def test_missing_section_is_empty_ladders(self):
        out = ar.parse_price_levels("## Verdict\nHold.\n")
        self.assertIsNone(out["buy_below"])
        self.assertIsNone(out["trim_above"])
        self.assertEqual(out["buy_ladder"], [])
        self.assertEqual(out["trim_ladder"], [])
        self.assertEqual(out["currency"], "")

    def test_json_block_with_margins_derives_prices(self):
        report = (
            "## Price levels\n"
            "Here is the ladder.\n"
            '```json\n'
            '{"fair_value": 400, "buy_ladder": [{"discount_pct": 0.10, "size_pct": 0.5},'
            ' {"discount_pct": 0.20, "size_pct": 0.5}], "trim_ladder": [{"premium_pct": 0.25, "size_pct": 1.0}]}\n'
            '```\n'
            "## Sources\nNone.\n"
        )
        out = ar.parse_price_levels(report, "USD")
        self.assertEqual(out["fair_value"], 400.0)
        # Buy ladder sorted by price descending (shallowest discount first).
        self.assertEqual([t["price"] for t in out["buy_ladder"]], [360.0, 320.0])
        self.assertEqual(out["buy_below"], 360.0)
        self.assertEqual(out["trim_ladder"][0]["price"], 500.0)
        self.assertEqual(out["trim_above"], 500.0)
        self.assertAlmostEqual(sum(t["size_pct"] for t in out["buy_ladder"]), 1.0)

    def test_json_block_not_confused_by_data_payload(self):
        # The DATA snapshot at the bottom also has a ```json block; the parser
        # must read the Price levels ladder, not the data payload.
        report = (
            "## Price levels\n"
            '```json\n{"fair_value": 50, "buy_ladder": [{"price": 40, "size_pct": 1.0}]}\n```\n'
            "\nDATA\n```json\n{\"symbol\": \"X\", \"price\": {\"value\": 99}}\n```\n"
        )
        out = ar.parse_price_levels(report, "USD")
        self.assertEqual(out["fair_value"], 50.0)
        self.assertEqual(out["buy_below"], 40.0)
        self.assertEqual(out["trim_ladder"], [])


if __name__ == "__main__":
    unittest.main()
