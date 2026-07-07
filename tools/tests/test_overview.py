"""Tests for the "Today" cockpit builders: snapshot freshness, plan drift
counts (including price-gate states), research-funnel triage (unresearched
picks, stale segments, the cross-segment queue with its exclusions), and the
ordered next-step recommendation. All pure — no disk, no network.
"""

from __future__ import annotations

import datetime as dt
import unittest

import _support  # noqa: F401
import overview


NOW = dt.datetime(2026, 7, 5, 12, 0, tzinfo=dt.timezone.utc)


def _iso(days_ago: int) -> str:
    return (NOW - dt.timedelta(days=days_ago)).isoformat(timespec="seconds")


class TestAttributionSummary(unittest.TestCase):
    def test_absent_or_thin_cache_degrades_to_nudge(self):
        self.assertEqual(overview.attribution_summary(None, now=NOW), {"exists": False})
        self.assertEqual(overview.attribution_summary({"enough_data": False}, now=NOW), {"exists": False})

    def test_reshapes_cache_and_flags_freshness(self):
        cached = {"enough_data": True, "as_of": "2026-07-04", "range": "1y",
                  "benchmark": "SPY", "actual_pct": 4.0, "vs_hold_pp": -6.0,
                  "vs_benchmark_pp": -2.5, "updated_at": _iso(1)}
        got = overview.attribution_summary(cached, now=NOW)
        self.assertTrue(got["exists"])
        self.assertAlmostEqual(got["vs_hold_pp"], -6.0)
        self.assertEqual(got["age_days"], 1)
        self.assertFalse(got["stale"])

    def test_old_verdict_is_stale(self):
        cached = {"enough_data": True, "updated_at": _iso(30), "actual_pct": 1.0}
        self.assertTrue(overview.attribution_summary(cached, now=NOW)["stale"])


class TestAgeDays(unittest.TestCase):
    def test_parses_z_suffix_and_clamps_at_zero(self):
        self.assertEqual(overview.age_days(_iso(3).replace("+00:00", "Z"), NOW), 3)
        self.assertEqual(overview.age_days((NOW + dt.timedelta(hours=2)).isoformat(), NOW), 0)

    def test_garbage_reads_as_none(self):
        self.assertIsNone(overview.age_days("not-a-date", NOW))
        self.assertIsNone(overview.age_days(None, NOW))


class TestSnapshotSummary(unittest.TestCase):
    def test_fresh_snapshot(self):
        s = overview.snapshot_summary(
            {"generated_at": _iso(2), "positions": [{"symbol": "AAA"}]}, now=NOW)
        self.assertTrue(s["exists"])
        self.assertEqual(s["age_days"], 2)
        self.assertFalse(s["stale"])

    def test_stale_and_missing(self):
        s = overview.snapshot_summary(
            {"generated_at": _iso(overview.STALE_SNAPSHOT_DAYS + 1),
             "positions": [{"symbol": "AAA"}]}, now=NOW)
        self.assertTrue(s["stale"])
        self.assertFalse(overview.snapshot_summary(None, now=NOW)["exists"])
        self.assertFalse(overview.snapshot_summary({"positions": []}, now=NOW)["exists"])


class TestPlanSummary(unittest.TestCase):
    def test_counts_actions_gates_and_conflicts(self):
        plan = {
            "rows": [
                {"status": "BELOW", "action": "buy"},
                {"status": "ABOVE", "action": "trim", "research_conflict": True},
                {"status": "ABOVE", "action": "review"},
                {"status": "IN", "action": None},
                # gate satisfied + band still wants the move -> open
                {"status": "BELOW", "action": "buy", "price_gate": {"buy_below": 10}},
                # gate blocking its side -> waiting (action downgraded to wait upstream)
                {"status": "BELOW", "action": None, "price_gate": {"blocked_action": "buy"}},
            ],
            "untargeted": [{"symbol": "ZZZ"}],
            "untargeted_pct": 4.2,
        }
        p = overview.plan_summary(plan)
        self.assertEqual(p["rows"], 6)
        self.assertEqual(p["out_of_band"], 5)
        self.assertEqual((p["buy"], p["trim"], p["review"]), (2, 1, 1))
        self.assertEqual(p["actionable"], 4)
        self.assertEqual(p["conflicts"], 1)
        self.assertEqual(p["gates_open"], 1)
        self.assertEqual(p["gates_waiting"], 1)
        self.assertEqual(p["untargeted"], 1)


class TestStagedBasketAndJournal(unittest.TestCase):
    def test_basket_totals(self):
        b = overview.staged_basket_summary(
            [{"symbol": "A", "delta_czk": 1000}, {"symbol": "B", "delta_czk": -400}])
        self.assertEqual((b["count"], b["buys"], b["sells"]), (2, 1, 1))
        self.assertEqual(b["total_abs_czk"], 1400)
        self.assertEqual(overview.staged_basket_summary(None)["count"], 0)

    def test_journal_pending_and_review_due(self):
        entries = [
            {"created_at": _iso(10), "outcome": None},
            {"created_at": _iso(overview.STALE_JOURNAL_DAYS + 5), "outcome": None},
            {"created_at": _iso(200), "outcome": {"price": 10}},
        ]
        j = overview.journal_summary(entries, now=NOW)
        self.assertEqual(j["total"], 3)
        self.assertEqual(j["pending_outcomes"], 2)
        self.assertEqual(j["oldest_pending_days"], overview.STALE_JOURNAL_DAYS + 5)
        self.assertEqual(j["review_due"], 1)


class TestResearchSummary(unittest.TestCase):
    def _index(self):
        return [
            {"symbol": "RSCH", "has_analysis": True},
            {"symbol": "BARE", "has_analysis": False},
        ]

    def test_unresearched_picks_sorted_oldest_first(self):
        items = [
            {"symbol": "RSCH", "added_at": _iso(50), "tier": "want"},   # analysed: skipped
            {"symbol": "BARE", "added_at": _iso(40), "tier": "curious"},
            {"symbol": "NEWP", "added_at": _iso(2), "tier": "want"},
        ]
        r = overview.research_summary(items, self._index(), [], [], now=NOW)
        got = [p["symbol"] for p in r["basket"]["unresearched"]]
        self.assertEqual(got, ["BARE", "NEWP"])
        self.assertEqual(r["basket"]["unresearched_count"], 2)
        self.assertEqual(r["basket"]["aging_count"], 1)  # only BARE is > 30d

    def test_stale_segments(self):
        segs = [
            {"name": "fresh", "cached": True, "cached_at": _iso(3)},
            {"name": "old", "title": "Old", "cached": True,
             "cached_at": _iso(overview.STALE_SEGMENT_DAYS + 10)},
            {"name": "never", "cached": False},
        ]
        r = overview.research_summary([], [], segs, [], now=NOW)
        self.assertEqual(r["segments"]["cached"], 2)
        self.assertEqual(r["segments"]["stale_count"], 1)
        self.assertEqual(r["segments"]["stale"][0]["name"], "old")

    def test_queue_excludes_held_starred_and_analysed(self):
        records = [{
            "segment": "semis",
            "members": [
                {"symbol": "HELD", "research_score": 90, "owned_pct_nav": 2.0},
                {"symbol": "RSCH", "research_score": 85},           # analysed
                {"symbol": "BARE", "research_score": 80},           # in basket
                {"symbol": "FIND", "research_score": 70},           # genuinely new
                {"symbol": "NOSC"},                                  # no score
            ],
        }, {
            "segment": "other",
            "members": [{"symbol": "FIND", "research_score": 75}],  # dedupe: best score wins
        }]
        basket_items = [{"symbol": "BARE", "added_at": _iso(1)}]
        r = overview.research_summary(basket_items, self._index(), [], records, now=NOW)
        self.assertEqual([q["symbol"] for q in r["queue"]], ["FIND"])
        self.assertEqual(r["queue"][0]["score"], 75)
        self.assertEqual(r["queue"][0]["segment"], "other")


class TestNextStep(unittest.TestCase):
    def _payload(self, **over):
        base = {
            "snapshot": {"exists": True, "stale": False, "age_days": 1},
            "plan": {"actionable": 0, "gates_open": 0},
            "draft": {"has_draft": False, "pending": 0},
            "staged_basket": {"count": 0},
            "research": {"basket": {"unresearched_count": 0}, "queue": []},
        }
        base.update(over)
        return base

    def test_priority_order(self):
        self.assertEqual(overview.next_step(
            self._payload(snapshot={"exists": False}))["id"], "setup")
        self.assertEqual(overview.next_step(
            self._payload(snapshot={"exists": True, "stale": True, "age_days": 12}))["id"], "resync")
        self.assertEqual(overview.next_step(
            self._payload(draft={"has_draft": True, "pending": 3}))["id"], "commit-draft")
        self.assertEqual(overview.next_step(
            self._payload(staged_basket={"count": 2}))["id"], "place-basket")
        self.assertEqual(overview.next_step(
            self._payload(plan={"actionable": 2, "gates_open": 1}))["id"], "gates-open")
        self.assertEqual(overview.next_step(
            self._payload(plan={"actionable": 2, "gates_open": 0}))["id"], "rebalance")
        self.assertEqual(overview.next_step(
            self._payload(research={"basket": {"unresearched_count": 1}, "queue": []}))["id"],
            "research-picks")
        step = overview.next_step(
            self._payload(research={"basket": {"unresearched_count": 0},
                                    "queue": [{"symbol": "FIND", "score": 70}]}))
        self.assertEqual(step["id"], "research-queue")
        self.assertEqual(step["symbol"], "FIND")
        self.assertEqual(overview.next_step(self._payload())["id"], "all-clear")

    def test_draft_outranks_plan_drift(self):
        step = overview.next_step(self._payload(
            draft={"has_draft": True, "pending": 1}, plan={"actionable": 5, "gates_open": 2}))
        self.assertEqual(step["id"], "commit-draft")

    def test_ledger_drift_outranks_in_flight_decisions(self):
        # A fresh snapshot that's still behind the ledger: ground truth is wrong,
        # so it outranks a pending draft/basket but not a calendar-stale snapshot.
        step = overview.next_step(self._payload(
            drift={"stale_vs_ledger": True, "n_trades_after": 2},
            draft={"has_draft": True, "pending": 3}))
        self.assertEqual(step["id"], "drift-resync")
        self.assertIn("2 executions", step["reason"])

    def test_calendar_stale_outranks_ledger_drift(self):
        step = overview.next_step(self._payload(
            snapshot={"exists": True, "stale": True, "age_days": 12},
            drift={"stale_vs_ledger": True, "n_trades_after": 1}))
        self.assertEqual(step["id"], "resync")


class TestAutomationSummary(unittest.TestCase):
    TASKS = [
        {"name": "holdings-resync", "label": "Holdings resync", "enabled": True, "interval_s": 86400},
        {"name": "gate-quotes", "label": "Gate quotes", "enabled": False, "interval_s": 3600},
    ]

    def test_disabled_with_no_history(self):
        out = overview.automation_summary({}, self.TASKS, enabled=False, now=NOW)
        self.assertFalse(out["enabled"])
        self.assertFalse(out["any_ran"])
        self.assertEqual(len(out["tasks"]), 2)
        self.assertIsNone(out["tasks"][0]["last_run"])
        self.assertIsNone(out["tasks"][0]["next_eligible"])

    def test_last_run_drives_age_and_next_eligible(self):
        state = {"holdings-resync": {"last_run": _iso(2), "last_result": "resync spawned #j1"}}
        out = overview.automation_summary(state, self.TASKS, enabled=True, now=NOW)
        self.assertTrue(out["enabled"])
        self.assertTrue(out["any_ran"])
        row = out["tasks"][0]
        self.assertEqual(row["label"], "Holdings resync")
        self.assertEqual(row["age_days"], 2)
        self.assertEqual(row["last_result"], "resync spawned #j1")
        # next_eligible = last_run + 24h -> one day after the 2-days-ago stamp.
        self.assertEqual(row["next_eligible"], (NOW - dt.timedelta(days=1)).isoformat(timespec="seconds"))
        self.assertTrue(row["enabled"])
        self.assertFalse(out["tasks"][1]["enabled"])

    def test_tolerates_missing_state_and_tasks(self):
        out = overview.automation_summary(None, None, enabled=True, now=NOW)
        self.assertEqual(out["tasks"], [])
        self.assertFalse(out["any_ran"])


if __name__ == "__main__":
    unittest.main()
