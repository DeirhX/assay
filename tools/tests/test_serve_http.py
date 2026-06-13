"""Tests for serve.py's HTTP request guards: malformed JSON bodies must be a
400 (not a silent {} or a 500), oversized bodies are refused, and main()
refuses to bind a non-loopback host. Runs a real ThreadingHTTPServer on an
ephemeral loopback port -- offline, no data submodule needed."""

from __future__ import annotations

import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

import _support  # noqa: F401
import serve


class RequestGuards(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)

    def _post(self, path: str, body: bytes, headers: dict | None = None):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=body,
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read().decode("utf-8"))

    def test_malformed_json_is_400(self):
        status, payload = self._post("/api/deep-job/cancel", b"{not json at all")
        self.assertEqual(status, 400)
        self.assertIn("malformed JSON", payload["error"])

    def test_non_object_json_is_400(self):
        status, payload = self._post("/api/deep-job/cancel", b'["a", "list"]')
        self.assertEqual(status, 400)
        self.assertIn("must be an object", payload["error"])

    def test_oversized_body_is_400(self):
        # Lie about the size in the header; the guard must fire before reading.
        status, payload = self._post(
            "/api/deep-job/cancel", b"{}",
            headers={"Content-Length": str(serve._MAX_BODY_BYTES + 1)},
        )
        self.assertEqual(status, 400)
        self.assertIn("too large", payload["error"])

    def test_valid_body_still_works(self):
        status, payload = self._post("/api/deep-job/cancel", b'{"id": ""}')
        self.assertEqual(status, 400)  # empty id is rejected by the endpoint...
        self.assertIn("missing job id", payload["error"])  # ...not by the body guard


class HoldingsSyncJob(unittest.TestCase):
    """The IBKR sync runs as a registered background job, not a blocking request.
    The underlying Flex pull (_sync_holdings) is mocked so these stay offline."""

    def _wait(self, job_id, *, timeout=4.0, state=None):
        deadline = time.time() + timeout
        terminal = (state,) if state else ("done", "error", "cancelled")
        while time.time() < deadline:
            pub = serve.jobs.get_public(job_id)
            if pub and pub["state"] in terminal:
                return pub
            time.sleep(0.02)
        self.fail(f"job {job_id} never reached {terminal}")

    def test_sync_runs_as_registered_job_and_carries_result(self):
        def fake_sync(progress=None):
            if progress:
                progress("working…")
            return {"site": {"ok": True, "written": []}, "generated_at": "2026-06-13T00:00:00+00:00"}

        with mock.patch.object(serve, "_sync_holdings", side_effect=fake_sync):
            job = serve._start_holdings_sync()
            self.assertEqual(job["kind"], "ibkr_sync")
            pub = self._wait(job["id"])
        self.assertEqual(pub["state"], "done")
        self.assertTrue(pub["result"]["site"]["ok"])

    def test_only_one_sync_at_a_time(self):
        release = threading.Event()

        def blocker(progress=None):
            release.wait(timeout=5)
            return {"site": None, "generated_at": None}

        with mock.patch.object(serve, "_sync_holdings", side_effect=blocker):
            job = serve._start_holdings_sync()
            self._wait(job["id"], state="running")
            with self.assertRaises(RuntimeError):
                serve._start_holdings_sync()   # second sync is refused while one runs
            release.set()
            self._wait(job["id"])

    def test_sync_failure_becomes_error_state(self):
        with mock.patch.object(serve, "_sync_holdings",
                               side_effect=ValueError("IBKR credentials not configured")):
            job = serve._start_holdings_sync()
            pub = self._wait(job["id"])
        self.assertEqual(pub["state"], "error")
        self.assertIn("credentials", pub["error"])


class HostGuard(unittest.TestCase):
    def test_non_loopback_host_is_refused(self):
        # The guard fires before any socket is bound, so this never serves.
        with mock.patch("sys.argv", ["serve.py", "--host", "0.0.0.0"]):
            self.assertEqual(serve.main(), 2)


if __name__ == "__main__":
    unittest.main()
