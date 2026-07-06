"""Tests for trade_service pure helpers. The gateway-touching entry points are
gated and exercised elsewhere; here we pin the per-order band context that feeds
the preview's "effect on band" tracks, since a wrong before/after weight at the
confirmation moment is worse than none."""

from __future__ import annotations

import unittest

import _support  # noqa: F401
import rebalance
import trade_service


def _holdings():
    return {
        "net_asset_value": 1100.0,
        "base_currency": "CZK",
        "generated_at": "2026-06-13T10:00:00+00:00",
        "positions": [
            {"symbol": "AMD", "base_market_value": 200.0},   # 20% of 1000 invested
            {"symbol": "REST", "base_market_value": 800.0},   # 80%
        ],
    }


MODEL = {
    "as_of": "2026-06-13",
    "targets": {
        "AMD": {"low": 10, "high": 12, "rule": "trim_only"},
        "REST": {"low": 70, "high": 85, "rule": "hold"},
    },
}


class OrderBandContext(unittest.TestCase):
    def test_merges_before_and_after_weights_per_target(self):
        holdings = _holdings()
        # After trimming AMD by 100 CZK the invested book shrinks to 900; AMD is
        # 100/900 = 11.1% (in band), matching the what-if's own recompute.
        after_plan = rebalance.plan(MODEL, {
            **holdings,
            "positions": [
                {"symbol": "AMD", "base_market_value": 100.0},
                {"symbol": "REST", "base_market_value": 800.0},
            ],
        })
        ctx = trade_service._order_band_context(MODEL, holdings, after_plan)
        self.assertIn("AMD", ctx)
        amd = ctx["AMD"]
        self.assertEqual((amd["low"], amd["high"]), (10.0, 12.0))
        self.assertAlmostEqual(amd["before_pct"], 20.0, places=1)   # 200/1000
        self.assertAlmostEqual(amd["after_pct"], 11.11, places=1)   # 100/900
        self.assertEqual(amd["status_after"], "IN")

    def test_empty_without_model_or_after_plan(self):
        self.assertEqual(trade_service._order_band_context({}, _holdings(), {"rows": []}), {})
        self.assertEqual(trade_service._order_band_context(MODEL, {}, {"rows": []}), {})
        self.assertEqual(trade_service._order_band_context(MODEL, _holdings(), None), {})

    def test_skips_non_target_rows(self):
        holdings = _holdings()
        after_plan = {"rows": [
            {"kind": "sleeve", "name": "[Growth]", "current_pct": 5.0, "low": 3, "high": 8, "status": "IN"},
            {"kind": "untargeted", "symbol": "ZZZ", "current_pct": 1.0},
        ]}
        self.assertEqual(trade_service._order_band_context(MODEL, holdings, after_plan), {})


if __name__ == "__main__":
    unittest.main()
