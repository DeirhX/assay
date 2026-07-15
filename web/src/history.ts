import { $$, api, el, esc, fmtStamp, freshnessNote, sensitive, statTile } from "./core";
import { metaStrip, analyticsSection } from "./display/chrome";
import { runDeepJobAction } from "./deep-job-action";
import { groupActivity, groupBySector, type ActivityRow, type Trade } from "./history/data";
import { ccyTag, fmtMoney, fmtSigned } from "./history/format";
import { legend, navChart, type NavPoint } from "./history/nav-chart";
import { activityTable, sectorTable, tradeTable } from "./history/tables";

// ---- portfolio history -----------------------------------------------------
// Finalized history comes from read-only Flex; the authenticated Client Portal
// session appends executions newer than Flex's last statement. Live rows are
// provisional until the next Flex sync supplies authoritative cash flow/P&L.

// Cheap credential probe (/api/ibkr/status) used to guide setup before a pull.
interface IbkrStatus {
  history_configured?: boolean;
  token_set?: boolean;
  [key: string]: unknown;
}

interface HistorySummary {
  n_trades?: number;
  n_nav_points?: number;
  windows?: number;
  by_symbol?: ActivityRow[];
  realized_pnl_total?: number | null;
}

// /api/portfolio-history: the reconstructed ledger + daily NAV series.
interface HistoryPayload {
  summary?: HistorySummary;
  account?: string | null;
  from_date?: string | null;
  to_date?: string | null;
  base_currency?: string | null;
  generated_at?: string | null;
  sectors_updated_at?: string | null;
  nav_series?: NavPoint[];
  trades?: Trade[];
  history_sources?: {
    flex_to_date?: string | null;
    live_as_of?: string | null;
    live_trade_count?: number;
    live_available?: boolean;
    live_error?: string | null;
  };
}

type HistoryDetailView = "sector" | "name" | "trades";
export interface HistoryDetailPanel {
  id: HistoryDetailView;
  label: string;
  count: number;
  body: HTMLElement;
  hint: string;
}

const HISTORY_DETAIL_VIEW_KEY = "assay.history.detailView";
let _wired = false;

// Cheap, token-free credential check so we can guide setup *before* a pull fails.
async function fetchIbkrStatus(): Promise<IbkrStatus | null> {
  try {
    return await api<IbkrStatus>("/api/ibkr/status");
  } catch {
    return null; // a missing status endpoint must never block showing the cache
  }
}

// A banner shown when the dedicated history Flex query isn't configured yet.
// History needs its own Activity query (Trades + NAV in Base); the positions
// query the Holdings tab uses lacks those sections, so a pull would just error.
function setupNote(st: IbkrStatus | null) {
  if (!st || st.history_configured) return null;
  const box = el("div", "hist-note");
  const link =
    `<button type="button" class="linklike" data-go-setup>Settings</button>`;
  box.innerHTML = !st.token_set
    ? `<strong>IBKR not connected.</strong> Save your read-only Flex token and a ` +
      `history query in ${link} before pulling history.`
    : `<strong>No history Flex query set.</strong> The full trade &amp; NAV history needs a ` +
      `dedicated <em>Activity</em> Flex query with the <strong>Trades</strong> and ` +
      `<strong>Net Asset Value (NAV) in Base</strong> sections — the positions query won't work. ` +
      `Add a <strong>History Flex Query ID</strong> in ${link}, then pull.`;
  const go = box.querySelector("[data-go-setup]");
  if (go) go.addEventListener("click", () => {
    document.querySelector<HTMLElement>('.tab[data-view="setup"]')?.click();
  });
  return box;
}

async function loadHistory() {
  const status = $$("#hist-status");
  const out = $$("#hist-result");
  status.classList.remove("err");
  status.textContent = "Loading cached history…";
  out.innerHTML = "";
  const readyP = fetchIbkrStatus(); // in parallel; tolerant of failure
  try {
    const h = await api<HistoryPayload>("/api/portfolio-history");
    status.textContent = "";
    renderHistory(h);
    const note = setupNote(await readyP);
    if (note) out.insertBefore(note, out.firstChild);
  } catch (e) {
    out.innerHTML = "";
    if ((e as { status?: number }).status === 404) {
      // Expected on a fresh setup: nothing pulled yet. Nudge, don't alarm.
      status.textContent = "";
      out.appendChild(emptyState(await readyP));
      return;
    }
    status.textContent = "Could not load history: " + (e as Error).message;
    status.classList.add("err");
  }
}

function emptyState(st: IbkrStatus | null) {
  const box = el("div", "hist-empty");
  const note = setupNote(st);
  if (note) box.appendChild(note);
  const body = el("div");
  body.innerHTML =
    `<p>No portfolio history cached yet.</p>` +
    `<p class="hint">Click <strong>Update from IBKR</strong> above. The first run walks your account ` +
    `back to inception via read-only Flex (one ≤365-day window at a time), so it can take a minute. ` +
    `After that, <strong>Update</strong> only fetches the days since the last pull.</p>`;
  box.appendChild(body);
  return box;
}

function renderHistory(h: HistoryPayload) {
  const out = $$("#hist-result");
  out.innerHTML = "";
  const s = h.summary || {};
  const sources = h.history_sources;
  const liveCount = Number(sources?.live_trade_count) || 0;

  out.appendChild(metaStrip([
    `account ${sensitive(esc(h.account || "n/a"), "account id")}`,
    `${esc(h.from_date || "?")} → ${esc(h.to_date || "?")}`,
    `${esc(s.n_trades ?? 0)} trades · ${esc(s.n_nav_points ?? 0)} NAV points`,
    `${esc(s.windows ?? 0)} Flex window(s)`,
    ...(sources?.live_available
      ? [`${liveCount} provisional live execution${liveCount === 1 ? "" : "s"}`]
      : []),
    ...(h.base_currency ? [`base ${esc(h.base_currency)}`] : []),
    `pulled ${freshnessNote(h.generated_at) || esc(fmtStamp(h.generated_at))}`,
  ]));

  out.appendChild(statCards(h));

  const series = (h.nav_series || []).filter((p) => p && p.date && p.nav != null);
  const chartSec = analyticsSection("Portfolio value & actions");
  if (series.length >= 2) {
    chartSec.appendChild(el("p", "hint",
      "Net asset value over time. Each marker is a day you traded (bigger = more fills); " +
      "green = buys only, red = sells only, amber = both. Hover a marker to see what traded that day. " +
      "Scroll to zoom the timeline, drag to pan, double-click to reset."));
    chartSec.appendChild(navChart(series, h.trades || [], h.base_currency || ""));
    chartSec.appendChild(legend());
  } else {
    chartSec.appendChild(el("p", "hint err",
      "Not enough NAV points to chart. Add the “Net Asset Value (NAV) in Base” section to the Flex query (it emits the daily NAV; “Change in NAV” is a different, period-summary section), then re-pull."));
  }
  out.appendChild(chartSec);

  const detailPanels: HistoryDetailPanel[] = [];
  if (s.by_symbol && s.by_symbol.length) {
    const bcy = h.base_currency || "";
    const secGroups = groupBySector(s.by_symbol);
    const known = secGroups.filter((g) => g.sector !== "Unknown").length;
    const unknown = secGroups.find((g) => g.sector === "Unknown");
    const body = el("div");
    body.appendChild(sectorToolbar(h, unknown ? (unknown.names ?? 0) : 0));
    body.appendChild(sectorTable(secGroups, bcy));
    detailPanels.push({
      id: "sector",
      label: "By sector",
      count: known,
      body,
      hint: `Sectors come from research dossiers + Yahoo; names we couldn't resolve sit in "Unknown". ` +
      `Click a ${"\u25B8"} sector to expand its names. Cash flow & P&L are base${bcy ? " " + bcy : ""}.`,
    });
  }

  if (s.by_symbol && s.by_symbol.length) {
    const groups = groupActivity(s.by_symbol);
    const optGroups = groups.filter((g) => g.is_option).length;
    const bcy = h.base_currency || "";
    const foldHint = optGroups
      ? `Option contracts are folded under their underlying — click a ${"\u25B8"} row to expand its contracts. `
      : "";
    detailPanels.push({
      id: "name",
      label: "By name",
      count: groups.length,
      body: activityTable(groups, bcy),
      hint: foldHint + `Ccy is each name's trading currency; cash flow & P&L are in base${bcy ? " " + bcy : ""} so they sum across names.`,
    });
  }

  if (h.trades && h.trades.length) {
    const bcy = h.base_currency || "";
    detailPanels.push({
      id: "trades",
      label: "Trade history",
      count: h.trades.length,
      body: tradeTable(h.trades, bcy),
      hint: `Newest first. Price is in each trade's native currency; cash flow is base${bcy ? " " + bcy : ""} ` +
      `(negative for buys, positive for sells).`,
    });
  }
  if (detailPanels.length) out.appendChild(createHistoryDetailSwitcher(detailPanels));
}

export function createHistoryDetailSwitcher(panels: HistoryDetailPanel[]): HTMLElement {
  const available = panels.filter((panel) => panel.body);
  const workspace = el("section", "hist-detail-workspace");
  workspace.setAttribute("aria-label", "Portfolio history details");
  const tabs = el("div", "hist-detail-tabs");
  tabs.setAttribute("role", "tablist");
  tabs.setAttribute("aria-label", "Group portfolio history by");
  const panelWrap = el("div", "hist-detail-panels");
  workspace.appendChild(tabs);
  workspace.appendChild(panelWrap);

  const buttons = new Map<HistoryDetailView, HTMLButtonElement>();
  const panelNodes = new Map<HistoryDetailView, HTMLElement>();
  available.forEach((panel) => {
    const tab = el("button", "hist-detail-tab") as HTMLButtonElement;
    const tabId = `hist-detail-tab-${panel.id}`;
    const panelId = `hist-detail-panel-${panel.id}`;
    tab.type = "button";
    tab.id = tabId;
    tab.dataset.historyDetail = panel.id;
    tab.setAttribute("role", "tab");
    tab.setAttribute("aria-controls", panelId);
    tab.innerHTML = `<span>${esc(panel.label)}</span>` +
      `<span class="hist-detail-count">${esc(panel.count)}</span>`;
    tabs.appendChild(tab);
    buttons.set(panel.id, tab);

    const content = el("div", "hist-detail-panel risk-section");
    content.id = panelId;
    content.dataset.historyPanel = panel.id;
    content.setAttribute("role", "tabpanel");
    content.setAttribute("aria-labelledby", tabId);
    content.appendChild(el("p", "hint hist-detail-hint", panel.hint));
    content.appendChild(panel.body);
    panelWrap.appendChild(content);
    panelNodes.set(panel.id, content);
  });

  const ids = available.map((panel) => panel.id);
  let stored = "";
  try {
    stored = localStorage.getItem(HISTORY_DETAIL_VIEW_KEY) || "";
  } catch {
    // Storage can be unavailable in hardened/private browser contexts.
  }
  const initial = ids.includes(stored as HistoryDetailView)
    ? stored as HistoryDetailView
    : ids[0];

  const activate = (id: HistoryDetailView, persist = true): void => {
    if (!buttons.has(id)) return;
    buttons.forEach((button, candidate) => {
      const active = candidate === id;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", String(active));
      button.tabIndex = active ? 0 : -1;
    });
    panelNodes.forEach((panel, candidate) => {
      panel.hidden = candidate !== id;
    });
    if (persist) {
      try {
        localStorage.setItem(HISTORY_DETAIL_VIEW_KEY, id);
      } catch {
        // The visible switch still works when persistence is blocked.
      }
    }
  };

  buttons.forEach((button, id) => {
    button.addEventListener("click", () => activate(id));
    button.addEventListener("keydown", (event) => {
      const current = ids.indexOf(id);
      let next: number;
      if (event.key === "ArrowRight") next = (current + 1) % ids.length;
      else if (event.key === "ArrowLeft") next = (current - 1 + ids.length) % ids.length;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = ids.length - 1;
      else return;
      event.preventDefault();
      const nextId = ids[next];
      activate(nextId);
      buttons.get(nextId)?.focus();
    });
  });
  if (initial) activate(initial, false);
  return workspace;
}

// Toolbar above the sector table: how fresh the sector map is + a button to
// resolve the still-unknown names from Yahoo (a background job).
function sectorToolbar(h: HistoryPayload, unknownNames: number) {
  const bar = el("div", "hist-sectools");
  const fresh = h.sectors_updated_at
    ? `sectors ${freshnessNote(h.sectors_updated_at) || esc(fmtStamp(h.sectors_updated_at))}`
    : "sectors not fetched yet";
  bar.innerHTML = `<span class="hint">${fresh}` +
    (unknownNames ? ` · ${unknownNames} name(s) Unknown` : "") + `</span>`;
  const btn = el("button", "linklike", "Fetch sectors");
  btn.id = "hist-sectors";
  const status = el("span", "hint");
  status.id = "hist-sectors-status";
  bar.appendChild(btn);
  bar.appendChild(status);
  btn.addEventListener("click", () => runSectorFetch());
  return bar;
}

async function runSectorFetch() {
  const btn = $$<HTMLButtonElement>("#hist-sectors");
  const status = $$("#hist-sectors-status");
  if (!btn) return;
  await runDeepJobAction({
    buttons: [btn],
    status,
    pendingStatusHtml: `<span class="spinner"></span> resolving sectors from Yahoo…`,
    activeButton: btn,
    activeLabel: "Resolving…",
    startJob: () => api("/api/portfolio-history/sectors", "POST", {}),
    jobLabel: "sector lookup",
    failPrefix: "Sector lookup failed: ",
    onDone: async (done) => {
      await loadHistory();
      const r = (done.result || {}) as Record<string, unknown>;
      status.textContent = `Done — resolved ${r.resolved ?? 0} new, ${r.unresolved ?? 0} still unknown.`;
    },
  });
}

function statCards(h: HistoryPayload) {
  const s = h.summary || {};
  const bcy = h.base_currency || "";
  const series = h.nav_series || [];
  const latest = series.length ? series[series.length - 1].nav : null;
  const first = series.length ? series[0].nav : null;
  const change = latest != null && first != null ? Number(latest) - Number(first) : null;
  const wrap = el("div", "risk-stats");
  // NAV, change and the realized total are all base-currency figures.
  wrap.appendChild(statTile("Latest NAV",
    sensitive(fmtMoney(latest), "net asset value") + ccyTag(bcy),
    { family: "risk-stat", html: true, cls: "muted" }));
  wrap.appendChild(statTile("Change over span",
    change == null ? "n/a" : sensitive(fmtSigned(change), "nav change") + ccyTag(bcy),
    { family: "risk-stat", html: true, cls: change == null ? "muted" : change >= 0 ? "good" : "bad" }));
  wrap.appendChild(statTile(`Realized P&L${bcy ? " (base)" : ""}`,
    s.realized_pnl_total == null ? "n/a" : sensitive(fmtSigned(s.realized_pnl_total), "realized pnl") + ccyTag(bcy),
    { family: "risk-stat", html: true, cls: s.realized_pnl_total == null ? "muted" : s.realized_pnl_total >= 0 ? "good" : "bad" }));
  wrap.appendChild(statTile("Trades", String(s.n_trades ?? 0), { family: "risk-stat", cls: "muted" }));
  return wrap;
}

async function runSync(full: boolean) {
  const status = $$("#hist-status");
  const syncBtn = $$<HTMLButtonElement>("#hist-sync");
  const fullBtn = $$<HTMLButtonElement>("#hist-full");
  const btns = [syncBtn, fullBtn].filter(Boolean) as HTMLButtonElement[];
  const active = full ? fullBtn : syncBtn;
  await runDeepJobAction({
    buttons: btns,
    status,
    pendingStatusHtml: full
      ? `<span class="spinner"></span> Rebuilding full history from IBKR (read-only, can take a minute)…`
      : `<span class="spinner"></span> Fetching new days from IBKR (read-only)…`,
    activeButton: active,
    activeLabel: full ? "Rebuilding…" : "Updating…",
    startJob: () => api("/api/portfolio-history/sync", "POST", { full }),
    jobLabel: "IBKR history",
    failPrefix: "History pull failed: ",
    onDone: async (done) => {
      await loadHistory();
      const r = ((done.result as Record<string, unknown>)?.summary || {}) as Record<string, unknown>;
      const u = r.update as {
        new_trades?: number;
        new_nav_points?: number;
        live_trades?: number;
      } | undefined;
      status.textContent = u
        ? `Done — +${u.new_trades} finalized trades, ${u.live_trades ?? 0} provisional live executions, ` +
          `+${u.new_nav_points} NAV points since the last pull.`
        : `Done — ${r.n_trades ?? 0} trades reconstructed.`;
    },
  });
}

function initHistoryControls() {
  if (_wired) return;
  const sync = $$("#hist-sync");
  const full = $$("#hist-full");
  if (!sync && !full) return;
  _wired = true;
  if (sync) sync.addEventListener("click", () => runSync(false));
  if (full) full.addEventListener("click", () => runSync(true));
}

export { loadHistory, renderHistory, initHistoryControls };
// Re-exported for the data-shaping unit tests (tests/history.test.ts), which
// import these from "./history" — kept stable through the module split.
export { contractLabel, dayGroups, groupActivity, groupBySector, paginate } from "./history/data";
