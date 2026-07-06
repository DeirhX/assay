"""Tests for the read-only MCP adapter (mcp_server.py): the allowlist invariant
(no trade routes, no mutating POSTs), request building from tool arguments
(symbol/slug sanitization, what-if body validation), and the stdio JSON-RPC
loop (initialize, tools/list, tools/call, notifications, error paths) -- all
offline against a stubbed fetch."""

from __future__ import annotations

import io
import json
import unittest
import urllib.error
from typing import Any

import _support  # noqa: F401  -- puts tools/ on sys.path
import mcp_server
from mcp_server import TOOLS, TOOLS_BY_NAME, handle_message, run_tool, serve_stdio


def ok_fetch(method: str, path: str, body: dict[str, Any] | None) -> str:
    return json.dumps({"echo": {"method": method, "path": path, "body": body}})


class Registry(unittest.TestCase):
    def test_names_unique_and_schemas_are_objects(self):
        names = [t.name for t in TOOLS]
        self.assertEqual(len(names), len(set(names)))
        for t in TOOLS:
            self.assertEqual(t.schema.get("type"), "object", t.name)
            self.assertTrue(t.description.strip(), t.name)

    def test_every_route_is_api_and_never_trade(self):
        # The read-only guarantee: no tool can build a path outside /api/ or
        # anywhere near the trade desk, and only whatif may POST.
        for t in TOOLS:
            args = _minimal_args(t.name)
            method, path, body = t.build(args)
            self.assertTrue(path.startswith("/api/"), (t.name, path))
            self.assertNotIn("/api/trade", path, t.name)
            if t.name == "simulate_basket":
                self.assertEqual(method, "POST")
            else:
                self.assertEqual(method, "GET", t.name)
                self.assertIsNone(body, t.name)


def _minimal_args(name: str) -> dict[str, Any]:
    if name in ("ticker_research", "price_history"):
        return {"symbol": "AMD"}
    if name == "segment_peers":
        return {"segment": "semiconductors"}
    if name == "simulate_basket":
        return {"trades": [{"symbol": "AMD", "delta_czk": -1000}]}
    return {}


class BuildRequests(unittest.TestCase):
    def test_symbol_is_sanitized_and_quoted(self):
        _m, path, _b = TOOLS_BY_NAME["ticker_research"].build({"symbol": "005930.ks"})
        self.assertEqual(path, "/api/research/005930.KS")

    def test_path_traversal_symbol_rejected(self):
        res = run_tool("ticker_research", {"symbol": "../secrets"}, ok_fetch)
        self.assertTrue(res.get("isError"))
        self.assertIn("bad arguments", res["content"][0]["text"])

    def test_bad_segment_slug_rejected(self):
        for slug in ("../x", "", "a b", "-lead"):
            res = run_tool("segment_peers", {"segment": slug}, ok_fetch)
            self.assertTrue(res.get("isError"), slug)
        # Case is normalized, not rejected -- serve.py lowercases the slug too.
        _m, path, _b = TOOLS_BY_NAME["segment_peers"].build({"segment": "Semiconductors"})
        self.assertEqual(path, "/api/segment/semiconductors")

    def test_range_validated_with_default(self):
        _m, path, _b = TOOLS_BY_NAME["risk_report"].build({})
        self.assertEqual(path, "/api/risk?range=1y")
        res = run_tool("risk_report", {"range": "7y"}, ok_fetch)
        self.assertTrue(res.get("isError"))

    def test_whatif_body_normalized(self):
        _m, path, body = TOOLS_BY_NAME["simulate_basket"].build(
            {"trades": [{"symbol": "amd", "delta_czk": "-1500"}]})
        self.assertEqual(path, "/api/whatif")
        self.assertEqual(body, {"trades": [{"symbol": "AMD", "delta_czk": -1500.0}]})

    def test_whatif_rejects_malformed(self):
        for args in ({}, {"trades": []}, {"trades": ["AMD"]},
                     {"trades": [{"symbol": "AMD"}]}):
            res = run_tool("simulate_basket", args, ok_fetch)
            self.assertTrue(res.get("isError"), args)


class RunTool(unittest.TestCase):
    def test_success_wraps_text(self):
        res = run_tool("holdings", {}, ok_fetch)
        self.assertNotIn("isError", res)
        echoed = json.loads(res["content"][0]["text"])
        self.assertEqual(echoed["echo"]["path"], "/api/holdings")

    def test_server_down_names_the_fix(self):
        def down(method: str, path: str, body: dict[str, Any] | None) -> str:
            raise urllib.error.URLError(ConnectionRefusedError(111, "refused"))
        res = run_tool("holdings", {}, down)
        self.assertTrue(res.get("isError"))
        self.assertIn("serve.py", res["content"][0]["text"])

    def test_upstream_http_error_relayed(self):
        def gone(method: str, path: str, body: dict[str, Any] | None) -> str:
            raise urllib.error.HTTPError(
                "http://127.0.0.1:6060" + path, 404, "not found", None,  # type: ignore[arg-type]
                io.BytesIO(b'{"error": "no cached research for ZZZ"}'))
        res = run_tool("ticker_research", {"symbol": "ZZZ"}, gone)
        self.assertTrue(res.get("isError"))
        self.assertIn("404", res["content"][0]["text"])
        self.assertIn("no cached research", res["content"][0]["text"])

    def test_oversized_payload_refused(self):
        def huge(method: str, path: str, body: dict[str, Any] | None) -> str:
            return "x" * (mcp_server.MAX_RELAY_CHARS + 1)
        res = run_tool("portfolio_history", {}, huge)
        self.assertTrue(res.get("isError"))
        self.assertIn("too large", res["content"][0]["text"])


class JsonRpc(unittest.TestCase):
    def test_initialize_echoes_protocol(self):
        reply = handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-03-26"}}, ok_fetch)
        self.assertEqual(reply["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(reply["result"]["serverInfo"]["name"], "assay")
        self.assertIn("tools", reply["result"]["capabilities"])

    def test_tools_list_matches_registry(self):
        reply = handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, ok_fetch)
        listed = {t["name"] for t in reply["result"]["tools"]}
        self.assertEqual(listed, set(TOOLS_BY_NAME))
        for t in reply["result"]["tools"]:
            self.assertIn("inputSchema", t)

    def test_unknown_tool_is_invalid_params(self):
        reply = handle_message(
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "place_order", "arguments": {}}}, ok_fetch)
        self.assertEqual(reply["error"]["code"], -32602)

    def test_unknown_method_and_notifications(self):
        reply = handle_message({"jsonrpc": "2.0", "id": 4, "method": "resources/list"}, ok_fetch)
        self.assertEqual(reply["error"]["code"], -32601)
        self.assertIsNone(handle_message(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}, ok_fetch))

    def test_stdio_loop_end_to_end(self):
        lines = [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            "not json at all",
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {"name": "portfolio_overview"}}),
        ]
        out = io.StringIO()
        serve_stdio(io.StringIO("\n".join(lines) + "\n"), out, ok_fetch)
        replies = [json.loads(x) for x in out.getvalue().strip().splitlines()]
        self.assertEqual(len(replies), 3)  # init + parse error + call (no notification reply)
        self.assertEqual(replies[0]["id"], 1)
        self.assertEqual(replies[1]["error"]["code"], -32700)
        self.assertEqual(json.loads(replies[2]["result"]["content"][0]["text"])
                         ["echo"]["path"], "/api/overview")


class LoopbackGuard(unittest.TestCase):
    def test_non_loopback_base_refused(self):
        with self.assertRaises(SystemExit):
            mcp_server.make_fetch("http://example.com:6060")

    def test_loopback_accepted(self):
        self.assertTrue(callable(mcp_server.make_fetch("http://127.0.0.1:6060")))


if __name__ == "__main__":
    unittest.main()
