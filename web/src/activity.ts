import { $$, api, el, esc, relAge } from "./core";
import { navFromUrl, pushNav, replaceViewState, restoreNav } from "./shell";
import { navForTask, taskTitle } from "./tasks";
import { openTicker } from "./ticker-nav";
import { asJob, dayLabel } from "./activity-util";
import type { ActivityEvent, ActivityResponse } from "./api-types";

// ---- Activity view ---------------------------------------------------------
// A durable, newest-first feed of what happened: tickers opened + background
// tasks finished. Backed by GET /api/activity (a JSONL log on disk), so unlike
// the in-memory Task Center this survives a server restart and crosses devices.
// Each row deep-links back to what it produced.

type ActFilter = "all" | "view" | "task";
let _filter: ActFilter = "all";
let _events: ActivityEvent[] = [];

const STATE_TONE: Record<string, string> = {
  done: "ok", error: "bad", cancelled: "muted", needs_login: "warn",
};

function rowFor(ev: ActivityEvent): HTMLElement | null {
  const when = relAge(ev.ts);
  if (ev.type === "view") {
    const sym = (ev.symbol || "").toUpperCase();
    if (!sym) return null;
    const row = el("button", "act-row act-view navigable");
    row.type = "button";
    row.innerHTML =
      `<span class="act-icon" title="Ticker viewed">\u25CE</span>` +
      `<span class="act-title"><span class="act-sym">${esc(sym)}</span>` +
      (ev.name ? `<span class="act-name">${esc(ev.name)}</span>` : "") + `</span>` +
      `<span class="act-kindtag muted">viewed</span>` +
      `<span class="act-when muted">${esc(when)}</span>`;
    row.addEventListener("click", () => openTicker(sym));
    return row;
  }
  // task
  const job = asJob(ev);
  const nav = navForTask(job);
  const tone = STATE_TONE[ev.state || ""] || "muted";
  const detail = ev.state === "error" ? (ev.error || ev.message || "failed") : (ev.message || "");
  const row = el("button", "act-row act-task" + (nav ? " navigable" : ""));
  row.type = "button";
  row.disabled = !nav;
  row.innerHTML =
    `<span class="act-icon" title="Task">\u2699</span>` +
    `<span class="act-title">${esc(taskTitle(job))}` +
    (detail ? `<span class="act-detail muted">${esc(detail)}</span>` : "") + `</span>` +
    `<span class="act-state abadge ${tone}">${esc(ev.state || "")}</span>` +
    `<span class="act-when muted">${esc(when)}</span>`;
  if (nav) {
    row.addEventListener("click", () => {
      pushNav(nav);
      restoreNav(navFromUrl());
    });
  }
  return row;
}

function renderFilters(): void {
  const wrap = $$("#act-filters");
  if (!wrap) return;
  wrap.innerHTML = "";
  const counts = {
    all: _events.length,
    view: _events.filter((e) => e.type === "view").length,
    task: _events.filter((e) => e.type === "task").length,
  };
  ([["all", "All"], ["view", "Tickers"], ["task", "Tasks"]] as [ActFilter, string][]).forEach(([key, label]) => {
    const b = el("button", "chip tone-chip ui-segment-pill" + (_filter === key ? " active" : ""), `${label} ${counts[key]}`);
    b.type = "button";
    b.addEventListener("click", () => {
      _filter = key;
      replaceViewState({ filter: key === "all" ? "" : key });
      render();
    });
    wrap.appendChild(b);
  });
}

function render(): void {
  renderFilters();
  const out = $$("#act-result");
  out.innerHTML = "";
  const shown = _events.filter((e) => _filter === "all" || e.type === _filter);
  if (!shown.length) {
    out.appendChild(el("p", "hint", _events.length
      ? "Nothing matches this filter."
      : "No activity yet. Open a ticker or run a task and it'll show up here."));
    return;
  }
  const card = el("div", "card act-card");
  let lastDay = "";
  shown.forEach((ev) => {
    const day = dayLabel(ev.ts);
    if (day !== lastDay) {
      lastDay = day;
      card.appendChild(el("div", "act-day", day));
    }
    const row = rowFor(ev);
    if (row) card.appendChild(row);
  });
  out.appendChild(card);
}

export async function loadActivity(): Promise<void> {
  const requested = navFromUrl().filter;
  _filter = requested === "view" || requested === "task" ? requested : "all";
  const status = $$("#act-status");
  status.textContent = "Loading activity\u2026";
  try {
    const res = await api<ActivityResponse>("/api/activity");
    _events = res.events || [];
    status.textContent = "";
  } catch (_e) {
    status.textContent = "";
    _events = [];
  }
  render();
}
