"""Tests for the Alpaca options provider (HTTP mocked, no network touched)."""

from __future__ import annotations

import datetime as dt
import unittest
from unittest import mock

import _support  # noqa: F401  (sys.path side effect)
from providers import alpaca

# One expiry's worth of AAPL contracts: a liquid call with greeks, a further-OTM
# call missing greeks/IV, and a put.
_SNAPSHOTS = {
    "AAPL260821C00320000": {
        "latestQuote": {"bp": 6.0, "ap": 6.4, "bs": 10, "as": 12},
        "latestTrade": {"p": 6.2},
        "greeks": {"delta": 0.38, "gamma": 0.02, "theta": -0.05, "vega": 0.1},
        "impliedVolatility": 0.29,
        "dailyBar": {"o": 6.0, "h": 6.5, "l": 5.8, "c": 6.2, "v": 350},
    },
    "AAPL260821C00340000": {
        "latestQuote": {"bp": 2.0, "ap": 2.3},
        "latestTrade": {"p": 2.1},
        "greeks": {},                    # no greeks -> delta None
        "dailyBar": {"v": 40},           # no impliedVolatility key
    },
    "AAPL260821P00300000": {
        "latestQuote": {"bp": 3.0, "ap": 3.4},
        "latestTrade": {"p": 3.2},
        "greeks": {"delta": -0.30},
        "impliedVolatility": 0.31,
        "dailyBar": {"v": 90},
    },
}


def _fake_get_json(urls: list[str]):
    """Route the stock latest-trade vs the options-snapshots call, recording every
    URL so the test can assert the windowing query params."""
    def _inner(url: str, **_kw: object):
        urls.append(url)
        if "/v2/stocks/" in url:
            return {"trade": {"p": 316.0}}
        if "/v1beta1/options/snapshots/" in url:
            return {"snapshots": _SNAPSHOTS, "next_page_token": None}
        raise AssertionError(f"unexpected url {url}")
    return _inner


class OccParsing(unittest.TestCase):
    def test_parse_well_formed(self):
        self.assertEqual(alpaca._parse_occ("AAPL260821C00320000"), ("2026-08-21", "C", 320.0))
        self.assertEqual(alpaca._parse_occ("SPY260101P00450500"), ("2026-01-01", "P", 450.5))

    def test_parse_rejects_garbage(self):
        self.assertIsNone(alpaca._parse_occ("NOTASYMBOL"))
        self.assertIsNone(alpaca._parse_occ(""))
        self.assertIsNone(alpaca._parse_occ("AAPL269921C00320000"))  # month 99


class Enablement(unittest.TestCase):
    def test_disabled_without_keys(self):
        with mock.patch.object(alpaca, "api_keys", return_value=(None, None)):
            self.assertFalse(alpaca.enabled())
            self.assertIsNone(alpaca.option_chain("AAPL"))

    def test_default_feed_is_indicative(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("ALPACA_OPTIONS_FEED", None)
            # config may still resolve nothing -> the documented default.
            self.assertIn(alpaca._feed(), ("indicative", "opra"))


class OptionChain(unittest.TestCase):
    def setUp(self):
        self.keys = mock.patch.object(alpaca, "api_keys", return_value=("kid", "sec"))
        self.keys.start()
        self.addCleanup(self.keys.stop)

    def _chain(self, urls):
        with mock.patch.object(alpaca, "get_json", side_effect=_fake_get_json(urls)):
            return alpaca.option_chain("AAPL", max_expiries=2, strike_window_pct=0.1,
                                       as_of=dt.date(2026, 7, 1))

    def test_builds_yahoo_shaped_chain(self):
        urls: list[str] = []
        chain = self._chain(urls)
        assert chain is not None
        self.assertEqual(chain["source"], "alpaca")
        self.assertEqual(chain["currency"], "USD")
        self.assertEqual(chain["underlying_price"], 316.0)
        self.assertEqual(len(chain["expiries"]), 1)
        exp = chain["expiries"][0]
        self.assertEqual(exp["expiry"], "2026-08-21")
        self.assertEqual([c["strike"] for c in exp["calls"]], [320.0, 340.0])
        self.assertEqual([p["strike"] for p in exp["puts"]], [300.0])

    def test_contract_fields_and_missing_greeks(self):
        chain = self._chain([])
        calls = {c["strike"]: c for c in chain["expiries"][0]["calls"]}
        liquid = calls[320.0]
        self.assertEqual(liquid["bid"], 6.0)
        self.assertEqual(liquid["ask"], 6.4)
        self.assertEqual(liquid["last"], 6.2)
        self.assertEqual(liquid["implied_vol"], 0.29)
        self.assertEqual(liquid["delta"], 0.38)
        self.assertEqual(liquid["volume"], 350)
        self.assertIsNone(liquid["open_interest"])   # never on the data feed
        thin = calls[340.0]
        self.assertIsNone(thin["delta"])             # greeks absent -> None
        self.assertIsNone(thin["implied_vol"])
        self.assertEqual(thin["volume"], 40)

    def test_windowing_query_params(self):
        urls: list[str] = []
        self._chain(urls)
        opt_url = next(u for u in urls if "/options/snapshots/" in u)
        self.assertIn("feed=indicative", opt_url)
        self.assertIn("expiration_date_gte=2026-07-01", opt_url)
        self.assertIn("strike_price_gte=", opt_url)   # spot resolved -> window applied
        self.assertIn("strike_price_lte=", opt_url)

    def test_empty_snapshots_returns_none(self):
        def _empty(url, **_kw):
            if "/v2/stocks/" in url:
                return {"trade": {"p": 316.0}}
            return {"snapshots": {}, "next_page_token": None}
        with mock.patch.object(alpaca, "get_json", side_effect=_empty):
            self.assertIsNone(alpaca.option_chain("AAPL", as_of=dt.date(2026, 7, 1)))


if __name__ == "__main__":
    unittest.main()
