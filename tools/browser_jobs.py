#!/usr/bin/env python3
"""Perplexity browser automation: auth state + the three Playwright-backed jobs.

Extracted from serve.py. Everything here talks to the shared, slot-limited
Playwright worker (pplx_deep_research) to drive Perplexity Deep Research:

* Cached login state -- read/write the gitignored auth flag, plus a synchronous
  ~8s live probe (verify_login).
* The three background jobs -- a deep-research run, a one-off login window, and
  an import-by-URL -- all built on one _browser_job scaffold that owns the worker
  import, the running flip, exception capture, and the all-important release of
  the single active-job slot.

Concurrency is bounded by jobs.claim_active / jobs.release_active; callers must
claim a slot before starting a job and the scaffold releases it. Public names are
underscore-free; serve.py imports them aliased to its existing private names.
"""

from __future__ import annotations

import datetime as dt
import re
import threading

import jobs
from apierror import Conflict
from config import AUTH_STATE_FILE, SEGMENT_DEF_DIR
from deep_runs import _save_deep_artifact as save_deep_artifact
from jobs import (
    claim_active, new_job, public, release_active, slots_busy_msg, update_job,
)
from store import load, slugify, write_json


def get_auth_state() -> dict:
    st = load(AUTH_STATE_FILE) or {}
    return {
        "logged_in": bool(st.get("logged_in")),
        "updated_at": st.get("updated_at"),
        "note": st.get("note", ""),
    }


def set_auth_state(logged_in: bool, note: str = "") -> None:
    write_json(AUTH_STATE_FILE, {
        "logged_in": bool(logged_in),
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "note": note,
    })


def verify_login() -> dict:
    """Synchronous, ~8s live probe that refreshes the cached login flag."""
    if not claim_active():
        raise Conflict(slots_busy_msg())
    try:
        import pplx_deep_research as worker
        res = worker.check_login()
        if res.get("status") == "error":
            raise Conflict(res.get("detail") or "login check failed")
        set_auth_state(res.get("status") == "logged_in", "active check")
        return get_auth_state()
    finally:
        release_active()


def clarify_answer_for(segment: str) -> str:
    """A concrete reply the worker can submit if Perplexity asks what is in the
    segment, so the run finishes unattended."""
    definition = load(SEGMENT_DEF_DIR / f"{segment}.json") or {}
    syms = [m.get("symbol") for m in definition.get("members", []) if m.get("symbol")]
    if syms:
        return (
            "My segment is exactly these tickers, treated as individual stocks: "
            + ", ".join(syms)
            + ". Do not ask further clarifying questions; proceed with the full "
            "deep research now."
        )
    return (
        "Use exactly the tickers and scope in my original request. Do not ask "
        "further clarifying questions; proceed now."
    )


# Appended to the import-failure message of the deep-research job so a user who
# never set Playwright up gets the install commands inline.
_PLAYWRIGHT_INSTALL_HINT = (
    ". Install with `py -3 -m pip install playwright` then "
    "`py -3 -m playwright install chromium`."
)


def browser_job(job_id: str, *, running_msg: str, call, handle, install_hint: str = "") -> None:
    """Shared scaffold for the three Playwright-backed jobs (deep run, login,
    import). It owns the boilerplate that was duplicated across all three: import
    the worker (mapping a missing Playwright to an error), flip the job to
    running, capture worker exceptions, and ALWAYS release the single active-job
    slot via finally. `call(worker, progress)` performs the actual worker call
    and returns its result dict; `handle(res)` maps that result to job state and
    must not release the slot itself."""
    def progress(msg: str) -> None:
        update_job(job_id, message=msg)

    try:
        import pplx_deep_research as worker
    except Exception as exc:  # noqa: BLE001
        update_job(job_id, state="error",
                   error=f"Playwright not available: {type(exc).__name__}: {exc}{install_hint}")
        release_active()
        return

    update_job(job_id, state="running", message=running_msg)
    try:
        res = call(worker, progress)
    except Exception as exc:  # noqa: BLE001
        update_job(job_id, state="error", error=f"{type(exc).__name__}: {exc}")
        release_active()
        return

    try:
        handle(res)
    finally:
        release_active()


def save_run_result(job_id: str, res: dict, segment: str, date: str, *,
                    source_url, auth_label: str, done_msg: str) -> None:
    """Shared 'done' handling for the deep-run and import jobs: persist the
    artifact, refresh auth state, and finish the job with a uniform result.
    Leaves the active-slot release to the browser_job scaffold."""
    try:
        artifact = save_deep_artifact({
            "segment": segment,
            "date": date,
            "report": res.get("report", ""),
            "citations": res.get("citations", []),
            "source_url": source_url,
        })
    except Exception as exc:  # noqa: BLE001
        update_job(job_id, state="error", error=f"saved nothing: {type(exc).__name__}: {exc}")
        return
    set_auth_state(True, auth_label)
    update_job(job_id, state="done", message=done_msg,
               result={
                   "source_url": source_url,
                   "citations": res.get("citations", []),
                   "report_chars": len(res.get("report", "")),
               },
               artifact=artifact)


def run_deep_job(job_id: str, segment: str, date: str, prompt: str, window_mode: str) -> None:
    def call(worker, progress):
        return worker.run_deep_research(
            prompt, window_mode=window_mode,
            clarify_answer=clarify_answer_for(segment), progress=progress,
            clone_profile=True,  # run on a throwaway clone so runs can parallelize
            on_url=lambda url: update_job(job_id, source_url=url),
            cancel=lambda: jobs.is_cancelled(job_id),
        )

    def handle(res: dict) -> None:
        status = res.get("status")
        if status == "done":
            save_run_result(job_id, res, segment, date,
                            source_url=res.get("source_url"),
                            auth_label="deep run", done_msg="report saved")
        elif status == "cancelled":
            update_job(job_id, state="cancelled", message="cancelled")
        elif status == "needs_login":
            set_auth_state(False, "run hit login wall")
            update_job(job_id, state="needs_login",
                       message="Not logged in. Use 'Set up Perplexity login' once, then re-run.")
        elif status == "needs_captcha":
            update_job(job_id, state="error",
                       error=("A human-verification check (CAPTCHA) appeared and was not "
                              "solved in time. Re-run, and when the browser window pops to "
                              "the front, complete the check to continue."))
        elif status == "computer_trap":
            update_job(job_id, state="error",
                       error=f"Hit the paid Computer path ({res.get('url')}); aborted to protect credits.")
        elif status == "needs_clarification":
            update_job(job_id, state="error",
                       error=("Perplexity kept asking clarifying questions. Open "
                              f"{res.get('source_url')} , answer it there, then paste "
                              "the finished report on the Report step."))
        elif status == "timeout":
            url = res.get("source_url") or ""
            detail = "Deep Research timed out before a finished report could be confirmed."
            if url:
                detail += f" If the Perplexity page later finishes, import this URL: {url}"
            update_job(job_id, state="error", error=detail)
        else:
            update_job(job_id, state="error",
                       error=res.get("detail") or f"deep research {status}")

    browser_job(job_id, running_msg="starting browser", call=call, handle=handle,
                install_hint=_PLAYWRIGHT_INSTALL_HINT)


def run_login_job(job_id: str) -> None:
    def call(worker, progress):
        return worker.ensure_login(progress=progress,
                                   cancel=lambda: jobs.is_cancelled(job_id))

    def handle(res: dict) -> None:
        if res.get("status") == "logged_in":
            set_auth_state(True, "login window")
            update_job(job_id, state="done", message="Perplexity login confirmed")
        elif res.get("status") == "cancelled":
            update_job(job_id, state="cancelled", message="cancelled")
        else:
            update_job(job_id, state="error", message="login window timed out",
                       error="login not completed in time")

    browser_job(job_id, running_msg="opening login window", call=call, handle=handle)


def start_deep_research(body: dict) -> dict:
    segment = slugify(str(body.get("segment") or ""))
    date = str(body.get("date") or dt.datetime.now(dt.timezone.utc).date().isoformat())
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise ValueError("date must be YYYY-MM-DD")
    prompt = str(body.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("prompt is required")
    window_mode = str(body.get("window_mode") or "offscreen")
    if window_mode not in ("offscreen", "visible", "headless"):
        raise ValueError("window_mode must be offscreen, visible, or headless")
    if not claim_active():
        raise Conflict(slots_busy_msg())
    job = new_job("deep_research", segment=segment, date=date, window_mode=window_mode)
    threading.Thread(target=run_deep_job,
                     args=(job["id"], segment, date, prompt, window_mode),
                     daemon=True).start()
    return public(job)


def start_login() -> dict:
    if not claim_active():
        raise Conflict(slots_busy_msg())
    job = new_job("login")
    threading.Thread(target=run_login_job, args=(job["id"],), daemon=True).start()
    return public(job)


def run_import_job(job_id: str, segment: str, date: str, url: str) -> None:
    def call(worker, progress):
        # The import URL is known up front -> surface it as the live link now.
        update_job(job_id, source_url=url)
        return worker.fetch_by_url(url, clone_profile=True, progress=progress,
                                   cancel=lambda: jobs.is_cancelled(job_id))

    def handle(res: dict) -> None:
        status = res.get("status")
        if status == "done":
            save_run_result(job_id, res, segment, date,
                            source_url=res.get("source_url", url),
                            auth_label="import", done_msg="imported report saved")
        elif status == "cancelled":
            update_job(job_id, state="cancelled", message="cancelled")
        elif status == "needs_login":
            set_auth_state(False, "import hit login wall")
            update_job(job_id, state="needs_login",
                       message="Not logged in. Use 'Set up Perplexity login' once, then import.")
        elif status == "needs_captcha":
            update_job(job_id, state="error",
                       error=("A human-verification check (CAPTCHA) appeared and was not "
                              "solved in time. Re-run the import and complete the check "
                              "when the browser window appears."))
        elif status == "needs_clarification":
            update_job(job_id, state="error",
                       error="That run is still awaiting a clarifying answer. Answer it in "
                             "Perplexity, wait for it to finish, then import again.")
        else:
            update_job(job_id, state="error", error=res.get("detail") or f"import {status}")

    browser_job(job_id, running_msg="opening run URL", call=call, handle=handle)


def start_import(body: dict) -> dict:
    segment = slugify(str(body.get("segment") or ""))
    if not segment:
        raise ValueError("segment is required")
    date = str(body.get("date") or dt.datetime.now(dt.timezone.utc).date().isoformat())
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise ValueError("date must be YYYY-MM-DD")
    url = str(body.get("url") or "").strip()
    if "perplexity.ai" not in url:
        raise ValueError("a perplexity.ai run URL is required")
    if not claim_active():
        raise Conflict(slots_busy_msg())
    job = new_job("import", segment=segment, date=date)
    threading.Thread(target=run_import_job,
                     args=(job["id"], segment, date, url),
                     daemon=True).start()
    return public(job)
