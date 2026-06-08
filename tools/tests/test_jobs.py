"""Tests for the in-memory job registry, including the cooperative-cancel path
that lets a user abort an in-flight Q&A and ask something else."""

from __future__ import annotations

import unittest

import _support  # noqa: F401
import jobs


class Lifecycle(unittest.TestCase):
    def test_new_job_defaults_and_public_shape(self):
        job = jobs.new_job("ticker_qa", symbol="AMD")
        self.assertEqual(job["state"], "queued")
        pub = jobs.get_public(job["id"])
        self.assertEqual(pub["kind"], "ticker_qa")
        self.assertFalse(pub["cancelled"])
        self.assertIn("created_at", job)

    def test_update_job_merges_fields(self):
        job = jobs.new_job("ticker_qa", symbol="X")
        jobs.update_job(job["id"], state="running", message="thinking")
        pub = jobs.get_public(job["id"])
        self.assertEqual(pub["state"], "running")
        self.assertEqual(pub["message"], "thinking")
        self.assertIn("updated_at", pub)

    def test_get_public_unknown_is_none(self):
        self.assertIsNone(jobs.get_public("deadbeef"))


class Cancel(unittest.TestCase):
    def test_cancel_running_job(self):
        job = jobs.new_job("ticker_qa", symbol="X")
        jobs.update_job(job["id"], state="running")
        self.assertTrue(jobs.cancel_job(job["id"]))
        self.assertTrue(jobs.is_cancelled(job["id"]))

    def test_cannot_cancel_finished_job(self):
        job = jobs.new_job("ticker_qa", symbol="X")
        jobs.update_job(job["id"], state="done")
        self.assertFalse(jobs.cancel_job(job["id"]))
        self.assertFalse(jobs.is_cancelled(job["id"]))

    def test_cancel_unknown_job(self):
        self.assertFalse(jobs.cancel_job("nope"))


class Concurrency(unittest.TestCase):
    def test_active_slot_is_exclusive(self):
        self.assertTrue(jobs.claim_active())
        try:
            self.assertFalse(jobs.claim_active())  # already held
        finally:
            jobs.release_active()
        self.assertTrue(jobs.claim_active())
        jobs.release_active()

    def test_find_predicate(self):
        job = jobs.new_job("ticker_qa", symbol="FINDME")
        jobs.update_job(job["id"], state="running")
        self.assertTrue(jobs.find(lambda j: j.get("symbol") == "FINDME"
                                 and j.get("state") == "running"))


if __name__ == "__main__":
    unittest.main()
