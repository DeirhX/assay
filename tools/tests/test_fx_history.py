"""Offline tests for the daily FX panel. The Yahoo fetch is replaced by an
injectable seam (``fetch=``) that records the range each pair was asked for, so
we can assert the incremental window without touching the network."""
import _support  # noqa: F401
import datetime as dt
import tempfile
import unittest
from pathlib import Path

import fx_history


def _pts(pairs):
    """{'YYYY-MM-DD': rate, ...} -> [{'date':.., 'close':..}, ...]."""
    return [{"date": d, "close": r} for d, r in pairs.items()]


class FakeFetch:
    """Returns queued points per pair, recording (pair, range) calls. A queued
    Exception is raised to simulate a provider failure for that pair."""

    def __init__(self, by_pair):
        self.by_pair = by_pair
        self.calls = []

    def __call__(self, pair, rng):
        self.calls.append((pair, rng))
        val = self.by_pair.get(pair)
        if isinstance(val, BaseException):
            raise val
        return val


class RangeLadder(unittest.TestCase):
    def test_empty_cache_seeds_full_history(self):
        self.assertEqual(fx_history._range_for(None, dt.date(2026, 7, 7)), fx_history.SEED_RANGE)

    def test_gap_maps_to_smallest_covering_window(self):
        today = dt.date(2026, 7, 7)
        self.assertEqual(fx_history._range_for(today - dt.timedelta(days=3), today), "1mo")
        self.assertEqual(fx_history._range_for(today - dt.timedelta(days=40), today), "3mo")
        self.assertEqual(fx_history._range_for(today - dt.timedelta(days=120), today), "6mo")
        self.assertEqual(fx_history._range_for(today - dt.timedelta(days=300), today), "1y")
        self.assertEqual(fx_history._range_for(today - dt.timedelta(days=500), today), "2y")
        self.assertEqual(fx_history._range_for(today - dt.timedelta(days=2000), today), fx_history.SEED_RANGE)


class UpdatePanel(unittest.TestCase):
    def _path(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        return Path(self._tmp.name) / "fx-history.json"

    def test_seed_writes_series_and_requests_seed_range(self):
        path = self._path()
        fetch = FakeFetch({
            "USDCZK": _pts({"2026-07-01": 23.1, "2026-07-02": 23.2}),
            "EURCZK": _pts({"2026-07-01": 25.0}),
        })
        panel = fx_history.update_panel(("USDCZK", "EURCZK"), today=dt.date(2026, 7, 3),
                                        fetch=fetch, path=path)
        self.assertEqual(panel["base"], "CZK")
        self.assertEqual(fx_history.pair_series(panel, "USDCZK"),
                         {"2026-07-01": 23.1, "2026-07-02": 23.2})
        # empty cache -> both pairs asked for the seed range
        self.assertEqual({r for _, r in fetch.calls}, {fx_history.SEED_RANGE})
        # persisted and reloadable
        self.assertEqual(fx_history.pair_series(fx_history.load_panel(path), "EURCZK"),
                         {"2026-07-01": 25.0})

    def test_incremental_merges_new_days_and_dedupes_overlap(self):
        path = self._path()
        fx_history.update_panel(("USDCZK",), today=dt.date(2026, 7, 2),
                                fetch=FakeFetch({"USDCZK": _pts({"2026-07-01": 23.1, "2026-07-02": 23.2})}),
                                path=path)
        # second run: overlap re-quotes 07-02 (new value) and adds 07-03
        fetch = FakeFetch({"USDCZK": _pts({"2026-07-02": 23.25, "2026-07-03": 23.3})})
        panel = fx_history.update_panel(("USDCZK",), today=dt.date(2026, 7, 3),
                                        fetch=fetch, path=path)
        self.assertEqual(fx_history.pair_series(panel, "USDCZK"),
                         {"2026-07-01": 23.1, "2026-07-02": 23.25, "2026-07-03": 23.3})
        # a 1-day gap requests the smallest window, not the full seed
        self.assertEqual(fetch.calls, [("USDCZK", "1mo")])

    def test_failed_fetch_keeps_stale_series(self):
        path = self._path()
        fx_history.update_panel(("USDCZK",), today=dt.date(2026, 7, 2),
                                fetch=FakeFetch({"USDCZK": _pts({"2026-07-01": 23.1})}), path=path)
        panel = fx_history.update_panel(("USDCZK",), today=dt.date(2026, 7, 3),
                                        fetch=FakeFetch({"USDCZK": RuntimeError("boom")}), path=path)
        self.assertEqual(fx_history.pair_series(panel, "USDCZK"), {"2026-07-01": 23.1})

    def test_one_pair_failure_does_not_sink_the_others(self):
        path = self._path()
        fetch = FakeFetch({
            "USDCZK": RuntimeError("provider down"),
            "EURCZK": _pts({"2026-07-01": 25.0}),
        })
        panel = fx_history.update_panel(("USDCZK", "EURCZK"), today=dt.date(2026, 7, 2),
                                        fetch=fetch, path=path)
        self.assertEqual(fx_history.pair_series(panel, "USDCZK"), {})
        self.assertEqual(fx_history.pair_series(panel, "EURCZK"), {"2026-07-01": 25.0})

    def test_bad_points_are_skipped(self):
        path = self._path()
        pts = [
            {"date": "2026-07-01", "close": 23.1},
            {"date": "2026-07-02", "close": None},   # missing close
            {"date": "not-a-date", "close": 23.4},   # unparseable date
            {"date": "2026-07-03", "close": "x"},    # non-numeric
        ]
        panel = fx_history.update_panel(("USDCZK",), today=dt.date(2026, 7, 4),
                                        fetch=FakeFetch({"USDCZK": pts}), path=path)
        self.assertEqual(fx_history.pair_series(panel, "USDCZK"), {"2026-07-01": 23.1})


class RateOn(unittest.TestCase):
    def _panel(self):
        return {"base": "CZK", "series": {
            "USDCZK": {"2026-07-01": 23.1, "2026-07-03": 23.3, "2026-07-06": 23.6}}}

    def test_exact_day(self):
        self.assertEqual(fx_history.rate_on(self._panel(), "USDCZK", "2026-07-03"), 23.3)

    def test_weekend_falls_back_to_most_recent_prior(self):
        # 07-04/07-05 don't quote; as-of returns Friday's 07-03
        self.assertEqual(fx_history.rate_on(self._panel(), "USDCZK", dt.date(2026, 7, 5)), 23.3)

    def test_before_first_quote_is_none(self):
        self.assertIsNone(fx_history.rate_on(self._panel(), "USDCZK", "2026-06-30"))

    def test_untracked_pair_is_none(self):
        self.assertIsNone(fx_history.rate_on(self._panel(), "GBPCZK", "2026-07-03"))


class LoadPanel(unittest.TestCase):
    def test_missing_file_is_well_shaped_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            panel = fx_history.load_panel(Path(tmp) / "nope.json")
            self.assertEqual(panel["base"], "CZK")
            self.assertEqual(panel["series"], {})

    def test_corrupt_file_degrades_to_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fx-history.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(fx_history.load_panel(path)["series"], {})


# --------------------------------------------------------------------------- #
# Currency lens (exposure + FX window) -- the risk-view consumer
# --------------------------------------------------------------------------- #
class ExposureByCurrency(unittest.TestCase):
    def test_base_excluded_weights_and_order(self):
        holdings = {"base_currency": "CZK", "positions": [
            {"currency": "USD", "base_market_value": 6000.0},
            {"currency": "EUR", "base_market_value": 2000.0},
            {"currency": "CZK", "base_market_value": 2000.0},
            {"currency": "USD", "base_market_value": 0.0},
        ]}
        exp = fx_history.exposure_by_currency(holdings, "CZK")
        self.assertEqual([e["currency"] for e in exp], ["USD", "EUR"])
        self.assertAlmostEqual(exp[0]["weight_pct"], 60.0)   # 6000 / 10000
        self.assertAlmostEqual(exp[1]["weight_pct"], 20.0)

    def test_no_foreign_is_empty(self):
        holdings = {"base_currency": "CZK", "positions": [
            {"currency": "CZK", "base_market_value": 5000.0}]}
        self.assertEqual(fx_history.exposure_by_currency(holdings, "CZK"), [])


class WindowMove(unittest.TestCase):
    def _panel(self):
        return {"base": "CZK", "series": {"USDCZK": {
            "2024-01-02": 20.0, "2024-09-01": 22.0, "2025-01-01": 23.0}}}

    def test_hand_computed_over_year(self):
        mv = fx_history.window_move(self._panel(), "USDCZK", days=365,
                                    today=dt.date(2025, 1, 1))
        self.assertAlmostEqual(mv["return"], 23.0 / 20.0 - 1.0)
        self.assertEqual(mv["from"], "2024-01-02")
        self.assertEqual(mv["to"], "2025-01-01")

    def test_asof_picks_last_on_or_before_open(self):
        # 90-day window off 2025-01-01 opens ~2024-10-03; nearest prior is 09-01.
        mv = fx_history.window_move(self._panel(), "USDCZK", days=90,
                                    today=dt.date(2025, 1, 1))
        self.assertEqual(mv["from"], "2024-09-01")
        self.assertAlmostEqual(mv["return"], 23.0 / 22.0 - 1.0)

    def test_untracked_or_thin_is_none(self):
        self.assertIsNone(fx_history.window_move(self._panel(), "GBPCZK", days=365))
        thin = {"base": "CZK", "series": {"USDCZK": {"2025-01-01": 23.0}}}
        self.assertIsNone(fx_history.window_move(thin, "USDCZK", days=365,
                                                 today=dt.date(2025, 1, 1)))


class WindowReport(unittest.TestCase):
    def _panel(self):
        return {"base": "CZK", "fetched_at": "2025-01-01T00:00:00+00:00", "series": {
            "USDCZK": {"2024-01-02": 20.0, "2024-12-31": 23.0}}}

    def test_usd_only_move_is_all_fx(self):
        holdings = {"base_currency": "CZK", "positions": [
            {"currency": "USD", "base_market_value": 10000.0}]}
        rep = fx_history.window_report(holdings, rng="1y", panel=self._panel(),
                                       today=dt.date(2025, 1, 1))
        self.assertAlmostEqual(rep["foreign_pct"], 100.0)
        row = rep["window"][0]
        self.assertEqual(row["currency"], "USD")
        self.assertAlmostEqual(row["fx_return_pct"], 15.0)
        self.assertAlmostEqual(row["contribution_pct"], 15.0)   # 100% weight

    def test_no_foreign_has_no_window(self):
        holdings = {"base_currency": "CZK", "positions": [
            {"currency": "CZK", "base_market_value": 5000.0}]}
        rep = fx_history.window_report(holdings, rng="1y", panel=self._panel())
        self.assertEqual(rep["exposure"], [])
        self.assertEqual(rep["window"], [])
        self.assertEqual(rep["foreign_pct"], 0.0)

    def test_uncovered_currency_degrades_with_caveat(self):
        holdings = {"base_currency": "CZK", "positions": [
            {"currency": "EUR", "base_market_value": 10000.0}]}   # panel has no EURCZK
        rep = fx_history.window_report(holdings, rng="1y", panel=self._panel(),
                                       today=dt.date(2025, 1, 1))
        self.assertAlmostEqual(rep["foreign_pct"], 100.0)         # exposure still exact
        self.assertEqual(rep["window"], [])
        self.assertTrue(any("No FX history" in c for c in rep["caveats"]))

    def test_multiple_uncovered_currencies_share_one_caveat(self):
        # Two uncovered pairs must fold into a single line -- one "...its FX effect
        # is omitted." caveat per currency reads like the same sentence twice.
        holdings = {"base_currency": "CZK", "positions": [
            {"currency": "KRW", "base_market_value": 5000.0},
            {"currency": "HKD", "base_market_value": 5000.0}]}   # panel covers neither
        rep = fx_history.window_report(holdings, rng="1y", panel=self._panel(),
                                       today=dt.date(2025, 1, 1))
        omitted = [c for c in rep["caveats"] if "No FX history" in c]
        self.assertEqual(len(omitted), 1)
        self.assertIn("KRWCZK=X", omitted[0])
        self.assertIn("HKDCZK=X", omitted[0])
        self.assertIn("their FX effects are omitted", omitted[0])


if __name__ == "__main__":
    unittest.main()
