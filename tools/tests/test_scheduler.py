#!/usr/bin/env python3
"""Scheduler tests: everything that decides *whether* to run is pure and injected
-- no thread, no network, no sleep. The tick loop is exercised with fake tasks to
pin dispatch order, the enabled filter, and error isolation."""
import datetime as dt
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import overview
import scheduler

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 1, 5, 16, 0, tzinfo=UTC)   # a Monday, 16:00 UTC


def obs(**over):
    base = dict(now=NOW, is_running=lambda _k: False)
    base.update(over)
    return scheduler.Obs(**base)


class ShouldResync(unittest.TestCase):
    def test_fires_on_a_stale_snapshot(self):
        o = obs(ibkr_configured=True, snapshot_age_days=overview.STALE_SNAPSHOT_DAYS + 1)
        self.assertTrue(scheduler._should_resync(None, o))

    def test_skips_a_fresh_snapshot(self):
        o = obs(ibkr_configured=True, snapshot_age_days=1)
        self.assertFalse(scheduler._should_resync(None, o))

    def test_skips_when_creds_missing(self):
        o = obs(ibkr_configured=False, snapshot_age_days=99)
        self.assertFalse(scheduler._should_resync(None, o))

    def test_missing_snapshot_is_not_treated_as_stale(self):
        # age None (never synced) -> we don't auto-pull the first snapshot.
        o = obs(ibkr_configured=True, snapshot_age_days=None)
        self.assertFalse(scheduler._should_resync(None, o))

    def test_skips_while_a_sync_is_running(self):
        o = obs(ibkr_configured=True, snapshot_age_days=99, is_running=lambda k: k == "ibkr_sync")
        self.assertFalse(scheduler._should_resync(None, o))

    def test_honours_the_24h_throttle(self):
        o = obs(ibkr_configured=True, snapshot_age_days=99)
        recent = NOW - dt.timedelta(hours=5)
        self.assertFalse(scheduler._should_resync(recent, o))
        old = NOW - dt.timedelta(hours=25)
        self.assertTrue(scheduler._should_resync(old, o))


class ShouldHistory(unittest.TestCase):
    def test_fires_when_configured_and_due(self):
        self.assertTrue(scheduler._should_history(None, obs(ibkr_history_configured=True)))

    def test_skips_without_history_creds(self):
        self.assertFalse(scheduler._should_history(None, obs(ibkr_history_configured=False)))

    def test_weekly_throttle(self):
        o = obs(ibkr_history_configured=True)
        self.assertFalse(scheduler._should_history(NOW - dt.timedelta(days=3), o))
        self.assertTrue(scheduler._should_history(NOW - dt.timedelta(days=8), o))


class ShouldSegments(unittest.TestCase):
    def test_fires_when_a_stale_segment_exists(self):
        self.assertTrue(scheduler._should_segments(None, obs(stale_segments=["ai-infra"])))

    def test_skips_when_none_stale(self):
        self.assertFalse(scheduler._should_segments(None, obs(stale_segments=[])))

    def test_daily_throttle(self):
        o = obs(stale_segments=["ai-infra"])
        self.assertFalse(scheduler._should_segments(NOW - dt.timedelta(hours=5), o))
        self.assertTrue(scheduler._should_segments(NOW - dt.timedelta(hours=25), o))


class ShouldQuotes(unittest.TestCase):
    def test_fires_in_window_with_gated_names(self):
        self.assertTrue(scheduler._should_quotes(None, obs(gated_symbols=["NVDA"], market_open=True)))

    def test_skips_outside_the_market_window(self):
        self.assertFalse(scheduler._should_quotes(None, obs(gated_symbols=["NVDA"], market_open=False)))

    def test_skips_without_gated_names(self):
        self.assertFalse(scheduler._should_quotes(None, obs(gated_symbols=[], market_open=True)))

    def test_hourly_throttle(self):
        o = obs(gated_symbols=["NVDA"], market_open=True)
        self.assertFalse(scheduler._should_quotes(NOW - dt.timedelta(minutes=30), o))
        self.assertTrue(scheduler._should_quotes(NOW - dt.timedelta(minutes=61), o))


class MarketWindow(unittest.TestCase):
    def test_parse_gate_hours(self):
        self.assertEqual(scheduler.parse_gate_hours("15:00-22:30"), (900, 1350))

    def test_parse_gate_hours_falls_back_on_garbage(self):
        self.assertEqual(scheduler.parse_gate_hours("not-a-window"), (900, 1350))

    def test_window_open_weekday_and_time(self):
        self.assertTrue(scheduler._window_open(0, 900, "15:00-22:30"))     # Mon 15:00
        self.assertTrue(scheduler._window_open(4, 1350, "15:00-22:30"))    # Fri 22:30 edge
        self.assertFalse(scheduler._window_open(0, 899, "15:00-22:30"))    # one min early
        self.assertFalse(scheduler._window_open(5, 1000, "15:00-22:30"))   # Saturday
        self.assertFalse(scheduler._window_open(6, 1000, "15:00-22:30"))   # Sunday


class NegativeQuoteSuppression(unittest.TestCase):
    def test_recent_error_is_suppressed(self):
        entry = {"price": None, "error": "no data", "at": (NOW - dt.timedelta(hours=2)).isoformat()}
        self.assertTrue(scheduler._neg_suppressed(entry, NOW))

    def test_old_error_is_retried(self):
        entry = {"price": None, "error": "no data", "at": (NOW - dt.timedelta(hours=48)).isoformat()}
        self.assertFalse(scheduler._neg_suppressed(entry, NOW))

    def test_good_quote_is_not_suppressed(self):
        entry = {"price": 100.0, "at": NOW.isoformat()}
        self.assertFalse(scheduler._neg_suppressed(entry, NOW))


class StatePersistence(unittest.TestCase):
    def setUp(self):
        self._orig = scheduler.STATE_FILE
        self._tmp = Path(__file__).resolve().parent / "_tmp-scheduler-state.json"
        scheduler.STATE_FILE = self._tmp

    def tearDown(self):
        scheduler.STATE_FILE = self._orig
        if self._tmp.exists():
            self._tmp.unlink()

    def test_missing_file_reads_as_never_ran(self):
        self.assertEqual(scheduler.load_state(), {})

    def test_corrupt_file_reads_as_never_ran(self):
        self._tmp.write_text("{ this is not json", encoding="utf-8")
        self.assertEqual(scheduler.load_state(), {})

    def test_round_trip(self):
        scheduler.save_state({"holdings-resync": {"last_run": NOW.isoformat(), "last_result": "ok"}})
        got = scheduler.load_state()
        self.assertEqual(got["holdings-resync"]["last_result"], "ok")


class TickLoop(unittest.TestCase):
    def setUp(self):
        self._orig_tasks = scheduler.TASKS
        self._orig_state = scheduler.STATE_FILE
        self._orig_stagger = scheduler.STAGGER_SECONDS
        self._tmp = Path(__file__).resolve().parent / "_tmp-tick-state.json"
        scheduler.STATE_FILE = self._tmp
        scheduler.STAGGER_SECONDS = 0
        os.environ["ASSAY_AUTO_REFRESH"] = "1"
        self.calls: list[str] = []

    def tearDown(self):
        scheduler.TASKS = self._orig_tasks
        scheduler.STATE_FILE = self._orig_state
        scheduler.STAGGER_SECONDS = self._orig_stagger
        os.environ.pop("ASSAY_AUTO_REFRESH", None)
        os.environ.pop("ASSAY_AUTO_SEGMENTS", None)
        if self._tmp.exists():
            self._tmp.unlink()

    def _task(self, name, *, fires=True, raises=False, flag=None):
        def run(_obs):
            self.calls.append(name)
            if raises:
                raise RuntimeError("boom")
            return f"{name} ok"
        return scheduler.Task(name, flag, lambda _lr, _o: fires, run)

    def test_dispatch_order_and_stamps(self):
        scheduler.TASKS = [self._task("a"), self._task("b")]
        fired = scheduler.tick(now=NOW, obs=obs())
        self.assertEqual(self.calls, ["a", "b"])
        self.assertEqual(fired, ["a", "b"])
        state = scheduler.load_state()
        self.assertEqual(state["a"]["last_result"], "a ok")
        self.assertEqual(state["b"]["last_run"], NOW.isoformat(timespec="seconds"))

    def test_a_raising_task_does_not_kill_the_tick(self):
        scheduler.TASKS = [self._task("a", raises=True), self._task("b")]
        fired = scheduler.tick(now=NOW, obs=obs())
        self.assertEqual(self.calls, ["a", "b"])          # b still ran
        self.assertEqual(fired, ["a", "b"])
        state = scheduler.load_state()
        self.assertTrue(state["a"]["last_result"].startswith("error:"))
        self.assertEqual(state["b"]["last_result"], "b ok")

    def test_skips_a_task_whose_predicate_is_false(self):
        scheduler.TASKS = [self._task("a", fires=False), self._task("b")]
        scheduler.tick(now=NOW, obs=obs())
        self.assertEqual(self.calls, ["b"])

    def test_master_switch_off_runs_nothing(self):
        os.environ.pop("ASSAY_AUTO_REFRESH", None)
        scheduler.TASKS = [self._task("a")]
        fired = scheduler.tick(now=NOW, obs=obs())
        self.assertEqual(fired, [])
        self.assertEqual(self.calls, [])

    def test_granular_flag_off_disables_one_task(self):
        os.environ["ASSAY_AUTO_SEGMENTS"] = "0"
        scheduler.TASKS = [self._task("a"), self._task("seg", flag="ASSAY_AUTO_SEGMENTS")]
        fired = scheduler.tick(now=NOW, obs=obs())
        self.assertEqual(fired, ["a"])


if __name__ == "__main__":
    unittest.main()
