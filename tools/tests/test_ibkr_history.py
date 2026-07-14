"""Tests for the Flex history reconstructor: date-window planning, trade/NAV/cash
parsing, de-duplication across overlapping windows, inception auto-detection, and
the misconfigured-query guard. An injected ``fetch`` keeps it fully offline."""

from __future__ import annotations

import json
import unittest
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from unittest import mock

import _support  # noqa: F401
import ibkr_history as H


def _flex_1003():
    return RuntimeError("SendRequest failed: Status=Fail Code=1003 Statement is not available.")


def _flex_1018():
    return RuntimeError("SendRequest failed: Status=Fail Code=1018 Too many requests have been made from this token.")


# --------------------------------------------------------------------------- #
# Fake Flex documents
# --------------------------------------------------------------------------- #
def _trade_xml(t: dict) -> str:
    attrs = {
        "tradeID": t.get("trade_id", ""),
        "transactionID": t.get("txn", ""),
        "symbol": t.get("symbol", ""),
        "assetCategory": t.get("asset", "STK"),
        "currency": t.get("ccy", "USD"),
        "fxRateToBase": str(t.get("fx", 1)),
        "buySell": t.get("side", ""),
        "quantity": str(t.get("qty", 0)),
        "tradePrice": str(t.get("price", 0)),
        "proceeds": str(t.get("proceeds", 0)),
        "ibCommission": str(t.get("commission", 0)),
        "netCash": str(t.get("net", 0)),
        "fifoPnlRealized": str(t.get("pnl", 0)),
        "tradeDate": t.get("date", ""),
        "dateTime": t.get("dt", ""),
        "openCloseIndicator": t.get("oc", ""),
        "levelOfDetail": t.get("level", "EXECUTION"),
    }
    return "<Trade " + " ".join(f'{k}="{v}"' for k, v in attrs.items()) + " />"


def _nav_xml(date_str: str, total: float) -> str:
    return f'<EquitySummaryByReportDateInBase reportDate="{date_str}" total="{total}" cash="0" stock="{total}" />'


def statement(trades=(), nav=(), cash=(), account="U123", to_date="") -> ET.Element:
    trade_rows = "".join(_trade_xml(t) for t in trades)
    nav_rows = "".join(_nav_xml(d, v) for d, v in nav)
    cash_rows = "".join(
        f'<CashTransaction transactionID="{c["txn"]}" type="{c.get("type","")}" '
        f'amount="{c.get("amount",0)}" currency="USD" fxRateToBase="1" '
        f'dateTime="{c.get("dt","")}" reportDate="{c.get("date","")}" />'
        for c in cash
    )
    xml = (
        '<FlexQueryResponse><FlexStatements count="1">'
        f'<FlexStatement accountId="{account}" toDate="{to_date}">'
        f"<Trades>{trade_rows}</Trades>"
        f"<EquitySummaryInBase>{nav_rows}</EquitySummaryInBase>"
        f"<CashTransactions>{cash_rows}</CashTransactions>"
        "</FlexStatement></FlexStatements></FlexQueryResponse>"
    )
    return ET.fromstring(xml)


class FakeFetch:
    """Returns queued docs in call order, ignoring the date window."""

    def __init__(self, docs):
        self.docs = list(docs)
        self.calls = 0

    def __call__(self, token, query_id, *, from_date=None, to_date=None, verbose=False):
        doc = self.docs[self.calls] if self.calls < len(self.docs) else statement()
        self.calls += 1
        return doc


class RuleFetch:
    """Calls a rule(from_date, to_date, span_days, call_index) that returns a doc
    or raises, so tests can model inception boundaries and rate limits."""

    def __init__(self, rule):
        self.rule = rule
        self.calls = 0

    def __call__(self, token, query_id, *, from_date=None, to_date=None, verbose=False):
        fd = H._parse_date(from_date)
        td = H._parse_date(to_date)
        span = (td - fd).days
        i = self.calls
        self.calls += 1
        return self.rule(fd, td, span, i)


# --------------------------------------------------------------------------- #
# Date helpers + window planning
# --------------------------------------------------------------------------- #
class Dates(unittest.TestCase):
    def test_parse_date_variants(self):
        self.assertEqual(H._parse_date("20240115"), date(2024, 1, 15))
        self.assertEqual(H._parse_date("2024-01-15"), date(2024, 1, 15))
        self.assertIsNone(H._parse_date(""))
        self.assertIsNone(H._parse_date("garbage"))

    def test_iso_date(self):
        self.assertEqual(H._iso_date("20240115"), "2024-01-15")

    def test_windows_stop_at_explicit_start(self):
        w = H.windows(date(2026, 1, 1), start=date(2024, 1, 1))
        self.assertEqual(w[0][1], date(2026, 1, 1))      # newest window ends today
        self.assertEqual(w[-1][0], date(2024, 1, 1))     # oldest window starts at floor
        # No window spans more than 365 days.
        for fd, td in w:
            self.assertLessEqual((td - fd).days, H.MAX_WINDOW_DAYS - 1)
        # Windows are contiguous and descending.
        for newer, older in zip(w, w[1:]):
            self.assertEqual((newer[0] - older[1]).days, 1)

    def test_windows_respect_max(self):
        w = H.windows(date(2026, 1, 1), start=None, max_windows=3)
        self.assertEqual(len(w), 3)


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
class Parsing(unittest.TestCase):
    def test_parse_trade_buy_sign_and_base(self):
        doc = statement(trades=[{
            "txn": "t1", "symbol": "AMD", "side": "BUY", "qty": 10, "price": 100,
            "proceeds": -1000, "commission": -1, "net": -1001, "fx": 1, "date": "20240115",
        }])
        t = H.parse_window(doc)["trades"][0]
        self.assertEqual(t["side"], "BUY")
        self.assertEqual(t["base_cash_flow"], -1001.0)   # cash out
        self.assertEqual(t["base_value"], -1000.0)
        self.assertEqual(t["date"], "2024-01-15")

    def test_parse_trade_infers_side_from_qty(self):
        doc = statement(trades=[{"txn": "t2", "symbol": "NVDA", "qty": -5, "net": 499}])
        t = H.parse_window(doc)["trades"][0]
        self.assertEqual(t["side"], "SELL")

    def test_fx_scales_base_cash_flow(self):
        doc = statement(trades=[{"txn": "t3", "symbol": "ASML", "side": "BUY",
                                 "qty": 2, "net": -100, "fx": 1.1}])
        t = H.parse_window(doc)["trades"][0]
        self.assertAlmostEqual(t["base_cash_flow"], -110.0)

    def test_non_execution_rows_dropped(self):
        doc = statement(trades=[
            {"txn": "exec", "symbol": "AMD", "side": "BUY", "net": -10, "level": "EXECUTION"},
            {"txn": "order", "symbol": "AMD", "side": "BUY", "net": -10, "level": "ORDER"},
        ])
        trades = H.parse_window(doc)["trades"]
        self.assertEqual([t["transaction_id"] for t in trades], ["exec"])

    def test_parse_nav_and_cash(self):
        doc = statement(
            nav=[("20240115", 50000.0), ("20240116", 51000.0)],
            cash=[{"txn": "c1", "type": "Dividends", "amount": 12.5, "date": "20240116"}],
        )
        parsed = H.parse_window(doc)
        self.assertEqual(len(parsed["nav"]), 2)
        self.assertEqual(parsed["nav"][0]["nav"], 50000.0)
        self.assertEqual(parsed["cash"][0]["amount"], 12.5)


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #
class BuildHistory(unittest.TestCase):
    def test_dedup_and_sort_across_windows(self):
        # Overlapping windows repeat t1; it must appear once, sorted by datetime.
        w1 = statement(
            trades=[{"txn": "t2", "symbol": "AMD", "side": "BUY", "net": -200,
                     "dt": "20250110;120000", "date": "20250110"}],
            nav=[("2025-01-10".replace("-", ""), 60000.0)],
        )
        w2 = statement(
            trades=[
                {"txn": "t1", "symbol": "NVDA", "side": "BUY", "net": -100,
                 "dt": "20240115;100000", "date": "20240115"},
                {"txn": "t2", "symbol": "AMD", "side": "BUY", "net": -200,
                 "dt": "20250110;120000", "date": "20250110"},  # dupe of w1
            ],
            nav=[("20240115", 50000.0)],
        )
        empty = statement()
        fetch = FakeFetch([w1, w2, empty, empty])
        h = H.build_history("tok", "qid", today=date(2026, 1, 1), sleep=0, fetch=fetch)
        self.assertEqual(h["summary"]["n_trades"], 2)
        self.assertEqual([t["transaction_id"] for t in h["trades"]], ["t1", "t2"])
        self.assertEqual(h["nav_series"][0]["date"], "2024-01-15")  # ascending
        self.assertEqual(h["from_date"], "2024-01-15")

    def test_auto_detect_inception_stops_after_two_empty(self):
        with_data = statement(trades=[{"txn": "t1", "symbol": "AMD", "side": "BUY", "net": -10}],
                              nav=[("20250101", 1000.0)])
        empty = statement()
        fetch = FakeFetch([with_data, empty, empty, with_data])
        h = H.build_history("tok", "qid", today=date(2026, 1, 1), sleep=0, fetch=fetch)
        # window1 data, window2 empty, window3 empty -> stop (don't reach the 4th).
        self.assertEqual(h["summary"]["windows"], 3)
        self.assertEqual(fetch.calls, 3)

    def test_explicit_start_pulls_all_windows(self):
        d = statement(trades=[{"txn": "x", "symbol": "AMD", "side": "BUY", "net": -10}],
                      nav=[("20250101", 1000.0)])
        fetch = FakeFetch([d, d, d, d, d])
        h = H.build_history("tok", "qid", start=date(2024, 1, 1),
                            today=date(2026, 1, 1), sleep=0, fetch=fetch)
        # 2026-01-01 back to 2024-01-01 in 365d windows = 3 windows; no early stop.
        self.assertEqual(h["summary"]["windows"], 3)

    def test_empty_first_window_raises(self):
        fetch = FakeFetch([statement()])
        with self.assertRaises(ValueError):
            H.build_history("tok", "qid", today=date(2026, 1, 1), sleep=0, fetch=fetch)

    def test_young_account_binary_searches_inception(self):
        # Flex 1003s any window starting before inception (span > 100 days here);
        # the build must find the largest servable window and stop, not abort.
        data = statement(trades=[{"txn": "t1", "symbol": "AMD", "side": "BUY", "net": -50}],
                         nav=[("20260101", 1234.0)])

        def rule(fd, td, span, i):
            if span > 100:
                raise _flex_1003()
            return data

        fetch = RuleFetch(rule)
        h = H.build_history("tok", "qid", today=date(2026, 6, 13), sleep=0, fetch=fetch)
        self.assertEqual(h["summary"]["n_trades"], 1)
        self.assertEqual(h["summary"]["windows"], 1)
        # First call is the full window (1003); the rest are the bounded search.
        self.assertGreater(fetch.calls, 1)

    def test_rate_limit_is_retried(self):
        data = statement(trades=[{"txn": "t1", "symbol": "AMD", "side": "BUY", "net": -50}],
                         nav=[("20260101", 1234.0)])

        def rule(fd, td, span, i):
            if i < 2:  # first two attempts get rate-limited, then it succeeds
                raise _flex_1018()
            return data if i == 2 else statement()

        fetch = RuleFetch(rule)
        with mock.patch.object(H.time, "sleep"):  # don't actually back off in tests
            h = H.build_history("tok", "qid", today=date(2026, 1, 1), sleep=0, fetch=fetch)
        self.assertEqual(h["summary"]["n_trades"], 1)
        self.assertGreaterEqual(fetch.calls, 3)  # 2 rate-limited + 1 success

    def test_persistent_rate_limit_raises(self):
        def rule(fd, td, span, i):
            raise _flex_1018()

        fetch = RuleFetch(rule)
        with mock.patch.object(H.time, "sleep"):
            with self.assertRaises(RuntimeError):
                H.build_history("tok", "qid", today=date(2026, 1, 1), sleep=0, fetch=fetch)


class AnchorAndErrors(unittest.TestCase):
    def test_error_kinds(self):
        self.assertEqual(H._flex_error_kind(_flex_1018()), "rate")
        self.assertEqual(H._flex_error_kind(_flex_1003()), "unavailable")
        self.assertEqual(
            H._flex_error_kind(RuntimeError("Status=Warn Code=1025 Too many failed attempts.")),
            "locked")
        self.assertEqual(H._flex_error_kind(RuntimeError("boom")), "other")

    def test_lockout_becomes_actionable_error(self):
        def fetch(token, qid, *, from_date=None, to_date=None, verbose=False):
            raise RuntimeError("SendRequest failed: Status=Warn Code=1025 Too many failed attempts.")

        with self.assertRaises(RuntimeError) as ctx:
            H._fetch_window(fetch, "tok", "qid", date(2026, 1, 1), date(2026, 1, 2),
                            verbose=False, progress=None, sleep=0)
        self.assertIn("1025", str(ctx.exception))

    def test_anchor_reads_statement_to_date(self):
        def fetch(token, qid, *, from_date=None, to_date=None, verbose=False):
            # No override is sent for the anchor probe.
            assert from_date is None and to_date is None
            return statement(nav=[("20260612", 1000.0)], to_date="20260612")

        end, parsed = H._anchor_end(fetch, "tok", "qid", fallback=date(2026, 6, 13),
                                    verbose=False, progress=None, sleep=0)
        self.assertEqual(end, date(2026, 6, 12))
        self.assertEqual(len(parsed["nav"]), 1)

    def test_anchor_falls_back_when_unavailable(self):
        def fetch(token, qid, *, from_date=None, to_date=None, verbose=False):
            raise _flex_1003()

        end, parsed = H._anchor_end(fetch, "tok", "qid", fallback=date(2026, 6, 13),
                                    verbose=False, progress=None, sleep=0)
        self.assertEqual(end, date(2026, 6, 13))
        self.assertIsNone(parsed)


class Incremental(unittest.TestCase):
    def _initial(self):
        init = statement(
            trades=[{"txn": "t1", "symbol": "AMD", "side": "BUY", "qty": 10,
                     "net": -100, "date": "20260105", "dt": "20260105;120000"}],
            nav=[("20260109", 100.0), ("20260110", 101.0)],
        )
        existing = H.build_history("tok", "qid", today=date(2026, 1, 10),
                                   start=date(2026, 1, 1), sleep=0, fetch=FakeFetch([init]))
        self.assertEqual(existing["summary"]["n_trades"], 1)
        self.assertEqual(existing["to_date"], "2026-01-10")
        return existing

    def test_seed_round_trips_payload(self):
        existing = self._initial()
        seed = H._seed_from_payload(existing)
        self.assertEqual(len(seed["trades"]), 1)
        self.assertEqual(len(seed["nav"]), 2)
        self.assertEqual(seed["account"], existing["account"])

    def test_covered_through_prefers_to_date(self):
        self.assertEqual(H._covered_through({"to_date": "2026-01-10"}), date(2026, 1, 10))
        # Falls back to the latest NAV / trade date when to_date is absent.
        self.assertEqual(
            H._covered_through({"nav_series": [{"date": "2026-01-08"}],
                                "trades": [{"date": "2026-01-09"}]}),
            date(2026, 1, 9))
        self.assertIsNone(H._covered_through({}))

    def test_extend_merges_only_new_days_and_reports_delta(self):
        existing = self._initial()
        # The overlap re-pulls t1 (deduped) and brings t2 + a fresh NAV day.
        new = statement(
            trades=[
                {"txn": "t1", "symbol": "AMD", "side": "BUY", "qty": 10, "net": -100,
                 "date": "20260105", "dt": "20260105;120000"},
                {"txn": "t2", "symbol": "NVDA", "side": "SELL", "qty": -5, "net": 200,
                 "date": "20260111", "dt": "20260111;120000"},
            ],
            nav=[("20260110", 101.0), ("20260112", 105.0)],
        )
        fetch = FakeFetch([new])
        updated = H.extend_history(existing, "tok", "qid", today=date(2026, 1, 12),
                                   sleep=0, fetch=fetch)
        self.assertEqual(fetch.calls, 1)  # a single tail window
        self.assertEqual(updated["summary"]["n_trades"], 2)  # t1 deduped, t2 added
        self.assertEqual(updated["summary"]["n_nav_points"], 3)
        self.assertEqual(updated["to_date"], "2026-01-12")
        u = updated["summary"]["update"]
        self.assertEqual(u["mode"], "incremental")
        self.assertEqual(u["new_trades"], 1)
        self.assertEqual(u["new_nav_points"], 1)
        self.assertEqual(u["previous_to_date"], "2026-01-10")

    def test_extend_without_existing_runs_full(self):
        stmt = statement(
            trades=[{"txn": "t1", "symbol": "AMD", "side": "BUY", "qty": 1,
                     "net": -10, "date": "20260110", "dt": "20260110;120000"}],
            nav=[("20260110", 100.0)],
        )
        res = H.extend_history(None, "tok", "qid", today=date(2026, 1, 10),
                               sleep=0, fetch=FakeFetch([stmt]))
        self.assertEqual(res["summary"]["n_trades"], 1)
        self.assertNotIn("update", res["summary"])  # full pull, no delta block

    def test_summary_by_symbol_aggregates(self):
        w = statement(
            trades=[
                {"txn": "a", "symbol": "AMD", "side": "BUY", "net": -100, "pnl": 0},
                {"txn": "b", "symbol": "AMD", "side": "SELL", "net": 150, "pnl": 50},
                {"txn": "c", "symbol": "NVDA", "side": "BUY", "net": -80, "pnl": 0},
            ],
            nav=[("20250101", 1000.0)],
        )
        fetch = FakeFetch([w, statement(), statement()])
        h = H.build_history("tok", "qid", today=date(2026, 1, 1), sleep=0, fetch=fetch)
        by = {r["symbol"]: r for r in h["summary"]["by_symbol"]}
        self.assertEqual(by["AMD"]["n"], 2)
        self.assertEqual(by["AMD"]["buys"], 1)
        self.assertEqual(by["AMD"]["sells"], 1)
        self.assertEqual(by["AMD"]["net_base_cash_flow"], 50.0)
        self.assertEqual(by["AMD"]["realized_pnl"], 50.0)
        self.assertEqual(h["summary"]["realized_pnl_total"], 50.0)


class DeriveUnderlying(unittest.TestCase):
    def test_stock_groups_under_its_own_symbol(self):
        self.assertEqual(H.derive_underlying("STK", "AMD", "Advanced Micro Devices"), "AMD")

    def test_option_prefers_explicit_underlying_symbol(self):
        self.assertEqual(
            H.derive_underlying("OPT", "META  240322P00470000", "META 22MAR24 470 P",
                                underlying_symbol="meta"),
            "META")

    def test_option_falls_back_to_description_first_token(self):
        # The raw symbol's first token is NOT the underlying ("P VO3 ...").
        self.assertEqual(
            H.derive_underlying("OPT", "P VO3  20240621 110 M", "VOW3 21JUN24 110 P"),
            "VOW3")

    def test_option_without_description_uses_symbol_token(self):
        self.assertEqual(H.derive_underlying("OPT", "BABA  240419P00060000", ""), "BABA")

    def test_blank_symbol_is_placeholder(self):
        self.assertEqual(H.derive_underlying("STK", "", ""), "?")


class EnrichPayload(unittest.TestCase):
    def _payload(self):
        # Mimic an OLD cache: trades + by_symbol rows lacking the new fields.
        return {
            "trades": [
                {"symbol": "AMD", "asset_class": "STK", "side": "BUY"},
                {"symbol": "P VO3  20240621 110 M", "asset_class": "OPT",
                 "description": "VOW3 21JUN24 110 P", "side": "SELL"},
            ],
            "summary": {"by_symbol": [
                {"symbol": "AMD", "n": 1},
                {"symbol": "P VO3  20240621 110 M", "n": 1},
            ]},
        }

    def test_backfills_trade_grouping_fields(self):
        h = H.enrich_history_payload(self._payload())
        amd, opt = h["trades"]
        self.assertEqual(amd["underlying"], "AMD")
        self.assertFalse(amd["is_option"])
        self.assertEqual(opt["underlying"], "VOW3")
        self.assertTrue(opt["is_option"])

    def test_backfills_by_symbol_from_trades(self):
        h = H.enrich_history_payload(self._payload())
        rows = {r["symbol"]: r for r in h["summary"]["by_symbol"]}
        self.assertEqual(rows["P VO3  20240621 110 M"]["underlying"], "VOW3")
        self.assertTrue(rows["P VO3  20240621 110 M"]["is_option"])
        self.assertFalse(rows["AMD"]["is_option"])

    def test_is_idempotent(self):
        once = H.enrich_history_payload(self._payload())
        twice = H.enrich_history_payload(json.loads(json.dumps(once)))
        self.assertEqual(once, twice)

    def test_none_passes_through(self):
        self.assertIsNone(H.enrich_history_payload(None))


class LiveExecutionTail(unittest.TestCase):
    def _flex_payload(self):
        return H.normalize(
            "U123",
            {"flex-1": {
                "trade_id": "flex-1", "transaction_id": "txn-1",
                "datetime": "2026-07-13T15:00:00+00:00", "date": "2026-07-13",
                "symbol": "AMD", "asset_class": "STK", "currency": "USD",
                "side": "BUY", "quantity": 2.0, "price": 150.0,
                "proceeds": -300.0, "commission": -1.0, "net_cash": -301.0,
                "fx_rate_to_base": 1.0, "base_cash_flow": -301.0,
                "base_value": -300.0, "realized_pnl": 0.0, "open_close": "O",
                "description": "AMD", "listing_exchange": "NASDAQ",
                "underlying_symbol": "", "underlying": "AMD", "put_call": "",
                "strike": None, "expiry": "",
            }},
            {"2026-07-13": {"date": "2026-07-13", "nav": 1000.0, "cash": 100.0,
                            "stock": 900.0}},
            {},
            end=date(2026, 7, 13),
            windows_done=1,
        )

    @staticmethod
    def _live(execution_id, stamp, symbol="EEFT", sec_type="STK"):
        return {
            "execution_id": execution_id,
            "symbol": symbol,
            "side": "S",
            "trade_time_r": stamp,
            "size": 3,
            "price": "75.50",
            "commission": "1.25",
            "net_amount": 225.25,
            "account": "U123",
            "contract_description_1": symbol,
            "sec_type": sec_type,
            "listing_exchange": "NASDAQ",
            "conid": 99,
        }

    def test_only_appends_executions_after_flex_coverage(self):
        old_ms = int(datetime(2026, 7, 13, 16, tzinfo=timezone.utc).timestamp() * 1000)
        today_ms = int(datetime(2026, 7, 14, 8, tzinfo=timezone.utc).timestamp() * 1000)
        merged = H.merge_live_executions(
            self._flex_payload(),
            [self._live("old", old_ms), self._live("today", today_ms)],
            fetched_at=datetime(2026, 7, 14, 8, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(merged["summary"]["n_trades"], 2)
        live = next(t for t in merged["trades"] if t.get("source") == "live")
        self.assertEqual(live["execution_id"], "today")
        self.assertTrue(live["provisional"])
        self.assertEqual(live["base_cash_flow"], 0.0)
        self.assertEqual(merged["flex_to_date"], "2026-07-13")
        self.assertEqual(merged["to_date"], "2026-07-14")
        self.assertEqual(merged["history_sources"]["live_trade_count"], 1)
        # Provisional live rows must not invent base-currency cash/P&L.
        self.assertEqual(merged["summary"]["realized_pnl_total"], 0.0)

    def test_stripping_live_tail_restores_flex_coverage(self):
        today_ms = int(datetime(2026, 7, 14, 8, tzinfo=timezone.utc).timestamp() * 1000)
        merged = H.merge_live_executions(
            self._flex_payload(), [self._live("today", today_ms)]
        )
        stripped = H.strip_live_executions(merged)
        self.assertEqual(stripped["summary"]["n_trades"], 1)
        self.assertEqual(stripped["to_date"], "2026-07-13")
        self.assertEqual(H._covered_through(merged), date(2026, 7, 13))

    def test_option_uses_underlying_for_grouping(self):
        row = self._live("opt", 1784016000000, symbol="PYPL", sec_type="OPT")
        row["contract_description_1"] = "PYPL 17JUL26 80 C"
        trade = H.normalize_live_trade(row)
        self.assertEqual(trade["symbol"], "PYPL 17JUL26 80 C")
        self.assertEqual(trade["underlying"], "PYPL")
        self.assertEqual(trade["quantity"], -3.0)


class MultiCurrency(unittest.TestCase):
    """Trades span currencies, so totals must be in base (fx==1) currency; raw
    native P&L summed across USD/CZK/HKD is meaningless."""

    def _history(self):
        # CZK is base (fx 1); USD converts at 23x. Both realize 100 native.
        w = statement(
            trades=[
                {"txn": "a", "symbol": "CEZ", "ccy": "CZK", "fx": 1, "side": "SELL",
                 "net": 500, "pnl": 100},
                {"txn": "b", "symbol": "AMD", "ccy": "USD", "fx": 23, "side": "SELL",
                 "net": 200, "pnl": 100},
            ],
            nav=[("20250101", 1000.0)],
        )
        return H.build_history("tok", "qid", today=date(2026, 1, 1), sleep=0,
                               fetch=FakeFetch([w, statement(), statement()]))

    def test_base_currency_is_the_fx1_currency(self):
        self.assertEqual(self._history()["base_currency"], "CZK")

    def test_realized_total_is_base_not_naive_sum(self):
        h = self._history()
        # NOT 200 (naive 100+100); base = 100*1 (CEZ) + 100*23 (AMD) = 2400.
        self.assertEqual(h["summary"]["realized_pnl_total"], 2400.0)

    def test_by_symbol_carries_native_currency_and_base_pnl(self):
        by = {r["symbol"]: r for r in self._history()["summary"]["by_symbol"]}
        self.assertEqual(by["AMD"]["currency"], "USD")
        self.assertEqual(by["AMD"]["base_realized_pnl"], 2300.0)  # 100 USD * 23
        self.assertEqual(by["AMD"]["realized_pnl"], 100.0)        # native untouched
        self.assertEqual(by["CEZ"]["currency"], "CZK")
        self.assertEqual(by["CEZ"]["base_realized_pnl"], 100.0)

    def test_trades_get_base_realized_pnl(self):
        amd = next(t for t in self._history()["trades"] if t["symbol"] == "AMD")
        self.assertEqual(amd["base_realized_pnl"], 2300.0)
        self.assertEqual(amd["realized_pnl"], 100.0)


class BoughtSoldAmounts(unittest.TestCase):
    """by_symbol carries gross base cash bought/sold, and sold - bought ties out
    to the net cash flow."""

    def _history(self):
        w = statement(
            trades=[
                # AAA: buy 1000 (cash out), then sell 1500 (cash in) -> net +500.
                {"txn": "a1", "symbol": "AAA", "ccy": "USD", "fx": 1, "side": "BUY", "net": -1000},
                {"txn": "a2", "symbol": "AAA", "ccy": "USD", "fx": 1, "side": "SELL", "net": 1500},
            ],
            nav=[("20250101", 1000.0)],
        )
        return H.build_history("tok", "qid", today=date(2026, 1, 1), sleep=0,
                               fetch=FakeFetch([w, statement(), statement()]))

    def test_bought_and_sold_are_gross_and_net_ties_out(self):
        row = self._history()["summary"]["by_symbol"][0]
        self.assertEqual(row["bought_base"], 1000.0)
        self.assertEqual(row["sold_base"], 1500.0)
        self.assertEqual(round(row["sold_base"] - row["bought_base"], 2), row["net_base_cash_flow"])


if __name__ == "__main__":
    unittest.main()
