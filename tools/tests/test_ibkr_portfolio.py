"""Tests for the vendored IBKR Flex reader's parsing layer: Flex XML ->
Portfolio, the lot-only fallback aggregation, timestamp conversion, and the
secrets.env parser. Pure parsing -- no network, no credentials."""

from __future__ import annotations

import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import _support  # noqa: F401
import ibkr_portfolio as ib

# A trimmed Flex XML in the real shape: summary + lot rows, cash report, and an
# equity summary. Embedded as a string on purpose -- .gitignore blocks *.xml.
# The account id uses IBKR's paper-account format (DU...) so the pre-commit
# personal-data scanner's live-account pattern (U + 7 digits) never matches.
FLEX_XML = """<FlexQueryResponse queryName="portfolio" type="AF">
  <FlexStatements count="1">
    <FlexStatement accountId="DU000001" fromDate="20260530" toDate="20260530" whenGenerated="20260530;120000">
      <EquitySummaryInBase>
        <EquitySummaryByReportDateInBase reportDate="20260529" total="900000" />
        <EquitySummaryByReportDateInBase reportDate="20260530" total="1000000" />
      </EquitySummaryInBase>
      <CashReport>
        <CashReportCurrency currency="BASE_SUMMARY" endingCash="50000.5" />
        <CashReportCurrency currency="USD" endingCash="2000" />
      </CashReport>
      <OpenPositions>
        <OpenPosition levelOfDetail="SUMMARY" symbol="AMD" assetCategory="STK"
          description="ADVANCED MICRO DEVICES" currency="USD" fxRateToBase="23.0"
          position="100" costBasisPrice="90" markPrice="120" positionValue="12000"
          fifoPnlUnrealized="3000" percentOfNAV="27.6" listingExchange="NASDAQ" />
        <OpenPosition levelOfDetail="LOT" symbol="AMD" assetCategory="STK"
          description="ADVANCED MICRO DEVICES" currency="USD" fxRateToBase="23.0"
          position="100" costBasisPrice="90" costBasisMoney="9000" markPrice="120"
          positionValue="12000" fifoPnlUnrealized="3000"
          openDateTime="20240102;093005" holdingPeriodDateTime="20240102;093005"
          code="LT" originatingOrderID="11" originatingTransactionID="22" />
      </OpenPositions>
    </FlexStatement>
  </FlexStatements>
</FlexQueryResponse>"""

LOTS_ONLY_XML = """<FlexQueryResponse queryName="portfolio" type="AF">
  <FlexStatements count="1">
    <FlexStatement accountId="DU000001" fromDate="20260530" toDate="20260530" whenGenerated="20260530;120000">
      <EquitySummaryInBase>
        <EquitySummaryByReportDateInBase reportDate="20260530" total="1000" />
      </EquitySummaryInBase>
      <OpenPositions>
        <OpenPosition levelOfDetail="LOT" symbol="ARM" assetCategory="STK"
          description="ARM HOLDINGS" currency="USD" fxRateToBase="1.0"
          position="3" costBasisPrice="100" costBasisMoney="300" markPrice="110"
          positionValue="330" fifoPnlUnrealized="30" openDateTime="20240102;093005" />
        <OpenPosition levelOfDetail="LOT" symbol="ARM" assetCategory="STK"
          description="ARM HOLDINGS" currency="USD" fxRateToBase="1.0"
          position="1" costBasisPrice="120" costBasisMoney="120" markPrice="110"
          positionValue="110" fifoPnlUnrealized="-10" openDateTime="20250304;100000" />
      </OpenPositions>
    </FlexStatement>
  </FlexStatements>
</FlexQueryResponse>"""


class ParsePortfolio(unittest.TestCase):
    def setUp(self):
        self.pf = ib.parse_portfolio(ET.fromstring(FLEX_XML))

    def test_statement_metadata(self):
        self.assertEqual(self.pf.account, "DU000001")
        self.assertEqual(self.pf.report_from_date, "20260530")
        self.assertEqual(self.pf.report_to_date, "20260530")

    def test_nav_uses_last_equity_summary_row(self):
        self.assertEqual(self.pf.net_asset_value, 1_000_000.0)

    def test_summary_and_lot_rows_are_split(self):
        self.assertEqual(len(self.pf.positions), 1)
        self.assertEqual(len(self.pf.lots), 1)
        pos = self.pf.positions[0]
        self.assertEqual(pos.symbol, "AMD")
        self.assertEqual(pos.quantity, 100.0)
        self.assertEqual(pos.percent_of_nav, 27.6)

    def test_base_market_value_applies_fx(self):
        pos = self.pf.positions[0]
        self.assertAlmostEqual(pos.base_market_value, 12000 * 23.0)

    def test_lot_timestamps_become_iso_utc(self):
        lot = self.pf.lots[0]
        self.assertEqual(lot.open_datetime, "2024-01-02T09:30:05+00:00")

    def test_cash_report(self):
        self.assertEqual(self.pf.cash[0]["currency"], "BASE_SUMMARY")
        self.assertAlmostEqual(self.pf.cash[0]["ending_cash"], 50000.5)

    def test_missing_statement_raises(self):
        with self.assertRaises(RuntimeError):
            ib.parse_portfolio(ET.fromstring("<FlexQueryResponse/>"))


class LotOnlyFallback(unittest.TestCase):
    def test_positions_are_aggregated_from_lots(self):
        pf = ib.parse_portfolio(ET.fromstring(LOTS_ONLY_XML))
        self.assertEqual(len(pf.lots), 2)
        self.assertEqual(len(pf.positions), 1)
        pos = pf.positions[0]
        self.assertEqual(pos.symbol, "ARM")
        self.assertEqual(pos.quantity, 4.0)
        self.assertAlmostEqual(pos.market_value, 440.0)
        self.assertAlmostEqual(pos.unrealized_pnl, 20.0)
        # cost price = total cost basis / total quantity
        self.assertAlmostEqual(pos.cost_price, 105.0)
        # percent of NAV recomputed from base value and the equity summary
        self.assertAlmostEqual(pos.percent_of_nav, 44.0)


class Helpers(unittest.TestCase):
    def test_dec_is_forgiving(self):
        self.assertEqual(ib._dec("1.5"), 1.5)
        self.assertEqual(ib._dec(""), 0.0)
        self.assertEqual(ib._dec(None), 0.0)
        self.assertEqual(ib._dec("garbage"), 0.0)

    def test_optional_dec_distinguishes_missing(self):
        self.assertIsNone(ib._optional_dec(""))
        self.assertIsNone(ib._optional_dec(None))
        self.assertEqual(ib._optional_dec("2.5"), 2.5)

    def test_ibkr_datetime_passthrough_on_unknown_format(self):
        self.assertEqual(ib._ibkr_datetime("2024-01-02"), "2024-01-02")
        self.assertEqual(ib._ibkr_datetime(""), "")
        self.assertEqual(ib._ibkr_datetime(None), "")

    def test_load_env_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / "secrets.env"
            env.write_text(
                "# comment\n"
                "IBKR_FLEX_TOKEN=  'abc123'  \n"
                "IBKR_FLEX_QUERY_ID=\"42\"\n"
                "MALFORMED LINE\n",
                encoding="utf-8",
            )
            vals = ib.load_env_file(env)
        self.assertEqual(vals["IBKR_FLEX_TOKEN"], "abc123")
        self.assertEqual(vals["IBKR_FLEX_QUERY_ID"], "42")
        self.assertNotIn("MALFORMED LINE", vals)

    def test_load_env_file_missing_path(self):
        self.assertEqual(ib.load_env_file(Path("does/not/exist.env")), {})


if __name__ == "__main__":
    unittest.main()
