"""Tests for serve.py's HTTP request guards: malformed JSON bodies must be a
400 (not a silent {} or a 500), oversized bodies are refused, and main()
refuses to bind a non-loopback host. Runs a real ThreadingHTTPServer on an
ephemeral loopback port -- offline, no data submodule needed."""

from __future__ import annotations

import json
import threading
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


class HostGuard(unittest.TestCase):
    def test_non_loopback_host_is_refused(self):
        # The guard fires before any socket is bound, so this never serves.
        with mock.patch("sys.argv", ["serve.py", "--host", "0.0.0.0"]):
            self.assertEqual(serve.main(), 2)


if __name__ == "__main__":
    unittest.main()
