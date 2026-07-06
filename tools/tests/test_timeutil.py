#!/usr/bin/env python3
"""Tests for the shared UTC timestamp helpers.

The whole point of extracting timeutil was that four modules parsed ISO stamps
with subtly different tolerance for a trailing ``Z`` and for naive stamps. These
tests pin down the union of behaviours the callers relied on so a future edit
can't quietly reintroduce the divergence.
"""
import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import timeutil

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 1, 5, 12, 0, 0, tzinfo=UTC)


class ParseIsoUtc(unittest.TestCase):
    def test_offset_form(self):
        got = timeutil.parse_iso_utc("2026-01-05T10:00:00+00:00")
        self.assertEqual(got, dt.datetime(2026, 1, 5, 10, 0, tzinfo=UTC))

    def test_z_suffix_is_tolerated(self):
        # The exact trap risk._fresh / exit_plan._cache_fresh had: a bare Z used
        # to raise in <3.11 and read as "not fresh".
        got = timeutil.parse_iso_utc("2026-01-05T10:00:00Z")
        self.assertEqual(got, dt.datetime(2026, 1, 5, 10, 0, tzinfo=UTC))

    def test_naive_stamp_promoted_to_utc(self):
        got = timeutil.parse_iso_utc("2026-01-05T10:00:00")
        self.assertIsNotNone(got)
        self.assertEqual(got.tzinfo, UTC)

    def test_unparsable_and_empty_return_none(self):
        self.assertIsNone(timeutil.parse_iso_utc("not-a-date"))
        self.assertIsNone(timeutil.parse_iso_utc(""))
        self.assertIsNone(timeutil.parse_iso_utc(None))


class Ages(unittest.TestCase):
    def test_age_seconds(self):
        stamp = (NOW - dt.timedelta(hours=1)).isoformat()
        self.assertEqual(timeutil.age_seconds(stamp, now=NOW), 3600.0)

    def test_future_stamp_clamps_to_zero(self):
        stamp = (NOW + dt.timedelta(hours=5)).isoformat()
        self.assertEqual(timeutil.age_seconds(stamp, now=NOW), 0.0)

    def test_age_days(self):
        stamp = (NOW - dt.timedelta(days=3, hours=5)).isoformat()
        self.assertEqual(timeutil.age_days(stamp, now=NOW), 3)

    def test_age_of_unparsable_is_none(self):
        self.assertIsNone(timeutil.age_seconds("nope", now=NOW))
        self.assertIsNone(timeutil.age_days(None, now=NOW))


class CacheFresh(unittest.TestCase):
    def test_within_ttl_is_fresh(self):
        stamp = (NOW - dt.timedelta(seconds=100)).isoformat()
        self.assertTrue(timeutil.cache_fresh(stamp, 200, now=NOW))

    def test_at_or_past_ttl_is_stale(self):
        stamp = (NOW - dt.timedelta(seconds=200)).isoformat()
        self.assertFalse(timeutil.cache_fresh(stamp, 200, now=NOW))

    def test_future_stamp_is_not_fresh(self):
        stamp = (NOW + dt.timedelta(seconds=50)).isoformat()
        self.assertFalse(timeutil.cache_fresh(stamp, 200, now=NOW))

    def test_unparsable_is_not_fresh(self):
        self.assertFalse(timeutil.cache_fresh("bad", 200, now=NOW))
        self.assertFalse(timeutil.cache_fresh(None, 200, now=NOW))


if __name__ == "__main__":
    unittest.main()
