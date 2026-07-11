import { api, el, esc, relAge } from "./core";
import { navFromUrl, pushNav, restoreNav } from "./shell";
import type { JobListing, JobsResponse } from "./api-types";

// ---- Central Task Center ---------------------------------------------------
// Long-running work (Deep Research, ticker analysis, Q&A, IBKR syncs, guided
// strategy runs) all live in the server's in-memory job registry. This module
// is the single client-side surface for them: a global poller reads GET
// /api/jobs, a floating pill shows live progress, and a slide-over panel lists
// every in-progress and recently-finished task with a click-through to its
// result. Because it polls the server (not per-action callbacks), a task stays
// visible when you navigate away and come back -- and reappears after a page
// reload, as long as the server is still running (the registry is in-memory and
// resets on restart, which the panel says out loud).

const ACTIVE_STATES = new Set(["queued", "running", "needs_login"]);
// Kinds whose runner actually honors cooperative cancellation (jobs.is_cancelled
// -> kills the subprocess). Others ignore the flag, so we don't offer a button
// that would silently do nothing.
const CANCELLABLE = new Set(["ticker_qa", "deep_qa", "segment_draft"]);

const FAST_POLL_MS = 3500;   // a task is active or the panel is open
const IDLE_POLL_MS = 20000;  // nothing happening -- just catch jobs started elsewhere

let jobsById = new Map<string, JobListing>();
let panelOpen = false;
let pollScheduled: ReturnType<typeof setTimeout> | null = null;
let started = false;

// ---- per-kind labels + result routing --------------------------------------

const KIND_LABELS: Record<string, string> = {
  deep_research: "Deep research",
  import: "Import run",
  login: "Perplexity login",
  ticker_analysis: "Analysis",
  ticker_qa: "Q&A",
  deep_qa: "Report Q&A",
  segment_draft: "Segment draft",
  ibkr_sync: "IBKR sync",
  ibkr_history: "IBKR history",
  ibkr_sectors: "Sector lookup",
  strategy: "Strategy run",
  portfolio_review: "Portfolio review",
};

function kindLabel(kind: string): string {
  return KIND_LABELS[kind] || kind;
}

// A short, human title for a task row / pill: "<kind> · <subject>".
function taskTitle(job: JobListing): string {
  const subject =
    job.symbol ||
    (job.artifact && job.artifact.stem) ||
    job.stem ||
    (job.result && (job.result as Record<string, unknown>).slug as string) ||
    job.segment ||
    job.run_id ||
    "";
  const base = kindLabel(job.kind);
  return subject ? `${base} \u00b7 ${subject}` : base;
}

interface NavTarget { view: string; ticker?: string; segment?: string; run?: string; }

// Map a job to the deep-link of the result it produced. Returns null for jobs
// with no navigable result (e.g. a bare login). Mirrors the URL param scheme in
// shell.ts (?view=&ticker=&segment=&run=).
function navForTask(job: JobListing): NavTarget | null {
  const stem = (job.artifact && job.artifact.stem) || job.stem || "";
  const slug = (job.result && (job.result as Record<string, unknown>).slug as string) || job.segment || "";
  switch (job.kind) {
    case "ticker_analysis":
    case "ticker_qa":
      return job.symbol ? { view: "deepdive", ticker: job.symbol } : null;
    case "deep_research":
    case "import":
      if (stem) return { view: "analyses", run: stem };
      return job.segment ? { view: "pipeline", segment: job.segment } : null;
    case "deep_qa":
      return stem ? { view: "analyses", run: stem } : null;
    case "segment_draft":
      return slug ? { view: "pipeline", segment: slug } : null;
    case "ibkr_sync":
      return { view: "holdings" };
    case "ibkr_history":
    case "ibkr_sectors":
      return { view: "history" };
    case "strategy":
      return job.run_id ? { view: "strategy", run: job.run_id } : null;
    case "portfolio_review":
      return { view: "optimizer" };
    default:
      return null;
  }
}

// ---- store / poll ----------------------------------------------------------

function jobsList(): JobListing[] {
  // Server already returns newest-first; keep that order.
  return [...jobsById.values()];
}

function activeJobs(): JobListing[] {
  return jobsList().filter((j) => ACTIVE_STATES.has(j.state));
}

// A guided strategy run spawns a child deep-research job that does the actual
// work; both land in the registry with the same slug and mirrored progress, so
// listing both reads as a duplicate. Fold the child into its parent: the
// strategy card carries the mirrored message + the live-run URL. We keep a
// child that's stuck at needs_login visible, so its login prompt isn't hidden.
function foldChildJobs(jobs: JobListing[]): JobListing[] {
  const strategyRuns = new Set(
    jobs.filter((j) => j.kind === "strategy" && j.run_id).map((j) => j.run_id),
  );
  return jobs.filter(
    (j) =>
      !(
        j.kind === "deep_research" &&
        j.parent_run_id &&
        strategyRuns.has(j.parent_run_id) &&
        j.state !== "needs_login"
      ),
  );
}

async function pollOnce(): Promise<void> {
  try {
    const res = await api<JobsResponse>("/api/jobs");
    const next = new Map<string, JobListing>();
    foldChildJobs(res.jobs || []).forEach((j) => next.set(j.id, j));
    jobsById = next;
  } catch (_e) {
    // The server is down/unreachable; api() already records it centrally. Keep
    // the last known list so the panel doesn't flicker empty on a transient blip.
  }
  renderPill();
  if (panelOpen) renderPanel();
}

function scheduleNext(): void {
  if (pollScheduled) clearTimeout(pollScheduled);
  const delay = activeJobs().length || panelOpen ? FAST_POLL_MS : IDLE_POLL_MS;
  pollScheduled = setTimeout(loop, delay);
}

async function loop(): Promise<void> {
  await pollOnce();
  scheduleNext();
}

// Nudge the poller to refresh promptly (e.g. right after starting a job) so the
// pill/panel reflect new work without waiting out the idle interval.
function kickTaskPoll(): void {
  if (!started) return;
  if (pollScheduled) clearTimeout(pollScheduled);
  pollScheduled = setTimeout(loop, 250);
}

// ---- pill ------------------------------------------------------------------

let _pillEl: HTMLElement | null = null;

function ensureTaskPill(): HTMLElement {
  if (_pillEl) return _pillEl;
  _pillEl = el("div", "global-pill");
  _pillEl.hidden = true;
  _pillEl.setAttribute("role", "button");
  _pillEl.setAttribute("tabindex", "0");
  _pillEl.title = "Show tasks";
  _pillEl.addEventListener("click", () => toggleTaskPanel());
  _pillEl.addEventListener("keydown", (e) => {
    if ((e as KeyboardEvent).key === "Enter" || (e as KeyboardEvent).key === " ") {
      e.preventDefault();
      toggleTaskPanel();
    }
  });
  document.body.appendChild(_pillEl);
  return _pillEl;
}

function renderPill(): void {
  const pill = ensureTaskPill();
  const active = activeJobs();
  if (!active.length) {
    pill.hidden = true;
    pill.innerHTML = "";
    syncTaskIndicator(0);
    return;
  }
  const latest = active[0]; // newest-first
  const text = latest.message ? `${taskTitle(latest)} \u2014 ${latest.message}` : taskTitle(latest);
  const more = active.length > 1 ? `<span class="global-pill-count">+${active.length - 1}</span>` : "";
  pill.hidden = false;
  pill.innerHTML = `<span class="spinner"></span><span class="global-pill-text">${esc(text)}</span>${more}`;
  syncTaskIndicator(active.length);
}

// The always-visible header button: shows an active-count badge so the Task
// Center is reachable even when no pill is up (e.g. to review finished tasks).
function syncTaskIndicator(activeCount: number): void {
  const btn = document.getElementById("task-indicator");
  if (!btn) return;
  btn.innerHTML = activeCount
    ? `Tasks <span class="task-indicator-count">${activeCount}</span>`
    : "Tasks";
  btn.classList.toggle("busy", activeCount > 0);
}

// ---- panel -----------------------------------------------------------------

function toggleTaskPanel(force?: boolean): void {
  const panel = document.getElementById("task-panel");
  if (!panel) return;
  panelOpen = Boolean(force != null ? force : (panel as HTMLElement).hidden);
  (panel as HTMLElement).hidden = !panelOpen;
  const btn = document.getElementById("task-indicator");
  if (btn) btn.setAttribute("aria-expanded", panelOpen ? "true" : "false");
  if (panelOpen) {
    renderPanel();
    kickTaskPoll();
  }
}

function rowHtml(job: JobListing): string {
  const when = relAge(job.updated_at || job.created_at);
  const live = job.source_url && ACTIVE_STATES.has(job.state)
    ? ` <a href="${esc(job.source_url)}" target="_blank" rel="noopener" class="live-run-link">view live run \u2197</a>`
    : "";
  const detail = job.state === "error"
    ? esc(job.error || job.message || "failed")
    : esc(job.message || job.state);
  const navigable = !!navForTask(job);
  const cancelBtn = ACTIVE_STATES.has(job.state) && CANCELLABLE.has(job.kind) && !job.cancelled
    ? `<button class="task-cancel" data-cancel="${esc(job.id)}" type="button">Cancel</button>`
    : "";
  return (
    `<div class="task-item task-${esc(job.state)}${navigable ? " navigable" : ""}" data-open="${esc(job.id)}">` +
      `<div class="task-item-head">` +
        `<span class="task-kind">${esc(kindLabel(job.kind))}</span>` +
        `<span class="task-state task-state-${esc(job.state)}">${esc(job.state)}</span>` +
        (when ? `<span class="task-age">${esc(when)}</span>` : "") +
      `</div>` +
      `<div class="task-subject">${esc(taskTitle(job))}</div>` +
      `<div class="task-detail">${detail}${live}</div>` +
      (cancelBtn ? `<div class="task-actions">${cancelBtn}</div>` : "") +
    `</div>`
  );
}

function renderPanel(): void {
  const list = document.getElementById("task-list");
  if (!list) return;
  const all = jobsList();
  const active = all.filter((j) => ACTIVE_STATES.has(j.state));
  const section = (heading: string, rows: JobListing[]) =>
    rows.length
      ? `<div class="task-group-head">${esc(heading)}</div>` + rows.map(rowHtml).join("")
      : "";
  const history = `<div class="task-history-link">Completed work is kept in the durable Activity log. ` +
    `<button type="button" class="linklike" data-task-activity>View activity →</button></div>`;
  list.innerHTML = active.length
    ? section("In progress", active) + history
    : `<div class="task-empty">No tasks running.</div>${history}`;
}

// Delegated handlers (wired once): open a task's result, or cancel it.
function wirePanelEvents(): void {
  const list = document.getElementById("task-list");
  if (!list) return;
  list.addEventListener("click", async (e) => {
    const target = e.target as HTMLElement;
    if (target.closest("[data-task-activity]")) {
      toggleTaskPanel(false);
      pushNav({ view: "activity" });
      restoreNav(navFromUrl());
      return;
    }
    const cancelId = target.closest<HTMLElement>("[data-cancel]")?.dataset.cancel;
    if (cancelId) {
      e.stopPropagation();
      try { await api("/api/deep-job/cancel", "POST", { id: cancelId }); } catch (_e) { /* recorded centrally */ }
      kickTaskPoll();
      return;
    }
    const openId = target.closest<HTMLElement>("[data-open]")?.dataset.open;
    if (!openId) return;
    const job = jobsById.get(openId);
    if (!job) return;
    const nav = navForTask(job);
    if (!nav) return;
    toggleTaskPanel(false);
    pushNav(nav);
    restoreNav(navFromUrl());
  });
}

// ---- boot ------------------------------------------------------------------

// Start the global poller and wire the panel chrome. Called once from main()'s
// boot, after the DOM exists (the header button + panel are in index.html).
function startTaskCenter(): void {
  if (started) return;
  started = true;
  const btn = document.getElementById("task-indicator");
  if (btn) btn.addEventListener("click", () => toggleTaskPanel());
  const close = document.getElementById("task-close");
  if (close) close.addEventListener("click", () => toggleTaskPanel(false));
  ensureTaskPill();
  wirePanelEvents();
  loop();
}

export {
  startTaskCenter,
  kickTaskPoll,
  toggleTaskPanel,
  navForTask,
  taskTitle,
  kindLabel,
};
