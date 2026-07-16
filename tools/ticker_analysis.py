#!/usr/bin/env python3
"""On-demand, in-depth single-ticker analysis via a local agent CLI.

This is the *cheap workhorse* tier of the research stack. It does NOT crawl the
web like Perplexity Deep Research (that quota is scarce and reserved for whole
segments); instead it runs a local agent CLI as a pure reasoning pass over the
deterministic numbers we already pulled (Yahoo / SEC / FMP) and turns them into
a skeptical, structured analyst note.

This module is the *runner*: it owns how a backend is driven (argv, cancellable
subprocess, quota/auth fallback) and the analyze/ask/draft entry points. The
surrounding concerns live in focused siblings it re-imports, so callers reaching
``ticker_analysis.X`` keep working regardless of where X now lives:

* ``analysis_config``   -- provider vocabulary + config load/save.
* ``analysis_backends`` -- LLM-CLI detection, credential probing, setup status.
* ``analysis_prompts``  -- the prompt builders.
* ``analysis_report``   -- parsing the model's output back into structured data.

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
    (which DOES include a web-access tool) and additionally gets ``--force`` --
    headless ``-p`` auto-REJECTS any tool call needing approval, so without
    ``--force`` the web tool is denied and the model apologises. Verified that
    ``--mode ask --force`` enables web while keeping file-write/shell DENIED, so
    --force can't escalate past read-only.
  We still never enable Bash/Edit/Write on either -- an analyst note has no
  business shelling out or touching the filesystem (``--mode ask`` enforces this
  even under ``--force``).
* Backends consume YOUR interactive coding quota. Cheap != free; that's why this
  is gated behind an explicit button, not run on every page view.
* Windows is a first-class target. ``cursor-agent`` ships as a PowerShell/.cmd
  shim around ``node index.js``; we resolve and call node directly so arbitrary
  prompt text (full of quotes and newlines) isn't mangled by cmd.exe.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, Callable

import errorlog

# Re-export the extracted halves so callers reaching ticker_analysis.<name> are
# unchanged. Names the runner uses itself are imported plainly; pure re-exports
# use the redundant `X as X` form so the linter treats them as intentional.
from analysis_config import (
    CONFIG_PATH as CONFIG_PATH,
    DEFAULT_CONFIG as DEFAULT_CONFIG,
    PROVIDER_LABELS,
    load_config,
    save_config as save_config,
)
from analysis_backends import (
    available_backends,
    provider_models as provider_models,
    setup_status as setup_status,
    _claude_exe,
    _cursor_argv_base,
    _is_transient_failure,
    _looks_like_auth,
    _looks_like_quota,
)
from analysis_prompts import (
    build_doc_qa_prompt,
    build_prompt,
    build_qa_prompt,
    build_segment_draft_prompt,
    _qa_followup_text,
)
from analysis_report import _extract_json_object, _norm_usage
from analysis_report import parse_price_levels as parse_price_levels  # re-export for callers/mocks


# --------------------------------------------------------------------------- #
# Running a backend
# --------------------------------------------------------------------------- #

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
    #
    # Tool policy is governed by --mode, NOT by bare -p: we pin read-only "ask"
    # unconditionally so an analyst note can never edit files or shell out. Bare
    # -p would grant ALL tools (write + shell); --mode ask overrides that.
    #
    # Web research: ask mode DOES include a read-only web-access tool, but in
    # headless -p mode a tool call that needs approval is auto-REJECTED (there's
    # no human to press "y") -- which is why a web-enabled prompt without --force
    # gets the model's "web search was rejected in this session" apology. --force
    # ("allow commands unless explicitly denied") pre-approves the read-only web
    # tool. Verified empirically: with `--mode ask --force`, web search works
    # while file-write and shell stay DENIED by ask-mode policy. So we only add
    # --force when the user actually asked for web, keeping the surface minimal.
    argv = base + ["-p", prompt, "--output-format", "text", "--trust", "--mode", "ask"]
    if cfg.get("allow_web"):
        argv.append("--force")
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
_PROVIDER_ORDER = {"claude": 0, "cursor": 1}


def _ordered_providers(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Canonical backend preference: Claude first, Cursor as fallback.

    The order is enforced here (via _PROVIDER_ORDER) rather than trusting the
    config file's array order, so the default engine is deterministic and a
    hand-edited config can't accidentally change which backend leads. When
    Claude is out of quota (or its credentials lapse) _run_with_fallback treats
    that as a transient failure and defers to Cursor. Note: this governs the
    in-depth/Q&A analyses only -- deep research has its own engine.
    """
    providers = [p for p in (cfg.get("providers") or []) if p.get("id") in _RUNNERS]
    return sorted(providers, key=lambda p: _PROVIDER_ORDER.get(p.get("id"), 99))


def _run_with_fallback(prompt: str, cfg: dict,
                       progress: Callable[[str], None] | None = None,
                       cancel: Callable[[], bool] | None = None,
                       *, label: str = "") -> dict[str, Any]:
    """Run a prompt through the configured backends in order, falling back on
    quota/auth failure. Returns the first success, or an aggregate error.

    ``label`` is a short tag for the operational error log (e.g. "analysis",
    "qa") so a logged backend failure says what kind of request it choked on."""
    attempts: list[str] = []
    for provider in _ordered_providers(cfg):
        if not provider.get("enabled"):
            continue
        pid = str(provider.get("id") or "")
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
        blob = res.get("error", "") or f"{pid} failed"
        attempts.append(blob)
        # A real failure worth a durable record -- crucially, this fires even when
        # the *next* backend goes on to succeed, so the lead backend (Claude)
        # silently hitting its quota and us quietly deferring to Cursor stops
        # being invisible.
        reason = ("auth" if _looks_like_auth(blob)
                  else "quota" if _looks_like_quota(blob)
                  else "error")
        errorlog.warn("llm_backend", blob, backend=pid, reason=reason,
                      fatal=bool(res.get("fatal")), op=label or None)
        if res.get("fatal"):
            # A real failure (timeout / bad output), not a quota miss: stop here
            # rather than burning the fallback on the same broken input.
            break
        if progress:
            progress(f"{PROVIDER_LABELS.get(pid, pid)} unavailable, trying next…")
    errorlog.error("llm_backend",
                   "all analysis backends failed: " + ("; ".join(attempts) or "none enabled"),
                   op=label or None)
    return {"ok": False, "error": "; ".join(attempts) or "no enabled backends available",
            "attempts": attempts}


def analyze(rec: dict[str, Any], *, cfg: dict | None = None,
            progress: Callable[[str], None] | None = None,
            cancel: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Generate the structured in-depth note over the deterministic dossier."""
    cfg = cfg or load_config()
    return _run_with_fallback(build_prompt(rec, allow_web=cfg.get("allow_web", False)),
                              cfg, progress, cancel, label="analysis")


# --------------------------------------------------------------------------- #
# Segment drafting: turn a freeform theme ("space exploration") into a list of
# real, currently-listed public tickers. This is how the research console stops
# being limited to names you already hold -- the LLM proposes the universe, the
# deterministic pull + review gate still vet it downstream. The prompt builder
# (build_segment_draft_prompt) and the tolerant JSON extractor
# (_extract_json_object) live in analysis_prompts / analysis_report; this owns
# the run-and-parse orchestration.
# --------------------------------------------------------------------------- #
def draft_segment_members(query: str, *, cfg: dict | None = None,
                          progress: Callable[[str], None] | None = None,
                          cancel: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Ask the configured backend for a themed list of public tickers. Returns
    {ok, members, title, comment, sleeves, backend_label, ...} or {ok: False,
    error}. Members are raw model output; the caller validates symbols."""
    cfg = cfg or load_config()
    prompt = build_segment_draft_prompt(query, allow_web=cfg.get("allow_web", False))
    res = _run_with_fallback(prompt, cfg, progress, cancel, label="segment-draft")
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
    text = raw
    usage: dict[str, Any] = {}
    sid = session_id
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
    return _run_with_fallback(prompt, cfg, progress, cancel, label="doc-qa")


if __name__ == "__main__":
    import argparse

    try:  # Windows consoles default to cp1252; reports use em-dashes etc.
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
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
