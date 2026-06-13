import { el, esc } from "./core";

// ---- Global "task running" pill --------------------------------------------
// A page-level affordance so a long LLM job (analysis, Q&A, deep research) is
// visible even when the originating card is scrolled off screen. Driven by the
// shared pollDeepJob loop; multiple concurrent jobs collapse into one pill with
// a "+N" badge for the rest.
interface TaskInfo { label: string; message: string; }
const activeTasks = new Map<string, TaskInfo>(); // jobId -> { label, message }
let _pillEl: HTMLElement | null = null;

function ensureTaskPill(): HTMLElement {
  if (_pillEl) return _pillEl;
  _pillEl = el("div", "global-pill");
  _pillEl.hidden = true;
  document.body.appendChild(_pillEl);
  return _pillEl;
}

function renderTaskPill() {
  const pill = ensureTaskPill();
  const tasks = [...activeTasks.values()];
  if (!tasks.length) {
    pill.hidden = true;
    pill.innerHTML = "";
    return;
  }
  const t = tasks[tasks.length - 1]; // surface the most recently started
  const label = t.message ? `${t.label} \u2014 ${t.message}` : t.label;
  const more = tasks.length > 1 ? `<span class="global-pill-count">+${tasks.length - 1}</span>` : "";
  pill.hidden = false;
  pill.innerHTML =
    `<span class="spinner"></span><span class="global-pill-text">${esc(label)}</span>${more}`;
}

function taskStart(id: string, label: string) { activeTasks.set(id, { label, message: "" }); renderTaskPill(); }
function taskUpdate(id: string, message: string) {
  const t = activeTasks.get(id);
  if (t) { t.message = message || ""; renderTaskPill(); }
}
function taskEnd(id: string) { activeTasks.delete(id); renderTaskPill(); }

export {
  activeTasks,
  _pillEl,
  ensureTaskPill,
  renderTaskPill,
  taskStart,
  taskUpdate,
  taskEnd,
};
