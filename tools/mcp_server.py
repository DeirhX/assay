#!/usr/bin/env python3
"""Read-only MCP server over the local Assay API -- ask your portfolio questions.

Exposes a hard-allowlisted subset of serve.py's GET endpoints (plus the pure
what-if simulator) as MCP tools over stdio JSON-RPC, so an MCP client (Claude
Code, Claude Desktop, ...) can act as a conversational analyst over live
portfolio state: "which underweights have the best momentum?", "what does the
staged basket do to my cash floor?".

Safety posture, mirroring the rest of the repo:

* **Read-only by construction.** The registry below is the only route table;
  nothing under ``/api/trade/`` is present, no mutating POST is present, and
  the one POST that is exposed (``/api/whatif``) is a pure recompute that
  writes nothing. Advice != orders (ARCHITECTURE.md invariant 11.4) holds:
  this surface physically cannot stage, place, or cancel anything.
* **Loopback only.** The upstream base must resolve to localhost; requests to
  any other host are refused at startup.
* **Thin adapter.** No domain logic here -- serve.py stays the single source
  of truth. If serve.py is not running, tools return a clear error saying so.

Privacy note: connecting an LLM client sends portfolio values to that model's
provider -- the same trust decision as the built-in LLM analysis jobs
(analysis_jobs.py), made explicit here.

Setup (Claude Code):

    claude mcp add assay -- py -3 tools/mcp_server.py

or Claude Desktop / any MCP client, stdio transport:

    {"command": "py", "args": ["-3", "<repo>/tools/mcp_server.py"]}

Env: ``ASSAY_API_BASE`` overrides the upstream (default http://127.0.0.1:6060).

The JSON-RPC loop implements the stable MCP core (initialize, tools/list,
tools/call, ping) by hand -- newline-delimited JSON over stdio -- keeping the
backend's stdlib-only stance instead of pulling in the SDK.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TextIO

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
from store import safe_symbol

DEFAULT_BASE = "http://127.0.0.1:6060"
HTTP_TIMEOUT_S = 120  # a cold /api/risk pull can take a while
# Refuse to relay a payload past this size: it would blow the client's context
# anyway, and every large endpoint has a narrower alternative.
MAX_RELAY_CHARS = 800_000

PROTOCOL_FALLBACK = "2025-06-18"
SERVER_INFO = {"name": "assay", "version": "0.1.0"}

# Mirrors serve.py's PRICE_HISTORY_RANGES keys (kept as a literal so this
# module never imports serve, which starts threads on some paths).
RANGES = ("1d", "1w", "1mo", "3mo", "6mo", "1y", "2y", "5y", "max")

# A built request: everything call_api needs, nothing it doesn't.
RequestSpec = tuple[str, str, dict[str, Any] | None]  # (method, path+query, json body)


def _slug(value: Any) -> str:
    s = str(value or "").strip().lower()
    if not s or len(s) > 64 or not all(c.isalnum() or c == "-" for c in s) or s[0] == "-":
        raise ValueError(f"bad segment slug: {value!r}")
    return s


def _range(args: dict[str, Any]) -> str:
    rng = str(args.get("range") or "1y").lower()
    if rng not in RANGES:
        raise ValueError(f"bad range {rng!r}; one of {', '.join(RANGES)}")
    return rng


def _sym_path(prefix: str, args: dict[str, Any], query: str = "") -> str:
    sym = safe_symbol(str(args.get("symbol") or ""))
    return f"{prefix}{urllib.parse.quote(sym, safe='')}{query}"


def _whatif_body(args: dict[str, Any]) -> dict[str, Any]:
    trades = args.get("trades")
    if not isinstance(trades, list) or not trades:
        raise ValueError("trades must be a non-empty list of {symbol, delta_czk}")
    clean = []
    for t in trades:
        if not isinstance(t, dict):
            raise ValueError("each trade must be an object with symbol and delta_czk")
        clean.append({"symbol": safe_symbol(str(t.get("symbol") or "")),
                      "delta_czk": float(t["delta_czk"])})
    return {"trades": clean}


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict[str, Any]
    build: Callable[[dict[str, Any]], RequestSpec]


_NO_ARGS: dict[str, Any] = {"type": "object", "properties": {}}
_RANGE_ARG: dict[str, Any] = {
    "type": "object",
    "properties": {"range": {"type": "string", "enum": list(RANGES),
                             "description": "History window (default 1y)."}},
}
_SYMBOL_ARG: dict[str, Any] = {
    "type": "object",
    "properties": {"symbol": {"type": "string", "description": "Ticker symbol, e.g. AMD."}},
    "required": ["symbol"],
}

TOOLS: tuple[Tool, ...] = (
    Tool("portfolio_overview",
         "One aggregate snapshot of where everything stands: holdings freshness, rebalance-plan "
         "summary (out-of-band counts, cash status), staged basket, journal counters, research "
         "funnel, and the single suggested next step. Start here.",
         _NO_ARGS, lambda a: ("GET", "/api/overview", None)),
    Tool("holdings",
         "Current positions from the sanitized IBKR snapshot: symbols, quantities, marks, "
         "weights, currencies. The ground truth every plan is computed from.",
         _NO_ARGS, lambda a: ("GET", "/api/holdings", None)),
    Tool("rebalance_plan",
         "The full rebalance plan against the active target model (working draft if one exists): "
         "per-name current weight, target band, drift status (BELOW/IN/ABOVE), rule, suggested "
         "band-closing CZK delta, research overlay (decision, conflicts, price gates), and cash.",
         _NO_ARGS, lambda a: ("GET", "/api/rebalance", None)),
    Tool("target_model",
         "The live target allocation model as data: per-symbol bands {low, high} in percent of "
         "invested book, rules, sleeves with member caps, cash target, provenance.",
         _NO_ARGS, lambda a: ("GET", "/api/target-model", None)),
    Tool("risk_report",
         "Portfolio risk lens over held names: correlation matrix, effective bets, co-movement "
         "share, volatility, and factor-shock stress estimates. Computed from daily closes over "
         "the window; can take up to a minute cold.",
         _RANGE_ARG, lambda a: ("GET", f"/api/risk?range={_range(a)}", None)),
    Tool("portfolio_history",
         "Full portfolio ledger: day-by-day NAV series plus every executed trade. Large payload "
         "(years of history) -- prefer portfolio_overview unless the question is about history.",
         _NO_ARGS, lambda a: ("GET", "/api/portfolio-history", None)),
    Tool("journal",
         "The decision journal: every logged buy/trim/hold decision with thesis, size, and "
         "outcome calibration (hit rate vs later prices).",
         _NO_ARGS, lambda a: ("GET", "/api/journal", None)),
    Tool("price_levels",
         "Human-confirmed price triggers per symbol (buy-below / trim-above), lock state, and "
         "latest gate quotes. A locked level gates the plan row and becomes an order limit.",
         _NO_ARGS, lambda a: ("GET", "/api/price-levels", None)),
    Tool("list_segments",
         "All defined research segments (peer universes / market lenses) with member counts and "
         "cache freshness. Segments may overlap; they are lenses, not allocation sleeves.",
         _NO_ARGS, lambda a: ("GET", "/api/segments", None)),
    Tool("segment_peers",
         "The cached peer-comparison table for one segment: per-member decision, score, held%, "
         "price, market cap, forward P/E, P/S, revenue growth, gross margin, 3M/12M momentum, "
         "distance from 52-week high.",
         {"type": "object",
          "properties": {"segment": {"type": "string",
                                     "description": "Segment slug, e.g. add-ai-semiconductors "
                                                    "(see list_segments)."}},
          "required": ["segment"]},
         lambda a: ("GET", f"/api/segment/{_slug(a.get('segment'))}", None)),
    Tool("ticker_research",
         "The cached research dossier for one ticker: source-stamped fundamentals with "
         "cross-checks (Yahoo vs SEC vs FMP), momentum, and the human thesis block. 404s if the "
         "name was never pulled.",
         _SYMBOL_ARG, lambda a: ("GET", _sym_path("/api/research/", a), None)),
    Tool("price_history",
         "OHLC price history for one ticker (live Yahoo fetch, public market data).",
         {"type": "object",
          "properties": {"symbol": _SYMBOL_ARG["properties"]["symbol"],
                         "range": _RANGE_ARG["properties"]["range"]},
          "required": ["symbol"]},
         lambda a: ("GET", _sym_path("/api/price-history/", a, f"?range={_range(a)}"), None)),
    Tool("exit_plan",
         "Advisory scale-out ladders for the current trim set: tax-timed (Czech 3-year "
         "exemption aware), liquidity-sliced sell schedules. Advice only.",
         _NO_ARGS, lambda a: ("GET", "/api/exit-plan", None)),
    Tool("simulate_basket",
         "Pure what-if: recompute post-trade weights, band statuses, cash vs floor, and realized "
         "taxable gain for a hypothetical basket of CZK deltas. Writes nothing, stages nothing, "
         "places nothing -- it is the same simulator the planner UI uses.",
         {"type": "object",
          "properties": {"trades": {
              "type": "array", "minItems": 1,
              "items": {"type": "object",
                        "properties": {"symbol": {"type": "string"},
                                       "delta_czk": {"type": "number",
                                                     "description": "Signed CZK: + buy, - trim."}},
                        "required": ["symbol", "delta_czk"]}}},
          "required": ["trades"]},
         lambda a: ("POST", "/api/whatif", _whatif_body(a))),
)

TOOLS_BY_NAME: dict[str, Tool] = {t.name: t for t in TOOLS}

Fetch = Callable[[str, str, dict[str, Any] | None], str]


def make_fetch(base: str) -> Fetch:
    """Real upstream caller. Refuses non-loopback bases outright."""
    host = urllib.parse.urlsplit(base).hostname or ""
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit(f"ASSAY_API_BASE must be loopback, got {base!r}")

    def fetch(method: str, path: str, body: dict[str, Any] | None) -> str:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(base.rstrip("/") + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:  # noqa: S310 (loopback)
            return resp.read().decode("utf-8", "replace")

    return fetch


def run_tool(name: str, args: dict[str, Any], fetch: Fetch) -> dict[str, Any]:
    """Execute one tools/call. Tool-level failures come back as isError results
    (per MCP), so the model can read the message and adjust; only an unknown
    tool is a protocol error (raised as ValueError by the caller's lookup)."""
    tool = TOOLS_BY_NAME[name]
    try:
        method, path, body = tool.build(args or {})
    except (ValueError, TypeError, KeyError) as exc:
        return _err_result(f"bad arguments: {exc}")
    try:
        text = fetch(method, path, body)
    except urllib.error.HTTPError as exc:  # upstream 4xx/5xx: relay its message
        detail = exc.read().decode("utf-8", "replace")[:2000]
        return _err_result(f"assay API {exc.code}: {detail}")
    except (urllib.error.URLError, ConnectionError, OSError) as exc:
        return _err_result(
            f"could not reach the Assay server ({exc}). Is serve.py running? "
            f"Start it with: py -3 tools/serve.py")
    if len(text) > MAX_RELAY_CHARS:
        return _err_result(
            f"response too large to relay ({len(text)} chars). Use a narrower tool "
            f"(e.g. portfolio_overview instead of portfolio_history).")
    return {"content": [{"type": "text", "text": text}]}


def _err_result(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


# ---- JSON-RPC over stdio ----------------------------------------------------
def _response(msg_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def handle_message(msg: dict[str, Any], fetch: Fetch) -> dict[str, Any] | None:
    """One JSON-RPC message in, at most one response out (None = notification)."""
    method = msg.get("method")
    msg_id = msg.get("id")
    is_notification = "id" not in msg
    if method == "initialize":
        params = msg.get("params") or {}
        proto = params.get("protocolVersion") or PROTOCOL_FALLBACK
        return _response(msg_id, {"protocolVersion": proto,
                                  "capabilities": {"tools": {}},
                                  "serverInfo": SERVER_INFO})
    if method == "ping":
        return _response(msg_id, {})
    if method == "tools/list":
        return _response(msg_id, {"tools": [
            {"name": t.name, "description": t.description, "inputSchema": t.schema}
            for t in TOOLS]})
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name") or ""
        if name not in TOOLS_BY_NAME:
            return _error(msg_id, -32602, f"unknown tool: {name}")
        return _response(msg_id, run_tool(name, params.get("arguments") or {}, fetch))
    if is_notification:  # notifications/initialized, cancellations, ...
        return None
    return _error(msg_id, -32601, f"method not found: {method}")


def serve_stdio(inp: TextIO, out: TextIO, fetch: Fetch) -> None:
    """Newline-delimited JSON-RPC loop. stdout carries only protocol messages;
    anything diagnostic goes to stderr."""
    for line in inp:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            print(json.dumps(_error(None, -32700, "parse error")), file=out, flush=True)
            continue
        if not isinstance(msg, dict):
            print(json.dumps(_error(None, -32600, "invalid request")), file=out, flush=True)
            continue
        reply = handle_message(msg, fetch)
        if reply is not None:
            print(json.dumps(reply), file=out, flush=True)


def main() -> None:
    base = config.config_value("ASSAY_API_BASE") or DEFAULT_BASE
    serve_stdio(sys.stdin, sys.stdout, make_fetch(base))


if __name__ == "__main__":
    main()
