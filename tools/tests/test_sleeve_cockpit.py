"""Allocation-sleeve cockpit: members, bands, advisory OC ranks."""

from __future__ import annotations

import unittest

import _support  # noqa: F401
import sleeve_cockpit as sc


class SleeveCockpit(unittest.TestCase):
    def setUp(self):
        self.model = {
            "as_of": "2026-07-19",
            "cash_target_pct": 5,
            "sleeves": {
                "analog": {
                    "low": 3, "high": 5, "rule": "accumulate",
                    "members": ["TXN", "ADI"],
                    "member_caps": {"TXN": 3.0},
                },
                "nuclear": {
                    "low": 1, "high": 3, "rule": "hold",
                    "members": ["CCJ"],
                },
            },
            "targets": {},
            "provenance": {
                "TXN": {"conviction": "high"},
                "ADI": {"conviction": "medium"},
                "CCJ": {"conviction": "low"},
            },
        }
        self.holdings = {
            "positions": [
                {"symbol": "TXN", "base_market_value": 4.0},
                {"symbol": "ADI", "base_market_value": 1.0},
                {"symbol": "CCJ", "base_market_value": 2.0},
                {"symbol": "REST", "base_market_value": 93.0},
            ],
            "net_asset_value": 105.0,
        }

    def test_index_lists_sleeves(self):
        idx = sc.index(model=self.model, holdings=self.holdings)
        names = {s["name"] for s in idx["sleeves"]}
        self.assertEqual(names, {"analog", "nuclear"})
        analog = next(s for s in idx["sleeves"] if s["name"] == "analog")
        self.assertEqual(analog["member_count"], 2)
        self.assertAlmostEqual(analog["current_pct"], 5.0, places=1)
        self.assertEqual(analog["status"], "IN")  # held 5% inside band 3–5

    def test_detail_ranks_members(self):
        d = sc.detail("analog", model=self.model, holdings=self.holdings)
        self.assertIsNotNone(d)
        assert d is not None
        self.assertEqual(d["name"], "analog")
        self.assertEqual(d["sleeve"]["rule"], "accumulate")
        syms = [m["symbol"] for m in d["members"]]
        self.assertEqual(set(syms), {"TXN", "ADI"})
        # High conviction beats medium → TXN should rank first when scored.
        txn = next(m for m in d["members"] if m["symbol"] == "TXN")
        adi = next(m for m in d["members"] if m["symbol"] == "ADI")
        self.assertEqual(txn["oc_rank"], 1)
        self.assertEqual(adi["oc_rank"], 2)
        self.assertEqual(txn["home_segment"], "analog")
        self.assertEqual(txn["cap"], 3.0)

    def test_detail_unknown_returns_none(self):
        self.assertIsNone(sc.detail("nope", model=self.model, holdings=self.holdings))


if __name__ == "__main__":
    unittest.main()
