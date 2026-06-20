import { $, api, el, esc, fmtStamp, freshnessNote, sensitive } from "./core";
import { pollDeepJob } from "./errors";
import { openTicker } from "./rebalance";
import { contractLabel, dayGroups, groupActivity, groupBySector, paginate, type ActivityGroup } from "./history/data";

// ---- portfolio history -----------------------------------------------------
// Reconstructed from read-only Flex: the full executed-trade ledger plus the
// day-by-day NAV series. The headline is one chart — portfolio value over time
// with every buy/sell marked — because that's the thing single snapshots can't
// show: the shape of the journey, not just where it ended.

const SVG_NS = "http://www.w3.org/2000/svg";
const W = 1000;
const H = 340;
const PAD = { l: 70, r: 16, t: 16, b: 30 };

const msOf = (d) => new Date(String(d) + "T00:00:00Z").getTime();
const fmtMoney = (v) =>
  v == null || Number.isNaN(v) ? "n/a" : Math.round(Number(v)).toLocaleString();
const fmtSigned = (v) =>
  v == null || Number.isNaN(v) ? "n/a" : (v >= 0 ? "+" : "") + Math.round(Number(v)).toLocaleString();

function svg(tag, attrs = {}) {
  const n = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, String(v));
  return n;
}

// A muted currency-code chip. Not sensitive (a code isn't a value), so it stays
// visible under privacy mode while the number beside it blurs.
const ccyTag = (code) => (code ? ` <span class="ccy">${esc(code)}</span>` : "");

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

// Nearest NAV at-or-before a date, so a trade marker sits on the line.
function navAtOrBefore(series, ms) {
  let lo = 0, hi = series.length - 1, ans = series[0];
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (msOf(series[mid].date) <= ms) { ans = series[mid]; lo = mid + 1; }
    else hi = mid - 1;
  }
  return ans;
}

const DAY_MS = 86400000;

function navChart(series, trades, baseCcy = "") {
  const x0 = msOf(series[0].date);
  const x1 = msOf(series[series.length - 1].date);
  const fullSpan = x1 - x0 || 1;
  const minSpan = Math.min(fullSpan, 5 * DAY_MS); // can't zoom tighter than ~5 days
  const navs = series.map((p) => Number(p.nav));
  let ymin = Math.min(...navs);
  let ymax = Math.max(...navs);
  const padY = (ymax - ymin) * 0.08 || Math.abs(ymax) * 0.08 || 1;
  ymin -= padY; ymax += padY;
  const yspan = ymax - ymin || 1;
  const plotW = W - PAD.l - PAD.r;

  // The currently visible time window, mapped across the full plot width. Zoom
  // narrows it (spreading points apart); pan slides it. y stays fixed so the
  // line doesn't rescale vertically as you scrub along time.
  const view = { a: x0, b: x1 };
  const xFor = (ms) => PAD.l + ((ms - view.a) / (view.b - view.a || 1)) * plotW;
  const yFor = (v) => H - PAD.b - ((v - ymin) / yspan) * (H - PAD.t - PAD.b);

  const root = svg("svg", {
    class: "hist-chart", viewBox: `0 0 ${W} ${H}`,
    preserveAspectRatio: "none", role: "img",
    "aria-label": "Portfolio value over time with trade markers",
  });

  // Clip dynamic content to the plot rect so panned line/dots don't spill over
  // the axes. Unique id in case more than one chart ever shares a page.
  const cid = "histclip-" + Math.random().toString(36).slice(2, 8);
  const defs = svg("defs");
  const clip = svg("clipPath", { id: cid });
  clip.appendChild(svg("rect", { x: PAD.l, y: PAD.t, width: plotW, height: H - PAD.t - PAD.b }));
  defs.appendChild(clip);
  root.appendChild(defs);

  // Static horizontal gridlines + y labels (y domain never changes on zoom).
  const ticks = 4;
  const axisG = svg("g", { "data-sensitive": "", class: "hist-axis" });
  for (let i = 0; i <= ticks; i++) {
    const v = ymin + (yspan * i) / ticks;
    const y = yFor(v);
    root.appendChild(svg("line", { class: "hist-grid", x1: PAD.l, x2: W - PAD.r, y1: y, y2: y }));
    const label = svg("text", { class: "hist-ylabel", x: PAD.l - 8, y: y + 4, "text-anchor": "end" });
    label.textContent = fmtMoney(v);
    axisG.appendChild(label);
  }

  const plotG = svg("g", { "clip-path": `url(#${cid})` }); // area + line + dots
  const xlabG = svg("g", { class: "hist-axis" });
  root.appendChild(plotG);
  root.appendChild(axisG);
  root.appendChild(xlabG);

  const wrap = el("div", "hist-chart-wrap");
  const tip = el("div", "hist-tip");
  tip.hidden = true;
  const place = (ev) => {
    const r = wrap.getBoundingClientRect();
    const x = ev.clientX - r.left;
    const y = ev.clientY - r.top;
    const left = Math.max(6, Math.min(x + 14, wrap.clientWidth - tip.offsetWidth - 8));
    const top = Math.max(6, y - tip.offsetHeight - 12);
    tip.style.left = left + "px";
    tip.style.top = top + "px";
  };

  const allDays = dayGroups(trades);
  let dragging = false;

  // Rebuild everything that depends on the visible window.
  const render = () => {
    plotG.innerHTML = "";
    xlabG.innerHTML = "";

    const linePts = series.map((p) => `${xFor(msOf(p.date)).toFixed(1)},${yFor(Number(p.nav)).toFixed(1)}`);
    const areaD = `M ${linePts[0]} L ${linePts.join(" L ")} ` +
      `L ${xFor(x1).toFixed(1)},${(H - PAD.b).toFixed(1)} L ${xFor(x0).toFixed(1)},${(H - PAD.b).toFixed(1)} Z`;
    plotG.appendChild(svg("path", { class: "hist-area", d: areaD }));
    plotG.appendChild(svg("polyline", { class: "hist-line", points: linePts.join(" ") }));

    // x labels at the window's start / middle / end dates.
    const fmtMs = (ms) => new Date(ms).toISOString().slice(0, 10);
    [[view.a, "start"], [(view.a + view.b) / 2, "middle"], [view.b, "end"]].forEach(([ms, anchor]) => {
      const tx = svg("text", { class: "hist-xlabel", x: xFor(ms), y: H - 10, "text-anchor": anchor });
      tx.textContent = fmtMs(ms);
      xlabG.appendChild(tx);
    });

    // Markers only for days inside the window (one per trading day).
    allDays.forEach((d) => {
      const ms = msOf(d.date);
      if (ms < view.a || ms > view.b) return;
      const ref = navAtOrBefore(series, ms);
      const cx = xFor(ms);
      const cy = yFor(Number(ref.nav));
      const sides = new Set(d.trades.map((t) => t.side));
      const cls = sides.size > 1 ? "mixed" : sides.has("BUY") ? "buy" : "sell";
      const rad = Math.min(6.5, 3 + Math.log2(d.trades.length + 1));
      plotG.appendChild(svg("circle", {
        class: "hist-dot " + cls, cx: cx.toFixed(1), cy: cy.toFixed(1), r: rad.toFixed(1),
      }));
      const hit = svg("circle", {
        class: "hist-hit", cx: cx.toFixed(1), cy: cy.toFixed(1), r: Math.max(9, rad + 5).toFixed(1),
      });
      hit.addEventListener("mouseenter", (ev) => {
        if (dragging) return;
        tip.innerHTML = dayTipHtml(d, baseCcy);
        tip.hidden = false;
        place(ev);
      });
      hit.addEventListener("mousemove", (ev) => { if (!dragging) place(ev); });
      hit.addEventListener("mouseleave", () => { tip.hidden = true; });
      plotG.appendChild(hit);
    });
  };

  // clientX -> SVG user-space x (viewBox is 0..W, stretched to the element).
  const userX = (clientX) => {
    const r = root.getBoundingClientRect();
    return r.width ? ((clientX - r.left) / r.width) * W : clientX;
  };
  const msForUserX = (ux) => view.a + ((ux - PAD.l) / plotW) * (view.b - view.a);
  const setWindow = (a, b) => {
    const span = Math.max(minSpan, Math.min(fullSpan, b - a));
    if (a < x0) a = x0;
    if (a + span > x1) a = x1 - span;
    view.a = Math.max(x0, a);
    view.b = Math.min(x1, view.a + span);
    render();
  };

  // Wheel = zoom toward the cursor; the date under the pointer stays put.
  root.addEventListener("wheel", (ev) => {
    ev.preventDefault();
    const anchor = msForUserX(userX(ev.clientX));
    const span = view.b - view.a;
    const factor = ev.deltaY < 0 ? 0.82 : 1 / 0.82;
    const newSpan = Math.max(minSpan, Math.min(fullSpan, span * factor));
    const ratio = (anchor - view.a) / span; // keep anchor at same fractional x
    setWindow(anchor - ratio * newSpan, anchor - ratio * newSpan + newSpan);
  }, { passive: false });

  // Drag = pan. Track on the document so a fast drag doesn't escape the svg.
  let dragStartX = 0, dragA = 0, dragB = 0;
  const onMove = (ev) => {
    const dms = (userX(dragStartX) - userX(ev.clientX)) / plotW * (dragB - dragA);
    setWindow(dragA + dms, dragB + dms);
  };
  const onUp = () => {
    dragging = false;
    root.classList.remove("dragging");
    document.removeEventListener("mousemove", onMove);
    document.removeEventListener("mouseup", onUp);
  };
  root.addEventListener("mousedown", (ev) => {
    if (view.b - view.a >= fullSpan) return; // nothing to pan when fully zoomed out
    dragging = true;
    tip.hidden = true;
    dragStartX = ev.clientX; dragA = view.a; dragB = view.b;
    root.classList.add("dragging");
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
    ev.preventDefault();
  });
  // Double-click anywhere resets to the full span.
  root.addEventListener("dblclick", () => setWindow(x0, x1));

  render();
  wrap.appendChild(root);
  wrap.appendChild(tip);
  return wrap;
}

// Hover card for a single trading day: a per-name digest (options folded under
// their underlying) plus the day's net cash flow, capped so a 29-fill day stays
// readable.
function dayTipHtml(d, baseCcy = "") {
  const n = d.trades.length;
  const head =
    `<div class="hist-tip-date">${esc(d.date)} · ${n} trade${n > 1 ? "s" : ""}</div>`;
  const byName = new Map();
  for (const t of d.trades) {
    const name = t.underlying || t.symbol || "?";
    if (!byName.has(name)) byName.set(name, []);
    byName.get(name).push(t);
  }
  const entries = [...byName.entries()];
  const shown = entries.slice(0, 8);
  const rows = shown.map(([name, ts]) => tradeLine(name, ts)).join("");
  const more = entries.length > shown.length
    ? `<div class="hist-tip-more">+${entries.length - shown.length} more name(s)</div>`
    : "";
  const net = d.trades.reduce((s, t) => s + (Number(t.base_cash_flow) || 0), 0);
  const netLine =
    `<div class="hist-tip-net">net cash ${sensitive(fmtSigned(net), "cash flow")}${ccyTag(baseCcy)}</div>`;
  return head + `<div class="hist-tip-rows">${rows}</div>` + more + netLine;
}

function tradeLine(name, ts) {
  if (ts.length === 1 && !ts[0].is_option) {
    const t = ts[0];
    const side = t.side === "BUY" ? "buy" : "sell";
    return `<div class="hist-tip-row"><span class="hist-tip-side ${side}">${esc(t.side)}</span> ` +
      `${esc(Math.abs(Number(t.quantity)))} <strong>${esc(name)}</strong> @ ${esc(t.price)}${ccyTag(t.currency)}</div>`;
  }
  const buys = ts.filter((t) => t.side === "BUY").length;
  const sells = ts.length - buys;
  const isOpt = ts.some((t) => t.is_option);
  const what = isOpt ? `${ts.length} opt${ts.length > 1 ? "s" : ""}` : `${ts.length} trades`;
  return `<div class="hist-tip-row"><strong>${esc(name)}</strong> · ${what} ` +
    `<span class="muted">(${buys}B/${sells}S)</span></div>`;
}

function legend() {
  const l = el("div", "hist-legend");
  l.innerHTML =
    `<span><i class="hist-key line"></i> NAV</span>` +
    `<span><i class="hist-key buy"></i> Buy day</span>` +
    `<span><i class="hist-key sell"></i> Sell day</span>` +
    `<span><i class="hist-key mixed"></i> Buy + sell</span>`;
  return l;
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
