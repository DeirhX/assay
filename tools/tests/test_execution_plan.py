import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _support  # noqa: F401
import execution_plan


def _plan(snapshot="2026-07-12", delta=1.0):
    return {
        "as_of": "2026-07-12",
        "snapshot": snapshot,
        "invested": 1_000_000,
        "rows": [{
            "kind": "target",
            "key": "NVDA",
            "name": "NVDA",
            "interactive": True,
            "action": "buy",
            "current_pct": 2,
            "suggest_delta_pct": delta,
            "suggest_delta_czk": delta * 10_000,
        }],
    }


class ExecutionPlanStore(unittest.TestCase):
    def test_initializes_and_preserves_edits_for_same_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution-plan.json"
            state = execution_plan.state_for_plan(_plan(), path=path)
            item = state["items"][0]
            execution_plan.patch_item(
                item["id"],
                {"status": "deferred", "limit_price": 100},
                expected_version=state["version"],
                path=path,
            )
            again = execution_plan.state_for_plan(_plan(), path=path)
            self.assertEqual(again["items"][0]["status"], "deferred")
            self.assertEqual(again["items"][0]["limit_price"], 100)
            self.assertFalse(again["stale"])

    def test_new_plan_is_stale_until_explicit_replace(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution-plan.json"
            first = execution_plan.state_for_plan(_plan(), path=path)
            execution_plan.patch_item(
                first["items"][0]["id"],
                {"status": "dismissed"},
                path=path,
            )
            newer = _plan(snapshot="2026-07-13", delta=2)
            stale = execution_plan.state_for_plan(newer, path=path)
            self.assertTrue(stale["stale"])
            self.assertEqual(stale["items"][0]["status"], "dismissed")
            replaced = execution_plan.replace_rebalance(newer, path=path)
            self.assertFalse(replaced["stale"])
            self.assertEqual(replaced["items"][0]["status"], "suggested")
            self.assertEqual(replaced["items"][0]["delta_czk"], 20_000)

    def test_replace_preserves_queued_and_manual_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution-plan.json"
            state = execution_plan.state_for_plan(_plan(), path=path)
            execution_plan.patch_item(
                state["items"][0]["id"],
                {"status": "queued"},
                path=path,
            )
            execution_plan.add_manual({
                "symbol": "AMD",
                "delta_czk": -50_000,
                "route_policy": "sell_shares",
            }, path=path)
            replaced = execution_plan.replace_rebalance(
                _plan(snapshot="2026-07-13", delta=2),
                path=path,
            )
            sources = [item["source"] for item in replaced["items"]]
            self.assertEqual(sources.count("rebalance"), 2)
            self.assertIn("ticker", sources)
            rebalance_items = [
                item for item in replaced["items"] if item["source"] == "rebalance"
            ]
            residual = next(item for item in rebalance_items if item["status"] == "suggested")
            self.assertEqual(residual["delta_czk"], 10_000)

    def test_mark_queued_links_provenance_leg(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution-plan.json"
            state = execution_plan.state_for_plan(_plan(), path=path)
            item_id = state["items"][0]["id"]
            updated = execution_plan.mark_queued(
                [item_id],
                [{
                    "leg_id": "stock:NVDA",
                    "provenance": [{"execution_item_id": item_id}],
                }],
                path=path,
            )
            item = updated["items"][0]
            self.assertEqual(item["status"], "queued")
            self.assertEqual(item["queued_leg_id"], "stock:NVDA")

    def test_reconcile_deleted_queue_leg_returns_item_to_selected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution-plan.json"
            state = execution_plan.state_for_plan(_plan(), path=path)
            item_id = state["items"][0]["id"]
            execution_plan.mark_queued(
                [item_id],
                [{"leg_id": "stock:NVDA", "provenance": [{"execution_item_id": item_id}]}],
                path=path,
            )
            updated = execution_plan.reconcile_queue([], path=path)
            self.assertEqual(updated["items"][0]["status"], "selected")
            self.assertIsNone(updated["items"][0]["queued_leg_id"])

    def test_resolves_only_execution_items_on_residual_orders(self):
        basket = [
            {
                "leg_id": "stock:AMD",
                "symbol": "AMD",
                "provenance": [{"execution_item_id": "item-amd"}],
            },
            {
                "leg_id": "covered_call:NVDA:123",
                "symbol": "NVDA",
                "provenance": [{"execution_item_id": "item-nvda"}],
            },
        ]
        got = execution_plan.execution_item_ids_for_orders(
            basket,
            [{"symbol": "AMD"}, {"leg_id": "unknown", "symbol": "OTHER"}],
        )
        self.assertEqual(got, ["item-amd"])

    def test_mark_submitted_only_changes_named_queued_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution-plan.json"
            state = execution_plan.state_for_plan(_plan(), path=path)
            first_id = state["items"][0]["id"]
            _, manual = execution_plan.add_manual({
                "symbol": "AMD",
                "delta_czk": 5_000,
                "route_policy": "buy_shares",
            }, path=path)
            execution_plan.mark_queued(
                [first_id, manual["id"]],
                [
                    {"leg_id": "stock:NVDA", "provenance": [{"execution_item_id": first_id}]},
                    {"leg_id": "stock:AMD", "provenance": [{"execution_item_id": manual["id"]}]},
                ],
                path=path,
            )
            updated = execution_plan.mark_submitted([first_id], path=path)
            by_id = {item["id"]: item for item in updated["items"]}
            self.assertEqual(by_id[first_id]["status"], "submitted")
            self.assertEqual(by_id[manual["id"]]["status"], "queued")
            unchanged = execution_plan.mark_submitted([], path=path)
            self.assertEqual(
                next(item for item in unchanged["items"] if item["id"] == manual["id"])["status"],
                "queued",
            )

    def test_failed_correlated_submission_returns_to_selected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution-plan.json"
            state = execution_plan.state_for_plan(_plan(), path=path)
            item_id = state["items"][0]["id"]
            execution_plan.mark_queued(
                [item_id],
                [{"leg_id": "stock:NVDA", "provenance": [{"execution_item_id": item_id}]}],
                path=path,
            )
            execution_plan.mark_submitted([item_id], path=path)
            reopened = execution_plan.reopen_broker_failed([item_id], path=path)
            item = reopened["items"][0]
            self.assertEqual(item["status"], "selected")
            self.assertIsNone(item["queued_leg_id"])

    def test_queue_selected_consolidates_sources_by_symbol(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution-plan.json"
            state = execution_plan.state_for_plan(_plan(delta=-1), path=path)
            generated = state["items"][0]
            execution_plan.patch_item(
                generated["id"],
                {"status": "selected", "route_policy": "sell_shares"},
                path=path,
            )
            _, manual = execution_plan.add_manual({
                "symbol": "NVDA",
                "delta_czk": -5_000,
                "route_policy": "sell_shares",
            }, path=path)

            def staged(_holdings, trades, selections, **_kwargs):
                ids = selections[0]["execution_item_ids"]
                return {
                    "basket": [{
                        "leg_id": "stock:NVDA",
                        "provenance": [
                            {"execution_item_id": item_id} for item_id in ids
                        ],
                    }],
                    "trades": trades,
                }

            with mock.patch("rebalance_routes.stage_routes", side_effect=staged):
                out = execution_plan.queue_selected({}, path=path)
            self.assertEqual(out["trades"], [{"symbol": "NVDA", "delta_czk": -15_000.0}])
            by_id = {item["id"]: item for item in out["state"]["items"]}
            self.assertEqual(by_id[generated["id"]]["status"], "queued")
            self.assertEqual(by_id[manual["id"]]["status"], "queued")


if __name__ == "__main__":
    unittest.main()
