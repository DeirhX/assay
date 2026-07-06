#!/usr/bin/env python3
"""Dev live-reload for the local server (opt-in via ``serve.py --reload``).

Three pieces, extracted from serve.py so the server module owns routing and
this one owns the restart machinery:

* ``run_reloader()`` -- the supervisor (parent process). Respawns the serving
  child whenever it exits with code 3 (a requested reload). Keeps a stable PID
  and the console, so Ctrl+C and stdout behave normally across reloads --
  unlike execv, which on Windows detaches into a new, console-less process.
* ``reload_watcher()`` -- the child-side thread. When ``tools/*.py`` changes,
  exits with code 3 so the supervisor respawns a fresh process.
* ``assets_version()`` -- the opaque token ``/api/dev/livereload`` serves; the
  browser reloads itself when it changes.

Production never touches this module: without ``--reload`` none of it runs.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from config import REPO_ROOT, ROOT_STATIC_SUFFIXES, WEB_DIR
from jobs import any_active as _any_active_deep_job

# Env marker the supervisor sets on the child so the child actually serves
# instead of supervising again.
RELOAD_CHILD_ENV = "_REBAL_RELOAD_CHILD"


def assets_version(boot_token: str) -> str:
    """Opaque token that changes whenever a served asset changes OR the server
    restarts (the caller passes its per-process boot token). The browser
    reloads when this differs from what it last saw."""
    latest = 0.0
    for p in WEB_DIR.rglob("*"):
        if p.is_file():
            m = p.stat().st_mtime
            if m > latest:
                latest = m
    for p in REPO_ROOT.iterdir():  # root mini-site assets (site.css, *.html)
        if p.is_file() and p.suffix in ROOT_STATIC_SUFFIXES:
            m = p.stat().st_mtime
            if m > latest:
                latest = m
    return f"{latest:.3f}-{boot_token}"


def _server_sources() -> list[Path]:
    """Python files whose edits warrant restarting the API process."""
    return sorted((REPO_ROOT / "tools").glob("*.py"))


def reload_watcher() -> None:
    """Child-side watcher. When server code changes, exit with code 3 so the
    supervisor respawns a fresh process. Guards: never restart on code that fails
    to compile (keep serving the last good version), and never interrupt an
    in-flight Deep Research run (defer the exit until it ends)."""
    mtimes = {p: p.stat().st_mtime for p in _server_sources() if p.exists()}
    pending = False
    waited = False
    while True:
        time.sleep(1.0)
        for p in _server_sources():
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            if mtimes.get(p) == m:
                continue
            mtimes[p] = m
            try:
                compile(p.read_text(encoding="utf-8"), str(p), "exec")
            except SyntaxError as exc:
                sys.stderr.write(f"[reload] {p.name}: syntax error, staying on current code ({exc.msg} line {exc.lineno})\n")
                continue
            sys.stderr.write(f"[reload] {p.name} changed\n")
            pending = True
        if not pending:
            continue
        if _any_active_deep_job():
            if not waited:
                sys.stderr.write("[reload] change pending; holding restart until deep-research job(s) finish\n")
                waited = True
            continue
        sys.stderr.write("[reload] restarting to apply changes\n")
        sys.stderr.flush()
        os._exit(3)


def run_reloader() -> int:
    """Supervisor (parent). Runs the server as a child and respawns it whenever
    the child exits with code 3 (a requested reload)."""
    import subprocess

    child_env = dict(os.environ, **{RELOAD_CHILD_ENV: "1"})
    argv = [sys.executable, *sys.argv]
    print("[reload] supervisor watching tools/*.py — edits restart the API in place")
    while True:
        proc = subprocess.Popen(argv, env=child_env)
        try:
            code = proc.wait()
        except KeyboardInterrupt:
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
            return 0
        if code == 3:
            continue  # requested reload -> respawn with the new code
        return code  # clean exit or crash -> stop supervising
