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
* Tools are disabled (claude ``--tools ""``) / read-only (cursor ``--mode ask``)
  so a backend can't shell out, edit files, or hang on a permission prompt. The
  analysis is grounded ONLY in the numbers we hand it -- if a figure is missing
  it is told to say so, not to guess.
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
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "data" / "analysis-config.json"

# Default backend policy. Order == fallback order. A user override file at
# CONFIG_PATH is shallow-merged over this (see load_config).
DEFAULT_CONFIG: dict[str, Any] = {
    "providers": [
        {"id": "claude", "enabled": True, "model": "", "extra_args": []},
        {"id": "cursor", "enabled": True, "model": "", "extra_args": []},
    ],
    "timeout_sec": 300,
    # When true, backends may use their web tools for fresher context. Off by
    # default: keeps runs fast, cheap, and grounded in our deterministic data.
    "allow_web": False,
}

# Phrases that mean "this backend is out of quota / not usable right now" so the
# orchestrator falls through to the next provider instead of surfacing an error.
_QUOTA_HINTS = (
    "usage limit",
    "rate limit",
    "quota",
    "5-hour limit",
    "limit reached",
    "too many requests",
    "authentication required",
    "please run 'agent login'",
    "not logged in",
)

PROVIDER_LABELS = {"claude": "Claude CLI", "cursor": "Cursor CLI"}


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def load_config() -> dict[str, Any]:
    """Defaults merged with the on-disk override (if any). Always returns a
    well-formed config even if the file is missing or partially specified."""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return cfg
    if isinstance(raw.get("providers"), list) and raw["providers"]:
        cleaned = []
        for p in raw["providers"]:
            if not isinstance(p, dict) or p.get("id") not in PROVIDER_LABELS:
                continue
            cleaned.append({
                "id": p["id"],
                "enabled": bool(p.get("enabled", True)),
                "model": str(p.get("model") or ""),
                "extra_args": [str(a) for a in (p.get("extra_args") or []) if str(a).strip()],
            })
        if cleaned:
            cfg["providers"] = cleaned
    if isinstance(raw.get("timeout_sec"), (int, float)) and raw["timeout_sec"] > 0:
        cfg["timeout_sec"] = int(raw["timeout_sec"])
    cfg["allow_web"] = bool(raw.get("allow_web", cfg["allow_web"]))
    return cfg


def save_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """Validate and persist a config; returns the normalized, stored version."""
    merged = json.loads(json.dumps(DEFAULT_CONFIG))
    if isinstance(cfg.get("providers"), list):
        cleaned = []
        seen = set()
        for p in cfg["providers"]:
            pid = (p or {}).get("id")
            if pid not in PROVIDER_LABELS or pid in seen:
                continue
            seen.add(pid)
            cleaned.append({
                "id": pid,
                "enabled": bool(p.get("enabled", True)),
                "model": str(p.get("model") or "").strip(),
                "extra_args": [str(a) for a in (p.get("extra_args") or []) if str(a).strip()],
            })
        if cleaned:
            merged["providers"] = cleaned
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


def build_prompt(rec: dict[str, Any]) -> str:
    sym = rec.get("symbol", "?")
    data = json.dumps(_compact_record(rec), indent=2, default=str)
    return f"""You are a skeptical, evidence-driven equity analyst writing an in-depth note on ${sym} for a self-directed investor who already holds a diversified portfolio. Your job is to improve the quality of their decision, not to cheerlead.

GROUND RULES
- Use ONLY the numbers in the DATA block below. Do not invent figures or cite prices/multiples not present. If something important is missing, say "not in the data" rather than guessing.
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
2-3 concrete, observable triggers (numbers, events) that would flip your verdict.

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


def _run_claude(prompt: str, provider: dict, cfg: dict) -> dict[str, Any]:
    exe = _claude_exe()
    if not exe:
        return {"ok": False, "fatal": False, "error": "claude CLI not found on PATH"}
    argv = [exe, "-p", "--output-format", "text"]
    if not cfg.get("allow_web"):
        argv += ["--tools", ""]
    if provider.get("model"):
        argv += ["--model", provider["model"]]
    argv += list(provider.get("extra_args") or [])
    try:
        proc = subprocess.run(
            argv, input=prompt, capture_output=True, text=True,
            timeout=cfg.get("timeout_sec", 300), encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "fatal": True, "error": f"claude timed out after {cfg.get('timeout_sec', 300)}s"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "fatal": False, "error": f"claude failed to launch: {type(exc).__name__}: {exc}"}
    return _finish("claude", provider, proc)


def _run_cursor(prompt: str, provider: dict, cfg: dict) -> dict[str, Any]:
    base = _cursor_argv_base()
    if not base:
        return {"ok": False, "fatal": False, "error": "cursor-agent CLI not found / unresolved"}
    # cursor-agent does NOT read the prompt from stdin in -p mode; it must be a
    # positional arg. We pass it as a real argv element (no shell) so quotes and
    # newlines survive intact.
    argv = base + ["-p", prompt, "--output-format", "text", "--trust"]
    if not cfg.get("allow_web"):
        argv += ["--mode", "ask"]  # read-only Q&A; no shell, no edits
    if provider.get("model"):
        argv += ["--model", provider["model"]]
    argv += list(provider.get("extra_args") or [])
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True,
            timeout=cfg.get("timeout_sec", 300), encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "fatal": True, "error": f"cursor-agent timed out after {cfg.get('timeout_sec', 300)}s"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "fatal": False, "error": f"cursor-agent failed to launch: {type(exc).__name__}: {exc}"}
    return _finish("cursor", provider, proc)


def _finish(pid: str, provider: dict, proc: subprocess.CompletedProcess) -> dict[str, Any]:
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0 or not out:
        blob = err or out or f"exit {proc.returncode}"
        # Quota/auth failures are non-fatal: let the next backend try.
        return {"ok": False, "fatal": not _looks_like_quota(blob),
                "error": f"{PROVIDER_LABELS[pid]}: {blob.splitlines()[-1] if blob.splitlines() else blob}"}
    return {"ok": True, "report": out, "backend": pid,
            "backend_label": PROVIDER_LABELS[pid], "model": provider.get("model") or "(default)"}


_RUNNERS: dict[str, Callable[..., dict]] = {"claude": _run_claude, "cursor": _run_cursor}


def analyze(rec: dict[str, Any], *, cfg: dict | None = None,
            progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Run the analysis through the configured backends in order, falling back on
    quota/auth failure. Returns the first success, or an aggregate error."""
    cfg = cfg or load_config()
    prompt = build_prompt(rec)
    attempts: list[str] = []
    for provider in cfg.get("providers", []):
        if not provider.get("enabled"):
            continue
        pid = provider.get("id")
        runner = _RUNNERS.get(pid)
        if not runner:
            continue
        if progress:
            progress(f"asking {PROVIDER_LABELS.get(pid, pid)}…")
        res = runner(prompt, provider, cfg)
        if res.get("ok"):
            res["attempts"] = attempts
            return res
        attempts.append(res.get("error", f"{pid} failed"))
        if res.get("fatal"):
            # A real failure (timeout / bad output), not a quota miss: stop here
            # rather than burning the fallback on the same broken input.
            break
        if progress:
            progress(f"{PROVIDER_LABELS.get(pid, pid)} unavailable, trying next…")
    return {"ok": False, "error": "; ".join(attempts) or "no enabled backends available",
            "attempts": attempts}


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
    args = ap.parse_args()
    if args.backends:
        print(json.dumps(available_backends(), indent=2))
        sys.exit(0)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import research_pull  # noqa: E402
    record = research_pull.pull_ticker(args.symbol.upper(), write=False)
    result = analyze(record, progress=lambda m: print(f"[{dt.datetime.now():%H:%M:%S}] {m}", file=sys.stderr))
    if result.get("ok"):
        print(result["report"])
        print(f"\n--- via {result['backend_label']} ({result['model']}) ---", file=sys.stderr)
    else:
        print("FAILED: " + result.get("error", "unknown"), file=sys.stderr)
        sys.exit(1)
