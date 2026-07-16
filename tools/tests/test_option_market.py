"""Tests for provider selection and caching at the option-market boundary."""
from __future__ import annotations

import tempfile
import time
import unittest
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ibkr_trade  # noqa: E402
import option_market  # noqa: E402
import options_math  # noqa: E402
import store  # noqa: E402
import timeutil  # noqa: E402
from providers import alpaca, fred, yahoo  # noqa: E402


def _chain(source: str) -> dict:
    return {
        "source": source,
        "symbol": "NVDA",
        "quote_timestamp": timeutil.now_iso(),
        "expiries": [{"expiry": "2026-08-21", "calls": [], "puts": []}],
    }


class SessionReady(unittest.TestCase):
    def setUp(self):
        option_market.reset_session_cache()

    def tearDown(self):
        option_market.reset_session_cache()

    def test_reads_auth_status_and_fails_quietly(self):
        with mock.patch.object(
            ibkr_trade, "auth_status", return_value={"authenticated": True},
        ):
            self.assertTrue(option_market.session_ready())
        option_market.reset_session_cache()
        with mock.patch.object(ibkr_trade, "auth_status", side_effect=RuntimeError("down")):
            self.assertFalse(option_market.session_ready())

    def test_memoizes_within_ttl(self):
        with mock.patch.object(
            ibkr_trade, "auth_status", return_value={"authenticated": True},
        ) as auth:
            self.assertTrue(option_market.session_ready())
            self.assertTrue(option_market.session_ready())
        auth.assert_called_once()


class ChainSelection(unittest.TestCase):
    def test_prefers_ibkr_when_authenticated(self):
        with mock.patch.object(option_market, "session_ready", return_value=True), \
                mock.patch.object(ibkr_trade, "option_chain",
                                  return_value=_chain("ibkr")) as ibkr_fn, \
                mock.patch.object(yahoo, "option_chain") as yahoo_fn:
            out = option_market.fetch_option_chain("NVDA")
        self.assertEqual(out["source"], "ibkr")
        ibkr_fn.assert_called_once()
        yahoo_fn.assert_not_called()

    def test_uses_alpaca_between_ibkr_and_yahoo(self):
        with mock.patch.object(option_market, "session_ready", return_value=False), \
                mock.patch.object(alpaca, "enabled", return_value=True), \
                mock.patch.object(alpaca, "option_chain",
                                  return_value=_chain("alpaca")) as alpaca_fn, \
                mock.patch.object(yahoo, "option_chain") as yahoo_fn:
            out = option_market.fetch_option_chain("NVDA")
        self.assertEqual(out["source"], "alpaca")
        alpaca_fn.assert_called_once()
        yahoo_fn.assert_not_called()

    def test_connected_ibkr_miss_does_not_fall_back(self):
        with mock.patch.object(option_market, "session_ready", return_value=True), \
                mock.patch.object(ibkr_trade, "option_chain", return_value=None), \
                mock.patch.object(alpaca, "enabled", return_value=True), \
                mock.patch.object(alpaca, "option_chain",
                                  return_value=_chain("alpaca")) as alpaca_fn, \
                mock.patch.object(yahoo, "option_chain",
                                  return_value=_chain("yahoo")) as yahoo_fn:
            out = option_market.fetch_option_chain("NVDA")
        self.assertIsNone(out)
        alpaca_fn.assert_not_called()
        yahoo_fn.assert_not_called()

    def test_connected_ibkr_error_does_not_fall_back(self):
        with mock.patch.object(option_market, "session_ready", return_value=True), \
                mock.patch.object(ibkr_trade, "option_chain",
                                  side_effect=RuntimeError("gateway hiccup")), \
                mock.patch.object(alpaca, "enabled", return_value=True), \
                mock.patch.object(alpaca, "option_chain",
                                  return_value=_chain("alpaca")) as alpaca_fn, \
                mock.patch.object(yahoo, "option_chain",
                                  return_value=_chain("yahoo")) as yahoo_fn:
            out = option_market.fetch_option_chain("NVDA")
        self.assertIsNone(out)
        alpaca_fn.assert_not_called()
        yahoo_fn.assert_not_called()

    def test_disconnected_falls_through_provider_misses_to_yahoo(self):
        with mock.patch.object(option_market, "session_ready", return_value=False), \
                mock.patch.object(ibkr_trade, "option_chain") as ibkr_fn, \
                mock.patch.object(alpaca, "enabled", return_value=True), \
                mock.patch.object(alpaca, "option_chain", return_value=None), \
                mock.patch.object(yahoo, "option_chain",
                                  return_value=_chain("yahoo")) as yahoo_fn:
            out = option_market.fetch_option_chain("NVDA")
        self.assertEqual(out["source"], "yahoo")
        ibkr_fn.assert_not_called()
        yahoo_fn.assert_called_once()

    def test_ibkr_budget_is_passed_as_cooperative_deadline(self):
        seen: dict[str, float] = {}

        def bounded_chain(_symbol, **kwargs):
            seen["deadline"] = kwargs.get("deadline_monotonic")
            return None

        with mock.patch.object(option_market, "session_ready", return_value=True), \
                mock.patch.object(ibkr_trade, "option_chain", side_effect=bounded_chain), \
                mock.patch.object(alpaca, "option_chain") as alpaca_fn, \
                mock.patch.object(yahoo, "option_chain") as yahoo_fn, \
                mock.patch.object(option_market, "IBKR_CHAIN_BUDGET_SECONDS", 0.05):
            started = time.perf_counter()
            out = option_market.fetch_option_chain("NVDA")
            elapsed = time.perf_counter() - started
        self.assertIsNone(out)
        alpaca_fn.assert_not_called()
        yahoo_fn.assert_not_called()
        self.assertLess(elapsed, 1.0)
        self.assertGreaterEqual(seen["deadline"], started + 0.04)


class MarketCaches(unittest.TestCase):
    def test_option_chain_persists_and_serves(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            with mock.patch.object(
                option_market, "fetch_option_chain", return_value=_chain("ibkr"),
            ) as fetch:
                first = option_market.cached_option_chain("NVDA", cache_dir=cache_dir)
                second = option_market.cached_option_chain("NVDA", cache_dir=cache_dir)
        self.assertEqual(first["source"], "ibkr")
        self.assertEqual(second["source"], "ibkr")
        fetch.assert_called_once_with("NVDA", target_dte=37)

    def test_directional_route_chain_has_a_small_separate_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            with mock.patch.object(
                option_market, "fetch_option_chain", return_value=_chain("ibkr"),
            ) as fetch:
                first = option_market.cached_option_chain(
                    "NVDA", cache_dir=cache_dir, right="P",
                )
                second = option_market.cached_option_chain(
                    "NVDA", cache_dir=cache_dir, right="P",
                )
                cache_exists = (cache_dir / "NVDA-route-p-monthly.json").exists()
        self.assertEqual(first, second)
        self.assertTrue(cache_exists)
        fetch.assert_called_once_with(
            "NVDA",
            max_expiries=option_market.ROUTE_CHAIN_MAX_EXPIRIES,
            strikes_per_side=option_market.ROUTE_CHAIN_STRIKES_PER_SIDE,
            rights=("P",),
            target_dte=37,
        )

    def test_fallback_chain_uses_short_cache_before_retrying_ibkr(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            store.write_json(cache_dir / "NVDA.json", {
                "symbol": "NVDA",
                "fetched_at": timeutil.now_iso(),
                "chain": _chain("yahoo"),
            })
            with mock.patch.object(option_market, "FALLBACK_CACHE_TTL_SECONDS", -1), \
                    mock.patch.object(
                        option_market, "fetch_option_chain", return_value=_chain("ibkr"),
                    ) as fetch:
                out = option_market.cached_option_chain("NVDA", cache_dir=cache_dir)
        self.assertEqual(out["source"], "ibkr")
        fetch.assert_called_once_with("NVDA", target_dte=37)

    def test_stale_ibkr_quotes_refresh_without_rebuilding_contracts(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            chain = _chain("ibkr")
            chain["quote_timestamp"] = "2000-01-01T00:00:00+00:00"
            now = timeutil.now_iso()
            store.write_json(cache_dir / "NVDA.json", {
                "symbol": "NVDA",
                "fetched_at": now,
                "reference_fetched_at": now,
                "chain": chain,
            })
            refreshed = {**chain, "quote_timestamp": now}
            with mock.patch.object(option_market, "session_ready", return_value=True), \
                    mock.patch.object(
                        ibkr_trade,
                        "refresh_option_chain_quotes",
                        return_value=refreshed,
                    ) as refresh, \
                    mock.patch.object(option_market, "fetch_option_chain") as fetch:
                out = option_market.cached_option_chain("NVDA", cache_dir=cache_dir)
        self.assertEqual(out["quote_timestamp"], now)
        refresh.assert_called_once()
        fetch.assert_not_called()

    def test_force_refresh_rebuilds_despite_fresh_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            now = timeutil.now_iso()
            stale = {
                "source": "ibkr",
                "underlying_price": 100.0,
                "quote_timestamp": now,
                "expiries": [{
                    "expiry": "2026-08-21",
                    "calls": [],
                    "puts": [{"strike": 93.0, "conid": 1}],
                }],
            }
            store.write_json(cache_dir / "NVDA-route-p-monthly.json", {
                "symbol": "NVDA",
                "fetched_at": now,
                "reference_fetched_at": now,
                "chain": stale,
            })
            rebuilt = {
                **stale,
                "expiries": [{
                    "expiry": "2026-08-21",
                    "calls": [],
                    "puts": [{"strike": 90.0, "conid": 9}],
                }],
            }
            with mock.patch.object(option_market, "session_ready", return_value=True), \
                    mock.patch.object(
                        option_market, "fetch_option_chain", return_value=rebuilt,
                    ) as fetch:
                out = option_market.cached_option_chain(
                    "NVDA", cache_dir=cache_dir, right="P", force_refresh=True,
                )
        self.assertEqual(out["expiries"][0]["puts"][0]["strike"], 90.0)
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args.kwargs.get("target_dte"), 37)

    def test_nearest_mode_uses_separate_cache_and_target_dte(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            with mock.patch.object(option_market, "session_ready", return_value=True), \
                    mock.patch.object(
                        option_market, "fetch_option_chain", return_value=_chain("ibkr"),
                    ) as fetch:
                option_market.cached_option_chain(
                    "NVDA",
                    cache_dir=cache_dir,
                    right="P",
                    force_refresh=True,
                    expiry_mode="nearest",
                )
            self.assertTrue((cache_dir / "NVDA-route-p-nearest.json").exists())
            self.assertFalse((cache_dir / "NVDA-route-p-monthly.json").exists())
            self.assertEqual(fetch.call_args.kwargs.get("target_dte"), 7)
            self.assertEqual(
                fetch.call_args.kwargs.get("strikes_per_side"),
                option_market.ROUTE_CHAIN_STRIKES_NEAREST,
            )

    def test_force_refresh_falls_back_to_cache_when_live_fetch_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            now = timeutil.now_iso()
            cached = {
                "source": "ibkr",
                "underlying_price": 100.0,
                "quote_timestamp": now,
                "expiries": [{
                    "expiry": "2026-08-21",
                    "calls": [],
                    "puts": [{"strike": 93.0, "conid": 1}],
                }],
            }
            store.write_json(cache_dir / "NVDA-route-p-monthly.json", {
                "symbol": "NVDA",
                "fetched_at": now,
                "reference_fetched_at": now,
                "chain": cached,
            })
            with mock.patch.object(option_market, "session_ready", return_value=True), \
                    mock.patch.object(
                        option_market, "fetch_option_chain",
                        side_effect=RuntimeError("gateway down"),
                    ) as fetch, \
                    mock.patch.object(
                        ibkr_trade, "refresh_option_chain_quotes", return_value=cached,
                    ):
                out = option_market.cached_option_chain(
                    "NVDA", cache_dir=cache_dir, right="P", force_refresh=True,
                )
        self.assertEqual(out["expiries"][0]["puts"][0]["strike"], 93.0)
        fetch.assert_called_once()

    def test_connected_refuses_non_ibkr_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            store.write_json(cache_dir / "ENTG-route-p-nearest.json", {
                "symbol": "ENTG",
                "fetched_at": timeutil.now_iso(),
                "chain": _chain("alpaca"),
            })
            with mock.patch.object(option_market, "session_ready", return_value=True), \
                    mock.patch.object(
                        option_market, "fetch_option_chain", return_value=None,
                    ) as fetch:
                out = option_market.cached_option_chain(
                    "ENTG",
                    cache_dir=cache_dir,
                    right="P",
                    force_refresh=True,
                    expiry_mode="nearest",
                )
        self.assertIsNone(out)
        fetch.assert_called_once()

    def test_incoherent_ibkr_put_cache_is_rebuilt(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            now = timeutil.now_iso()
            store.write_json(cache_dir / "ADI-route-p-monthly.json", {
                "symbol": "ADI",
                "fetched_at": now,
                "reference_fetched_at": now,
                "chain": {
                    "source": "ibkr",
                    "underlying_price": 380.7,
                    "quote_timestamp": now,
                    "expiries": [{
                        "expiry": "2026-08-21",
                        "calls": [],
                        "puts": [{"strike": 570.0, "conid": 1}],
                    }],
                },
            })
            rebuilt = {
                "source": "ibkr",
                "underlying_price": 380.7,
                "quote_timestamp": now,
                "expiries": [{
                    "expiry": "2026-08-21",
                    "calls": [],
                    "puts": [{"strike": 360.0, "conid": 9}],
                }],
            }
            with mock.patch.object(
                option_market, "fetch_option_chain", return_value=rebuilt,
            ) as fetch:
                out = option_market.cached_option_chain(
                    "ADI", cache_dir=cache_dir, right="P",
                )
        self.assertEqual(out["expiries"][0]["puts"][0]["strike"], 360.0)
        fetch.assert_called_once()

    def test_forced_refresh_bypasses_fresh_quote_ttl_for_entire_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            chain = _chain("ibkr")
            now = timeutil.now_iso()
            chain["quote_timestamp"] = now
            store.write_json(cache_dir / "NVDA.json", {
                "symbol": "NVDA",
                "fetched_at": now,
                "reference_fetched_at": now,
                "chain": chain,
            })
            refreshed = {**chain, "quote_timestamp": "2026-07-11T20:00:00+00:00"}
            with mock.patch.object(option_market, "session_ready", return_value=True), \
                    mock.patch.object(
                        ibkr_trade,
                        "refresh_option_chain_quotes",
                        return_value=refreshed,
                    ) as refresh, \
                    mock.patch.object(option_market, "fetch_option_chain") as fetch:
                out = option_market.cached_option_chain(
                    "NVDA",
                    cache_dir=cache_dir,
                    force_quotes=True,
                )
        self.assertEqual(out["quote_timestamp"], refreshed["quote_timestamp"])
        refresh.assert_called_once()
        fetch.assert_not_called()

    def test_risk_free_rate_uses_fresh_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rate.json"
            store.write_json(path, {"rate": 0.042, "fetched_at": timeutil.now_iso()})
            self.assertEqual(
                option_market.cached_risk_free_rate(cache_path=path),
                0.042,
            )

    def test_risk_free_rate_fetches_once_then_uses_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rate.json"
            with mock.patch.object(fred, "series_snapshot", return_value={"value": 4.3}) as series, \
                    mock.patch.object(options_math, "risk_free_rate", return_value=0.043) as rate:
                first = option_market.cached_risk_free_rate(cache_path=path)
                second = option_market.cached_risk_free_rate(cache_path=path)
        self.assertEqual((first, second), (0.043, 0.043))
        series.assert_called_once_with("DGS10")
        rate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
