"""Allocation-segment home partition: resolve, backfill, validate, conflict."""

from __future__ import annotations

import unittest

import _support  # noqa: F401
import segment_home as sh


class ResolveHome(unittest.TestCase):
    def test_membership_beats_provenance_and_tag(self):
        model = {
            "sleeves": {"analog": {"low": 2, "high": 4, "members": ["TXN"]}},
            "targets": {"TXN": {"low": 1, "high": 2, "sleeve": "other"}},
            "provenance": {"TXN": {"home_segment": "other"}},
        }
        self.assertEqual(sh.resolve_home(model, "TXN"), "analog")

    def test_provenance_used_for_standalone(self):
        model = {
            "sleeves": {"analog": {"low": 2, "high": 4, "members": ["ADI"]}},
            "targets": {"NVDA": {"low": 8, "high": 10, "rule": "accumulate"}},
            "provenance": {"NVDA": {"home_segment": "analog"}},
        }
        self.assertEqual(sh.resolve_home(model, "NVDA"), "analog")

    def test_compatible_tag_when_no_provenance(self):
        model = {
            "sleeves": {"analog": {"low": 2, "high": 4, "members": []}},
            "targets": {"TXN": {"low": 1, "high": 2, "sleeve": "analog"}},
        }
        self.assertEqual(sh.resolve_home(model, "TXN"), "analog")

    def test_unknown_tag_is_not_home(self):
        model = {
            "sleeves": {"analog": {"low": 2, "high": 4, "members": []}},
            "targets": {"TXN": {"low": 1, "high": 2, "sleeve": "semis-compute"}},
        }
        self.assertIsNone(sh.resolve_home(model, "TXN"))


class EnsureHomes(unittest.TestCase):
    def test_backfills_from_membership(self):
        model = {
            "sleeves": {"analog": {"low": 2, "high": 4, "members": ["TXN", "ADI"]}},
            "targets": {},
            "provenance": {},
        }
        homes = sh.ensure_homes(model)
        self.assertEqual(homes["TXN"], "analog")
        self.assertEqual(model["provenance"]["TXN"]["home_segment"], "analog")
        self.assertEqual(model["provenance"]["ADI"]["home_segment"], "analog")

    def test_does_not_overwrite_existing_home(self):
        model = {
            "sleeves": {"analog": {"low": 2, "high": 4, "members": ["TXN"]}},
            "targets": {},
            "provenance": {"TXN": {"home_segment": "analog", "source": "user"}},
        }
        sh.ensure_homes(model)
        self.assertEqual(model["provenance"]["TXN"]["source"], "user")


class ValidateHomes(unittest.TestCase):
    def test_mismatch_is_error(self):
        model = {
            "sleeves": {"analog": {"low": 2, "high": 4, "members": ["TXN"]}},
            "targets": {},
            "provenance": {"TXN": {"home_segment": "semis-etf"}},
        }
        errs = [f for f in sh.validate_homes(model) if f[0] == "ERROR"]
        self.assertTrue(any("disagrees" in m for _, _, m in errs))

    def test_orphan_is_warn(self):
        model = {
            "sleeves": {"analog": {"low": 2, "high": 4, "members": []}},
            "targets": {"NVDA": {"low": 8, "high": 10, "rule": "accumulate"}},
            "provenance": {},
        }
        warns = [f for f in sh.validate_homes(model) if f[0] == "WARN"]
        self.assertTrue(any(a == "home:NVDA" for _, a, _ in warns))

    def test_unknown_declared_home_is_error(self):
        model = {
            "sleeves": {"analog": {"low": 2, "high": 4, "members": []}},
            "targets": {"NVDA": {"low": 8, "high": 10, "rule": "accumulate"}},
            "provenance": {"NVDA": {"home_segment": "nope"}},
        }
        errs = [f for f in sh.validate_homes(model) if f[0] == "ERROR"]
        self.assertTrue(any("not an allocation sleeve" in m for _, _, m in errs))


class Conflict(unittest.TestCase):
    def test_blocks_rehome_without_flag(self):
        model = {
            "sleeves": {"analog": {"low": 2, "high": 4, "members": ["TXN"]}},
            "targets": {},
        }
        reason = sh.conflict_reason(model, "TXN", "semis-etf")
        self.assertIsNotNone(reason)
        self.assertIn("conflict", reason or "")

    def test_allow_rehome(self):
        model = {
            "sleeves": {"analog": {"low": 2, "high": 4, "members": ["TXN"]}},
            "targets": {},
        }
        self.assertIsNone(sh.conflict_reason(
            model, "TXN", "semis-etf", allow_rehome=True))


if __name__ == "__main__":
    unittest.main()
