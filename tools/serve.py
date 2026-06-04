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

import datetime as dt
import json
import re
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = REPO_ROOT / "web"
DATA_DIR = REPO_ROOT / "data"
RESEARCH_DIR = DATA_DIR / "research"
DEEP_DIR = RESEARCH_DIR / "deep"
SEGMENT_DEF_DIR = DATA_DIR / "segments"
SEGMENT_OUT_DIR = RESEARCH_DIR / "segments"
HOLDINGS_JSON = DATA_DIR / "current-holdings.json"
TARGET_MODEL_JSON = DATA_DIR / "target-model.json"
AUTH_STATE_FILE = DATA_DIR / "cache" / "pplx-auth.json"  # gitignored
ROOT_STATIC_FILES = {
    "site.css",
    "privacy.css",
    "privacy.js",
    "next-steps.html",
    "loser-position-recovery.html",
    "amd-detail.html",
    "arm-detail.html",
    "sofi-detail.html",
    "pypl-detail.html",
    "eeft-detail.html",
}

sys.path.insert(0, str(Path(__file__).resolve().parent))
import research_pull  # noqa: E402
import review_deep_research  # noqa: E402

_PULL_LOCK = threading.Lock()  # serialize outbound pulls; be polite to sources

# Deep Research browser jobs. Only ONE may run at a time: the Playwright worker
# opens a persistent Chrome profile (can't be opened twice) and each run spends
# scarce Pro quota. Jobs are in-memory and disappear on restart -- artifacts on
# disk are the durable record.
_DEEP_JOBS: dict[str, dict] = {}
_DEEP_JOBS_LOCK = threading.Lock()
_DEEP_ACTIVE = {"running": False}

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


def _write_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_text(path: Path, payload: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    if not slug or len(slug) > 64:
        raise ValueError("bad segment slug")
    return slug


def _safe_symbol(value: str) -> str:
    sym = (value or "").upper().strip()
    if not sym or len(sym) > 16 or not re.match(r"^[A-Z0-9.=-]+$", sym):
        raise ValueError(f"bad symbol: {value!r}")
    return sym


def _segment_path(name: str) -> Path:
    return SEGMENT_DEF_DIR / f"{_slugify(name)}.json"


def _validate_segment_definition(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("segment definition must be an object")
    title = str(raw.get("title") or "").strip()
    if not title:
        raise ValueError("segment title is required")
    members = raw.get("members")
    if not isinstance(members, list):
        raise ValueError("members must be a list")

    cleaned_members = []
    sleeves = set(str(s).strip() for s in raw.get("sleeves", []) if str(s).strip())
    for item in members:
        if not isinstance(item, dict):
            raise ValueError("each member must be an object")
        sym = _safe_symbol(str(item.get("symbol") or ""))
        sleeve = str(item.get("sleeve") or "other").strip().lower() or "other"
        sleeves.add(sleeve)
        cleaned = {
            "symbol": sym,
            "sleeve": sleeve,
        }
        for key in ("rationale", "confidence"):
            if item.get(key):
                cleaned[key] = str(item[key]).strip()
        cleaned_members.append(cleaned)

    cleaned = {
        "title": title,
        "kind": raw.get("kind") or "research",
        "status": raw.get("status") or "draft",
        "overlap_allowed": bool(raw.get("overlap_allowed", True)),
        "comment": str(raw.get("comment") or "").strip(),
        "sleeves": sorted(sleeves) or ["other"],
        "members": cleaned_members,
    }
    for key in ("origin", "description", "inclusion_criteria", "exclusion_criteria", "notes"):
        if key in raw:
            cleaned[key] = raw[key]
    return cleaned


_DRAFT_UNIVERSE = [
    ("NVDA", "compute", "Dominant AI accelerator and networking platform.", ["ai", "infrastructure", "semiconductor", "compute", "gpu"]),
    ("AMD", "compute", "AI GPU, EPYC CPU, and accelerator challenger.", ["ai", "infrastructure", "semiconductor", "compute", "gpu"]),
    ("ARM", "ip", "CPU IP and AI/data-center architecture exposure.", ["ai", "infrastructure", "semiconductor", "compute", "ip"]),
    ("TSM", "foundry", "Leading advanced-node foundry.", ["ai", "infrastructure", "semiconductor", "foundry"]),
    ("ASML", "equipment", "Lithography monopoly for advanced semis.", ["ai", "infrastructure", "semiconductor", "equipment"]),
    ("AMAT", "equipment", "Wafer-fab equipment exposure.", ["semiconductor", "equipment"]),
    ("LRCX", "equipment", "Etch/deposition wafer-fab equipment.", ["semiconductor", "equipment"]),
    ("KLAC", "equipment", "Process control and inspection equipment.", ["semiconductor", "equipment"]),
    ("TXN", "analog", "Analog and embedded semiconductor cycle exposure.", ["semiconductor", "analog", "industrial"]),
    ("ADI", "analog", "Analog and mixed-signal semiconductor exposure.", ["semiconductor", "analog", "industrial"]),
    ("MU", "memory", "Memory cycle and HBM/DRAM exposure.", ["semiconductor", "memory", "ai"]),
    ("SOXX", "etf", "Broad semiconductor ETF exposure.", ["semiconductor", "etf"]),
    ("XSD", "etf", "Equal-weight semiconductor ETF exposure.", ["semiconductor", "etf"]),
    ("PYPL", "payments", "Global checkout, wallet, Venmo, and merchant payments.", ["fintech", "payments", "value", "turnaround"]),
    ("SOFI", "digital-bank", "US digital bank, lending, and fintech infrastructure.", ["fintech", "bank", "credit", "growth"]),
    ("NU", "latam-fintech", "Scaled LatAm digital bank.", ["fintech", "bank", "latam", "growth"]),
    ("EEFT", "money-transfer", "ATM, prepaid/epay, and money-transfer network.", ["fintech", "payments", "money", "value", "turnaround"]),
]


def _draft_segment(query: str) -> dict:
    query = (query or "").strip()
    if not query:
        raise ValueError("query is required")
    slug = _slugify(query)
    tokens = set(re.findall(r"[a-z0-9]+", query.lower()))
    members = []
    for symbol, sleeve, rationale, keys in _DRAFT_UNIVERSE:
        score = len(tokens.intersection(keys))
        if score:
            members.append({
                "symbol": symbol,
                "sleeve": sleeve,
                "rationale": rationale,
                "confidence": "high" if score >= 2 else "medium",
            })
    llm_prompt = (
        f"Create a public-equity research segment for '{query}'. Return JSON with "
        "title, comment, sleeves, and members. Each member must include symbol, "
        "sleeve, rationale, and confidence. Exclude private companies unless you "
        "list them only in notes with public proxies."
    )
    return {
        "slug": slug,
        "definition": {
            "title": query.title(),
            "kind": "research",
            "status": "draft",
            "overlap_allowed": True,
            "comment": f"Draft research lens generated from freeform query: {query}",
            "sleeves": sorted({m["sleeve"] for m in members}) or ["other"],
            "members": members,
            "origin": {
                "type": "website_draft",
                "query": query,
            },
        },
        "llm_prompt": llm_prompt,
        "warnings": [] if members else [
            "No local candidates matched. Use the prompt with an LLM, paste/edit members, then validate before approving."
        ],
    }


def _segment_prompt(name: str) -> dict:
    slug = _slugify(name)
    definition = _load(SEGMENT_DEF_DIR / f"{slug}.json")
    if not definition:
        raise ValueError(f"unknown segment {slug}")
    holdings = _holdings_payload()
    held = {
        p["symbol"]: p.get("percent_of_nav")
        for p in holdings.get("positions", [])
        if p.get("percent_of_nav") is not None
    }
    symbols = [m["symbol"] for m in definition.get("members", [])]
    held_lines = [
        f"- {sym}: {held[sym]:.2f}% NAV"
        for sym in symbols
        if sym in held
    ]
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    prompt = (
        f"Deep research on my {definition.get('title', slug)} segment as a long-term investment, as of {today}.\n"
        f"Cover: {', '.join(symbols)}.\n"
        "Compare business momentum, valuation, competitive positioning, catalysts, risks, and likely recovery/underperformance over the next 6-24 months.\n"
        "Tie conclusions to portfolio action: keep, trim, sell, add, or wait.\n"
        "Include source citations and distinguish facts from opinion.\n"
        "Call out which numeric claims need deterministic verification.\n"
    )
    if held_lines:
        prompt += "\nCurrent owned weights:\n" + "\n".join(held_lines) + "\n"
    return {"segment": slug, "date": today, "prompt": prompt}


def _deep_runs() -> list[dict]:
    runs = {}
    for path in sorted(DEEP_DIR.glob("*")):
        if not path.is_file():
            continue
        name = path.name
        suffix = None
        stem = path.stem
        if name.endswith(".sources.json"):
            suffix = "sources"
            stem = name[:-len(".sources.json")]
        elif name.endswith(".target-proposal.json"):
            suffix = "proposal"
            stem = name[:-len(".target-proposal.json")]
        elif name.endswith(".review.md"):
            suffix = "review"
            stem = name[:-len(".review.md")]
        elif name.endswith(".md"):
            suffix = "report"
            stem = name[:-len(".md")]
        else:
            continue
        rec = runs.setdefault(stem, {"stem": stem, "files": {}})
        rec["files"][suffix] = str(path.relative_to(REPO_ROOT))
    return sorted(runs.values(), key=lambda r: r["stem"], reverse=True)


def _save_deep_artifact(body: dict) -> dict:
    segment = _slugify(str(body.get("segment") or ""))
    date = str(body.get("date") or dt.datetime.now(dt.timezone.utc).date().isoformat())
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise ValueError("date must be YYYY-MM-DD")
    stem = f"{segment}-{date}"
    report = str(body.get("report") or "").strip()
    if not report:
        raise ValueError("report text is required")
    citations = body.get("citations") or []
    if isinstance(citations, str):
        citations = json.loads(citations) if citations.strip() else []
    sources = {
        "schema_version": 1,
        "segment": segment,
        "source_url": body.get("source_url") or "",
        "mode": "perplexity_in_app_deep_research",
        "extracted_from": body.get("extracted_from") or "Perplexity Links tab",
        "extracted_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "citations": citations,
    }
    _write_text(DEEP_DIR / f"{stem}.md", report + "\n")
    _write_json(DEEP_DIR / f"{stem}.sources.json", sources)
    return {"stem": stem, "report": f"data/research/deep/{stem}.md", "sources": f"data/research/deep/{stem}.sources.json"}


def _apply_target_proposal(segment: str, date: str, confirm: bool) -> dict:
    if not confirm:
        raise ValueError("confirm=true is required")
    segment = _slugify(segment)
    proposal_path = DEEP_DIR / f"{segment}-{date}.target-proposal.json"
    proposal = _load(proposal_path)
    if not proposal:
        raise ValueError(f"proposal not found: {proposal_path.relative_to(REPO_ROOT)}")
    model = _load(TARGET_MODEL_JSON)
    if not model:
        raise ValueError("target model not found")
    targets = model.setdefault("targets", {})
    applied = []
    skipped = []
    for change in proposal.get("changes", []):
        sym = _safe_symbol(change.get("symbol", ""))
        if change.get("action") != "add_target":
            skipped.append({"symbol": sym, "reason": "unsupported action"})
            continue
        if sym in targets:
            skipped.append({"symbol": sym, "reason": "target already exists"})
            continue
        target = dict(change.get("proposed_target") or {})
        if not target:
            skipped.append({"symbol": sym, "reason": "missing proposed_target"})
            continue
        targets[sym] = target
        applied.append(sym)
    proposal["status"] = "applied" if applied else "reviewed"
    proposal["applied_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    proposal["applied_symbols"] = applied
    proposal["skipped"] = skipped
    _write_json(TARGET_MODEL_JSON, model)
    _write_json(proposal_path, proposal)
    return {"applied": applied, "skipped": skipped, "proposal": proposal}


def _holdings_payload():
    data = _load(HOLDINGS_JSON) or {}
    positions = data.get("positions", [])

    # Weight by actual market value, not the broker's percent_of_nav field. That
    # field is reliable for stocks but garbage for options (the SPY put is
    # stamped 100% -- a margin/notional artifact, not its ~870 CZK market value).
    # Recomputing from base_market_value over total invested reproduces the
    # familiar stock weights (the broker uses NAV-minus-cash as denominator) and
    # drops derivatives to their true tiny weight.
    invested = sum(
        p["base_market_value"]
        for p in positions
        if isinstance(p.get("base_market_value"), (int, float))
    )

    def weight(p):
        bmv = p.get("base_market_value")
        if not isinstance(bmv, (int, float)) or not invested:
            return None
        return bmv / invested * 100.0

    return {
        "net_asset_value": data.get("net_asset_value"),
        "invested_value": invested,
        "generated_at": data.get("generated_at"),
        "sizing_legend": data.get("sizing_legend", {}),
        "positions": [
            {
                "symbol": p["symbol"],
                "description": p.get("description"),
                "asset_class": p.get("asset_class"),
                "percent_of_nav": weight(p),
                "broker_percent_of_nav": p.get("percent_of_nav"),
                "base_market_value": p.get("base_market_value"),
                "currency": p.get("currency"),
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
            "kind": definition.get("kind", "research"),
            "status": definition.get("status", "approved"),
            "overlap_allowed": definition.get("overlap_allowed", True),
            "count": len(definition.get("members", [])),
            "cached": (SEGMENT_OUT_DIR / path.name).exists(),
        })
    return out


def _get_auth_state() -> dict:
    st = _load(AUTH_STATE_FILE) or {}
    return {
        "logged_in": bool(st.get("logged_in")),
        "updated_at": st.get("updated_at"),
        "note": st.get("note", ""),
    }


def _set_auth_state(logged_in: bool, note: str = "") -> None:
    _write_json(AUTH_STATE_FILE, {
        "logged_in": bool(logged_in),
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "note": note,
    })


def _verify_login() -> dict:
    """Synchronous, ~8s live probe that refreshes the cached login flag."""
    if not _claim_active():
        raise RuntimeError("a deep research / login job is already running")
    try:
        import pplx_deep_research as worker
        res = worker.check_login()
        if res.get("status") == "error":
            raise RuntimeError(res.get("detail") or "login check failed")
        _set_auth_state(res.get("status") == "logged_in", "active check")
        return _get_auth_state()
    finally:
        _release_active()


def _job_public(job: dict) -> dict:
    """The UI-safe view of a job (no giant report body)."""
    return {
        "id": job["id"],
        "kind": job["kind"],
        "state": job["state"],
        "message": job.get("message", ""),
        "segment": job.get("segment"),
        "date": job.get("date"),
        "result": job.get("result"),
        "artifact": job.get("artifact"),
        "error": job.get("error"),
        "updated_at": job.get("updated_at"),
    }


def _update_job(job_id: str, **fields) -> None:
    with _DEEP_JOBS_LOCK:
        job = _DEEP_JOBS.get(job_id)
        if not job:
            return
        job.update(fields)
        job["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _release_active() -> None:
    with _DEEP_JOBS_LOCK:
        _DEEP_ACTIVE["running"] = False


def _claim_active() -> bool:
    with _DEEP_JOBS_LOCK:
        if _DEEP_ACTIVE["running"]:
            return False
        _DEEP_ACTIVE["running"] = True
        return True


def _new_job(kind: str, **fields) -> dict:
    job = {
        "id": uuid.uuid4().hex[:8],
        "kind": kind,
        "state": "queued",
        "message": "",
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        **fields,
    }
    with _DEEP_JOBS_LOCK:
        _DEEP_JOBS[job["id"]] = job
    return job


def _run_deep_job(job_id: str, segment: str, date: str, prompt: str, window_mode: str) -> None:
    def progress(msg: str) -> None:
        _update_job(job_id, message=msg)

    try:
        import pplx_deep_research as worker
    except Exception as exc:  # noqa: BLE001
        _update_job(job_id, state="error",
                    error=("Playwright not available: "
                           f"{type(exc).__name__}: {exc}. Install with "
                           "`py -3 -m pip install playwright` then "
                           "`py -3 -m playwright install chromium`."))
        _release_active()
        return

    _update_job(job_id, state="running", message="starting browser")
    try:
        res = worker.run_deep_research(prompt, window_mode=window_mode, progress=progress)
    except Exception as exc:  # noqa: BLE001
        _update_job(job_id, state="error", error=f"{type(exc).__name__}: {exc}")
        _release_active()
        return

    status = res.get("status")
    if status == "done":
        try:
            artifact = _save_deep_artifact({
                "segment": segment,
                "date": date,
                "report": res.get("report", ""),
                "citations": res.get("citations", []),
                "source_url": res.get("source_url", ""),
            })
        except Exception as exc:  # noqa: BLE001
            _update_job(job_id, state="error", error=f"saved nothing: {type(exc).__name__}: {exc}")
            _release_active()
            return
        _set_auth_state(True, "deep run")
        _update_job(job_id, state="done", message="report saved",
                    result={
                        "source_url": res.get("source_url"),
                        "citations": res.get("citations", []),
                        "report_chars": len(res.get("report", "")),
                    },
                    artifact=artifact)
    elif status == "needs_login":
        _set_auth_state(False, "run hit login wall")
        _update_job(job_id, state="needs_login",
                    message="Not logged in. Use 'Set up Perplexity login' once, then re-run.")
    elif status == "computer_trap":
        _update_job(job_id, state="error",
                    error=f"Hit the paid Computer path ({res.get('url')}); aborted to protect credits.")
    else:
        _update_job(job_id, state="error",
                    error=res.get("detail") or f"deep research {status}")
    _release_active()


def _run_login_job(job_id: str) -> None:
    def progress(msg: str) -> None:
        _update_job(job_id, message=msg)

    try:
        import pplx_deep_research as worker
    except Exception as exc:  # noqa: BLE001
        _update_job(job_id, state="error",
                    error=f"Playwright not available: {type(exc).__name__}: {exc}")
        _release_active()
        return

    _update_job(job_id, state="running", message="opening login window")
    try:
        res = worker.ensure_login(progress=progress)
    except Exception as exc:  # noqa: BLE001
        _update_job(job_id, state="error", error=f"{type(exc).__name__}: {exc}")
        _release_active()
        return
    if res.get("status") == "logged_in":
        _set_auth_state(True, "login window")
        _update_job(job_id, state="done", message="Perplexity login confirmed")
    else:
        _update_job(job_id, state="error", message="login window timed out",
                    error="login not completed in time")
    _release_active()


def _start_deep_research(body: dict) -> dict:
    segment = _slugify(str(body.get("segment") or ""))
    date = str(body.get("date") or dt.datetime.now(dt.timezone.utc).date().isoformat())
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise ValueError("date must be YYYY-MM-DD")
    prompt = str(body.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("prompt is required")
    window_mode = str(body.get("window_mode") or "offscreen")
    if window_mode not in ("offscreen", "visible", "headless"):
        raise ValueError("window_mode must be offscreen, visible, or headless")
    if not _claim_active():
        raise RuntimeError("a deep research / login job is already running")
    job = _new_job("deep_research", segment=segment, date=date, window_mode=window_mode)
    threading.Thread(target=_run_deep_job,
                     args=(job["id"], segment, date, prompt, window_mode),
                     daemon=True).start()
    return _job_public(job)


def _start_login() -> dict:
    if not _claim_active():
        raise RuntimeError("a deep research / login job is already running")
    job = _new_job("login")
    threading.Thread(target=_run_login_job, args=(job["id"],), daemon=True).start()
    return _job_public(job)


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
        clean = rel.lstrip("/")
        if clean.startswith("web/"):
            target = (WEB_DIR / clean.removeprefix("web/")).resolve()
            allowed_root = WEB_DIR
        elif clean in ROOT_STATIC_FILES:
            target = (REPO_ROOT / clean).resolve()
            allowed_root = REPO_ROOT
        else:
            target = (WEB_DIR / clean).resolve()
            allowed_root = WEB_DIR
        if allowed_root not in target.parents and target != allowed_root:
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
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
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
        if path.startswith("/api/segment-def/"):
            name = path.rsplit("/", 1)[-1].lower()
            rec = _load(_segment_path(name))
            return self._send_json(rec) if rec else self._send_error_json(404, f"unknown segment {name}")
        if path == "/api/deep-runs":
            return self._send_json({"runs": _deep_runs()})
        if path == "/api/deep-research/login-status":
            return self._send_json(_get_auth_state())
        if path == "/api/deep-job":
            job_id = (query.get("id") or [""])[0]
            with _DEEP_JOBS_LOCK:
                job = _DEEP_JOBS.get(job_id)
                job = dict(job) if job else None
            if not job:
                return self._send_error_json(404, f"unknown job {job_id}")
            return self._send_json(_job_public(job))
        if path == "/api/deep-prompt":
            name = (query.get("segment") or [""])[0]
            try:
                return self._send_json(_segment_prompt(name))
            except ValueError as exc:
                return self._send_error_json(400, str(exc))
        if path.startswith("/api/deep-run/"):
            stem = _slugify(path.rsplit("/", 1)[-1])
            payload = {"stem": stem}
            for suffix, rel in {
                "report": DEEP_DIR / f"{stem}.md",
                "sources": DEEP_DIR / f"{stem}.sources.json",
                "review": DEEP_DIR / f"{stem}.review.md",
                "proposal": DEEP_DIR / f"{stem}.target-proposal.json",
            }.items():
                if rel.exists():
                    payload[suffix] = (
                        _load(rel) if rel.suffix == ".json" else rel.read_text(encoding="utf-8")
                    )
            return self._send_json(payload)
        if path == "/api/target-model":
            rec = _load(TARGET_MODEL_JSON)
            return self._send_json(rec) if rec else self._send_error_json(404, "target model not found")
        if path.startswith("/api/research/"):
            sym = path.rsplit("/", 1)[-1].upper()
            rec = _load(RESEARCH_DIR / f"{sym}.json")
            return self._send_json(rec) if rec else self._send_error_json(404, f"no cached research for {sym}")
        if path.startswith("/api/history/"):
            sym = path.rsplit("/", 1)[-1].upper()
            return self._send_json({"symbol": sym, "history": research_pull.history_for(sym)})
        if path.startswith("/api/segment/"):
            name = path.rsplit("/", 1)[-1].lower()
            rec = _load(SEGMENT_OUT_DIR / f"{name}.json")
            return self._send_json(rec) if rec else self._send_error_json(404, f"no cached segment {name}")
        return self._send_error_json(404, "unknown endpoint")

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/segment-draft":
                body = self._read_body()
                return self._send_json(_draft_segment(str(body.get("query") or "")))

            if path.startswith("/api/segment-def/"):
                name = _slugify(path.rsplit("/", 1)[-1])
                body = self._read_body()
                definition = _validate_segment_definition(body.get("definition") or body)
                _write_json(SEGMENT_DEF_DIR / f"{name}.json", definition)
                return self._send_json({"name": name, "definition": definition, "segments": _segments_list()})

            if path == "/api/deep-research/save":
                body = self._read_body()
                return self._send_json(_save_deep_artifact(body))

            if path == "/api/deep-research/run":
                body = self._read_body()
                try:
                    return self._send_json(_start_deep_research(body))
                except RuntimeError as exc:
                    return self._send_error_json(409, str(exc))

            if path == "/api/deep-research/login":
                try:
                    return self._send_json(_start_login())
                except RuntimeError as exc:
                    return self._send_error_json(409, str(exc))

            if path == "/api/deep-research/verify-login":
                try:
                    return self._send_json(_verify_login())
                except RuntimeError as exc:
                    return self._send_error_json(409, str(exc))

            if path == "/api/deep-research/review":
                body = self._read_body()
                segment = str(body.get("segment") or "")
                date = str(body.get("date") or "")
                if not segment or not date:
                    return self._send_error_json(400, "segment and date are required")
                return self._send_json(review_deep_research.review(segment, date))

            if path == "/api/target-proposal/apply":
                body = self._read_body()
                return self._send_json(_apply_target_proposal(
                    str(body.get("segment") or ""),
                    str(body.get("date") or ""),
                    bool(body.get("confirm")),
                ))

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
                    "source_confidence": body.get("source_confidence", ""),
                    "review_after": body.get("review_after", ""),
                    "source_artifact": body.get("source_artifact", ""),
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
