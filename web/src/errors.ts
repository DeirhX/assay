import { $, el, esc, relAge, setErrorSink } from "./core";

// ---- Centralized error center ----------------------------------------------
// Counterpart to the task pill: failures collect here instead of dying in a
// scrolled-off card. A topbar badge shows the count; clicking opens a panel
// listing each error with source, time, and a dismiss action. Sources funnel
// in from api() (network/5xx), failed background jobs, and the global
// unhandledrejection/error handlers.
//
// This module is a near-leaf (imports only core): the deep-research pipeline
// runners and the shared job poller that used to live here — and forced an
// errors↔pipeline import cycle — now live in pipeline.ts and jobs.ts. Job
// failures still land here via recordError, which jobs.ts imports.
// One collapsed failure in the error center; `count` bumps on rapid duplicates.
interface ErrorEntry {
  id: string;
  source: string;
  message: string;
  detail: string;
  time: number;
  count: number;
}

const errorLog: ErrorEntry[] = []; // newest last
const ERROR_LOG_MAX = 50;
let _errorSeq = 0;

function recordError(source: string, message: string, opts: { detail?: string } = {}) {
  const msg = String(message || "unknown error").trim();
  if (!msg) return null;
  const now = Date.now();
  // Collapse rapid duplicates (same source+message within 5s) into one entry
  // with a bumped count, so a flapping poll loop can't bury the panel.
  const dup = errorLog.find((e) => e.source === source && e.message === msg && now - e.time < 5000);
  if (dup) {
    dup.count += 1;
    dup.time = now;
    if (opts.detail) dup.detail = String(opts.detail);
    renderErrorCenter();
    return dup.id;
  }
  const id = "err-" + (++_errorSeq);
  errorLog.push({ id, source, message: msg, detail: opts.detail ? String(opts.detail) : "", time: now, count: 1 });
  while (errorLog.length > ERROR_LOG_MAX) errorLog.shift();
  renderErrorCenter();
  return id;
}

// Wire core.api()'s failures into the error center without core importing us
// (keeps core a dependency-free leaf -- see core.ts).
setErrorSink(recordError);

function dismissError(id: string) {
  const i = errorLog.findIndex((e) => e.id === id);
  if (i >= 0) errorLog.splice(i, 1);
  renderErrorCenter();
}

function clearErrors() {
  errorLog.length = 0;
  renderErrorCenter();
}

function toggleErrorPanel(force?: boolean) {
  const panel = $("#error-panel");
  if (!panel) return;
  const show = force != null ? force : panel.hidden;
  panel.hidden = !show;
  const btn = $("#error-indicator");
  if (btn) btn.setAttribute("aria-expanded", show ? "true" : "false");
  if (show) renderErrorCenter();
}

const ERROR_SOURCE_LABEL: Record<string, string> = { api: "Server", network: "Network", task: "Task", js: "App" };

function renderErrorCenter() {
  const btn = $("#error-indicator");
  if (btn) {
    const n = errorLog.length;
    btn.hidden = n === 0;
    btn.textContent = n === 0 ? "" : `Errors ${n}`;
    if (n === 0) toggleErrorPanel(false);
  }
  const list = $("#error-list");
  if (!list) return;
  if (!errorLog.length) {
    list.innerHTML = `<div class="error-empty">No errors. Carry on.</div>`;
    return;
  }
  list.innerHTML = "";
  // Newest first.
  [...errorLog].reverse().forEach((e) => {
    const row = el("div", "error-item");
    const src = ERROR_SOURCE_LABEL[e.source] || e.source;
    const times = e.count > 1 ? ` <span class="error-x">x${e.count}</span>` : "";
    row.innerHTML =
      `<div class="error-meta"><span class="error-src">${esc(src)}</span>` +
      `<span class="error-time">${esc(relAge(new Date(e.time).toISOString())) || "just now"}</span>${times}</div>` +
      `<div class="error-msg">${esc(e.message)}</div>` +
      (e.detail ? `<div class="error-detail">${esc(e.detail)}</div>` : "");
    const x = el("button", "error-dismiss", "&times;");
    x.title = "Dismiss";
    x.addEventListener("click", () => dismissError(e.id));
    row.appendChild(x);
    list.appendChild(row);
  });
}

export {
  errorLog,
  ERROR_LOG_MAX,
  _errorSeq,
  recordError,
  dismissError,
  clearErrors,
  toggleErrorPanel,
  ERROR_SOURCE_LABEL,
  renderErrorCenter,
};
