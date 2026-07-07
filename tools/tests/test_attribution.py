"""Offline tests for process attribution. The price fetch is an injectable seam
(``fetch=``) and the FX panel is passed in, so nothing here touches the network
or disk. The pure engine (TWR, reconstruction, curves) is checked with
hand-computed numbers; ``attribution_report`` is checked end-to-end against a
flat-FX fixture so a wrong conversion or flow leak would move a known answer."""
import _support  # noqa: F401
import tempfile
import unittest
from pathlib import Path

import attribution as A


def _panel(series):
    return {"base": "CZK", "fetched_at": None, "series": series}


class ExternalFlows(unittest.TestCase):
    def test_only_deposits_and_withdrawals_count(self):
        ct = [
            {"type": "Deposits/Withdrawals", "base_amount": 1000.0, "date": "2024-01-03"},
            {"type": "Dividends", "base_amount": 5.0, "date": "2024-01-04"},
            {"type": "Withholding Tax", "base_amount": -1.0, "date": "2024-01-04"},
            {"type": "Broker Interest Received", "base_amount": 2.0, "date": "2024-01-05"},
        ]
        self.assertEqual(A.external_flows(ct), {"2024-01-03": 1000.0})

    def test_same_day_flows_net_and_amount_fallback(self):
        ct = [
            {"type": "Deposits/Withdrawals", "amount": 500.0, "date": "2024-02-01"},  # base_amount missing
            {"type": "Withdrawal", "base_amount": -200.0, "date": "2024-02-01"},
        ]
        self.assertEqual(A.external_flows(ct), {"2024-02-01": 300.0})


class TimeWeightedReturn(unittest.TestCase):
    def test_mid_window_deposit_is_neutralized(self):
        # 100 -> 110 (+10%), then +100 deposit on a flat market day -> 210.
        # TWR must be +10%, not the +110% the raw balance implies.
        pts = [{"date": "d1", "value": 100.0}, {"date": "d2", "value": 110.0},
               {"date": "d3", "value": 210.0}]
        twr = A.time_weighted_return(pts, {"d3": 100.0})
        self.assertAlmostEqual(twr, 0.10, places=9)

    def test_no_flow_chains_daily_returns(self):
        pts = [{"date": "d1", "value": 100.0}, {"date": "d2", "value": 110.0},
               {"date": "d3", "value": 121.0}]
        self.assertAlmostEqual(A.time_weighted_return(pts), 0.21, places=9)

    def test_needs_two_points(self):
        self.assertIsNone(A.time_weighted_return([{"date": "d1", "value": 100.0}]))
        self.assertIsNone(A.time_weighted_return([]))


class PositionsAt(unittest.TestCase):
    def test_unwinds_trades_after_as_of(self):
        pos = [{"symbol": "AAA", "quantity": 100.0}, {"symbol": "BBB", "quantity": 50.0}]
        trades = [
            {"symbol": "AAA", "quantity": 40.0, "date": "2024-06-01"},   # bought later
            {"symbol": "BBB", "quantity": -10.0, "date": "2024-06-02"},  # sold later
            {"symbol": "AAA", "quantity": 5.0, "date": "2023-12-31"},    # before start: ignored
        ]
        self.assertEqual(A.positions_at(pos, trades, "2024-01-01"),
                         {"AAA": 60.0, "BBB": 60.0})

    def test_zeroed_positions_dropped(self):
        pos = [{"symbol": "AAA", "quantity": 10.0}]
        trades = [{"symbol": "AAA", "quantity": 10.0, "date": "2024-06-01"}]
        self.assertEqual(A.positions_at(pos, trades, "2024-01-01"), {})


class PriceSeries(unittest.TestCase):
    def test_converts_through_panel_day_by_day(self):
        native = [{"date": "2024-01-01", "close": 100.0}, {"date": "2024-01-02", "close": 110.0}]
        panel = _panel({"USDCZK": {"2024-01-01": 20.0, "2024-01-02": 25.0}})
        got = A.czk_price_series(native, panel, currency="USD", base="CZK")
        self.assertEqual(got, {"2024-01-01": 2000.0, "2024-01-02": 2750.0})

    def test_base_currency_passthrough(self):
        native = [{"date": "2024-01-01", "close": 42.0}]
        self.assertEqual(A.czk_price_series(native, _panel({}), currency="CZK"), {"2024-01-01": 42.0})

    def test_date_without_rate_is_dropped(self):
        native = [{"date": "2023-12-31", "close": 100.0}, {"date": "2024-01-01", "close": 100.0}]
        panel = _panel({"USDCZK": {"2024-01-01": 20.0}})  # nothing on/before 2023-12-31
        self.assertEqual(A.czk_price_series(native, panel, currency="USD"), {"2024-01-01": 2000.0})


class HoldIndexAndCurve(unittest.TestCase):
    def test_hold_index_is_start_weighted_total_return(self):
        price = {"WIN": {"s": 10.0, "e": 20.0}, "LOSE": {"s": 10.0, "e": 10.0}}
        bv = {"WIN": 100.0, "LOSE": 100.0}  # 50/50 at start
        idx = A.hold_index(bv, price, ["s", "e"], "s")
        self.assertAlmostEqual(idx["s"], 1.0)
        self.assertAlmostEqual(idx["e"], 1.5)  # 0.5*2 + 0.5*1

    def test_hold_index_drops_names_without_start_price(self):
        price = {"AAA": {"s": 10.0, "e": 12.0}}  # BBB has no series
        idx = A.hold_index({"AAA": 100.0, "BBB": 100.0}, price, ["s", "e"], "s")
        self.assertAlmostEqual(idx["e"], 1.2)  # only AAA contributes

    def test_flow_curve_accumulates_units(self):
        idx = {"d1": 10.0, "d2": 11.0, "d3": 11.0}
        curve = A.flow_curve(idx, seed_value=100.0, flows={"d3": 22.0}, dates=["d1", "d2", "d3"])
        self.assertAlmostEqual(curve[1]["value"], 110.0)  # 10 units * 11
        self.assertAlmostEqual(curve[2]["value"], 132.0)  # +2 units bought at 11 -> 12 * 11

    def test_flow_curve_empty_without_index(self):
        self.assertEqual(A.flow_curve({}, 100.0, {}, ["d1", "d2"]), [])


class AttributionReport(unittest.TestCase):
    def _fixture(self):
        # Flat FX (20 CZK/USD) so the conversion is transparent. Actual book is
        # flat; SPY and the single held name each rise 10% -> both counterfactuals
        # should beat the actual by ~10pp.
        panel = _panel({"USDCZK": {"2024-01-01": 20.0, "2024-01-02": 20.0, "2024-01-03": 20.0}})
        native = {
            "SPY": [{"date": "2024-01-01", "close": 100.0}, {"date": "2024-01-02", "close": 105.0},
                    {"date": "2024-01-03", "close": 110.0}],
            "AAA": [{"date": "2024-01-01", "close": 200.0}, {"date": "2024-01-02", "close": 210.0},
                    {"date": "2024-01-03", "close": 220.0}],
        }
        fetch = lambda sym, rng: native.get(sym)  # noqa: E731
        history = {
            "base_currency": "CZK",
            "from_date": "2024-01-01",
            "nav_series": [
                {"date": "2024-01-01", "nav": 2000.0},
                {"date": "2024-01-02", "nav": 2000.0},
                {"date": "2024-01-03", "nav": 2000.0},
            ],
            "cash_transactions": [],
            "trades": [],
        }
        holdings = {"base_currency": "CZK",
                    "positions": [{"symbol": "AAA", "quantity": 10.0, "currency": "USD"}]}
        return history, holdings, panel, fetch

    def test_benchmark_and_hold_beat_a_flat_book(self):
        history, holdings, panel, fetch = self._fixture()
        r = A.attribution_report(history, holdings, rng="1y", benchmark="SPY", fetch=fetch, panel=panel)
        self.assertTrue(r["enough_data"])
        self.assertEqual(r["base"], "CZK")
        self.assertAlmostEqual(r["twr"]["actual"], 0.0, places=6)
        self.assertAlmostEqual(r["twr"]["benchmark"], 10.0, places=4)  # SPY +10%, %-scaled
        self.assertAlmostEqual(r["twr"]["hold"], 10.0, places=4)       # AAA +10%
        # Curves are seeded with the same starting NAV (2000).
        self.assertAlmostEqual(r["curves"]["benchmark"][0]["value"], 2000.0, places=2)
        self.assertAlmostEqual(r["curves"]["benchmark"][-1]["value"], 2200.0, places=2)
        self.assertAlmostEqual(r["curves"]["hold"][-1]["value"], 2200.0, places=2)

    def test_deposit_is_neutralized_in_actual_twr(self):
        history, holdings, panel, fetch = self._fixture()
        # Add 1000 CZK on day 2; NAV rises by exactly the deposit (flat market).
        history["nav_series"][1]["nav"] = 3000.0
        history["nav_series"][2]["nav"] = 3000.0
        history["cash_transactions"] = [
            {"type": "Deposits/Withdrawals", "base_amount": 1000.0, "date": "2024-01-02"}]
        r = A.attribution_report(history, holdings, rng="1y", benchmark="SPY", fetch=fetch, panel=panel)
        self.assertAlmostEqual(r["twr"]["actual"], 0.0, places=6)  # deposit != performance
        self.assertAlmostEqual(r["flows_total"], 1000.0, places=2)

    def test_partial_ledger_raises_a_caveat(self):
        history, holdings, panel, fetch = self._fixture()
        history["from_date"] = "2024-06-01"  # ledger starts after the window
        r = A.attribution_report(history, holdings, rng="1y", benchmark="SPY", fetch=fetch, panel=panel)
        self.assertTrue(any("ledger begins" in c for c in r["caveats"]))

    def test_unpriceable_name_excluded_with_caveat(self):
        history, holdings, panel, fetch = self._fixture()
        holdings["positions"].append({"symbol": "ZZZ", "quantity": 5.0, "currency": "USD"})
        r = A.attribution_report(history, holdings, rng="1y", benchmark="SPY", fetch=fetch, panel=panel)
        self.assertTrue(any("ZZZ" in c for c in r["caveats"]))
        self.assertAlmostEqual(r["twr"]["hold"], 10.0, places=4)  # AAA still carries the index

    def test_missing_benchmark_price_omits_that_curve(self):
        history, holdings, panel, fetch = self._fixture()
        r = A.attribution_report(history, holdings, rng="1y", benchmark="NOPE", fetch=fetch, panel=panel)
        self.assertNotIn("benchmark", r["curves"])
        self.assertTrue(any("benchmark" in c.lower() for c in r["caveats"]))

    def test_no_history_reports_not_enough_data(self):
        r = A.attribution_report({"nav_series": []}, {}, fetch=lambda s, rng: None, panel=_panel({}))
        self.assertFalse(r["enough_data"])
        self.assertEqual(r["curves"], {})


class FetchMany(unittest.TestCase):
    """The parallel price fan-out: dedups providers, tolerates a per-symbol miss
    (records None, never raises), and never re-pulls the same provider twice."""

    def test_dedups_and_counts_one_pull_per_unique_symbol(self):
        calls: dict[str, int] = {}

        def fetch(sym, rng):
            calls[sym] = calls.get(sym, 0) + 1
            return [{"date": "2024-01-01", "close": 1.0}]

        got = A._fetch_many(fetch, ["SPY", "AAA", "SPY", ""], "1y")
        self.assertEqual(set(got), {"SPY", "AAA"})  # empty string dropped
        self.assertEqual(calls, {"SPY": 1, "AAA": 1})  # SPY fetched once despite the dupe

    def test_empty_input_is_noop(self):
        self.assertEqual(A._fetch_many(lambda s, r: [], [], "1y"), {})
        self.assertEqual(A._fetch_many(lambda s, r: [], ["", None], "1y"), {})

    def test_raising_fetch_becomes_none_not_an_error(self):
        def fetch(sym, rng):
            if sym == "BOOM":
                raise RuntimeError("provider down")
            return [{"date": "2024-01-01", "close": 1.0}]

        got = A._fetch_many(fetch, ["OK", "BOOM"], "1y")
        self.assertIsNone(got["BOOM"])
        self.assertIsNotNone(got["OK"])


class VerdictCache(unittest.TestCase):
    def _report(self):
        return {
            "enough_data": True, "as_of": "2024-01-03", "start": "2024-01-01",
            "range": "1y", "benchmark": "SPY",
            "twr": {"actual": 4.0, "hold": 10.0, "benchmark": 6.5},
        }

    def test_verdict_deltas_are_actual_minus_counterfactual(self):
        v = A.verdict_from_report(self._report())
        self.assertTrue(v["enough_data"])
        self.assertAlmostEqual(v["actual_pct"], 4.0)
        self.assertAlmostEqual(v["vs_hold_pp"], -6.0)       # 4 - 10: trailing "do nothing"
        self.assertAlmostEqual(v["vs_benchmark_pp"], -2.5)  # 4 - 6.5
        self.assertEqual(v["benchmark"], "SPY")

    def test_missing_counterfactual_leaves_delta_none(self):
        v = A.verdict_from_report({"enough_data": True, "twr": {"actual": 3.0}})
        self.assertIsNone(v["vs_hold_pp"])
        self.assertIsNone(v["vs_benchmark_pp"])

    def test_cache_round_trip_and_missing_reads_none(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "verdict.json"
            self.assertIsNone(A.load_verdict(path))  # nothing written yet
            written = A.cache_verdict(self._report(), path=path)
            self.assertIn("updated_at", written)
            loaded = A.load_verdict(path)
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertAlmostEqual(loaded["vs_hold_pp"], -6.0)
            self.assertEqual(loaded["as_of"], "2024-01-03")


if __name__ == "__main__":
    unittest.main()
