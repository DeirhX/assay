// Shared background-job polling. `pollDeepJob` drives the per-view status line
// for any /api/deep-job-backed task (analysis, Q&A, deep research, IBKR sync,
// segment draft) and is used by ~10 modules, so it lives in its own leaf rather
// than in errors.ts (where it used to sit and force an errors↔pipeline import
// cycle) or in pipeline.ts (which would drag the whole wizard into every caller).
//
// Dependencies point one way only: jobs -> {core, tasks, errors}. The one piece
// that genuinely belongs to the pipeline — recovering from a `needs_login` job —
// is injected via setNeedsLoginHandler (mirroring core.setErrorSink), so jobs
// never imports pipeline.
import type { Job } from "./api-types";
import { api, esc } from "./core";
import { recordError } from "./errors";
import { kickTaskPoll } from "./tasks";

// Escape a string for HTML, but turn bare http(s) URLs into clickable links.
// Job status/error messages embed a Perplexity run URL (e.g. the "answer the
// clarifying question here" stall) which was previously dropped into
// textContent as dead plain text. Everything except the URL is escaped, so
// this stays XSS-safe.
export function linkifyHtml(text: unknown) {
  const s = String(text == null ? "" : text);
  const re = /(https?:\/\/[^\s<>]+)/g;
  let out = "";
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(s)) !== null) {
    out += esc(s.slice(last, m.index));
    let url = m[1];
    // Don't swallow trailing sentence punctuation into the href.
    const trail = url.match(/[).,;]+$/);
    let tail = "";
    if (trail) { tail = trail[0]; url = url.slice(0, -tail.length); }
    out += `<a href="${esc(url)}" target="_blank" rel="noopener">${esc(url)} \u2197</a>` + esc(tail);
    last = m.index + m[1].length;
  }
  out += esc(s.slice(last));
  return out;
}

// A job can return `needs_login` (a Perplexity session went stale). Only the
// pipeline knows how to render that recovery — mark the gate, offer a login
// button — so it registers a handler here instead of jobs importing pipeline.
// Default is a plain message for callers that boot before the pipeline module.
type NeedsLoginHandler = (statusEl: HTMLElement, message?: string | null) => void;
let _needsLogin: NeedsLoginHandler = (statusEl, message) => {
  statusEl.classList.remove("err");
  statusEl.textContent = message || "Not logged in.";
};
export function setNeedsLoginHandler(fn: NeedsLoginHandler) {
  _needsLogin = fn;
}

// `label` is optional: pass it for LLM jobs that should surface in the global
// pill (analysis, Q&A, deep research); omit it for non-LLM jobs (e.g. login).
export async function pollDeepJob(
  jobId: string,
  statusEl: HTMLElement,
  onDone: (job: Job) => unknown,
  label?: string,
  onFail?: (msg: string) => void,
) {
  // The global Task Center poller (tasks.ts) owns the pill/panel now; just nudge
  // it so this freshly-started job shows up promptly. This loop keeps driving the
  // per-view status line + onDone refresh for whoever started the job.
  kickTaskPoll();
  try {
    for (;;) {
      await new Promise((r) => setTimeout(r, 4000));
      let job;
      try {
        job = await api("/api/deep-job?id=" + encodeURIComponent(jobId));
      } catch (e) {
        statusEl.classList.add("err");
        const msg = "lost the job: " + e.message;
        statusEl.textContent = msg;
        if (onFail) onFail(msg);
        return;
      }
      if (job.state === "queued" || job.state === "running") {
        statusEl.classList.remove("err");
        const live = job.source_url
          ? ` <a href="${esc(job.source_url)}" target="_blank" rel="noopener" class="live-run-link">view live run \u2197</a>`
          : "";
        statusEl.innerHTML = `<span class="spinner"></span> ${esc(job.message || job.state)}${live}`;
        continue;
      }
      if (job.state === "done") {
        statusEl.classList.remove("err");
        await onDone(job);
        return;
      }
      if (job.state === "cancelled") {
        // Clean stop on user request -- not an error.
        statusEl.classList.remove("err");
        statusEl.textContent = "";
        return;
      }
      if (job.state === "needs_login") {
        // The run proved the cached login flag was stale; hand off to the
        // pipeline's recovery UI (registered via setNeedsLoginHandler).
        _needsLogin(statusEl, job.message || job.error);
        return;
      }
      statusEl.classList.add("err");
      const jobErr = job.error || job.message || job.state;
      statusEl.innerHTML = linkifyHtml(jobErr);
      recordError("task", `${label || "Background task"} failed: ${jobErr}`);
      if (onFail) onFail(jobErr);
      return;
    }
  } finally {
    // Refresh the Task Center so the panel/pill reflect the terminal state fast.
    kickTaskPoll();
  }
}
