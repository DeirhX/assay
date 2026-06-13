#!/usr/bin/env python3
"""On-demand, in-depth single-ticker analysis via a local agent CLI.

This is the *cheap workhorse* tier of the research stack. It does NOT crawl the
web like Perplexity Deep Research (that quota is scarce and reserved for whole
segments); instead it runs a local agent CLI as a pure reasoning pass over the
deterministic numbers we already pulled (Yahoo / SEC / FMP) and turns them into
a skeptical, structured analyst note.

Two backends, tried in configured order with automatic fallback:

* ``claude``  -- Claude Code headless (``claude -p``). Prompt via STDIN. Runs on
  the subscription's rolling usage window, so it's ~free at the margin.
* ``cursor``  -- ``cursor-agent -p`` in read-only ask mode. Prompt as a CLI arg
  (it does not read the prompt from stdin). Request-based, also cheap.

Design choices / honest caveats:
* By default tools are disabled (claude ``--tools ""``) / read-only
  (cursor ``--mode ask``) so a backend can't shell out, edit files, or hang on a
  permission prompt; the note is grounded ONLY in the numbers we hand it -- if a
  figure is missing it is told to say so, not to guess.
* Optional web research (config ``allow_web``) spans BOTH backends, steered by
  the same prompt ground rules that REQUIRE a source URL for every web-derived
  fact. The CLIs differ in how the tool is granted:
  - Claude gets ONLY the scoped ``WebSearch`` / ``WebFetch`` tools, pre-approved
    via ``--allowedTools`` so the headless ``-p`` run can't be silently denied on
    a permission prompt it can't answer.
  - cursor-agent has no per-tool flag, so it stays in read-only ``--mode ask``
    and uses its built-in web search when available; web *intent* is carried by
    the prompt. If ask mode exposes no web tool, the note just stays grounded in
    the DATA.
  We still never enable Bash/Edit/Write on either -- an analyst note has no
  business shelling out or touching the filesystem.
* Backends consume YOUR interactive coding quota. Cheap != free; that's why this
  is gated behind an explicit button, not run on every page view.
* Windows is a first-class target. ``cursor-agent`` ships as a PowerShell/.cmd
  shim around ``node index.js``; we resolve and call node directly so arbitrary
  prompt text (full of quotes and newlines) isn't mangled by cmd.exe.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "data" / "analysis-config.json"

# Default backend policy. Order == fallback order. A user override file at
# CONFIG_PATH is shallow-merged over this (see load_config).
DEFAULT_CONFIG: dict[str, Any] = {
    "providers": [
        {"id": "cursor", "enabled": True, "model": "", "extra_args": []},
        {"id": "claude", "enabled": True, "model": "", "extra_args": []},
    ],
    "timeout_sec": 300,
    # When true, backends may use their web tools for fresher context. Off by
    # default: keeps runs fast, cheap, and grounded in our deterministic data.
    "allow_web": False,
}

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

PROVIDER_LABELS = {"claude": "Claude CLI", "cursor": "Cursor CLI"}
_SMOKE_PROMPT = "Reply with exactly OK."


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def _normalize_providers(raw: Any, *, strip_model: bool = False) -> list[dict[str, Any]] | None:
    if not isinstance(raw, list):
        return None
    cleaned: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        pid = item.get("id")
        if pid not in PROVIDER_LABELS or pid in seen:
            continue
        seen.add(pid)
        model = str(item.get("model") or "")
        if strip_model:
            model = model.strip()
        cleaned.append({
            "id": pid,
            "enabled": bool(item.get("enabled", True)),
            "model": model,
            "extra_args": [str(a) for a in (item.get("extra_args") or []) if str(a).strip()],
        })
    return cleaned or None


def load_config() -> dict[str, Any]:
    """Defaults merged with the on-disk override (if any). Always returns a
    well-formed config even if the file is missing or partially specified."""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return cfg
    providers = _normalize_providers(raw.get("providers"))
    if providers:
        cfg["providers"] = providers
    if isinstance(raw.get("timeout_sec"), (int, float)) and raw["timeout_sec"] > 0:
        cfg["timeout_sec"] = int(raw["timeout_sec"])
    cfg["allow_web"] = bool(raw.get("allow_web", cfg["allow_web"]))
    return cfg


def save_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """Validate and persist a config; returns the normalized, stored version."""
    merged = json.loads(json.dumps(DEFAULT_CONFIG))
    providers = _normalize_providers(cfg.get("providers"), strip_model=True)
    if providers:
        merged["providers"] = providers
    if isinstance(cfg.get("timeout_sec"), (int, float)) and cfg["timeout_sec"] > 0:
        merged["timeout_sec"] = int(cfg["timeout_sec"])
    merged["allow_web"] = bool(cfg.get("allow_web", merged["allow_web"]))
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    return merged


# --------------------------------------------------------------------------- #
# Backend resolution (find the actual executables)
# --------------------------------------------------------------------------- #
def _claude_exe() -> str | None:
    return os.environ.get("REBAL_CLAUDE_CLI") or shutil.which("claude")


def _cursor_argv_base() -> list[str] | None:
    """Resolve a directly-executable launcher for cursor-agent.

    On Windows the PATH entry is a .ps1/.cmd shim around ``node index.js``; we
    dig out node + index.js so subprocess can pass the (large, quote-heavy)
    prompt as a real argv element without cmd.exe quoting hell. Elsewhere the
    PATH entry is a normal binary and we use it directly.
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
        cands = [d for d in versions.iterdir() if d.is_dir() and (d / "index.js").exists()]
        if cands:
            latest = max(cands, key=_version_key)
            node = latest / "node.exe"
            if not node.exists():
                node = root / "node.exe"
            if node.exists():
                return [str(node), str(latest / "index.js")]
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


def setup_status(*, run_checks: bool = False) -> dict[str, Any]:
    cfg = load_config()
    available = available_backends()
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
            # A real smoke check is the source of truth; skip the cheap probe.
            rec["check"] = _smoke_check_backend(pid, provider)
            rec["status"] = rec["check"].get("status", rec["status"])
            if rec["check"].get("ok"):
                rec["authenticated"] = True
            elif rec["check"].get("status") == "auth":
                rec["authenticated"] = False
        elif rec["installed"]:
            # Cheap credential probe so load-time UI distinguishes logged-out from ready.
            auth = _auth_probe(pid)
            rec["authenticated"] = auth
            if auth is False:
                rec["status"] = "logged_out"
            elif auth is True:
                rec["status"] = "ready"
        backends.append(rec)
    return {
        "config_exists": CONFIG_PATH.exists(),
        "config_path": str(CONFIG_PATH),
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


# --------------------------------------------------------------------------- #
# Prompt
# --------------------------------------------------------------------------- #
def _compact_record(rec: dict[str, Any]) -> dict[str, Any]:
    """A trimmed, model-facing view of the deterministic dossier: only the
    fields worth reasoning over, with the giant price_history array dropped."""
    profile = rec.get("profile") or {}
    summary = (profile.get("summary") or "")[:1800]
    metrics = {}
    for key, node in (rec.get("metrics") or {}).items():
        if isinstance(node, dict):
            metrics[key] = {"value": node.get("value"), "display": node.get("display"), "source": node.get("source")}
    port = rec.get("portfolio") or {}
    target = port.get("target") or {}
    return {
        "symbol": rec.get("symbol"),
        "name": rec.get("name"),
        "currency": rec.get("currency"),
        "price": (rec.get("price") or {}).get("value") if isinstance(rec.get("price"), dict) else rec.get("price"),
        "as_of": rec.get("as_of"),
        "business": {
            "summary": summary,
            "sector": profile.get("sector"),
            "industry": profile.get("industry"),
            "country": profile.get("country"),
            "employees": profile.get("employees"),
        },
        "metrics": metrics,
        "momentum": rec.get("momentum") or {},
        "cross_checks": [
            {"severity": c.get("severity"), "metric": c.get("metric"), "message": c.get("message")}
            for c in (rec.get("cross_checks") or [])
        ],
        "portfolio": {
            "current_weight_pct": port.get("current_weight_pct"),
            "status": port.get("status"),
            "gap_to_band_pct": port.get("gap_to_band_pct"),
            "target_rule": target.get("rule"),
            "target_low": target.get("low"),
            "target_high": target.get("high"),
            "target_note": target.get("note"),
        },
        "data_errors": rec.get("errors") or [],
    }


def _data_rule(allow_web: bool) -> str:
    """The grounding rule swaps depending on whether web tools are live."""
    if not allow_web:
        return ('- Use ONLY the numbers in the DATA block below. Do not invent figures or cite '
                'prices/multiples not present. If something important is missing, say "not in the '
                'data" rather than guessing.')
    return (
        "- Anchor all position math and the valuation multiples in the DATA block; never invent "
        "those figures. You MAY use your web search / fetch tools for fresher qualitative context "
        "the DATA lacks: recent news, the latest earnings/guidance, analyst actions, regulatory or "
        "competitive developments.\n"
        "- Every web-derived fact MUST be followed by its source URL in parentheses, and prefer "
        "primary sources (company IR, SEC/EDGAR filings) over aggregators. Date any time-sensitive "
        'claim. If a web claim can\'t be verified, drop it rather than guess.\n'
        "- Keep web findings clearly distinct from the deterministic DATA so the reader knows which "
        "is which."
    )


def _qa_data_rule(allow_web: bool) -> str:
    if not allow_web:
        return ('- Answer ONLY from the DATA block (use the conversation and analyst note for '
                'continuity, not as new facts). Do not invent figures. If something needed isn\'t '
                'present, say "not in the data".')
    return (
        "- Anchor figures in the DATA block (don't invent them) and use the conversation/analyst "
        "note for continuity. You MAY use your web search / fetch tools for fresher facts the DATA lacks "
        "(recent news, latest earnings/guidance, analyst actions).\n"
        "- Cite every web-derived fact with its source URL in parentheses, preferring primary "
        "sources; date time-sensitive claims. Drop anything you can't verify."
    )


def _sources_section(allow_web: bool) -> str:
    if not allow_web:
        return ""
    return ("\n\n## Sources\nBullet every web source you used as `[title](url) — what it backed up`. "
            'Write "None — analysis is from the provided data only." if you did not search.')


def build_prompt(rec: dict[str, Any], *, allow_web: bool = False) -> str:
    sym = rec.get("symbol", "?")
    data = json.dumps(_compact_record(rec), indent=2, default=str)
    return f"""You are a skeptical, evidence-driven equity analyst writing an in-depth note on ${sym} for a self-directed investor who already holds a diversified portfolio. Your job is to improve the quality of their decision, not to cheerlead.

GROUND RULES
{_data_rule(allow_web)}
- Be concise and direct. No hype, no filler, no flattery. Prefer specifics over adjectives.
- Surface the bear case honestly and weight it against the bull case.
- Tag every company ticker with a leading $ on first mention (e.g. $AMD, $NVDA) so they can be auto-linked.
- The DATA already includes this position's weight vs its target band; make your verdict portfolio-aware (room to add vs trim pressure).
- If the deterministic data has cross-check warnings or errors, factor that uncertainty into your confidence.

OUTPUT (Markdown, use these exact section headings):
## Verdict
One line: a stance (Accumulate / Hold / Trim / Avoid) + a confidence (low/medium/high) + a one-sentence justification.

## What the business is
2-4 sentences: what they actually do and where the moat is (or isn't).

## Momentum read
What the 1m/3m/6m/12m moves and distance-from-52w-high imply. Is this strength or a falling knife?

## Valuation read
Interpret the multiples vs the growth. Priced for perfection, fair, or cheap? State which metric drives the call.

## Bull case
2-3 tight bullets.

## Bear case
2-3 tight bullets.

## What would change the thesis
2-3 concrete, observable triggers (numbers, events) that would flip your verdict.{_sources_section(allow_web)}

DATA
```json
{data}
```
"""


# --------------------------------------------------------------------------- #
# Running a backend
# --------------------------------------------------------------------------- #
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


# Built-in agent tools we expose when web research is on. WebSearch does
# query->results; WebFetch pulls a specific URL (earnings releases, SEC filings,
# IR pages). We deliberately stop there: Bash/Edit/Write/Read add risk and zero
# value to a read-only analyst note.
_CLAUDE_WEB_TOOLS = ["WebSearch", "WebFetch"]


def _claude_tool_args(cfg: dict) -> list[str]:
    """Tool + permission flags for the claude CLI.

    web off -> no tools at all (pure reasoning over our deterministic data).
    web on  -> ONLY the web tools, and ``--allowedTools`` pre-approves them so a
               headless ``-p`` run doesn't silently deny them on a permission
               prompt it has no way to answer (the gotcha that makes naive
               web-enabling a no-op).
    """
    if not cfg.get("allow_web"):
        return ["--tools", ""]
    return ["--tools", *_CLAUDE_WEB_TOOLS, "--allowedTools", *_CLAUDE_WEB_TOOLS]


def _run_timeout(cfg: dict) -> int:
    """Web research makes extra round-trips (search -> fetch -> reason), so give
    it more headroom than a pure reasoning pass over local numbers."""
    base = int(cfg.get("timeout_sec", 300))
    return max(base, 600) if cfg.get("allow_web") else base


class _Cancelled(Exception):
    """Raised when a cancellable run is torn down on user request."""


def _run_proc(argv: list[str], *, input_text: str | None, timeout: int,
              cancel: Callable[[], bool] | None) -> subprocess.CompletedProcess:
    """``subprocess.run`` that can be cancelled mid-flight.

    Without a ``cancel`` callback this is just ``subprocess.run``. With one, we
    drive the child via a worker thread and poll ``cancel()`` ~2x/sec; when it
    flips True we kill the process and raise ``_Cancelled`` -- so a cancelled
    Q&A actually stops burning CLI quota instead of running to completion in the
    background. Propagates ``TimeoutExpired`` like ``subprocess.run`` does."""
    if cancel is None:
        return subprocess.run(argv, input=input_text, capture_output=True, text=True,
                              timeout=timeout, encoding="utf-8", errors="replace")

    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE if input_text is not None else None,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    )
    holder: dict[str, Any] = {}

    def _pump() -> None:
        try:
            holder["out"], holder["err"] = proc.communicate(input=input_text, timeout=timeout)
        except subprocess.TimeoutExpired:
            holder["timeout"] = True
            proc.kill()
            try:
                proc.communicate()
            except Exception:  # noqa: BLE001
                pass
        except Exception as exc:  # noqa: BLE001
            holder["exc"] = exc

    worker = threading.Thread(target=_pump, daemon=True)
    worker.start()
    while worker.is_alive():
        if cancel():
            proc.kill()
            worker.join(timeout=5)
            raise _Cancelled()
        worker.join(timeout=0.5)

    if holder.get("exc"):
        raise holder["exc"]
    if holder.get("timeout"):
        raise subprocess.TimeoutExpired(argv, timeout)
    return subprocess.CompletedProcess(argv, proc.returncode or 0,
                                       holder.get("out", ""), holder.get("err", ""))


def _run_claude(prompt: str, provider: dict, cfg: dict,
                cancel: Callable[[], bool] | None = None) -> dict[str, Any]:
    exe = _claude_exe()
    if not exe:
        return {"ok": False, "fatal": False, "error": "claude CLI not found on PATH"}
    argv = [exe, "-p", "--output-format", "text"]
    argv += _claude_tool_args(cfg)
    if provider.get("model"):
        argv += ["--model", provider["model"]]
    argv += list(provider.get("extra_args") or [])
    timeout = _run_timeout(cfg)
    try:
        proc = _run_proc(argv, input_text=prompt, timeout=timeout, cancel=cancel)
    except _Cancelled:
        return {"ok": False, "fatal": True, "cancelled": True, "error": "cancelled"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "fatal": True, "error": f"claude timed out after {timeout}s"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "fatal": False, "error": f"claude failed to launch: {type(exc).__name__}: {exc}"}
    return _finish("claude", provider, proc)


def _run_cursor(prompt: str, provider: dict, cfg: dict,
                cancel: Callable[[], bool] | None = None) -> dict[str, Any]:
    base = _cursor_argv_base()
    if not base:
        return {"ok": False, "fatal": False, "error": "cursor-agent CLI not found / unresolved"}
    # cursor-agent does NOT read the prompt from stdin in -p mode; it must be a
    # positional arg. We pass it as a real argv element (no shell) so quotes and
    # newlines survive intact.
    # Always read-only. cursor-agent has no per-tool web flag: its only modes are
    # the read-only "ask"/"plan", while bare -p grants ALL tools (incl. shell +
    # write). An analyst note must never shell out or edit, so we pin "ask"
    # unconditionally and let web research be steered by the prompt's ground rules
    # (same as Claude). Cursor's ask mode uses its built-in web search when the
    # agent exposes it; if it doesn't, the run simply stays grounded in the
    # deterministic DATA -- safe either way, and never at the cost of full agent
    # powers like the old allow_web path did.
    argv = base + ["-p", prompt, "--output-format", "text", "--trust", "--mode", "ask"]
    if provider.get("model"):
        argv += ["--model", provider["model"]]
    argv += list(provider.get("extra_args") or [])
    timeout = _run_timeout(cfg)
    try:
        proc = _run_proc(argv, input_text=None, timeout=timeout, cancel=cancel)
    except _Cancelled:
        return {"ok": False, "fatal": True, "cancelled": True, "error": "cancelled"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "fatal": True, "error": f"cursor-agent timed out after {timeout}s"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "fatal": False, "error": f"cursor-agent failed to launch: {type(exc).__name__}: {exc}"}
    return _finish("cursor", provider, proc)


def _finish(pid: str, provider: dict, proc: subprocess.CompletedProcess) -> dict[str, Any]:
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0 or not out:
        blob = err or out or f"exit {proc.returncode}"
        # Quota/auth failures are non-fatal: let the next backend try.
        return {"ok": False, "fatal": not _is_transient_failure(blob),
                "error": f"{PROVIDER_LABELS[pid]}: {blob.splitlines()[-1] if blob.splitlines() else blob}"}
    return {"ok": True, "report": out, "backend": pid,
            "backend_label": PROVIDER_LABELS[pid], "model": provider.get("model") or "(default)"}


_RUNNERS: dict[str, Callable[..., dict]] = {"claude": _run_claude, "cursor": _run_cursor}
_PROVIDER_ORDER = {"cursor": 0, "claude": 1}


def _ordered_providers(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Canonical backend preference: Cursor first, Claude as fallback.

    The order is enforced here (via _PROVIDER_ORDER) rather than trusting the
    config file's array order, so the default engine is deterministic and a
    hand-edited config can't accidentally change which backend leads. Note: this
    governs the in-depth/Q&A analyses only -- deep research has its own engine.
    """
    providers = [p for p in (cfg.get("providers") or []) if p.get("id") in _RUNNERS]
    return sorted(providers, key=lambda p: _PROVIDER_ORDER.get(p.get("id"), 99))


def _run_with_fallback(prompt: str, cfg: dict,
                       progress: Callable[[str], None] | None = None,
                       cancel: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Run a prompt through the configured backends in order, falling back on
    quota/auth failure. Returns the first success, or an aggregate error."""
    attempts: list[str] = []
    for provider in _ordered_providers(cfg):
        if not provider.get("enabled"):
            continue
        pid = provider.get("id")
        runner = _RUNNERS.get(pid)
        if not runner:
            continue
        if progress:
            progress(f"asking {PROVIDER_LABELS.get(pid, pid)}…")
        res = runner(prompt, provider, cfg, cancel)
        if res.get("ok"):
            res["attempts"] = attempts
            return res
        if res.get("cancelled"):
            # User pulled the plug: don't fall through to the next backend and
            # spend more quota on a request they no longer want.
            return {"ok": False, "cancelled": True, "error": "cancelled", "attempts": attempts}
        attempts.append(res.get("error", f"{pid} failed"))
        if res.get("fatal"):
            # A real failure (timeout / bad output), not a quota miss: stop here
            # rather than burning the fallback on the same broken input.
            break
        if progress:
            progress(f"{PROVIDER_LABELS.get(pid, pid)} unavailable, trying next…")
    return {"ok": False, "error": "; ".join(attempts) or "no enabled backends available",
            "attempts": attempts}


def analyze(rec: dict[str, Any], *, cfg: dict | None = None,
            progress: Callable[[str], None] | None = None,
            cancel: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Generate the structured in-depth note over the deterministic dossier."""
    cfg = cfg or load_config()
    return _run_with_fallback(build_prompt(rec, allow_web=cfg.get("allow_web", False)),
                              cfg, progress, cancel)


# --------------------------------------------------------------------------- #
# Segment drafting: turn a freeform theme ("space exploration") into a list of
# real, currently-listed public tickers. This is how the research console stops
# being limited to names you already hold -- the LLM proposes the universe, the
# deterministic pull + review gate still vet it downstream.
# --------------------------------------------------------------------------- #
def _segment_web_rule(allow_web: bool) -> str:
    if allow_web:
        return (
            "- Use your web search / fetch tools to confirm each ticker is real and CURRENTLY "
            "listed, and to catch recent IPOs, de-listings, or symbol changes. Do not put "
            "citations or URLs in the JSON."
        )
    return (
        "- Use only tickers you are confident are real and currently listed. Do not guess at "
        "symbols; omit a name rather than invent a ticker."
    )


def build_segment_draft_prompt(query: str, *, allow_web: bool = False) -> str:
    """Prompt the backend to return a themed public-equity watchlist as JSON."""
    return f"""You are assembling a public-equity RESEARCH SEGMENT (a themed watchlist) for this theme:

"{query.strip()}"

Identify the most relevant PUBLICLY TRADED companies and ETFs for the theme and group them into 3-6 sleeves (sub-themes). Aim for 8-20 names: enough to be a real research universe, not so many it's noise. Cover the value chain, not just the obvious mega-caps.

GROUND RULES
{_segment_web_rule(allow_web)}
- The `symbol` field must be the real primary-listing ticker (US ticker or ADR where applicable), e.g. RKLB, LMT, BA. Never put a company name in the symbol field.
- Exclude private companies. If a key player is private, omit it (you may mention a public proxy in another member's rationale).
- `sleeve` values are short lowercase slugs, e.g. "launch", "satellites", "defense-prime".
- `confidence` is one of: high, medium, low.

OUTPUT
Respond with ONLY a single JSON object -- no markdown code fences, no commentary before or after. Exactly this shape:
{{
  "title": "<concise human title for the theme>",
  "comment": "<one sentence describing what this segment covers>",
  "sleeves": ["<sleeve-slug>", "..."],
  "members": [
    {{"symbol": "TICKER", "sleeve": "<sleeve-slug>", "rationale": "<why it belongs, one line>", "confidence": "high|medium|low"}}
  ]
}}
"""


def _extract_json_object(text: str) -> dict | None:
    """Pull a JSON object out of an LLM response. Tolerates ```json fences and
    leading/trailing prose by trying a fenced block first, then the outermost
    balanced {...}. Returns None if nothing parses to a dict."""
    if not text:
        return None
    s = text.strip()
    candidates: list[str] = []
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", s, re.DOTALL)
    if fenced:
        candidates.append(fenced.group(1))
    start = s.find("{")
    if start != -1:
        depth = 0
        for i in range(start, len(s)):
            ch = s[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(s[start:i + 1])
                    break
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def draft_segment_members(query: str, *, cfg: dict | None = None,
                          progress: Callable[[str], None] | None = None,
                          cancel: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Ask the configured backend for a themed list of public tickers. Returns
    {ok, members, title, comment, sleeves, backend_label, ...} or {ok: False,
    error}. Members are raw model output; the caller validates symbols."""
    cfg = cfg or load_config()
    prompt = build_segment_draft_prompt(query, allow_web=cfg.get("allow_web", False))
    res = _run_with_fallback(prompt, cfg, progress, cancel)
    if not res.get("ok"):
        return {"ok": False, "cancelled": bool(res.get("cancelled")),
                "error": res.get("error") or "all analysis backends failed"}
    parsed = _extract_json_object(res.get("report") or "")
    if not isinstance(parsed, dict):
        return {"ok": False, "error": "the model did not return parseable segment JSON"}
    members = parsed.get("members")
    return {
        "ok": True,
        "title": str(parsed.get("title") or "").strip(),
        "comment": str(parsed.get("comment") or "").strip(),
        "sleeves": [s for s in (parsed.get("sleeves") or []) if isinstance(s, str)],
        "members": members if isinstance(members, list) else [],
        "backend": res.get("backend"),
        "backend_label": res.get("backend_label"),
        "model": res.get("model"),
    }


def build_qa_prompt(rec: dict[str, Any], history: list[dict] | None,
                    question: str, note: str | None = None, *, allow_web: bool = False) -> str:
    """A follow-up Q&A prompt: same deterministic DATA as the note, plus the
    prior conversation (bounded) and, if present, the latest analyst note for
    continuity. Keeps the model grounded and the thread coherent."""
    sym = rec.get("symbol", "?")
    data = json.dumps(_compact_record(rec), indent=2, default=str)
    convo = ""
    # Keep the last ~6 exchanges so the prompt stays bounded as a thread grows;
    # long prior answers are truncated (full text still lives on disk).
    for t in [t for t in (history or []) if t.get("text")][-12:]:
        who = "Q" if t.get("role") == "user" else "A"
        txt = t["text"].strip()
        if who == "A" and len(txt) > 1500:
            txt = txt[:1500] + " …[truncated]"
        convo += f"{who}: {txt}\n\n"
    note_block = ""
    if note:
        note_block = "PRIOR ANALYST NOTE (context only; may be stale):\n" + note.strip()[:4000] + "\n\n"
    convo_block = ("CONVERSATION SO FAR:\n" + convo) if convo else ""
    return f"""You are a skeptical, evidence-driven equity analyst answering a follow-up question about ${sym} for a self-directed investor. Improve their decision; do not cheerlead.

GROUND RULES
{_qa_data_rule(allow_web)}
- Be concise and direct. Answer the specific question asked; skip boilerplate restatement of the whole thesis.
- Tag every company ticker with a leading $ on first mention (e.g. $AMD).
- If the data has cross-check warnings, factor that uncertainty into your answer.

{note_block}{convo_block}NEW QUESTION:
{question.strip()}

DATA
```json
{data}
```

Answer in Markdown.{' End with a "Sources" line listing any URLs you used.' if allow_web else ''}"""


def _qa_followup_text(question: str, allow_web: bool = False) -> str:
    """The minimal payload for a RESUMED Claude session: the DATA, ground rules
    and prior turns already live in the session, so we send only the question.
    That's the whole point -- the heavy prefix is served from the prompt cache."""
    web = (" You may use WebSearch/WebFetch for fresher facts; cite every web fact with its URL."
           if allow_web else
           ' If something needed is not there, say "not in the data".')
    return (f"{question.strip()}\n\n"
            "(Answer using the data and context already in this conversation." + web +
            " Be concise; tag tickers with a leading $.)")


def _norm_usage(u: dict | None) -> dict[str, int]:
    """Keep just the token counters worth showing (incl. prompt-cache read/write)."""
    if not isinstance(u, dict):
        return {}
    keys = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    return {k: int(u[k]) for k in keys if isinstance(u.get(k), (int, float))}


def _finish_claude_json(pid: str, provider: dict, proc: subprocess.CompletedProcess,
                        session_id: str) -> dict[str, Any]:
    """Parse ``claude --output-format json``: pull the answer, the session id (so
    the next turn can resume + reuse the cache) and the token/cache usage."""
    raw = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0 or not raw:
        blob = err or raw or f"exit {proc.returncode}"
        return {"ok": False, "fatal": not _is_transient_failure(blob),
                "error": f"{PROVIDER_LABELS[pid]}: {blob.splitlines()[-1] if blob.splitlines() else blob}"}
    text, usage, sid = raw, {}, session_id
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if isinstance(data, dict):
        if data.get("is_error"):
            msg = str(data.get("result") or data.get("error") or "claude error")
            return {"ok": False, "fatal": not _is_transient_failure(msg), "error": f"{PROVIDER_LABELS[pid]}: {msg}"}
        text = (data.get("result") or "").strip()
        usage = data.get("usage") or {}
        sid = data.get("session_id") or session_id
    if not text:
        return {"ok": False, "fatal": True, "error": f"{PROVIDER_LABELS[pid]}: empty result"}
    return {"ok": True, "report": text, "backend": pid, "backend_label": PROVIDER_LABELS[pid],
            "model": provider.get("model") or "(default)",
            "session": {"provider": pid, "id": sid}, "usage": _norm_usage(usage)}


def _run_claude_qa(rec: dict[str, Any], history: list[dict] | None, question: str,
                   note: str | None, provider: dict, cfg: dict, resume_id: str | None,
                   progress: Callable[[str], None] | None,
                   cancel: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Claude Q&A with prompt-cache reuse via session resume. On a warm session
    we send only the question (cache-read prefix); otherwise we open a new
    session with the full grounded prompt and fall back to that if a resume
    fails (expired/cleaned session)."""
    exe = _claude_exe()
    if not exe:
        return {"ok": False, "fatal": False, "error": "claude CLI not found on PATH"}

    def invoke(prompt_text: str, session_args: list[str]):
        # Keep this argv conservative. Some Claude CLI releases do not support
        # prompt-cache tuning flags, while -p/json/session-id/resume are the core
        # contract this integration needs.
        argv = [exe, "-p", "--output-format", "json"]
        argv += session_args
        argv += _claude_tool_args(cfg)
        if provider.get("model"):
            argv += ["--model", provider["model"]]
        argv += list(provider.get("extra_args") or [])
        timeout = _run_timeout(cfg)
        try:
            proc = _run_proc(argv, input_text=prompt_text, timeout=timeout, cancel=cancel)
        except _Cancelled:
            return None, {"ok": False, "fatal": True, "cancelled": True, "error": "cancelled"}
        except subprocess.TimeoutExpired:
            return None, {"ok": False, "fatal": True, "error": f"claude timed out after {timeout}s"}
        except Exception as exc:  # noqa: BLE001
            return None, {"ok": False, "fatal": False, "error": f"claude failed to launch: {type(exc).__name__}: {exc}"}
        return proc, None

    allow_web = bool(cfg.get("allow_web"))
    if resume_id:
        if progress:
            progress(f"asking {PROVIDER_LABELS['claude']} (resuming cached session)…")
        proc, err = invoke(_qa_followup_text(question, allow_web), ["--resume", resume_id])
        if err is not None and err.get("cancelled"):
            return err
        if err is None:
            res = _finish_claude_json("claude", provider, proc, resume_id)
            if res.get("ok"):
                return res
        if progress:
            progress("cached session unavailable; starting a fresh one…")

    sid = str(uuid.uuid4())
    if progress:
        progress(f"asking {PROVIDER_LABELS['claude']}…")
    proc, err = invoke(build_qa_prompt(rec, history, question, note, allow_web=allow_web),
                       ["--session-id", sid])
    if err:
        return err
    return _finish_claude_json("claude", provider, proc, sid)


def ask(rec: dict[str, Any], history: list[dict] | None, question: str, *,
        note: str | None = None, session: dict | None = None, cfg: dict | None = None,
        progress: Callable[[str], None] | None = None,
        cancel: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Answer a follow-up question, preferring a resumed (cache-warm) Claude
    session. ``session`` is the thread's last session {provider,id}; only a
    Claude session is resumable. Non-session providers get the full context.
    Returns the usual result plus ``session`` and ``usage`` for Claude."""
    cfg = cfg or load_config()
    full_prompt = build_qa_prompt(rec, history, question, note, allow_web=cfg.get("allow_web", False))
    attempts: list[str] = []
    for provider in _ordered_providers(cfg):
        if not provider.get("enabled"):
            continue
        pid = provider.get("id")
        if pid == "claude":
            resume_id = (session or {}).get("id") if (session or {}).get("provider") == "claude" else None
            res = _run_claude_qa(rec, history, question, note, provider, cfg, resume_id, progress, cancel)
        elif pid == "cursor":
            if progress:
                progress(f"asking {PROVIDER_LABELS['cursor']}…")
            res = _run_cursor(full_prompt, provider, cfg, cancel)
        else:
            continue
        if res.get("ok"):
            res["attempts"] = attempts
            return res
        if res.get("cancelled"):
            return {"ok": False, "cancelled": True, "error": "cancelled", "attempts": attempts}
        attempts.append(res.get("error", f"{pid} failed"))
        if res.get("fatal"):
            break
        if progress:
            progress(f"{PROVIDER_LABELS.get(pid, pid)} unavailable, trying next…")
    return {"ok": False, "error": "; ".join(attempts) or "no enabled backends available",
            "attempts": attempts}


def build_doc_qa_prompt(title: str, document: str, citations: list[dict] | None,
                        history: list[dict] | None, question: str, *,
                        allow_web: bool = False) -> str:
    """A follow-up Q&A prompt grounded in a Deep Research report (not a ticker
    DATA block). The report is the evidence base; the prior conversation keeps
    the thread coherent. The report and history are bounded so the prompt stays
    a sane size as both grow."""
    doc = (document or "").strip()
    if len(doc) > 16000:
        doc = doc[:16000] + "\n…[report truncated]"
    src_lines = []
    for c in (citations or [])[:40]:
        href = str(c.get("href") or "").strip()
        if not href:
            continue
        label = str(c.get("label") or "").split("\n")[0].strip()
        src_lines.append(f"- {label} {href}".strip())
    src_block = ("\nSOURCES (citations from the run):\n" + "\n".join(src_lines) + "\n") if src_lines else ""
    convo = ""
    for t in [t for t in (history or []) if t.get("text")][-12:]:
        who = "Q" if t.get("role") == "user" else "A"
        txt = t["text"].strip()
        if who == "A" and len(txt) > 1500:
            txt = txt[:1500] + " …[truncated]"
        convo += f"{who}: {txt}\n\n"
    convo_block = ("CONVERSATION SO FAR:\n" + convo) if convo else ""
    web_rule = _qa_data_rule(allow_web).replace("DATA block", "REPORT").replace("the data", "the report")
    return f"""You are a skeptical, evidence-driven equity analyst answering a follow-up question about a Deep Research report titled "{title or 'this segment'}" for a self-directed investor. Improve their decision; do not cheerlead.

GROUND RULES
{web_rule}
- Be concise and direct. Answer the specific question asked; don't restate the whole report.
- Tag every company ticker with a leading $ on first mention (e.g. $AMD).
- The report is narrative synthesis: treat its numbers as claims, and say so when a figure should be verified against a primary source.

REPORT
{doc}
{src_block}
{convo_block}NEW QUESTION:
{question.strip()}

Answer in Markdown.{' End with a "Sources" line listing any URLs you used.' if allow_web else ''}"""


def ask_about_doc(title: str, document: str, citations: list[dict] | None,
                  history: list[dict] | None, question: str, *, cfg: dict | None = None,
                  progress: Callable[[str], None] | None = None,
                  cancel: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Answer a follow-up question grounded in a Deep Research report. Unlike
    ``ask`` (which is tied to a ticker DATA record and can resume a Claude
    session), this runs the generic backend fallback with the report as context.
    Returns the usual {ok, report, backend, backend_label, model, ...} result."""
    cfg = cfg or load_config()
    prompt = build_doc_qa_prompt(title, document, citations, history, question,
                                 allow_web=cfg.get("allow_web", False))
    return _run_with_fallback(prompt, cfg, progress, cancel)


if __name__ == "__main__":
    import argparse

    try:  # Windows consoles default to cp1252; reports use em-dashes etc.
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    ap = argparse.ArgumentParser(description="Run an in-depth single-ticker analysis via a local agent CLI.")
    ap.add_argument("symbol")
    ap.add_argument("--backends", action="store_true", help="just report which backends resolve")
    ap.add_argument("--web", action="store_true", help="enable scoped web research (WebSearch/WebFetch) for this run")
    args = ap.parse_args()
    if args.backends:
        print(json.dumps(available_backends(), indent=2))
        sys.exit(0)
    cfg = load_config()
    if args.web:
        cfg["allow_web"] = True
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import research_pull  # noqa: E402
    record = research_pull.pull_ticker(args.symbol.upper(), write=False)
    result = analyze(record, cfg=cfg,
                     progress=lambda m: print(f"[{dt.datetime.now():%H:%M:%S}] {m}", file=sys.stderr))
    if result.get("ok"):
        print(result["report"])
        print(f"\n--- via {result['backend_label']} ({result['model']}) ---", file=sys.stderr)
    else:
        print("FAILED: " + result.get("error", "unknown"), file=sys.stderr)
        sys.exit(1)
