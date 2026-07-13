"""Shared HTTP harness for serve.py integration tests."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from typing import Any
from unittest import TestCase

import serve


class ServeHttpCase(TestCase):
    """Ephemeral loopback ThreadingHTTPServer bound to serve.Handler."""

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

    def _url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: bytes | dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, Any]:
        data: bytes | None
        req_headers = dict(headers or {})
        if isinstance(body, dict):
            data = json.dumps(body).encode("utf-8")
            req_headers.setdefault("Content-Type", "application/json")
        else:
            data = body
        if data is not None:
            req_headers.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(
            self._url(path), data=data, headers=req_headers, method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read().decode("utf-8"))

    def get_json(self, path: str) -> tuple[int, Any]:
        return self.request(path, method="GET")

    def post_json(
        self,
        path: str,
        body: bytes | dict,
        *,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, Any]:
        return self.request(path, method="POST", body=body, headers=headers)
