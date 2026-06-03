#!/usr/bin/env python3
"""Local, interactive research server for the rebalancing app.

Serves the single-page UI in ``web/`` and a small JSON API that runs the
on-demand deep-dive puller. Stdlib only -- no Flask, no FastAPI, no pip install,
no wheel roulette on bleeding-edge Python. Just::

    py -3 tools/serve.py
    # then open http://127.0.0.1:8765

Design notes / honest caveats:
* Binds to 127.0.0.1 only. This is a single-user local tool, not a web service.
  It happily runs code (network pulls) on request; do not expose it.
* Pulls are synchronous per request. A single ticker is a few seconds; a full
  segment is ~20 sequential pulls, so expect 30-60s. The server is threaded, so
  the UI stays responsive meanwhile.
* Secrets (FMP key) come from the environment / secrets.env, never from disk in
  this repo.
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = REPO_ROOT / "web"
DATA_DIR = REPO_ROOT / "data"
RESEARCH_DIR = DATA_DIR / "research"
SEGMENT_DEF_DIR = DATA_DIR / "segments"
SEGMENT_OUT_DIR = RESEARCH_DIR / "segments"
HOLDINGS_JSON = DATA_DIR / "current-holdings.json"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import research_pull  # noqa: E402

_PULL_LOCK = threading.Lock()  # serialize outbound pulls; be polite to sources

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}


def _load(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _holdings_payload():
    data = _load(HOLDINGS_JSON) or {}
    positions = data.get("positions", [])
    return {
        "net_asset_value": data.get("net_asset_value"),
        "generated_at": data.get("generated_at"),
        "sizing_legend": data.get("sizing_legend", {}),
        "positions": [
            {
                "symbol": p["symbol"],
                "description": p.get("description"),
                "percent_of_nav": p.get("percent_of_nav"),
                "unrealized_pnl": p.get("unrealized_pnl"),
                "issuer_country_code": p.get("issuer_country_code"),
            }
            for p in positions
        ],
    }


def _segments_list():
    out = []
    for path in sorted(SEGMENT_DEF_DIR.glob("*.json")):
        definition = _load(path) or {}
        out.append({
            "name": path.stem,
            "title": definition.get("title", path.stem.title()),
            "count": len(definition.get("members", [])),
            "cached": (SEGMENT_OUT_DIR / path.name).exists(),
        })
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "rebalancing-research/1.0"

    # ---- helpers -----------------------------------------------------------
    def _send_json(self, payload, status: int = 200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status: int, message: str):
        self._send_json({"error": message}, status=status)

    def _serve_static(self, rel: str):
        if rel in ("", "/"):
            rel = "index.html"
        target = (WEB_DIR / rel.lstrip("/")).resolve()
        if WEB_DIR not in target.parents and target != WEB_DIR:
            return self._send_error_json(403, "forbidden")
        if not target.is_file():
            return self._send_error_json(404, f"not found: {rel}")
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", _CONTENT_TYPES.get(target.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def log_message(self, fmt, *args):  # quieter, single-line logs
        sys.stderr.write(f"  {self.address_string()} {fmt % args}\n")

    # ---- routing -----------------------------------------------------------
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return None
        if not path.startswith("/api/"):
            return self._serve_static(path)

        if path == "/api/holdings":
            return self._send_json(_holdings_payload())
        if path == "/api/segments":
            return self._send_json({"segments": _segments_list()})
        if path.startswith("/api/research/"):
            sym = path.rsplit("/", 1)[-1].upper()
            rec = _load(RESEARCH_DIR / f"{sym}.json")
            return self._send_json(rec) if rec else self._send_error_json(404, f"no cached research for {sym}")
        if path.startswith("/api/segment/"):
            name = path.rsplit("/", 1)[-1].lower()
            rec = _load(SEGMENT_OUT_DIR / f"{name}.json")
            return self._send_json(rec) if rec else self._send_error_json(404, f"no cached segment {name}")
        return self._send_error_json(404, "unknown endpoint")

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path.startswith("/api/pull/"):
                sym = path.rsplit("/", 1)[-1].upper()
                if not sym.isascii() or not sym or len(sym) > 12:
                    return self._send_error_json(400, "bad symbol")
                with _PULL_LOCK:
                    rec = research_pull.pull_ticker(sym)
                return self._send_json(rec)

            if path.startswith("/api/pull-segment/"):
                name = path.rsplit("/", 1)[-1].lower()
                if not (SEGMENT_DEF_DIR / f"{name}.json").exists():
                    return self._send_error_json(404, f"unknown segment {name}")
                with _PULL_LOCK:
                    rec = research_pull.pull_segment(name)
                return self._send_json(rec)

            if path.startswith("/api/thesis/"):
                sym = path.rsplit("/", 1)[-1].upper()
                rec = _load(RESEARCH_DIR / f"{sym}.json")
                if not rec:
                    return self._send_error_json(404, f"pull {sym} before saving a thesis")
                body = self._read_body()
                import datetime as dt
                rec["thesis"] = {
                    "summary": body.get("summary", ""),
                    "action": body.get("action", ""),
                    "drivers": body.get("drivers", []),
                    "downside_triggers": body.get("downside_triggers", []),
                    "as_of": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                }
                (RESEARCH_DIR / f"{sym}.json").write_text(
                    json.dumps(rec, indent=2) + "\n", encoding="utf-8"
                )
                return self._send_json(rec)
        except research_pull.ProviderError as exc:  # type: ignore[attr-defined]
            return self._send_error_json(502, f"data source error: {exc}")
        except Exception as exc:  # noqa: BLE001
            return self._send_error_json(500, f"{type(exc).__name__}: {exc}")

        return self._send_error_json(404, "unknown endpoint")


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    _load_secrets_env()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print(f"Rebalancing research server on {url}  (Ctrl+C to stop)")
    print("  Static UI + JSON API. Localhost only. Pulls run live data sources.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        httpd.server_close()
    return 0


def _load_secrets_env():
    """Best-effort load of repo-root secrets.env (gitignored) for FMP_API_KEY."""
    env_path = REPO_ROOT / "secrets.env"
    if not env_path.exists():
        return
    import os
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


if __name__ == "__main__":
    raise SystemExit(main())
