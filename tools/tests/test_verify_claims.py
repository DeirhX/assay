"""Tests for the offline claim verifier: the arithmetic identity, snapshot
divergence, staleness, and the disproven-figure regression guard. All pure --
no data submodule or network needed."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

import _support  # noqa: F401
import verify_claims as vc


def severities(findings):
    return [f.severity for f in findings]


class Representative(unittest.TestCase):
    def test_point_value(self):
        self.assertEqual(vc.representative({"value": 12.5}), 12.5)

    def test_range_midpoint(self):
        self.assertEqual(vc.representative({"low": 10, "high": 20}), 15.0)

    def test_garbage_returns_none(self):
        self.assertIsNone(vc.representative(None))
        self.assertIsNone(vc.representative("12"))
        self.assertIsNone(vc.representative({"display": "n/a"}))


class CheckSymbol(unittest.TestCase):
    def test_consistent_claim_is_clean(self):
        claim = {
            "price_usd": {"value": 100.0},
            "shares_out_b": 2.0,
            "market_cap_usd_b": {"value": 200.0},
        }
        self.assertEqual(vc.check_symbol("AAA", claim, mark=100.0), [])

    def test_identity_violation_is_error(self):
        # 100 x 2B = 200B, claim says 300B -> arithmetic never possible.
        claim = {
            "price_usd": {"value": 100.0},
            "shares_out_b": 2.0,
            "market_cap_usd_b": {"value": 300.0},
        }
        findings = vc.check_symbol("AAA", claim, mark=None)
        self.assertEqual(severities(findings), ["ERROR"])
        self.assertIn("price x shares != market cap", findings[0].message)

    def test_price_divergence_from_mark_is_warn(self):
        claim = {"price_usd": {"value": 100.0}}
        findings = vc.check_symbol("AAA", claim, mark=110.0)
        self.assertEqual(severities(findings), ["WARN"])

    def test_mark_outside_claimed_range_is_info(self):
        # Range midpoint 100 is within price tolerance of mark 102, but the
        # mark sits outside the claimed band -> advisory only.
        claim = {"price_usd": {"low": 98.0, "high": 101.0}}
        findings = vc.check_symbol("AAA", claim, mark=102.0)
        self.assertEqual(severities(findings), ["INFO"])

    def test_disproven_cap_staying_bogus_is_info(self):
        claim = {
            "price_usd": {"value": 100.0},
            "shares_out_b": 2.0,
            "disproven_market_cap_usd_b": [350.0],
        }
        findings = vc.check_symbol("AAA", claim, mark=None)
        self.assertEqual(severities(findings), ["INFO"])
        self.assertIn("confirmed bogus", findings[0].message)

    def test_disproven_cap_passing_identity_is_error(self):
        # The figure documented as wrong now matches price x shares: the
        # claims drifted and the regression guard must scream.
        claim = {
            "price_usd": {"value": 100.0},
            "shares_out_b": 2.0,
            "disproven_market_cap_usd_b": [200.0],
        }
        findings = vc.check_symbol("AAA", claim, mark=None)
        self.assertEqual(severities(findings), ["ERROR"])
        self.assertIn("drifted", findings[0].message)

    def test_non_positive_multiple_is_error(self):
        claim = {"pe_ttm": {"value": -4.0}}
        findings = vc.check_symbol("AAA", claim, mark=None)
        self.assertEqual(severities(findings), ["ERROR"])
        self.assertIn("pe_ttm", findings[0].message)

    def test_missing_fields_produce_nothing(self):
        self.assertEqual(vc.check_symbol("AAA", {}, mark=None), [])


class SnapshotAge(unittest.TestCase):
    @staticmethod
    def _iso(days_ago: float) -> str:
        return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()

    def test_fresh_snapshot_is_clean(self):
        self.assertEqual(vc.check_snapshot_age({"generated_at": self._iso(1)}), [])

    def test_aging_snapshot_is_warn(self):
        findings = vc.check_snapshot_age({"generated_at": self._iso(vc.STALE_WARN_DAYS + 1)})
        self.assertEqual(severities(findings), ["WARN"])

    def test_ancient_snapshot_is_error(self):
        findings = vc.check_snapshot_age({"generated_at": self._iso(vc.STALE_ERROR_DAYS + 1)})
        self.assertEqual(severities(findings), ["ERROR"])

    def test_unparseable_timestamp_is_warn(self):
        findings = vc.check_snapshot_age({"generated_at": "yesterday-ish"})
        self.assertEqual(severities(findings), ["WARN"])

    def test_missing_timestamp_is_silent(self):
        self.assertEqual(vc.check_snapshot_age({}), [])

    def test_naive_timestamp_is_treated_as_utc(self):
        naive = (datetime.now(timezone.utc) - timedelta(days=1)).replace(tzinfo=None)
        self.assertEqual(vc.check_snapshot_age({"generated_at": naive.isoformat()}), [])


if __name__ == "__main__":
    unittest.main()
