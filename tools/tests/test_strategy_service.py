"""Tests for tools/strategy_service.py: the guided "Direction -> Rebalance" leg
runners and the three human gates.

The durable state machine (orchestrate) runs for real against a temp dir so we
assert genuine transitions and guards. The heavy collaborators -- segment draft,
the LLM, deep research, review, target construction/apply -- and the daemon
threads are mocked, so no real research, browser, or model work happens."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _support  # noqa: F401
import orchestrate as orch
import strategy_service as svc
from apierror import Conflict


class StrategyServiceBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig_dir = orch.STRATEGY_DIR
        orch.STRATEGY_DIR = Path(self.tmp.name) / "strategy"
        # No server boot context => orphan reaping disabled (bare unit test).
        self._orig_boot = orch._BOOT_TOKEN
        orch.set_boot_token(None)

    def tearDown(self):
        orch.STRATEGY_DIR = self._orig_dir
        orch.set_boot_token(self._orig_boot)
        self.tmp.cleanup()

    def _run_at(self, state, **fields):
        """A persisted run advanced to *state* via the legal gate sequence."""
        run = orch.new_run("rotate into AI semis")
        rid = run["run_id"]
        path = [orch.AWAITING_SEGMENT, orch.SYNTHESIS_RUNNING, orch.AWAITING_PROPOSAL]
        for nxt in path:
            orch.set_state(rid, nxt, **(fields if nxt == state else {}))
            if nxt == state:
                return rid
        return rid


class StartStrategy(StrategyServiceBase):
    def test_creates_run_and_hands_work_to_a_thread(self):
        with mock.patch.object(svc.threading, "Thread") as Thread:
            view = svc.start_strategy("rotate into AI semis")
        self.assertEqual(view["state"], orch.DRAFT_RUNNING)
        self.assertEqual(view["direction"], "rotate into AI semis")
        self.assertEqual(orch.load_run(view["run_id"])["state"], orch.DRAFT_RUNNING)
        Thread.assert_called_once()  # drafting runs off-thread, never inline


class RunStrategyDraft(StrategyServiceBase):
    def _baseline(self):
        return {
            "slug": "ai-semis",
            "definition": {"title": "AI semis", "members": [{"symbol": "NVDA", "sleeve": "ai"}]},
            "llm_prompt": "prompt",
            "warnings": [],
        }

    def test_keyword_only_draft_parks_at_segment_gate(self):
        run = orch.new_run("rotate into AI semis")
        rid = run["run_id"]
        with mock.patch.object(svc, "draft_segment", return_value=self._baseline()), \
             mock.patch.object(svc.ticker_analysis, "available_backends",
                               return_value={"claude": False, "cursor": False}):
            svc.run_strategy_draft(rid)
        loaded = orch.load_run(rid)
        self.assertEqual(loaded["state"], orch.AWAITING_SEGMENT)
        self.assertEqual(loaded["draft"]["slug"], "ai-semis")
        self.assertEqual([m["symbol"] for m in loaded["draft"]["definition"]["members"]], ["NVDA"])
        self.assertEqual(loaded["draft"]["definition"]["sleeves"], ["ai"])

    def test_llm_draft_merges_members_and_metadata(self):
        run = orch.new_run("rotate into AI semis")
        rid = run["run_id"]
        merged = [{"symbol": "NVDA", "sleeve": "ai"}, {"symbol": "AVGO", "sleeve": "ai"}]
        llm = {"ok": True, "members": [{"symbol": "AVGO", "sleeve": "ai"}],
               "backend_label": "Claude", "title": "AI Semiconductors", "comment": "thesis"}
        with mock.patch.object(svc, "draft_segment", return_value=self._baseline()), \
             mock.patch.object(svc.ticker_analysis, "available_backends",
                               return_value={"claude": True}), \
             mock.patch.object(svc.ticker_analysis, "draft_segment_members", return_value=llm), \
             mock.patch.object(svc, "merge_draft_members", return_value=merged):
            svc.run_strategy_draft(rid)
        draft = orch.load_run(rid)["draft"]
        self.assertEqual([m["symbol"] for m in draft["definition"]["members"]], ["NVDA", "AVGO"])
        self.assertEqual(draft["definition"]["title"], "AI Semiconductors")
        self.assertEqual(draft["backend_label"], "Claude")

    def test_draft_failure_lands_in_error(self):
        run = orch.new_run("rotate into AI semis")
        rid = run["run_id"]
        with mock.patch.object(svc, "draft_segment", side_effect=RuntimeError("keyword DB down")):
            svc.run_strategy_draft(rid)
        loaded = orch.load_run(rid)
        self.assertEqual(loaded["state"], orch.ERROR)
        self.assertIn("keyword DB down", loaded["error"])


class ApproveSegment(StrategyServiceBase):
    def test_unknown_run_raises_value_error(self):
        with self.assertRaises(ValueError):
            svc.approve_strategy_segment("deadbeef", None)

    def test_wrong_state_raises_conflict(self):
        run = orch.new_run("x")  # still DRAFT_RUNNING, not awaiting a segment
        with self.assertRaises(Conflict):
            svc.approve_strategy_segment(run["run_id"], None)

    def test_approval_writes_definition_and_starts_synthesis(self):
        rid = self._run_at(orch.AWAITING_SEGMENT, segment="ai-semis",
                           draft={"definition": {"title": "AI semis", "members": [{"symbol": "NVDA"}]}})
        definition = {"title": "AI semis", "members": [{"symbol": "NVDA"}], "status": "approved"}
        with mock.patch.object(svc, "validate_definition", return_value=definition) as validate, \
             mock.patch.object(svc, "write_json") as write_json, \
             mock.patch.object(svc.threading, "Thread") as Thread:
            out = svc.approve_strategy_segment(rid, {"members": [{"symbol": "NVDA"}]})
        validate.assert_called_once()
        write_json.assert_called_once()
        Thread.assert_called_once()  # synthesis spawned
        self.assertEqual(out["state"], orch.SYNTHESIS_RUNNING)
        self.assertEqual(out["segment"], "ai-semis")


class ApproveProposal(StrategyServiceBase):
    def test_unknown_run_raises_value_error(self):
        with self.assertRaises(ValueError):
            svc.approve_strategy_proposal("deadbeef", None)

    def test_wrong_state_raises_conflict(self):
        run = orch.new_run("x")  # DRAFT_RUNNING, not awaiting a proposal
        with self.assertRaises(Conflict):
            svc.approve_strategy_proposal(run["run_id"], None)

    def test_approval_stages_changes_into_the_working_draft(self):
        rid = self._run_at(orch.AWAITING_PROPOSAL, segment="ai-semis", date="2026-06-20",
                           proposal={"changes": []})
        changes = [{"symbol": "NVDA", "target": 0.05}]
        with mock.patch.object(svc, "load", return_value={}), \
             mock.patch.object(svc, "write_json") as write_json, \
             mock.patch.object(svc.target_staging, "stage_proposal",
                               return_value={"applied": [1, 2]}) as stage, \
             mock.patch.object(svc.target_staging, "diff_staged_vs_live",
                               return_value={"counts": {"total": 2}}):
            out = svc.approve_strategy_proposal(rid, changes)
        write_json.assert_called_once()  # edited proposal persisted before staging
        stage.assert_called_once_with("ai-semis", "2026-06-20", changes=changes,
                                      run_id=rid, source="strategy", allow_blocked=False)
        self.assertEqual(out["state"], orch.STAGED)  # stages, no longer commits live
        self.assertEqual(out["staged"], {"applied": [1, 2]})

    def test_staging_failure_propagates_and_leaves_run_at_the_gate(self):
        # Pins current behaviour: a stage_proposal failure surfaces as an
        # exception and the run stays at AWAITING_PROPOSAL. NOTE: the handler tries
        # set_state(ERROR), but ERROR is NOT a legal transition out of the proposal
        # gate (see orchestrate TRANSITIONS), so that set_state itself raises and
        # masks the original error -- a latent quirk of the #106 staging rework.
        rid = self._run_at(orch.AWAITING_PROPOSAL, segment="ai-semis", date="2026-06-20",
                           proposal={"changes": []})
        with mock.patch.object(svc, "load", return_value={}), \
             mock.patch.object(svc, "write_json"), \
             mock.patch.object(svc.target_staging, "stage_proposal",
                               side_effect=RuntimeError("draft locked")):
            with self.assertRaises(Exception):
                svc.approve_strategy_proposal(rid, [{"symbol": "NVDA"}])
        self.assertEqual(orch.load_run(rid)["state"], orch.AWAITING_PROPOSAL)
        self.assertNotEqual(orch.load_run(rid)["state"], orch.STAGED)


if __name__ == "__main__":
    unittest.main()
