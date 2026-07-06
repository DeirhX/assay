"""Tests for the order/fill watcher. The transition logic is pure and pinned
directly; poll_once is driven through injected gateway/notify/resync seams and a
temp state file, so nothing touches a real Client Portal Gateway."""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

import _support  # noqa: F401
import apierror
import ibkr_trade
import order_watch
import store

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 1, 5, 16, 0, tzinfo=UTC)


def _order(oid="1", status="Submitted", filled=0, side="BUY", sym="NVDA", total=10):
    return {"orderId": oid, "status": status, "filledQuantity": filled,
            "side": side, "ticker": sym, "totalSize": total}


class Classify(unittest.TestCase):
    def test_new_fill_from_working(self):
        ev = order_watch.classify({"status": "submitted", "filled": 0}, _order(status="Filled", filled=10))
        self.assertEqual(ev["kind"], "filled")

    def test_fill_with_no_prior_state(self):
        ev = order_watch.classify(None, _order(status="Filled", filled=10))
        self.assertEqual(ev["kind"], "filled")

    def test_partial_fill_is_an_increase_while_not_filled(self):
        ev = order_watch.classify({"status": "submitted", "filled": 2}, _order(status="Submitted", filled=5))
        self.assertEqual(ev["kind"], "partial")

    def test_cancelled_and_rejected(self):
        self.assertEqual(order_watch.classify({"status": "submitted"}, _order(status="Cancelled"))["kind"], "cancelled")
        self.assertEqual(order_watch.classify({"status": "submitted"}, _order(status="Rejected"))["kind"], "rejected")

    def test_no_change_is_silent(self):
        self.assertIsNone(order_watch.classify({"status": "submitted", "filled": 0}, _order()))

    def test_already_reported_fill_is_silent(self):
        self.assertIsNone(order_watch.classify({"status": "filled", "filled": 10}, _order(status="Filled", filled=10)))

    def test_terminal_state_beats_a_fill_bump(self):
        # A fully filled order reads as "filled", not "partial", even though qty rose.
        ev = order_watch.classify({"status": "submitted", "filled": 2}, _order(status="Filled", filled=10))
        self.assertEqual(ev["kind"], "filled")


class DiffOrders(unittest.TestCase):
    def test_ignores_orders_without_an_id(self):
        events, new_map = order_watch.diff_orders({}, [{"status": "Filled"}])
        self.assertEqual(events, [])
        self.assertEqual(new_map, {})

    def test_builds_map_and_events(self):
        prev = {"1": {"status": "submitted", "filled": 0}}
        events, new_map = order_watch.diff_orders(prev, [_order(oid="1", status="Filled", filled=10)])
        self.assertEqual(len(events), 1)
        self.assertEqual(new_map["1"]["status"], "filled")


class _PollCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "order-watch.json"
        self.notices = []
        self.resyncs = 0

    def tearDown(self):
        self.tmp.cleanup()

    def _notifier(self, title, body, *, tags=(), priority="default"):
        self.notices.append({"title": title, "body": body, "priority": priority})
        return ["webhook"]

    def _resync(self):
        self.resyncs += 1
        return {"id": "job1"}

    def _poll(self, orders, *, authed=True, resync=None):
        return order_watch.poll_once(
            now=NOW,
            fetch_orders=lambda: orders,
            fetch_auth=lambda: {"authenticated": authed},
            notifier=self._notifier,
            resync=resync or self._resync,
            state_path=self.state,
        )


class PollTransitions(_PollCase):
    def test_first_sight_seeds_state_without_events(self):
        res = self._poll([_order(status="Submitted")])
        self.assertTrue(res["ok"])
        self.assertEqual(res["events"], 0)
        self.assertEqual(self.notices, [])
        self.assertEqual(store.load(self.state)["orders"]["1"]["status"], "submitted")

    def test_fill_notifies_and_kicks_resync_once(self):
        self._poll([_order(status="Submitted")])           # seed
        res = self._poll([_order(status="Filled", filled=10)])
        self.assertEqual(res["fills"], 1)
        self.assertTrue(res["resynced"])
        self.assertEqual(self.resyncs, 1)
        self.assertEqual(len(self.notices), 1)
        self.assertIn("Fill", self.notices[0]["title"])
        # The fill is recorded for later reconciliation surfaces.
        self.assertEqual(len(store.load(self.state)["recent_fills"]), 1)

    def test_resync_conflict_is_swallowed(self):
        self._poll([_order(status="Submitted")])
        res = self._poll([_order(status="Filled", filled=10)],
                         resync=lambda: (_ for _ in ()).throw(apierror.Conflict("already running")))
        self.assertEqual(res["fills"], 1)
        self.assertFalse(res["resynced"])   # conflict -> not counted, but no crash


class PollSession(_PollCase):
    def test_session_down_alerts_once_when_orders_were_working(self):
        self._poll([_order(status="Submitted")])           # seed a working order
        res1 = self._poll([], authed=False)
        self.assertFalse(res1["ok"])
        self.assertEqual(len(self.notices), 1)
        self.assertIn("session down", self.notices[0]["title"].lower())
        # A second unauthenticated poll must NOT re-alert (dedupe flag).
        res2 = self._poll([], authed=False)
        self.assertFalse(res2["ok"])
        self.assertEqual(len(self.notices), 1)

    def test_session_down_with_no_known_orders_is_silent(self):
        res = self._poll([], authed=False)
        self.assertFalse(res["ok"])
        self.assertEqual(self.notices, [])


class PollDryRun(_PollCase):
    """The safe live-path verification: real reads, zero side effects."""

    def test_dry_run_reports_transitions_without_acting(self):
        self._poll([_order(status="Submitted")])            # seed real state
        before = store.load(self.state)
        res = order_watch.poll_once(
            now=NOW, fetch_orders=lambda: [_order(status="Filled", filled=10)],
            fetch_auth=lambda: {"authenticated": True},
            notifier=self._notifier, resync=self._resync, state_path=self.state,
            dry_run=True,
        )
        self.assertTrue(res["dry_run"])
        self.assertEqual(res["fills"], 1)
        self.assertTrue(res["would_resync"])
        self.assertEqual(res["event_detail"][0]["kind"], "filled")
        # ...but nothing actually happened: no notice, no resync, state untouched.
        self.assertEqual(self.notices, [])
        self.assertEqual(self.resyncs, 0)
        self.assertFalse(res["resynced"])
        self.assertEqual(store.load(self.state), before)

    def test_dry_run_reports_session_down_without_alerting(self):
        self._poll([_order(status="Submitted")])            # a working order exists
        res = order_watch.poll_once(
            now=NOW, fetch_orders=lambda: [], fetch_auth=lambda: {"authenticated": False},
            notifier=self._notifier, resync=self._resync, state_path=self.state,
            dry_run=True,
        )
        self.assertFalse(res["ok"])
        self.assertTrue(res["would_alert_session_down"])
        self.assertEqual(self.notices, [])                  # not actually fired
        # The dedupe flag stays as the seed left it (False) -- a real poll would
        # flip it to True after alerting; the dry-run must not.
        self.assertFalse(store.load(self.state).get("session_down_notified"))

    def test_render_is_readable(self):
        text = order_watch._render({
            "ok": True, "authenticated": True, "orders": 2, "would_resync": True,
            "event_detail": [{"kind": "filled", "symbol": "NVDA", "side": "SELL",
                              "filled": 10, "total": 10}],
        })
        self.assertIn("WOULD act on", text)
        self.assertIn("resync WOULD be kicked", text)
        self.assertIn("no state written", text)


class PollGatewayError(_PollCase):
    def test_gateway_error_is_handled(self):
        def boom():
            raise ibkr_trade.CPAPIError("gateway not running", status=0)

        res = order_watch.poll_once(
            now=NOW, fetch_orders=boom, fetch_auth=lambda: {"authenticated": True},
            notifier=self._notifier, resync=self._resync, state_path=self.state,
        )
        self.assertFalse(res["ok"])
        self.assertIn("gateway error", res["reason"])


if __name__ == "__main__":
    unittest.main()
