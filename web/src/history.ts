import { $, api, el, esc, fmtStamp, freshnessNote, sensitive } from "./core";
import { pollDeepJob } from "./errors";
import { groupActivity, groupBySector, type ActivityRow, type Trade } from "./history/data";
import { ccyTag, fmtMoney, fmtSigned } from "./history/format";
import { legend, navChart, type NavPoint } from "./history/nav-chart";
import { activityTable, sectorTable, tradeTable } from "./history/tables";

// ---- portfolio history -----------------------------------------------------
// Reconstructed from read-only Flex: the full executed-trade ledger plus the
// day-by-day NAV series. The headline is one chart — portfolio value over time
// with every buy/sell marked — because that's the thing single snapshots can't
// show: the shape of the journey, not just where it ended.

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
}

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
  const status = $("#hist-status");
  const out = $("#hist-result");
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
    if (e.status === 404) {
      // Expected on a fresh setup: nothing pulled yet. Nudge, don't alarm.
      status.textContent = "";
      out.appendChild(emptyState(await readyP));
      return;
    }
    status.textContent = "Could not load history: " + e.message;
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
  const out = $("#hist-result");
  out.innerHTML = "";
  const s = h.summary || {};

  const meta = el("div", "reb-meta");
  meta.innerHTML =
    `<span>account ${sensitive(esc(h.account || "n/a"), "account id")}</span>` +
    `<span>${esc(h.from_date || "?")} → ${esc(h.to_date || "?")}</span>` +
    `<span>${esc(s.n_trades ?? 0)} trades · ${esc(s.n_nav_points ?? 0)} NAV points</span>` +
    `<span>${esc(s.windows ?? 0)} Flex window(s)</span>` +
    (h.base_currency ? `<span>base ${esc(h.base_currency)}</span>` : "") +
    `<span>pulled ${freshnessNote(h.generated_at) || esc(fmtStamp(h.generated_at))}</span>`;
  out.appendChild(meta);

  out.appendChild(statCards(h));

  const series = (h.nav_series || []).filter((p) => p && p.date && p.nav != null);
  const chartSec = el("div", "risk-section");
  chartSec.appendChild(el("h3", null, "Portfolio value & actions"));
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

  if (s.by_symbol && s.by_symbol.length) {
    const bcy = h.base_currency || "";
    const secGroups = groupBySector(s.by_symbol);
    const known = secGroups.filter((g) => g.sector !== "Unknown").length;
    const unknown = secGroups.find((g) => g.sector === "Unknown");
    const body = el("div");
    body.appendChild(sectorToolbar(h, unknown ? unknown.names : 0));
    body.appendChild(sectorTable(secGroups, bcy));
    out.appendChild(section(
      `By sector (${known})`,
      body,
      `Sectors come from research dossiers + Yahoo; names we couldn't resolve sit in "Unknown". ` +
      `Click a ${"\u25B8"} sector to expand its names. Cash flow & P&L are base${bcy ? " " + bcy : ""}.`,
      false,
    ));
  }

  if (s.by_symbol && s.by_symbol.length) {
    const groups = groupActivity(s.by_symbol);
    const optGroups = groups.filter((g) => g.is_option).length;
    const bcy = h.base_currency || "";
    const foldHint = optGroups
      ? `Option contracts are folded under their underlying — click a ${"\u25B8"} row to expand its contracts. `
      : "";
    out.appendChild(section(
      `Activity by name (${groups.length})`,
      activityTable(groups, bcy),
      foldHint + `Ccy is each name's trading currency; cash flow & P&L are in base${bcy ? " " + bcy : ""} so they sum across names.`,
    ));
  }

  if (h.trades && h.trades.length) {
    const bcy = h.base_currency || "";
    out.appendChild(section(
      `Trade ledger (${h.trades.length})`,
      tradeTable(h.trades, bcy),
      `Newest first. Price is in each trade's native currency; cash flow is base${bcy ? " " + bcy : ""} ` +
      `(negative for buys, positive for sells).`,
    ));
  }
}

// Collapsible section: the heading is a <summary> so a long page can be folded
// down to just the parts you care about. Open by default.
function section(title: string, bodyNode: HTMLElement, hint?: string, open = true) {
  const d = el("details", "risk-section hist-section");
  d.open = open;
  const sum = el("summary", "hist-section-sum");
  sum.innerHTML = `<span>${esc(title)}</span>`;
  d.appendChild(sum);
  if (hint) d.appendChild(el("p", "hint", hint));
  d.appendChild(bodyNode);
  return d;
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
  const btn = $<HTMLButtonElement>("#hist-sectors");
  const status = $("#hist-sectors-status");
  if (!btn || btn.disabled) return;
  btn.disabled = true;
  const prev = btn.textContent;
  btn.textContent = "Resolving…";
  status.classList.remove("err");
  status.innerHTML = `<span class="spinner"></span> resolving sectors from Yahoo…`;
  try {
    const job = await api("/api/portfolio-history/sectors", "POST", {});
    await pollDeepJob(job.id, status, async (done) => {
      await loadHistory();
      const r = (done.result || {}) as Record<string, any>;
      status.textContent = `Done — resolved ${r.resolved ?? 0} new, ${r.unresolved ?? 0} still unknown.`;
    }, "sector lookup");
  } catch (e) {
    status.textContent = "Sector lookup failed: " + e.message;
    status.classList.add("err");
  } finally {
    btn.disabled = false;
    btn.textContent = prev;
  }
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
  wrap.appendChild(card("Latest NAV", sensitive(fmtMoney(latest), "net asset value") + ccyTag(bcy)));
  wrap.appendChild(card("Change over span",
    change == null ? "n/a" : sensitive(fmtSigned(change), "nav change") + ccyTag(bcy),
    change == null ? "muted" : change >= 0 ? "good" : "bad"));
  wrap.appendChild(card(`Realized P&L${bcy ? " (base)" : ""}`,
    s.realized_pnl_total == null ? "n/a" : sensitive(fmtSigned(s.realized_pnl_total), "realized pnl") + ccyTag(bcy),
    s.realized_pnl_total == null ? "muted" : s.realized_pnl_total >= 0 ? "good" : "bad"));
  wrap.appendChild(card("Trades", String(s.n_trades ?? 0)));
  return wrap;
}

function card(label: string, valueHtml: string, cls = "muted") {
  const c = el("div", "risk-stat");
  c.innerHTML =
    `<span class="risk-stat-k">${esc(label)}</span>` +
    `<span class="risk-stat-v ${esc(cls)}">${valueHtml}</span>`;
  return c;
}

async function runSync(full: boolean) {
  const status = $("#hist-status");
  const btns = [$<HTMLButtonElement>("#hist-sync"), $<HTMLButtonElement>("#hist-full")].filter(Boolean);
  if (btns.some((b) => b.disabled)) return;
  const prev = btns.map((b) => b.textContent);
  btns.forEach((b) => (b.disabled = true));
  const active = full ? $("#hist-full") : $("#hist-sync");
  if (active) active.textContent = full ? "Rebuilding…" : "Updating…";
  status.classList.remove("err");
  status.innerHTML = full
    ? `<span class="spinner"></span> Rebuilding full history from IBKR (read-only, can take a minute)…`
    : `<span class="spinner"></span> Fetching new days from IBKR (read-only)…`;
  try {
    const job = await api("/api/portfolio-history/sync", "POST", { full });
    await pollDeepJob(job.id, status, async (done) => {
      await loadHistory();
      const r = ((done.result as Record<string, any>)?.summary || {}) as Record<string, any>;
      const u = r.update;
      status.textContent = u
        ? `Done — +${u.new_trades} trades, +${u.new_nav_points} NAV points since the last pull.`
        : `Done — ${r.n_trades ?? 0} trades reconstructed.`;
    }, "IBKR history");
  } catch (e) {
    status.textContent = "History pull failed: " + e.message;
    status.classList.add("err");
  } finally {
    btns.forEach((b, i) => {
      b.disabled = false;
      b.textContent = prev[i];
    });
  }
}

function initHistoryControls() {
  if (_wired) return;
  const sync = $("#hist-sync");
  const full = $("#hist-full");
  if (!sync && !full) return;
  _wired = true;
  if (sync) sync.addEventListener("click", () => runSync(false));
  if (full) full.addEventListener("click", () => runSync(true));
}

export { loadHistory, renderHistory, initHistoryControls };
// Re-exported for the data-shaping unit tests (tests/history.test.ts), which
// import these from "./history" — kept stable through the module split.
export { contractLabel, dayGroups, groupActivity, groupBySector, paginate } from "./history/data";
