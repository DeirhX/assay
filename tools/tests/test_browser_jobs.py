"""Tests for tools/browser_jobs.py: the cached auth flag, the unattended clarify
reply, the shared browser_job scaffold (import guard / exception capture / the
all-important slot release), result persistence, the start-gate validation, and a
representative slice of the per-status handle dispatch.

Playwright never runs: the worker module (pplx_deep_research) is replaced with a
fake injected into sys.modules, and the job registry / artifact writer are mocked.
The auth + segment files live in a temp dir."""

from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import _support  # noqa: F401
import browser_jobs as bj
from apierror import Conflict


def fake_worker(**attrs):
    mod = types.ModuleType("pplx_deep_research")
    for name, val in attrs.items():
        setattr(mod, name, val)
    return mock.patch.dict(sys.modules, {"pplx_deep_research": mod})


class TempFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self._orig = {n: getattr(bj, n) for n in ("AUTH_STATE_FILE", "SEGMENT_DEF_DIR")}
        bj.AUTH_STATE_FILE = root / "auth-state.json"
        bj.SEGMENT_DEF_DIR = root / "segments"
        bj.SEGMENT_DEF_DIR.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        for n, v in self._orig.items():
            setattr(bj, n, v)
        self.tmp.cleanup()


class AuthState(TempFiles):
    def test_missing_state_reads_as_logged_out(self):
        self.assertEqual(bj.get_auth_state(), {"logged_in": False, "updated_at": None, "note": ""})

    def test_set_then_get_round_trips(self):
        bj.set_auth_state(True, "active check")
        st = bj.get_auth_state()
        self.assertTrue(st["logged_in"])
        self.assertEqual(st["note"], "active check")
        self.assertIsNotNone(st["updated_at"])


class ClarifyAnswer(TempFiles):
    def test_lists_segment_tickers_when_defined(self):
        from store import write_json
        write_json(bj.SEGMENT_DEF_DIR / "fintech.json",
                   {"members": [{"symbol": "SQ"}, {"symbol": "PYPL"}, {"symbol": ""}]})
        ans = bj.clarify_answer_for("fintech")
        self.assertIn("SQ, PYPL", ans)
        self.assertIn("individual stocks", ans)
        self.assertNotIn(", ,", ans)  # the empty symbol is filtered out

    def test_falls_back_when_no_members(self):
        ans = bj.clarify_answer_for("ghost")
        self.assertIn("original request", ans)


class VerifyLogin(TempFiles):
    def test_conflicts_when_no_slot(self):
        with mock.patch.object(bj, "claim_active", return_value=False), \
             mock.patch.object(bj, "slots_busy_msg", return_value="busy"):
            with self.assertRaises(Conflict):
                bj.verify_login()

    def test_refreshes_flag_and_releases_slot(self):
        with mock.patch.object(bj, "claim_active", return_value=True), \
             mock.patch.object(bj, "release_active") as release, \
             fake_worker(check_login=lambda: {"status": "logged_in"}):
            out = bj.verify_login()
        self.assertTrue(out["logged_in"])
        release.assert_called_once()

    def test_worker_error_becomes_conflict_but_still_releases(self):
        with mock.patch.object(bj, "claim_active", return_value=True), \
             mock.patch.object(bj, "release_active") as release, \
             fake_worker(check_login=lambda: {"status": "error", "detail": "nope"}):
            with self.assertRaises(Conflict):
                bj.verify_login()
        release.assert_called_once()


class BrowserJobScaffold(TempFiles):
    def test_missing_playwright_errors_and_releases(self):
        calls = []
        with mock.patch.object(bj, "update_job", side_effect=lambda jid, **k: calls.append(k)), \
             mock.patch.object(bj, "release_active") as release, \
             mock.patch.dict(sys.modules, {"pplx_deep_research": None}):
            bj.browser_job("j1", running_msg="go", call=lambda w, p: None,
                           handle=lambda r: None, install_hint=" hint")
        self.assertEqual(calls[-1]["state"], "error")
        self.assertIn("Playwright not available", calls[-1]["error"])
        self.assertIn("hint", calls[-1]["error"])
        release.assert_called_once()

    def test_happy_path_runs_handle_and_releases(self):
        handled = {}
        with mock.patch.object(bj, "update_job"), \
             mock.patch.object(bj, "release_active") as release, \
             fake_worker():
            bj.browser_job("j1", running_msg="go",
                           call=lambda w, p: {"status": "done"},
                           handle=lambda r: handled.update(r))
        self.assertEqual(handled, {"status": "done"})
        release.assert_called_once()

    def test_worker_exception_errors_and_releases(self):
        calls = []
        def boom(w, p):
            raise RuntimeError("worker blew up")
        with mock.patch.object(bj, "update_job", side_effect=lambda jid, **k: calls.append(k)), \
             mock.patch.object(bj, "release_active") as release, \
             fake_worker():
            bj.browser_job("j1", running_msg="go", call=boom, handle=lambda r: None)
        self.assertEqual(calls[-1]["state"], "error")
        self.assertIn("worker blew up", calls[-1]["error"])
        release.assert_called_once()


class SaveRunResult(TempFiles):
    def test_persists_artifact_and_marks_done(self):
        calls = []
        with mock.patch.object(bj, "save_deep_artifact", return_value={"path": "x"}) as save, \
             mock.patch.object(bj, "update_job", side_effect=lambda jid, **k: calls.append(k)):
            bj.save_run_result("j1", {"report": "hello", "citations": [1]}, "fintech", "2026-06-20",
                               source_url="http://run", auth_label="deep run", done_msg="report saved")
        save.assert_called_once()
        self.assertEqual(calls[-1]["state"], "done")
        self.assertEqual(calls[-1]["result"]["report_chars"], 5)
        self.assertTrue(bj.get_auth_state()["logged_in"])  # auth refreshed on success

    def test_save_failure_marks_error(self):
        calls = []
        with mock.patch.object(bj, "save_deep_artifact", side_effect=RuntimeError("disk full")), \
             mock.patch.object(bj, "update_job", side_effect=lambda jid, **k: calls.append(k)):
            bj.save_run_result("j1", {"report": "x"}, "seg", "2026-06-20",
                               source_url=None, auth_label="deep run", done_msg="done")
        self.assertEqual(calls[-1]["state"], "error")
        self.assertIn("saved nothing", calls[-1]["error"])


class StartGuards(TempFiles):
    def test_deep_research_validates_inputs(self):
        with self.assertRaises(ValueError):
            bj.start_deep_research({"segment": "s", "date": "06-2026", "prompt": "p"})
        with self.assertRaises(ValueError):
            bj.start_deep_research({"segment": "s", "date": "2026-06-20", "prompt": "   "})
        with self.assertRaises(ValueError):
            bj.start_deep_research({"segment": "s", "date": "2026-06-20", "prompt": "p",
                                    "window_mode": "warp"})

    def test_deep_research_conflicts_without_a_slot(self):
        with mock.patch.object(bj, "claim_active", return_value=False), \
             mock.patch.object(bj, "slots_busy_msg", return_value="busy"):
            with self.assertRaises(Conflict):
                bj.start_deep_research({"segment": "s", "date": "2026-06-20", "prompt": "p"})

    def test_deep_research_spawns_when_clear(self):
        with mock.patch.object(bj, "claim_active", return_value=True), \
             mock.patch.object(bj, "new_job", return_value={"id": "j1"}), \
             mock.patch.object(bj, "public", side_effect=lambda j: j), \
             mock.patch.object(bj.threading, "Thread") as Thread:
            out = bj.start_deep_research({"segment": "Fin Tech", "date": "2026-06-20", "prompt": "p"})
        Thread.assert_called_once()
        self.assertEqual(out, {"id": "j1"})

    def test_import_requires_segment_and_perplexity_url(self):
        with self.assertRaises(ValueError):
            bj.start_import({"segment": "", "url": "https://perplexity.ai/x"})
        with self.assertRaises(ValueError):
            bj.start_import({"segment": "s", "date": "2026-06-20", "url": "https://example.com/x"})

    def test_login_conflicts_without_a_slot(self):
        with mock.patch.object(bj, "claim_active", return_value=False), \
             mock.patch.object(bj, "slots_busy_msg", return_value="busy"):
            with self.assertRaises(Conflict):
                bj.start_login()


class HandleDispatch(TempFiles):
    """Drive the run_* bodies end to end through the scaffold with a fake worker
    returning a chosen status, asserting the resulting job state."""

    def _run(self, target, worker_attrs, *args):
        calls = []
        with mock.patch.object(bj, "update_job", side_effect=lambda jid, **k: calls.append(k)), \
             mock.patch.object(bj, "release_active"), \
             mock.patch.object(bj, "save_deep_artifact", return_value={"ok": True}), \
             fake_worker(**worker_attrs):
            target("j1", *args)
        return calls

    def test_deep_job_done_saves_report(self):
        calls = self._run(bj.run_deep_job,
                          {"run_deep_research": lambda *a, **k: {"status": "done", "report": "r", "citations": []}},
                          "fintech", "2026-06-20", "prompt", "offscreen")
        self.assertEqual(calls[-1]["state"], "done")

    def test_deep_job_needs_login_flips_auth(self):
        calls = self._run(bj.run_deep_job,
                          {"run_deep_research": lambda *a, **k: {"status": "needs_login"}},
                          "fintech", "2026-06-20", "prompt", "offscreen")
        self.assertEqual(calls[-1]["state"], "needs_login")
        self.assertFalse(bj.get_auth_state()["logged_in"])

    def test_login_job_timeout_errors(self):
        calls = self._run(bj.run_login_job, {"ensure_login": lambda **k: {"status": "timeout"}})
        self.assertEqual(calls[-1]["state"], "error")

    def test_import_job_done_saves(self):
        calls = self._run(bj.run_import_job,
                          {"fetch_by_url": lambda *a, **k: {"status": "done", "report": "r", "citations": []}},
                          "fintech", "2026-06-20", "https://perplexity.ai/search/x")
        self.assertEqual(calls[-1]["state"], "done")


if __name__ == "__main__":
    unittest.main()
