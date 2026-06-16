"""Tests for the guided-strategy state machine: legal/illegal transitions and
durable manifest CRUD (so a refresh/restart resumes at the last gate). Offline;
the manifest store is pointed at a temp dir."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import _support  # noqa: F401
import orchestrate as orch


class StateMachine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = orch.STRATEGY_DIR
        orch.STRATEGY_DIR = Path(self.tmp.name) / "strategy"

    def tearDown(self):
        orch.STRATEGY_DIR = self._orig
        self.tmp.cleanup()

    def test_new_run_starts_in_draft_running(self):
        run = orch.new_run("rotate into fintech")
        self.assertEqual(run["state"], orch.DRAFT_RUNNING)
        self.assertEqual(run["direction"], "rotate into fintech")
        self.assertTrue(orch.manifest_path(run["run_id"]).exists())

    def test_new_run_requires_direction(self):
        with self.assertRaises(ValueError):
            orch.new_run("   ")

    def test_transition_matrix(self):
        self.assertTrue(orch.can_transition(None, orch.DRAFT_RUNNING))
        self.assertTrue(orch.can_transition(orch.DRAFT_RUNNING, orch.AWAITING_SEGMENT))
        self.assertTrue(orch.can_transition(orch.AWAITING_SEGMENT, orch.SYNTHESIS_RUNNING))
        self.assertTrue(orch.can_transition(orch.SYNTHESIS_RUNNING, orch.AWAITING_PROPOSAL))
        self.assertTrue(orch.can_transition(orch.AWAITING_PROPOSAL, orch.APPLYING))
        self.assertTrue(orch.can_transition(orch.APPLYING, orch.DONE))
        self.assertTrue(orch.can_transition(orch.SYNTHESIS_RUNNING, orch.NEEDS_LOGIN))
        self.assertTrue(orch.can_transition(orch.NEEDS_LOGIN, orch.SYNTHESIS_RUNNING))
        # Illegal jumps the gates.
        self.assertFalse(orch.can_transition(orch.DRAFT_RUNNING, orch.DONE))
        self.assertFalse(orch.can_transition(orch.DRAFT_RUNNING, orch.APPLYING))
        self.assertFalse(orch.can_transition(orch.DONE, orch.DRAFT_RUNNING))

    def test_set_state_enforces_legality(self):
        run = orch.new_run("x")
        with self.assertRaises(ValueError):
            orch.set_state(run["run_id"], orch.DONE)
        # Legal advance succeeds and persists the merged field.
        orch.set_state(run["run_id"], orch.AWAITING_SEGMENT, segment="fintech")
        reloaded = orch.load_run(run["run_id"])
        self.assertEqual(reloaded["state"], orch.AWAITING_SEGMENT)
        self.assertEqual(reloaded["segment"], "fintech")

    def test_manifest_resume_round_trip(self):
        """A run advanced to a gate and then reloaded (simulating a restart)
        keeps its gate state and payload — the manifest is the durable spine."""
        run = orch.new_run("nuclear supply chain")
        rid = run["run_id"]
        orch.set_state(rid, orch.AWAITING_SEGMENT,
                       draft={"slug": "nuclear", "definition": {"members": [{"symbol": "CEG"}]}})
        # Simulate a fresh process: only the on-disk manifest survives.
        resumed = orch.load_run(rid)
        self.assertEqual(resumed["state"], orch.AWAITING_SEGMENT)
        self.assertEqual(resumed["draft"]["slug"], "nuclear")
        self.assertTrue(orch.can_transition(resumed["state"], orch.SYNTHESIS_RUNNING))

    def test_update_run_merges_without_transition(self):
        run = orch.new_run("x")
        orch.update_run(run["run_id"], message="researching…", job_id="abcd1234")
        reloaded = orch.load_run(run["run_id"])
        self.assertEqual(reloaded["state"], orch.DRAFT_RUNNING)  # unchanged
        self.assertEqual(reloaded["message"], "researching…")
        self.assertEqual(reloaded["job_id"], "abcd1234")

    def test_list_runs_includes_new_run(self):
        run = orch.new_run("space launch")
        runs = orch.list_runs()
        self.assertTrue(any(r["run_id"] == run["run_id"] for r in runs))

    def test_public_borrows_job_message_while_running(self):
        run = orch.new_run("x")
        view = orch.public(run, job={"message": "starting browser", "state": "running"})
        self.assertEqual(view["message"], "starting browser")
        self.assertFalse(view["resting"])
        # At a gate the manifest message wins (the job is done/absent).
        gated = orch.set_state(run["run_id"], orch.AWAITING_SEGMENT, message="approve me")
        view2 = orch.public(gated, job={"message": "stale", "state": "done"})
        self.assertEqual(view2["message"], "approve me")
        self.assertTrue(view2["at_gate"])


class OrphanReaping(unittest.TestCase):
    """A guided run whose worker thread was killed by a server restart must be
    detected (its boot token != the live process) and durably failed, instead of
    leaving the UI polling a spinner that will never advance."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig_dir = orch.STRATEGY_DIR
        orch.STRATEGY_DIR = Path(self.tmp.name) / "strategy"
        self._orig_boot = orch._BOOT_TOKEN
        orch.set_boot_token("boot-A")

    def tearDown(self):
        orch.STRATEGY_DIR = self._orig_dir
        orch.set_boot_token(self._orig_boot)
        self.tmp.cleanup()

    def test_running_run_from_a_dead_process_is_reaped_to_error(self):
        run = orch.new_run("add nuclear supply chain")
        self.assertEqual(run["boot"], "boot-A")          # stamped on entering DRAFT_RUNNING
        orch.set_boot_token("boot-B")                    # simulate a server restart
        loaded = orch.load_run(run["run_id"])
        self.assertTrue(orch.is_orphaned(loaded))
        healed = orch.reap_if_orphaned(loaded)
        self.assertEqual(healed["state"], orch.ERROR)
        self.assertIn("server restart", healed["error"])
        # ERROR is retryable, so the user can start the run over.
        self.assertTrue(orch.can_transition(healed["state"], orch.DRAFT_RUNNING))

    def test_running_run_in_the_same_process_is_not_orphaned(self):
        run = orch.new_run("x")
        self.assertFalse(orch.is_orphaned(orch.load_run(run["run_id"])))
        self.assertEqual(orch.reap_if_orphaned(orch.load_run(run["run_id"]))["state"], orch.DRAFT_RUNNING)

    def test_gate_state_is_never_orphaned(self):
        run = orch.new_run("x")
        orch.set_state(run["run_id"], orch.AWAITING_SEGMENT)
        orch.set_boot_token("boot-B")                    # restart while parked at a gate
        self.assertFalse(orch.is_orphaned(orch.load_run(run["run_id"])))

    def test_legacy_running_run_without_boot_field_is_orphaned(self):
        # The real-world case: a run created before the boot stamp existed, left
        # in draft_running by a restart, has no "boot" key at all.
        run = orch.new_run("x")
        m = orch.load_run(run["run_id"])
        m.pop("boot", None)
        orch.save_run(m)
        self.assertTrue(orch.is_orphaned(orch.load_run(run["run_id"])))

    def test_check_is_disabled_without_a_boot_token(self):
        run = orch.new_run("x")
        orch.set_boot_token(None)                         # no server context (e.g. bare unit test)
        self.assertFalse(orch.is_orphaned(orch.load_run(run["run_id"])))


if __name__ == "__main__":
    unittest.main()
