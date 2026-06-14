// @ts-nocheck
import { $, api, el, esc, fmtStamp, freshnessNote, sensitive } from "./core";
import { pollDeepJob } from "./errors";

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

let _wired = false;

async function loadHistory() {
  const status = $("#hist-status");
  const out = $("#hist-result");
  status.classList.remove("err");
  status.textContent = "Loading cached history…";
  out.innerHTML = "";
  try {
    const h = await api("/api/portfolio-history");
    status.textContent = "";
    renderHistory(h);
  } catch (e) {
    out.innerHTML = "";
    if (e.status === 404) {
      // Expected on a fresh setup: nothing pulled yet. Nudge, don't alarm.
      status.textContent = "";
      out.appendChild(emptyState());
      return;
    }
    status.textContent = "Could not load history: " + e.message;
    status.classList.add("err");
  }
}

function emptyState() {
  const box = el("div", "hist-empty");
  box.innerHTML =
    `<p>No portfolio history cached yet.</p>` +
    `<p class="hint">Click <strong>Update from IBKR</strong> above. The first run walks your account ` +
    `back to inception via read-only Flex (one ≤365-day window at a time), so it can take a minute. ` +
    `After that, <strong>Update</strong> only fetches the days since the last pull.</p>`;
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
    `<span>pulled ${freshnessNote(h.generated_at) || esc(fmtStamp(h.generated_at))}</span>`;
  out.appendChild(meta);

  out.appendChild(statCards(h));

  const series = (h.nav_series || []).filter((p) => p && p.date && p.nav != null);
  const chartSec = el("div", "risk-section");
  chartSec.appendChild(el("h3", null, "Portfolio value & actions"));
  if (series.length >= 2) {
    chartSec.appendChild(el("p", "hint",
      "Net asset value over time. Markers are trades on the day they executed — green buy, red sell. Hover any marker for detail."));
    chartSec.appendChild(navChart(series, h.trades || []));
    chartSec.appendChild(legend());
  } else {
    chartSec.appendChild(el("p", "hint err",
      "Not enough NAV points to chart. Add the “Net Asset Value (NAV) in Base” section to the Flex query (it emits the daily NAV; “Change in NAV” is a different, period-summary section), then re-pull."));
  }
  out.appendChild(chartSec);

  if (s.by_symbol && s.by_symbol.length) {
    const sec = el("div", "risk-section");
    sec.appendChild(el("h3", null, "Activity by name"));
    sec.appendChild(bySymbolTable(s.by_symbol));
    out.appendChild(sec);
  }

  if (h.trades && h.trades.length) {
    const sec = el("div", "risk-section");
    sec.appendChild(el("h3", null, `Trade ledger (${h.trades.length})`));
    sec.appendChild(el("p", "hint", "Newest first. Base cash flow is negative for buys (cash out), positive for sells."));
    sec.appendChild(tradeTable(h.trades));
    out.appendChild(sec);
  }
}

function statCards(h) {
  const s = h.summary || {};
  const series = h.nav_series || [];
  const latest = series.length ? series[series.length - 1].nav : null;
  const first = series.length ? series[0].nav : null;
  const change = latest != null && first != null ? latest - first : null;
  const wrap = el("div", "risk-stats");
  wrap.appendChild(card("Latest NAV", sensitive(fmtMoney(latest), "net asset value")));
  wrap.appendChild(card("Change over span", change == null ? "n/a" : sensitive(fmtSigned(change), "nav change"),
    change == null ? "muted" : change >= 0 ? "good" : "bad"));
  wrap.appendChild(card("Realized P&L", s.realized_pnl_total == null ? "n/a" : sensitive(fmtSigned(s.realized_pnl_total), "realized pnl"),
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

function navChart(series, trades) {
  const x0 = msOf(series[0].date);
  const x1 = msOf(series[series.length - 1].date);
  const span = x1 - x0 || 1;
  const navs = series.map((p) => Number(p.nav));
  let ymin = Math.min(...navs);
  let ymax = Math.max(...navs);
  const padY = (ymax - ymin) * 0.08 || Math.abs(ymax) * 0.08 || 1;
  ymin -= padY; ymax += padY;
  const yspan = ymax - ymin || 1;

  const xFor = (ms) => PAD.l + ((ms - x0) / span) * (W - PAD.l - PAD.r);
  const yFor = (v) => H - PAD.b - ((v - ymin) / yspan) * (H - PAD.t - PAD.b);

  const root = svg("svg", {
    class: "hist-chart", viewBox: `0 0 ${W} ${H}`,
    preserveAspectRatio: "none", role: "img",
    "aria-label": "Portfolio value over time with trade markers",
  });

  // Horizontal gridlines + y labels (NAV is money -> blur under privacy mode).
  const axisG = svg("g", { "data-sensitive": "", class: "hist-axis" });
  const ticks = 4;
  for (let i = 0; i <= ticks; i++) {
    const v = ymin + (yspan * i) / ticks;
    const y = yFor(v);
    root.appendChild(svg("line", { class: "hist-grid", x1: PAD.l, x2: W - PAD.r, y1: y, y2: y }));
    const label = svg("text", { class: "hist-ylabel", x: PAD.l - 8, y: y + 4, "text-anchor": "end" });
    label.textContent = fmtMoney(v);
    axisG.appendChild(label);
  }

  // NAV area + line.
  const linePts = series.map((p) => `${xFor(msOf(p.date)).toFixed(1)},${yFor(Number(p.nav)).toFixed(1)}`);
  const areaD = `M ${linePts[0]} L ${linePts.join(" L ")} L ${xFor(x1).toFixed(1)},${(H - PAD.b).toFixed(1)} L ${xFor(x0).toFixed(1)},${(H - PAD.b).toFixed(1)} Z`;
  root.appendChild(svg("path", { class: "hist-area", d: areaD }));
  root.appendChild(svg("polyline", { class: "hist-line", points: linePts.join(" ") }));

  // x labels: first, middle, last.
  const xlabG = svg("g", { class: "hist-axis" });
  [series[0], series[Math.floor(series.length / 2)], series[series.length - 1]].forEach((p, i) => {
    const anchor = i === 0 ? "start" : i === 2 ? "end" : "middle";
    const tx = svg("text", { class: "hist-xlabel", x: xFor(msOf(p.date)), y: H - 10, "text-anchor": anchor });
    tx.textContent = p.date;
    xlabG.appendChild(tx);
  });
  root.appendChild(xlabG);

  // Trade markers on the NAV line. Cap to keep the DOM sane on huge ledgers.
  const dotsG = svg("g", { class: "hist-dots" });
  const capped = trades.length > 2000 ? trades.slice(-2000) : trades;
  capped.forEach((t) => {
    if (!t.date) return;
    const ms = msOf(t.date);
    if (ms < x0 || ms > x1) return;
    const ref = navAtOrBefore(series, ms);
    const cx = xFor(ms);
    const cy = yFor(Number(ref.nav));
    const buy = t.side === "BUY";
    const dot = svg("circle", {
      class: "hist-dot " + (buy ? "buy" : "sell"),
      cx: cx.toFixed(1), cy: cy.toFixed(1), r: 3.2,
    });
    const title = svg("title");
    title.textContent =
      `${t.date} ${t.side} ${t.symbol} · ${Math.abs(Number(t.quantity))} @ ${t.price}`;
    dot.appendChild(title);
    dotsG.appendChild(dot);
  });
  root.appendChild(axisG);
  root.appendChild(dotsG);

  const wrap = el("div", "hist-chart-wrap");
  wrap.appendChild(root);
  return wrap;
}

function legend() {
  const l = el("div", "hist-legend");
  l.innerHTML =
    `<span><i class="hist-key line"></i> NAV</span>` +
    `<span><i class="hist-key buy"></i> Buy</span>` +
    `<span><i class="hist-key sell"></i> Sell</span>`;
  return l;
}

function bySymbolTable(rows) {
  const tbl = el("table", "risk-pos-table");
  tbl.innerHTML =
    `<thead><tr><th>Name</th><th class="num">Trades</th><th class="num">Buys</th><th class="num">Sells</th>` +
    `<th class="num">Net cash flow</th><th class="num">Realized P&L</th></tr></thead>`;
  const body = el("tbody");
  rows.forEach((r) => {
    const tr = el("tr");
    const flowCls = r.net_base_cash_flow >= 0 ? "good" : "bad";
    const pnlCls = r.realized_pnl > 0 ? "good" : r.realized_pnl < 0 ? "bad" : "muted";
    tr.innerHTML =
      `<td class="risk-pos-sym">${esc(r.symbol)}</td>` +
      `<td class="num">${esc(r.n)}</td>` +
      `<td class="num">${esc(r.buys)}</td>` +
      `<td class="num">${esc(r.sells)}</td>` +
      `<td class="num ${flowCls}">${sensitive(fmtSigned(r.net_base_cash_flow), "cash flow")}</td>` +
      `<td class="num ${pnlCls}">${sensitive(fmtSigned(r.realized_pnl), "realized pnl")}</td>`;
    body.appendChild(tr);
  });
  tbl.appendChild(body);
  return tbl;
}

function tradeTable(trades) {
  const tbl = el("table", "risk-pos-table hist-trades");
  tbl.innerHTML =
    `<thead><tr><th>Date</th><th>Side</th><th>Name</th><th class="num">Qty</th>` +
    `<th class="num">Price</th><th class="num">Base cash flow</th><th class="num">Realized P&L</th></tr></thead>`;
  const body = el("tbody");
  // Newest first; cap the rendered rows (full set lives in the JSON).
  const rows = [...trades].reverse().slice(0, 250);
  rows.forEach((t) => {
    const tr = el("tr");
    const buy = t.side === "BUY";
    const pnlCls = t.realized_pnl > 0 ? "good" : t.realized_pnl < 0 ? "bad" : "muted";
    tr.innerHTML =
      `<td>${esc(t.date)}</td>` +
      `<td class="${buy ? "good" : "bad"}">${esc(t.side)}</td>` +
      `<td class="risk-pos-sym">${esc(t.symbol)}</td>` +
      `<td class="num">${esc(Math.abs(Number(t.quantity)))}</td>` +
      `<td class="num">${esc(t.price)}</td>` +
      `<td class="num">${sensitive(fmtSigned(t.base_cash_flow), "cash flow")}</td>` +
      `<td class="num ${pnlCls}">${t.realized_pnl ? sensitive(fmtSigned(t.realized_pnl), "realized pnl") : "—"}</td>`;
    body.appendChild(tr);
  });
  tbl.appendChild(body);
  if (trades.length > rows.length) {
    const note = el("div", "hint", `Showing the latest ${rows.length} of ${trades.length} trades.`);
    const wrap = el("div");
    wrap.appendChild(tbl);
    wrap.appendChild(note);
    return wrap;
  }
  return tbl;
}

async function runSync(full) {
  const status = $("#hist-status");
  const btns = [$("#hist-sync"), $("#hist-full")].filter(Boolean);
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

export { loadHistory, renderHistory, initHistoryControls };
