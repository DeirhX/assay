import { $, api, el, esc, fmtStamp, freshnessNote, sensitive } from "./core";
import { pollDeepJob } from "./errors";
import { openTicker } from "./rebalance";
import { contractLabel, dayGroups, groupActivity, groupBySector, paginate, type ActivityGroup } from "./history/data";
import { ccyTag, fmtMoney, fmtSigned } from "./history/format";
import { legend, navChart } from "./history/nav-chart";

// ---- portfolio history -----------------------------------------------------
// Reconstructed from read-only Flex: the full executed-trade ledger plus the
// day-by-day NAV series. The headline is one chart — portfolio value over time
// with every buy/sell marked — because that's the thing single snapshots can't
// show: the shape of the journey, not just where it ended.

// Leading caret cell for a grouped row. Always rendered (empty when there's
// nothing to expand) so expandable and plain names share the same left edge.
const caretCell = (expandable) =>
  `<span class="hist-caret">${expandable ? "\u25B8" : ""}</span>`;

// Clickable ticker that opens the dossier (cache-first, via the shared
// rebalance opener). data-ticker carries the symbol so one per-row wiring pass
// can find it. ``text`` lets the visible label differ from the symbol (e.g.
// "GEN shares" links to GEN); defaults to the symbol itself.
const tickerSpan = (sym, text = null) =>
  `<span class="hist-tick" data-ticker="${esc(sym)}" title="Open ${esc(sym)} dossier">` +
  `${esc(text == null ? sym : text)}</span>`;

// Wire every .hist-tick inside a freshly built row to open its dossier. Stops
// propagation so clicking the ticker in an expandable row doesn't also toggle
// the row open/closed (the row's own click handler sits above it in bubbling).
function wireTickers(scope) {
  scope.querySelectorAll(".hist-tick[data-ticker]").forEach((node) => {
    node.addEventListener("click", (ev) => {
      ev.stopPropagation();
      const sym = node.dataset.ticker;
      if (sym) openTicker(sym);
    });
  });
}

let _wired = false;

// Cheap, token-free credential check so we can guide setup *before* a pull fails.
async function fetchIbkrStatus() {
  try {
    return await api("/api/ibkr/status");
  } catch {
    return null; // a missing status endpoint must never block showing the cache
  }
}

// A banner shown when the dedicated history Flex query isn't configured yet.
// History needs its own Activity query (Trades + NAV in Base); the positions
// query the Holdings tab uses lacks those sections, so a pull would just error.
function setupNote(st) {
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
    const h = await api("/api/portfolio-history");
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

function emptyState(st) {
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

function renderHistory(h) {
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
function section(title, bodyNode, hint, open = true) {
  const d = el("details", "risk-section hist-section");
  d.open = open;
  const sum = el("summary", "hist-section-sum");
  sum.innerHTML = `<span>${esc(title)}</span>`;
  d.appendChild(sum);
  if (hint) d.appendChild(el("p", "hint", hint));
  d.appendChild(bodyNode);
  return d;
}

// Prev / page-of / Next control. Renders nothing interactive for a single page.
function drawPager(pager, pg, onGo) {
  pager.innerHTML = "";
  if (pg.pages <= 1) {
    pager.appendChild(el("span", "hint", `${pg.total} row${pg.total === 1 ? "" : "s"}`));
    return;
  }
  const prev = el("button", "linklike", "\u2039 Prev");
  const next = el("button", "linklike", "Next \u203a");
  prev.disabled = pg.page <= 1;
  next.disabled = pg.page >= pg.pages;
  prev.addEventListener("click", () => onGo(pg.page - 1));
  next.addEventListener("click", () => onGo(pg.page + 1));
  pager.appendChild(prev);
  pager.appendChild(el("span", "hist-pager-label",
    `Page ${pg.page} of ${pg.pages} · ${pg.total} rows`));
  pager.appendChild(next);
}

// "By sector" table: one row per sector, expandable to the folded names within.
// Columns mirror "Activity by name" so member rows can reuse activityCells; the
// sector header row leaves the Ccy cell blank (a sector spans many currencies).
function sectorTable(secGroups, baseCcy) {
  const baseLbl = baseCcy ? ` (${esc(baseCcy)})` : "";
  const tbl = el("table", "risk-pos-table hist-activity");
  tbl.innerHTML =
    `<thead><tr><th>Sector</th><th class="num">Ccy</th><th class="num">Trades</th>` +
    `<th class="num">Bought${baseLbl}</th><th class="num">Sold${baseLbl}</th>` +
    `<th class="num">Net cash flow${baseLbl}</th><th class="num">Realized P&L${baseLbl}</th></tr></thead>`;
  const body = el("tbody");
  tbl.appendChild(body);
  secGroups.forEach((g) => {
    const expandable = g.groups.length > 0;
    const tr = el("tr", "hist-grp" + (expandable ? " expandable" : ""));
    const badge = ` <span class="hist-optbadge">${g.names} name${g.names === 1 ? "" : "s"}</span>`;
    tr.innerHTML = `<td class="risk-pos-sym">${caretCell(expandable)}${esc(g.sector)}${badge}</td>` + activityCells(g);
    body.appendChild(tr);
    if (!expandable) return;
    const memberRows = g.groups.map((m) => {
      const mtr = el("tr", "hist-member");
      mtr.hidden = true;
      mtr.innerHTML = `<td class="risk-pos-sym hist-member-sym">${tickerSpan(m.key, m.label)}</td>` + activityCells(m);
      body.appendChild(mtr);
      wireTickers(mtr);
      return mtr;
    });
    tr.addEventListener("click", () => {
      const open = tr.classList.toggle("open");
      const c = tr.querySelector(".hist-caret");
      if (c) c.textContent = open ? "\u25BE" : "\u25B8";
      memberRows.forEach((mr) => (mr.hidden = !open));
    });
  });
  return tbl;
}

// Toolbar above the sector table: how fresh the sector map is + a button to
// resolve the still-unknown names from Yahoo (a background job).
function sectorToolbar(h, unknownNames) {
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
      const r = done.result || {};
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

function statCards(h) {
  const s = h.summary || {};
  const bcy = h.base_currency || "";
  const series = h.nav_series || [];
  const latest = series.length ? series[series.length - 1].nav : null;
  const first = series.length ? series[0].nav : null;
  const change = latest != null && first != null ? latest - first : null;
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

function card(label, valueHtml, cls = "muted") {
  const c = el("div", "risk-stat");
  c.innerHTML =
    `<span class="risk-stat-k">${esc(label)}</span>` +
    `<span class="risk-stat-v ${esc(cls)}">${valueHtml}</span>`;
  return c;
}

const ACTIVITY_PAGE = 25;

// Money cells share one shape across the group row and its contract members.
// Both cash flow and P&L are BASE-currency (the only way cross-ticker sums are
// valid); the per-name native currency is shown in its own column instead.
function activityCells(r) {
  const pnl = Number(r.base_realized_pnl) || 0;
  const flowCls = r.net_base_cash_flow >= 0 ? "good" : "bad";
  const pnlCls = pnl > 0 ? "good" : pnl < 0 ? "bad" : "muted";
  // Gross cash out (bought) and in (sold), base currency. Unsigned magnitudes;
  // net cash flow keeps the signed good/bad treatment.
  const b = Number(r.buys) || 0, s = Number(r.sells) || 0;
  const split = `${b} buy${b === 1 ? "" : "s"} \u00b7 ${s} sell${s === 1 ? "" : "s"}`;
  return `<td class="num"><span class="ccy">${esc(r.currency || "")}</span></td>` +
    `<td class="num"><span class="hist-trades-n" title="${esc(split)}">${esc(r.n)}</span></td>` +
    `<td class="num muted">${sensitive(fmtMoney(r.bought_base), "amount bought")}</td>` +
    `<td class="num muted">${sensitive(fmtMoney(r.sold_base), "amount sold")}</td>` +
    `<td class="num ${flowCls}">${sensitive(fmtSigned(r.net_base_cash_flow), "cash flow")}</td>` +
    `<td class="num ${pnlCls}">${sensitive(fmtSigned(pnl), "realized pnl")}</td>`;
}

function activityTable(groups: ActivityGroup[], baseCcy) {
  const wrap = el("div");
  const baseLbl = baseCcy ? ` (${esc(baseCcy)})` : "";
  const tbl = el("table", "risk-pos-table hist-activity");
  tbl.innerHTML =
    `<thead><tr><th>Name</th><th class="num">Ccy</th><th class="num">Trades</th>` +
    `<th class="num">Bought${baseLbl}</th><th class="num">Sold${baseLbl}</th>` +
    `<th class="num">Net cash flow${baseLbl}</th><th class="num">Realized P&L${baseLbl}</th></tr></thead>`;
  const body = el("tbody");
  tbl.appendChild(body);
  const pager = el("div", "hist-pager");
  let page = 1;

  const draw = () => {
    const pg = paginate(groups, page, ACTIVITY_PAGE);
    page = pg.page;
    body.innerHTML = "";
    pg.items.forEach((g) => {
      // Expand when there's more than one leg to reveal (shares + options, or
      // several contracts). A lone stock or single contract has nothing to open.
      const expandable = g.members.length > 1;
      const tr = el("tr", "hist-grp" + (expandable ? " expandable" : ""));
      const badge = g.opt_count
        ? ` <span class="hist-optbadge">${g.opt_count} opt${g.opt_count > 1 ? "s" : ""}</span>`
        : "";
      tr.innerHTML = `<td class="risk-pos-sym">${caretCell(expandable)}${tickerSpan(g.key, g.label)}${badge}</td>` + activityCells(g);
      body.appendChild(tr);
      wireTickers(tr);
      if (!expandable) return;
      const memberRows = g.members.map((m) => {
        const mtr = el("tr", "hist-member");
        mtr.hidden = true;
        // Distinguish the equity leg from contracts when both sit under one name.
        // The equity leg's ticker links to its dossier; option contracts have no
        // ticker text of their own (the parent group row already links it).
        const labelHtml = m.is_option ? esc(contractLabel(m)) : `${tickerSpan(m.symbol)} shares`;
        mtr.innerHTML =
          `<td class="risk-pos-sym hist-member-sym">${labelHtml}</td>` + activityCells(m);
        body.appendChild(mtr);
        wireTickers(mtr);
        return mtr;
      });
      tr.addEventListener("click", () => {
        const open = tr.classList.toggle("open");
        const c = tr.querySelector(".hist-caret");
        if (c) c.textContent = open ? "\u25BE" : "\u25B8";
        memberRows.forEach((m) => (m.hidden = !open));
      });
    });
    drawPager(pager, pg, (p) => { page = p; draw(); });
  };
  draw();

  wrap.appendChild(tbl);
  wrap.appendChild(pager);
  return wrap;
}

const LEDGER_PAGE = 50;

function tradeTable(trades, baseCcy) {
  const all = [...trades].reverse(); // newest first; full set, now paginated
  const baseLbl = baseCcy ? ` (${esc(baseCcy)})` : "";
  const wrap = el("div");
  const tbl = el("table", "risk-pos-table hist-trades");
  tbl.innerHTML =
    `<thead><tr><th>Date</th><th>Side</th><th>Name</th><th class="num">Qty</th>` +
    `<th class="num">Price</th><th class="num">Cash flow${baseLbl}</th>` +
    `<th class="num">Realized P&L</th></tr></thead>`;
  const body = el("tbody");
  tbl.appendChild(body);
  const pager = el("div", "hist-pager");
  let page = 1;

  const draw = () => {
    const pg = paginate(all, page, LEDGER_PAGE);
    page = pg.page;
    body.innerHTML = "";
    pg.items.forEach((t) => {
      const tr = el("tr");
      const buy = t.side === "BUY";
      const pnlCls = t.realized_pnl > 0 ? "good" : t.realized_pnl < 0 ? "bad" : "muted";
      // Options: show the readable contract ("AMD 19APR24 7.5 P") not the cryptic
      // symbol, and leave it un-linked (it's a contract, not a ticker). Equities
      // link their ticker to the dossier.
      const nameHtml = t.is_option ? esc(t.description || t.symbol) : tickerSpan(t.symbol);
      // Price + realized P&L are NATIVE currency (per ticker); cash flow is base.
      tr.innerHTML =
        `<td>${esc(t.date)}</td>` +
        `<td class="${buy ? "good" : "bad"}">${esc(t.side)}</td>` +
        `<td class="risk-pos-sym">${nameHtml}</td>` +
        `<td class="num">${esc(Math.abs(Number(t.quantity)))}</td>` +
        `<td class="num">${esc(t.price)}${ccyTag(t.currency)}</td>` +
        `<td class="num">${sensitive(fmtSigned(t.base_cash_flow), "cash flow")}</td>` +
        `<td class="num ${pnlCls}">${t.realized_pnl ? sensitive(fmtSigned(t.realized_pnl), "realized pnl") + ccyTag(t.currency) : "\u2014"}</td>`;
      body.appendChild(tr);
      wireTickers(tr);
    });
    drawPager(pager, pg, (p) => { page = p; draw(); });
  };
  draw();

  wrap.appendChild(tbl);
  wrap.appendChild(pager);
  return wrap;
}

async function runSync(full) {
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
      const r = (done.result && done.result.summary) || {};
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

export { loadHistory, renderHistory, initHistoryControls, dayGroups, groupActivity, groupBySector, paginate, contractLabel };
