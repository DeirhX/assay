// @ts-nocheck
import { $, api, el, esc, fmtStamp } from "./core";

// ---- portfolio risk lens ---------------------------------------------------
// The whole point of this view: single-name bands hide correlated concentration.
// Lead with the caveats so nobody mistakes a calm-market correlation for a crash.

const fmtPct1 = (v) => (v == null ? "n/a" : Number(v).toFixed(1) + "%");
const fmtNum = (v, d = 2) => (v == null ? "n/a" : Number(v).toFixed(d));

// Correlation -> background. High positive correlation is the risk here, so make
// it loud (red); near-zero is calm (neutral); negative (a real hedge) is green.
function corrColor(c) {
  if (c == null) return "transparent";
  if (c >= 0) return `rgba(220, 80, 70, ${(0.12 + 0.78 * Math.min(1, c)).toFixed(3)})`;
  return `rgba(70, 170, 110, ${(0.12 + 0.78 * Math.min(1, -c)).toFixed(3)})`;
}

let _riskRange = "1y";

async function loadRisk() {
  const status = $("#risk-status");
  const out = $("#risk-result");
  status.classList.remove("err");
  status.textContent = "Computing portfolio risk (pulling price history)…";
  out.innerHTML = "";
  try {
    const r = await api("/api/risk?range=" + encodeURIComponent(_riskRange));
    status.textContent = "";
    renderRisk(r);
  } catch (e) {
    out.innerHTML = "";
    status.textContent = "Could not compute risk: " + e.message;
    status.classList.add("err");
  }
}

function renderRisk(r) {
  const out = $("#risk-result");
  out.innerHTML = "";
  const m = r.metrics || {};

  // 1) Caveat banner — first, on purpose.
  const banner = el("div", "risk-caveat");
  banner.innerHTML =
    `<strong>Read this before you trust a number below.</strong>` +
    `<ul>${(r.caveats || []).map((c) => `<li>${esc(c)}</li>`).join("")}</ul>`;
  out.appendChild(banner);

  // 2) Meta + headline metrics.
  const meta = el("div", "reb-meta");
  meta.innerHTML =
    `<span>names ${esc(m.n_names ?? 0)}</span>` +
    `<span>window ${esc(r.range || "?")} · ${esc(r.n_obs ?? 0)} obs</span>` +
    `<span>as of ${esc(r.as_of || "n/a")}</span>` +
    `<span>snapshot ${esc(fmtStamp(r.snapshot))}</span>` +
    `<span>source ${esc(r.source || "?")}</span>`;
  out.appendChild(meta);

  const effBets = m.effective_bets;
  const covShare = m.covariance_share_pct;
  const stats = el("div", "risk-stats");
  stats.appendChild(statCard("Effective bets",
    fmtNum(effBets, 1),
    effBets == null ? "muted" : effBets < 2 ? "bad" : effBets < 3.5 ? "warn" : "good",
    `Correlation-aware count of independent bets across ${m.n_names ?? 0} names. ` +
    `Near 1 means the whole book is one bet.`));
  stats.appendChild(statCard("Co-movement share",
    fmtPct1(covShare),
    covShare == null ? "muted" : covShare >= 80 ? "bad" : covShare >= 60 ? "warn" : "good",
    "Share of portfolio variance from names moving together rather than their own " +
    "idiosyncratic risk. High = a single correlated bet."));
  stats.appendChild(statCard("Portfolio volatility",
    m.portfolio_vol_pct == null ? "n/a" : fmtPct1(m.portfolio_vol_pct),
    "muted",
    `Annualized. Weighted-average single-name vol is ${fmtPct1(m.weighted_avg_vol_pct)}; ` +
    `the gap is your only diversification.`));
  stats.appendChild(statCard("Avg pairwise corr",
    fmtNum(m.avg_pairwise_corr, 2),
    m.avg_pairwise_corr == null ? "muted" : m.avg_pairwise_corr >= 0.6 ? "bad" : m.avg_pairwise_corr >= 0.4 ? "warn" : "good",
    "Mean correlation across every pair of held names over the window."));
  out.appendChild(stats);

  // 3) Stress scenarios.
  if (r.stress && r.stress.length) {
    const sec = el("div", "risk-section");
    sec.appendChild(el("h3", null, "Factor-shock stress test"));
    sec.appendChild(el("p", "hint",
      "Estimated NAV hit if a factor moves, using each holding's beta to that factor " +
      "over the window. Linear and beta-based — it ignores that correlations spike in a real crash."));
    r.stress.forEach((s) => sec.appendChild(stressCard(s)));
    out.appendChild(sec);
  }

  // 4) Correlation heatmap.
  const syms = (r.correlation && r.correlation.symbols) || [];
  if (syms.length) {
    const sec = el("div", "risk-section");
    sec.appendChild(el("h3", null, "Correlation matrix"));
    sec.appendChild(el("p", "hint", "Daily-return correlation. Red = move together (concentration), green = a genuine hedge. Hover any cell for the exact value."));
    sec.appendChild(heatmap(syms, r.correlation.matrix));
    out.appendChild(sec);
  }

  // 5) Per-name vol/weight table.
  if (r.positions && r.positions.length) {
    const sec = el("div", "risk-section");
    sec.appendChild(el("h3", null, "Per-name volatility"));
    sec.appendChild(posTable(r.positions));
    out.appendChild(sec);
  }
}

function statCard(label, value, cls, title) {
  const c = el("div", "risk-stat");
  c.title = title || "";
  c.innerHTML =
    `<span class="risk-stat-k">${esc(label)}</span>` +
    `<span class="risk-stat-v ${esc(cls)}">${esc(value)}</span>`;
  return c;
}

function stressCard(s) {
  const card = el("div", "risk-stress");
  const head = el("div", "risk-stress-head");
  const impactCls = s.nav_impact_pct == null ? "muted" : s.nav_impact_pct <= -15 ? "bad" : s.nav_impact_pct < 0 ? "warn" : "good";
  head.innerHTML =
    `<span class="risk-stress-name">${esc(s.label)} ` +
    `<small>(${esc(s.factor)} ${s.shock_pct > 0 ? "+" : ""}${esc(s.shock_pct)}%)</small></span>` +
    `<span class="risk-stress-impact ${impactCls}">` +
    (s.measurable ? `${s.nav_impact_pct > 0 ? "+" : ""}${esc(s.nav_impact_pct)}% NAV` : "not measurable") +
    `</span>`;
  card.appendChild(head);
  if (s.note) card.appendChild(el("div", "hint", esc(s.note)));
  if (s.measurable && s.contributions && s.contributions.length) {
    const det = el("details", "risk-contrib");
    det.appendChild(el("summary", null, "Per-name contribution"));
    const list = el("div", "risk-contrib-list");
    s.contributions.forEach((c) => {
      const cls = c.impact_pct == null ? "muted" : c.impact_pct < 0 ? "bad" : "good";
      list.appendChild(el("div", "risk-contrib-row",
        `<span>${esc(c.symbol)}</span>` +
        `<span class="muted">β ${c.beta == null ? "n/a" : esc(c.beta)}</span>` +
        `<span class="${cls}">${c.impact_pct == null ? "n/a" : (c.impact_pct > 0 ? "+" : "") + esc(c.impact_pct) + "%"}</span>`));
    });
    det.appendChild(list);
    card.appendChild(det);
  }
  return card;
}

function heatmap(syms, matrix) {
  const wrap = el("div", "risk-heatmap-wrap");
  const grid = el("div", "risk-heatmap");
  // Cap cell width (so a small book doesn't get absurd squares) but let columns
  // shrink to fit the panel as names grow -- no horizontal scroll either way.
  // Past the threshold, drop numbers and rely on colour + hover for the value.
  if (syms.length > 12) grid.classList.add("compact");
  grid.style.gridTemplateColumns = `minmax(44px, max-content) repeat(${syms.length}, minmax(0, 60px))`;
  grid.appendChild(el("div", "risk-hm-corner"));
  syms.forEach((s) => grid.appendChild(el("div", "risk-hm-col", esc(s))));
  syms.forEach((rowSym) => {
    grid.appendChild(el("div", "risk-hm-row", esc(rowSym)));
    syms.forEach((colSym) => {
      const c = matrix[rowSym] ? matrix[rowSym][colSym] : null;
      const cell = el("div", "risk-hm-cell", c == null ? "" : Number(c).toFixed(2));
      cell.style.background = corrColor(c);
      cell.title = `${rowSym} vs ${colSym}: ${c == null ? "n/a" : c}`;
      grid.appendChild(cell);
    });
  });
  wrap.appendChild(grid);
  return wrap;
}

// Annualized single-name vol -> severity. Lower is calmer (green); the loud end
// is what concentration risk looks like name-by-name.
function volClass(v) {
  if (v == null) return "muted";
  if (v >= 60) return "bad";
  if (v >= 40) return "warn";
  if (v >= 25) return "";
  return "good";
}

function posTable(positions) {
  const vols = positions.map((p) => p.ann_vol_pct).filter((v) => v != null);
  const maxVol = vols.length ? Math.max(...vols) : 0;
  const tbl = el("table", "risk-pos-table");
  tbl.innerHTML =
    `<thead><tr><th>Name</th><th class="num">Weight</th><th class="num">Norm. weight</th><th>Ann. vol</th></tr></thead>`;
  const body = el("tbody");
  positions.forEach((p) => {
    const tr = el("tr");
    const v = p.ann_vol_pct;
    const cls = volClass(v);
    const fill = maxVol > 0 && v != null ? Math.max(3, Math.round((v / maxVol) * 100)) : 0;
    tr.innerHTML =
      `<td class="risk-pos-sym">${esc(p.symbol)}</td>` +
      `<td class="num">${fmtPct1(p.weight_pct)}</td>` +
      `<td class="num muted">${fmtPct1(p.norm_weight_pct)}</td>` +
      `<td class="risk-vol-cell">` +
        `<span class="risk-vol-track"><span class="risk-vol-bar ${cls}" style="width:${fill}%"></span></span>` +
        `<span class="risk-vol-val ${cls}">${fmtPct1(v)}</span>` +
      `</td>`;
    body.appendChild(tr);
  });
  tbl.appendChild(body);
  return tbl;
}

function initRiskControls() {
  const sel = $("#risk-range");
  if (sel && !sel._wired) {
    sel._wired = true;
    sel.value = _riskRange;
    sel.addEventListener("change", () => { _riskRange = sel.value || "1y"; loadRisk(); });
  }
}

export { loadRisk, renderRisk, initRiskControls, corrColor };
