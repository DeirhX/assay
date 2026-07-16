#!/usr/bin/env python3
"""LLM-CLI detection, credential probing, and setup status.

The "which backends can we actually run" half of the analysis stack: resolve
the ``claude`` / ``cursor-agent`` executables (mirroring the official launcher's
version pick on Windows), classify a backend's stderr as quota/auth/error, do a
real smoke check, cheaply probe whether credentials are present, list model
suggestions, and compose the ``setup_status`` envelope the /api/setup endpoints
expose. Nothing here builds prompts or runs an actual analysis -- that is the
runner's job (ticker_analysis); this only answers "is a backend installed,
logged in, and healthy?".

Extracted from ticker_analysis.py; the runner re-imports these so callers
reaching ticker_analysis.available_backends / setup_status / provider_models
(and the internals the setup tests drive) are unchanged.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import analysis_config
from analysis_config import DEFAULT_CONFIG, PROVIDER_LABELS, load_config

# Phrases that mean "this backend is out of quota / rate-limited right now" so the
# orchestrator falls through to the next provider instead of surfacing an error.
_QUOTA_HINTS = (
    "usage limit",
    "rate limit",
    "quota",
    "5-hour limit",
    "limit reached",
    "too many requests",
)

# Phrases that mean "the CLI is installed but has no usable credentials" -> the
# user must log in. Also non-fatal so the orchestrator tries the other backend.
_AUTH_HINTS = (
    "authentication required",
    "please run 'agent login'",
    'please run "agent login"',
    "not logged in",
    "logged out",
    "not authenticated",
    "unauthorized",
    "login required",
    "invalid api key",
    "no api key",
    "credentials",
    "auth login",
)

_SMOKE_PROMPT = "Reply with exactly OK."


def _looks_like_quota(text: str) -> bool:
    low = text.lower()
    return any(h in low for h in _QUOTA_HINTS)


def _looks_like_auth(text: str) -> bool:
    low = text.lower()
    return any(h in low for h in _AUTH_HINTS)


def _is_transient_failure(text: str) -> bool:
    """Quota or auth failures are non-fatal: the orchestrator should try the next
    backend instead of aborting the whole run."""
    return _looks_like_quota(text) or _looks_like_auth(text)


# --------------------------------------------------------------------------- #
# Backend resolution (find the actual executables)
# --------------------------------------------------------------------------- #
def _claude_exe() -> str | None:
    return os.environ.get("REBAL_CLAUDE_CLI") or shutil.which("claude")


# Mirror the official cursor-agent.ps1 launcher: it only treats a versions/
# subdir as runnable if its name is exactly ``YYYY.MM.DD-<commit-hex>``. Folders
# with extra segments (e.g. ``2026.06.12-19-59-36-f6aba9a``) are a different/
# orphaned naming scheme the launcher silently ignores -- so we must too, or the
# app ends up running a version the user's CLI/IDE never touches (see the
# "multiple cursors" mismatch we debugged).
_CURSOR_VERSION_RE = re.compile(r"^\d{4}\.\d{1,2}\.\d{1,2}-[a-f0-9]+$")


def _cursor_argv_base() -> list[str] | None:
    """Resolve a directly-executable launcher for cursor-agent.

    On Windows the PATH entry is a .ps1/.cmd shim around ``node index.js``; we
    dig out node + index.js so subprocess can pass the (large, quote-heavy)
    prompt as a real argv element without cmd.exe quoting hell. Elsewhere the
    PATH entry is a normal binary and we use it directly.

    The version we pick MUST match what the official launcher runs, otherwise the
    app silently executes a different (possibly orphaned) build than the user's
    interactive ``cursor-agent``. We therefore replicate the launcher's choice:
    prefer the newest ``versions/<DATE-commit>`` dir matching the official name
    pattern, and only fall back to ``versions/dist-package`` when none exist.
    """
    override = os.environ.get("REBAL_CURSOR_CLI")
    if override:
        return [override]
    launcher = shutil.which("cursor-agent")
    if not launcher:
        return None
    if sys.platform != "win32":
        return [launcher]
    root = Path(launcher).parent
    if (root / "node.exe").exists() and (root / "index.js").exists():
        return [str(root / "node.exe"), str(root / "index.js")]
    versions = root / "versions"
    if versions.exists():
        official = [
            d
            for d in versions.iterdir()
            if d.is_dir() and (d / "index.js").exists() and _CURSOR_VERSION_RE.match(d.name)
        ]
        chosen = max(official, key=_version_key) if official else None
        if chosen is None:
            dist = versions / "dist-package"
            if (dist / "index.js").exists():
                chosen = dist
        if chosen is not None:
            node = chosen / "node.exe"
            if not node.exists():
                node = root / "node.exe"
            if node.exists():
                return [str(node), str(chosen / "index.js")]
    return None  # couldn't resolve a clean binary; caller reports it


def _version_key(d: Path) -> tuple:
    parts = d.name.split("-")[0].split(".")
    try:
        return tuple(int(x) for x in parts)
    except ValueError:
        return (0,)


def available_backends() -> dict[str, bool]:
    return {"claude": _claude_exe() is not None, "cursor": _cursor_argv_base() is not None}


def _configured_provider(pid: str, cfg: dict[str, Any]) -> dict[str, Any]:
    for provider in cfg.get("providers") or []:
        if provider.get("id") == pid:
            return dict(provider)
    for provider in DEFAULT_CONFIG["providers"]:
        if provider.get("id") == pid:
            return dict(provider)
    return {"id": pid, "enabled": False, "model": "", "extra_args": []}


def _last_line(text: str) -> str:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _smoke_argv(pid: str, provider: dict[str, Any]) -> tuple[list[str], str | None] | dict[str, Any]:
    """Build argv (+ optional stdin) for a smoke check, or return an error envelope."""
    if pid == "claude":
        exe = _claude_exe()
        if not exe:
            return {"ok": False, "status": "missing", "message": "claude CLI not found on PATH"}
        argv = [exe, "-p", "--output-format", "text", "--tools", ""]
        if provider.get("model"):
            argv += ["--model", provider["model"]]
        argv += list(provider.get("extra_args") or [])
        return argv, _SMOKE_PROMPT
    if pid == "cursor":
        base = _cursor_argv_base()
        if not base:
            return {"ok": False, "status": "missing", "message": "cursor-agent CLI not found / unresolved"}
        argv = base + ["-p", _SMOKE_PROMPT, "--output-format", "text", "--trust", "--mode", "ask"]
        if provider.get("model"):
            argv += ["--model", provider["model"]]
        argv += list(provider.get("extra_args") or [])
        return argv, None
    return {"ok": False, "status": "unsupported", "message": f"unknown backend {pid}"}


def _smoke_check_backend(pid: str, provider: dict[str, Any], *, timeout: int = 45) -> dict[str, Any]:
    built = _smoke_argv(pid, provider)
    if isinstance(built, dict):
        return built
    argv, input_text = built
    label = PROVIDER_LABELS.get(pid, pid)
    try:
        proc = subprocess.run(argv, input=input_text, capture_output=True, text=True,
                              timeout=timeout, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return {"ok": False, "status": "timeout", "message": f"{label} timed out after {timeout}s"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": "error", "message": f"{type(exc).__name__}: {exc}"}

    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode == 0 and out:
        return {"ok": True, "status": "ok", "message": _last_line(out) or "OK"}
    blob = err or out or f"exit {proc.returncode}"
    status = "auth" if _looks_like_auth(blob) else "quota" if _looks_like_quota(blob) else "error"
    return {
        "ok": False,
        "status": status,
        "message": _last_line(blob) or blob,
    }


def _auth_probe(pid: str, *, timeout: int = 12) -> bool | None:
    """Cheap 'are credentials present?' check that does NOT send a real prompt, so
    the UI can tell "installed but not logged in" apart from "installed and ready"
    on page load. Returns True (logged in), False (no credentials), or None
    (unknown / probe unavailable). Privacy: the CLIs print the account email/org
    here -- we extract only the boolean and never return or log that PII."""
    try:
        if pid == "claude":
            exe = _claude_exe()
            if not exe:
                return None
            proc = subprocess.run([exe, "auth", "status"], capture_output=True,
                                  text=True, timeout=timeout, encoding="utf-8", errors="replace")
            try:
                val = json.loads(proc.stdout or "{}").get("loggedIn")
                if isinstance(val, bool):
                    return val
            except (ValueError, AttributeError):
                pass
            blob = ((proc.stdout or "") + "\n" + (proc.stderr or "")).lower()
            if '"loggedin": true' in blob or "logged in" in blob:
                return True
            if _looks_like_auth(blob) or "logged out" in blob:
                return False
            return None
        if pid == "cursor":
            base = _cursor_argv_base()
            if not base:
                return None
            proc = subprocess.run(base + ["status"], capture_output=True,
                                  text=True, timeout=timeout, encoding="utf-8", errors="replace")
            blob = ((proc.stdout or "") + "\n" + (proc.stderr or "")).lower()
            if _looks_like_auth(blob):
                return False
            if proc.returncode == 0 and ("logged in" in blob or "login successful" in blob):
                return True
            if proc.returncode != 0:
                return False
            return None
    except subprocess.TimeoutExpired:
        return None
    except Exception:  # noqa: BLE001 -- a flaky probe must never break setup status
        return None
    return None


# `_auth_probe` shells out to the backend CLI (a Node process) -- ~1-1.5s each
# on Windows. /api/setup/status is fetched on every page load (it gates the
# initial route) and polled by the setup screen, so probing live every time
# blocked the whole app for ~3s per request. Auth state only changes on an
# explicit login/logout, so cache the boolean and let an explicit smoke check
# (run_checks=True) refresh it.
_AUTH_PROBE_TTL = 300.0  # seconds
_auth_probe_cache: dict[str, tuple[float, bool | None]] = {}
_auth_probe_lock = threading.Lock()


def _auth_probe_cached(pid: str) -> bool | None:
    now = time.monotonic()
    with _auth_probe_lock:
        hit = _auth_probe_cache.get(pid)
        if hit is not None and now - hit[0] < _AUTH_PROBE_TTL:
            return hit[1]
    val = _auth_probe(pid)
    with _auth_probe_lock:
        _auth_probe_cache[pid] = (time.monotonic(), val)
    return val


def _auth_probe_remember(pid: str, value: bool | None) -> None:
    """Seed the probe cache from an authoritative result (an explicit smoke
    check), so the next page-load status read reflects it without re-spawning."""
    with _auth_probe_lock:
        _auth_probe_cache[pid] = (time.monotonic(), value)


def _clear_auth_probe_cache() -> None:
    with _auth_probe_lock:
        _auth_probe_cache.clear()


def setup_status(*, run_checks: bool = False) -> dict[str, Any]:
    cfg = load_config()
    available = available_backends()
    # On the load path (no run_checks) the per-backend credential probe is the
    # one slow step. Probe all installed backends concurrently (cached), so the
    # cold call is one CLI launch wall-time, not the sum of them.
    auth_map: dict[str, bool | None] = {}
    if not run_checks:
        installed = [pid for pid in PROVIDER_LABELS if available.get(pid)]
        if installed:
            with ThreadPoolExecutor(max_workers=len(installed)) as pool:
                auth_map = dict(zip(installed, pool.map(_auth_probe_cached, installed)))
    backends = []
    for pid, label in PROVIDER_LABELS.items():
        provider = _configured_provider(pid, cfg)
        rec: dict[str, Any] = {
            "id": pid,
            "label": label,
            "installed": bool(available.get(pid)),
            "enabled": bool(provider.get("enabled", True)),
            "model": provider.get("model") or "",
            "extra_args": list(provider.get("extra_args") or []),
            "authenticated": None,
        }
        rec["status"] = "installed" if rec["installed"] else "missing"
        if run_checks:
            # A real smoke check is the source of truth; skip the cheap probe
            # and seed the probe cache from it so the next load read is instant.
            rec["check"] = _smoke_check_backend(pid, provider)
            rec["status"] = rec["check"].get("status", rec["status"])
            if rec["check"].get("ok"):
                rec["authenticated"] = True
                _auth_probe_remember(pid, True)
            elif rec["check"].get("status") == "auth":
                rec["authenticated"] = False
                _auth_probe_remember(pid, False)
        elif rec["installed"]:
            # Cheap credential probe so load-time UI distinguishes logged-out from ready.
            auth = auth_map.get(pid)
            rec["authenticated"] = auth
            if auth is False:
                rec["status"] = "logged_out"
            elif auth is True:
                rec["status"] = "ready"
        backends.append(rec)
    # Reference CONFIG_PATH through the module (not a bound name) so tests that
    # sandbox analysis_config.CONFIG_PATH to a temp dir are reflected here.
    return {
        "config_exists": analysis_config.CONFIG_PATH.exists(),
        "config_path": str(analysis_config.CONFIG_PATH),
        "config": cfg,
        "backends": backends,
    }


# Model suggestions for the config UI's autocomplete. Cursor exposes a real
# list; Claude Code has no list command, so we offer the documented aliases.
# Either way the UI keeps free-text entry, so an unlisted model still works.
_CLAUDE_MODEL_SUGGESTIONS = [
    {"value": "opus", "label": "opus (latest)"},
    {"value": "sonnet", "label": "sonnet (latest)"},
    {"value": "haiku", "label": "haiku (latest)"},
]
_MODELS_CACHE: dict[str, list[dict[str, str]]] | None = None


def _cursor_models() -> list[dict[str, str]]:
    base = _cursor_argv_base()
    if not base:
        return []
    try:
        proc = subprocess.run(base + ["--list-models"], capture_output=True,
                              text=True, timeout=30, encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return []
    if proc.returncode != 0:
        return []
    out: list[dict[str, str]] = []
    for line in (proc.stdout or "").splitlines():
        m = re.match(r"^\s*([A-Za-z0-9._-]+)\s+-\s+(.+?)\s*$", line)
        if m:
            out.append({"value": m.group(1), "label": m.group(2)})
    return out


def provider_models(force: bool = False) -> dict[str, list[dict[str, str]]]:
    """Per-provider model suggestions for the config autocomplete. Cached for the
    process lifetime since listing Cursor's models shells out (~seconds)."""
    global _MODELS_CACHE
    if _MODELS_CACHE is not None and not force:
        return _MODELS_CACHE
    _MODELS_CACHE = {
        "claude": list(_CLAUDE_MODEL_SUGGESTIONS),
        "cursor": _cursor_models(),
    }
    return _MODELS_CACHE
