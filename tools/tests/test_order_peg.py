"""Tests for the opt-in order peg (keep at top of book). The pricing decision is
pure and exhaustively unit-tested; the IO paths (start gating, the worker loop)
are exercised with every gateway call mocked, so nothing here touches the real
Client Portal Gateway."""

from __future__ import annotations

import unittest
from unittest import mock

import _support  # noqa: F401
import apierror
import jobs
import order_peg


class DesiredPrice(unittest.TestCase):
    # --- SELL: undercut the best ask, never below the floor, never cross ---
    def test_sell_holds_when_already_best(self):
        # best_ask == mine -> we are (tied for) the best offer; do not self-undercut.
        self.assertIsNone(order_peg.desired_price("SELL", 10.00, 9.90, 10.00, 0.01, 9.50))
        # best_ask above us shouldn't happen for a resting ask, but must also hold.
        self.assertIsNone(order_peg.desired_price("SELL", 10.00, 9.90, 10.05, 0.01, 9.50))

    def test_sell_undercuts_a_cheaper_competitor(self):
        self.assertAlmostEqual(
            order_peg.desired_price("SELL", 10.00, 9.90, 9.95, 0.01, 9.50), 9.94)

    def test_sell_clamps_to_floor(self):
        # A competitor far below us cannot drag us under the worst-acceptable floor.
        self.assertAlmostEqual(
            order_peg.desired_price("SELL", 10.00, 9.40, 9.45, 0.01, 9.50), 9.50)

    def test_sell_never_crosses_the_spread(self):
        # One-tick spread: undercutting would post at/through the bid, so we clamp
        # to bid+tick; here that isn't an improvement over mine -> hold.
        self.assertIsNone(order_peg.desired_price("SELL", 9.95, 9.94, 9.95, 0.01, 9.00))

    def test_sell_missing_quotes_hold(self):
        self.assertIsNone(order_peg.desired_price("SELL", 10.00, None, 9.95, 0.01, 9.50))
        self.assertIsNone(order_peg.desired_price("SELL", 10.00, 9.90, None, 0.01, 9.50))

    # --- BUY: mirror image ---
    def test_buy_holds_when_already_best(self):
        self.assertIsNone(order_peg.desired_price("BUY", 10.00, 10.00, 10.20, 0.01, 10.50))

    def test_buy_outbids_a_higher_competitor(self):
        self.assertAlmostEqual(
            order_peg.desired_price("BUY", 10.00, 10.05, 10.20, 0.01, 10.50), 10.06)

    def test_buy_clamps_to_ceiling(self):
        self.assertAlmostEqual(
            order_peg.desired_price("BUY", 10.00, 10.60, 10.80, 0.01, 10.50), 10.50)

    def test_buy_never_crosses_the_spread(self):
        # best_bid+tick would reach the ask, so clamp inside; not an improvement -> hold.
        self.assertIsNone(order_peg.desired_price("BUY", 10.05, 10.05, 10.06, 0.01, 11.00))

    def test_zero_tick_holds(self):
        self.assertIsNone(order_peg.desired_price("SELL", 10.0, 9.9, 9.95, 0.0, 9.5))

    def test_unknown_side_holds(self):
        self.assertIsNone(order_peg.desired_price("FLAT", 10.0, 9.9, 9.95, 0.01, 9.5))


class StartPegGating(unittest.TestCase):
    def tearDown(self):
        with order_peg._LOCK:
            order_peg._ACTIVE.clear()

    def test_refused_when_trading_disabled(self):
        with mock.patch.object(order_peg.ibkr_trade, "trading_enabled", return_value=False):
            with self.assertRaises(apierror.Forbidden):
                order_peg.start_peg("1", "DU1")

    def test_refused_when_auto_peg_disabled(self):
        with mock.patch.object(order_peg.ibkr_trade, "trading_enabled", return_value=True), \
                mock.patch.object(order_peg, "auto_peg_enabled", return_value=False):
            with self.assertRaises(apierror.Forbidden):
                order_peg.start_peg("1", "DU1")

    def test_refused_on_live_account_when_live_locked(self):
        with mock.patch.object(order_peg.ibkr_trade, "trading_enabled", return_value=True), \
                mock.patch.object(order_peg, "auto_peg_enabled", return_value=True), \
                mock.patch.object(order_peg.ibkr_trade, "live_allowed", return_value=False):
            with self.assertRaises(apierror.Forbidden):
                order_peg.start_peg("1", "U777")  # non-paper prefix

    def test_missing_order_is_a_bad_request(self):
        with mock.patch.object(order_peg.ibkr_trade, "trading_enabled", return_value=True), \
                mock.patch.object(order_peg, "auto_peg_enabled", return_value=True), \
                mock.patch.object(order_peg, "find_live_order", return_value=None):
            with self.assertRaises(apierror.BadRequest):
                order_peg.start_peg("nope", "DU1")

    def test_happy_start_registers_a_peg(self):
        order = {"orderId": "42", "side": "SELL", "conid": 222, "price": 10.00,
                 "remainingQuantity": 5, "tif": "GTC", "ticker": "AMD"}
        with mock.patch.object(order_peg.ibkr_trade, "trading_enabled", return_value=True), \
                mock.patch.object(order_peg, "auto_peg_enabled", return_value=True), \
                mock.patch.object(order_peg, "find_live_order", return_value=order), \
                mock.patch.object(order_peg.ibkr_trade, "contract_rules", return_value={}), \
                mock.patch.object(order_peg.jobs, "spawn", return_value={"id": "job-x"}) as spawn:
            res = order_peg.start_peg("42", "DU1", worst_price=9.50)
        self.assertTrue(spawn.called)
        self.assertEqual(res["order_id"], "42")
        self.assertEqual(res["side"], "SELL")
        self.assertAlmostEqual(res["bound"], 9.50)
        self.assertTrue(order_peg.is_pegging("42"))
        # A second start on the same order is a conflict.
        with mock.patch.object(order_peg.ibkr_trade, "trading_enabled", return_value=True), \
                mock.patch.object(order_peg, "auto_peg_enabled", return_value=True):
            with self.assertRaises(apierror.Conflict):
                order_peg.start_peg("42", "DU1")

    def test_bound_defaults_to_order_limit(self):
        order = {"orderId": "7", "side": "BUY", "conid": 9, "price": 25.0, "tif": "GTC"}
        with mock.patch.object(order_peg.ibkr_trade, "trading_enabled", return_value=True), \
                mock.patch.object(order_peg, "auto_peg_enabled", return_value=True), \
                mock.patch.object(order_peg, "find_live_order", return_value=order), \
                mock.patch.object(order_peg.ibkr_trade, "contract_rules", return_value={}), \
                mock.patch.object(order_peg.jobs, "spawn", return_value={"id": "j"}):
            res = order_peg.start_peg("7", "DU1")
        self.assertAlmostEqual(res["bound"], 25.0)


class PegWorker(unittest.TestCase):
    def tearDown(self):
        with order_peg._LOCK:
            order_peg._ACTIVE.clear()

    def test_worker_reprices_once_then_stops_on_cancel(self):
        oid = "111"
        cfg = order_peg.PegConfig(order_id=oid, account_id="DU1", conid=5, side="SELL",
                                  bound=9.50, tick=0.01, symbol="AMD",
                                  poll_s=0.01, min_interval_s=0.0)
        job = jobs.new_job("order_peg")
        with order_peg._LOCK:
            order_peg._ACTIVE[oid] = order_peg._record(cfg, job["id"])

        order = {"orderId": oid, "side": "SELL", "conid": 5, "price": 10.00,
                 "remainingQuantity": 10, "status": "Submitted", "tif": "GTC"}
        calls: list[dict] = []

        def fake_modify(acct, order_id, changes):
            calls.append(changes)
            order["price"] = changes["price"]     # reflect the reprice
            jobs.cancel_job(job["id"])            # one reprice, then unwind
            return [{"order_id": order_id}]

        with mock.patch.object(order_peg, "find_live_order", return_value=order), \
                mock.patch.object(order_peg.ibkr_trade, "auth_status",
                                  return_value={"authenticated": True}), \
                mock.patch.object(order_peg.ibkr_trade, "market_snapshot",
                                  return_value={5: {"84": "9.90", "86": "9.95"}}), \
                mock.patch.object(order_peg.ibkr_trade, "modify_order", side_effect=fake_modify):
            order_peg._peg_worker(job["id"], oid)

        self.assertEqual(len(calls), 1)
        self.assertAlmostEqual(calls[0]["price"], 9.94)
        self.assertEqual(calls[0]["side"], "SELL")
        # The worker deregisters itself and marks the job cancelled on exit.
        self.assertNotIn(oid, order_peg._ACTIVE)
        self.assertEqual(jobs.get_public(job["id"])["state"], "cancelled")

    def test_worker_stops_when_order_gone(self):
        oid = "222"
        cfg = order_peg.PegConfig(order_id=oid, account_id="DU1", conid=5, side="SELL",
                                  bound=9.50, tick=0.01, poll_s=0.01)
        job = jobs.new_job("order_peg")
        with order_peg._LOCK:
            order_peg._ACTIVE[oid] = order_peg._record(cfg, job["id"])
        with mock.patch.object(order_peg, "find_live_order", return_value=None), \
                mock.patch.object(order_peg.ibkr_trade, "auth_status",
                                  return_value={"authenticated": True}), \
                mock.patch.object(order_peg.ibkr_trade, "modify_order") as modify:
            order_peg._peg_worker(job["id"], oid)
        self.assertFalse(modify.called)          # nothing to reprice
        self.assertNotIn(oid, order_peg._ACTIVE)
        self.assertEqual(jobs.get_public(job["id"])["state"], "done")

    def test_worker_stops_when_gateway_unauthenticated(self):
        oid = "333"
        cfg = order_peg.PegConfig(order_id=oid, account_id="DU1", conid=5, side="SELL",
                                  bound=9.50, tick=0.01, poll_s=0.01)
        job = jobs.new_job("order_peg")
        with order_peg._LOCK:
            order_peg._ACTIVE[oid] = order_peg._record(cfg, job["id"])
        with mock.patch.object(order_peg.ibkr_trade, "auth_status",
                               return_value={"authenticated": False}), \
                mock.patch.object(order_peg.ibkr_trade, "modify_order") as modify:
            order_peg._peg_worker(job["id"], oid)
        self.assertFalse(modify.called)
        self.assertEqual(jobs.get_public(job["id"])["state"], "error")


if __name__ == "__main__":
    unittest.main()
