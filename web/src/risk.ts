import { $$, apiLoad, el, esc, fmtStamp, freshnessNote, simpleTable, statTile } from "./core";
import { fmtSignedPct1 } from "./display/format";
import { analyticsSection, caveatBanner, metaStrip } from "./display/chrome";
import { initUrlBoundSelect } from "./url-select";

// ---- portfolio risk lens ---------------------------------------------------
// The whole point of this view: single-name bands hide correlated concentration.
// Lead with the caveats so nobody mistakes a calm-market correlation for a crash.

// One row of the per-name vol/weight table (GET /api/risk -> positions[]). Not
// in api-types.ts since the risk endpoint is local to this view.
interface RiskPosition {
  symbol: string;
  weight_pct: number | null;
  norm_weight_pct: number | null;
  ann_vol_pct: number | null;
}

type RiskPositionSortKey = keyof RiskPosition;
interface RiskPositionSort { key: RiskPositionSortKey; dir: "asc" | "desc"; }

const RISK_POSITION_SORT_VALUE: Record<
  RiskPositionSortKey,
  (position: RiskPosition) => string | number | null
> = {
  symbol: (position) => String(position.symbol || "").toUpperCase(),
  weight_pct: (position) => position.weight_pct,
  norm_weight_pct: (position) => position.norm_weight_pct,
  ann_vol_pct: (position) => position.ann_vol_pct,
};

/** Sort a copy, keeping unavailable values last in either direction. */
function sortRiskPositions(
  positions: RiskPosition[],
  sort: RiskPositionSort,
): RiskPosition[] {
  const value = RISK_POSITION_SORT_VALUE[sort.key];
  const direction = sort.dir === "asc" ? 1 : -1;
  return [...positions].sort((a, b) => {
    const left = value(a);
    const right = value(b);
    if (left == null && right == null) return a.symbol.localeCompare(b.symbol);
    if (left == null) return 1;
    if (right == null) return -1;
    const compared = typeof left === "string"
      ? left.localeCompare(String(right))
      : Number(left) - Number(right);
    return compared !== 0
      ? compared * direction
      : a.symbol.localeCompare(b.symbol);
  });
}

const fmtPct1 = (v: number | null | undefined) => (v == null ? "n/a" : Number(v).toFixed(1) + "%");
const fmtNum = (v: number | null | undefined, d = 2) => (v == null ? "n/a" : Number(v).toFixed(d));

interface RiskMetrics {
  n_names?: number;
  effective_bets?: number | null;
  covariance_share_pct?: number | null;
  portfolio_vol_pct?: number | null;
  weighted_avg_vol_pct?: number | null;
  avg_pairwise_corr?: number | null;
}

interface StressContribution {
  symbol?: string;
  beta?: number | null;
  impact_pct?: number | null;
}

interface StressScenario {
  label?: string;
  factor?: string;
  shock_pct?: number;
  nav_impact_pct?: number | null;
  measurable?: boolean;
  note?: string;
  contributions?: StressContribution[];
}

type CorrMatrix = Record<string, Record<string, number | null>>;

// GET /api/risk -> fx: the currency lens (fx_history.window_report).
interface FxExposure { currency: string; base_value: number; weight_pct: number }
interface FxWindow {
  currency: string;
  fx_return_pct: number;
  contribution_pct: number;
  from?: string;
  to?: string;
}
interface FxReport {
  base?: string;
  exposure?: FxExposure[];
  foreign_pct?: number;
  window?: FxWindow[];
  range?: string;
  updated_at?: string | null;
  caveats?: string[];
}

// GET /api/risk: correlation-aware portfolio risk lens. Local to this view.
interface RiskPayload {
  metrics?: RiskMetrics;
  caveats?: string[];
  range?: string;
  n_obs?: number;
  as_of?: string | null;
  snapshot?: string | null;
  source?: string;
  stress?: StressScenario[];
  correlation?: { symbols?: string[]; matrix?: CorrMatrix };
  positions?: RiskPosition[];
  fx?: FxReport;
}

// Correlation -> background. High positive correlation is the risk here, so make
// it loud (red); near-zero is calm (neutral); negative (a real hedge) is green.
function corrColor(c: number | null | undefined) {
  if (c == null) return "transparent";
  if (c >= 0) return `rgba(220, 80, 70, ${(0.12 + 0.78 * Math.min(1, c)).toFixed(3)})`;
  return `rgba(70, 170, 110, ${(0.12 + 0.78 * Math.min(1, -c)).toFixed(3)})`;
}

let _riskRange = "1y";

async function loadRisk() {
  await apiLoad({
    path: "/api/risk?range=" + encodeURIComponent(_riskRange),
    status: $$("#risk-status"),
    clear: [$$("#risk-result")],
    loading: "Computing portfolio risk (pulling price history)…",
    errorLabel: "Could not compute risk",
    render: renderRisk,
  });
}

function renderRisk(r: RiskPayload) {
  const out = $$("#risk-result");
  out.innerHTML = "";
  const m = r.metrics || {};

  // 1) Caveat banner — first, on purpose.
  const banner = caveatBanner(r.caveats || [], { always: true });
  if (banner) out.appendChild(banner);

  // 2) Meta + headline metrics.
  out.appendChild(metaStrip([
    `names ${esc(m.n_names ?? 0)}`,
    `window ${esc(r.range || "?")} · ${esc(r.n_obs ?? 0)} obs`,
    `as of ${esc(r.as_of || "n/a")}`,
    `snapshot ${freshnessNote(r.snapshot) || esc(fmtStamp(r.snapshot))}`,
    `source ${esc(r.source || "?")}`,
  ]));

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
  const fx = r.fx;
  if (fx && (fx.exposure || []).length) {
    const top = (fx.exposure || []).map((e) => `${esc(e.currency)} ${fmtPct1(e.weight_pct)}`).join(" · ");
    stats.appendChild(statCard("Non-base FX exposure",
      fmtPct1(fx.foreign_pct),
      "muted",
      `Share of invested equity priced in a currency other than ${esc(fx.base || "base")}: ${top}. ` +
      `That slice's ${esc(fx.base || "base")} value moves with the exchange rate, not just the stock.`));
  }
  out.appendChild(stats);

  // 2b) Currency lens: exposure + how much of the window's CZK move was FX.
  if (fx && (fx.exposure || []).length) {
    const sec = analyticsSection(
      "Currency exposure & FX effect",
      `A ${esc(fx.base || "base")}-base book holding foreign names earns two returns: the stock's ` +
      `and the exchange rate's. "FX move" is the pair's change over the window; "est. contribution" ` +
      `is that move scaled by the sleeve's weight.`,
    );
    const win: Record<string, FxWindow> = {};
    (fx.window || []).forEach((w) => { win[w.currency] = w; });
    sec.appendChild(simpleTable<FxExposure>({
      className: "fx-table",
      head: "<tr><th>Currency</th><th class='num'>Exposure</th><th class='num'>FX move</th><th class='num'>Est. contribution</th></tr>",
      rows: fx.exposure || [],
      cells: (e) => {
        const w = win[e.currency];
        const contrib = w ? w.contribution_pct : null;
        const cc = contrib == null ? "muted" : contrib > 0 ? "good" : contrib < 0 ? "bad" : "muted";
        return `<td class="fx-ccy">${esc(e.currency)}</td>` +
          `<td class="num">${fmtPct1(e.weight_pct)}</td>` +
          `<td class="num">${w ? fmtSignedPct1(w.fx_return_pct) : "n/a"}</td>` +
          `<td class="num ${cc}">${w ? fmtSignedPct1(contrib) : "n/a"}</td>`;
      },
    }));
    (fx.caveats || []).forEach((c) => sec.appendChild(el("p", "hint muted", c)));
    out.appendChild(sec);
  }

  // 3) Stress scenarios.
  if (r.stress && r.stress.length) {
    const sec = analyticsSection(
      "Factor-shock stress test",
      "Estimated NAV hit if a factor moves, using each holding's beta to that factor " +
      "over the window. Linear and beta-based — it ignores that correlations spike in a real crash.",
    );
    r.stress.forEach((s) => sec.appendChild(stressCard(s)));
    out.appendChild(sec);
  }

  // 4) Correlation heatmap.
  const corr = r.correlation;
  const syms = (corr && corr.symbols) || [];
  if (syms.length && corr && corr.matrix) {
    const sec = el("div", "risk-section");
    sec.appendChild(el("h3", undefined, "Correlation matrix"));
    sec.appendChild(el("p", "hint", "Daily-return correlation. Red = move together (concentration), green = a genuine hedge. Hover any cell for the exact value."));
    sec.appendChild(heatmap(syms, corr.matrix));
    out.appendChild(sec);
  }

  // 5) Per-name vol/weight table.
  if (r.positions && r.positions.length) {
    const sec = el("div", "risk-section");
    sec.appendChild(el("h3", undefined, "Per-name volatility"));
    sec.appendChild(posTable(r.positions));
    out.appendChild(sec);
  }
}

const statCard = (label: string, value: string, cls?: string, title?: string) =>
  statTile(label, value, { cls, title, family: "risk-stat" });

function stressCard(s: StressScenario) {
  const card = el("div", "risk-stress");
  const head = el("div", "risk-stress-head");
  const impactCls = s.nav_impact_pct == null ? "muted" : s.nav_impact_pct <= -15 ? "bad" : s.nav_impact_pct < 0 ? "warn" : "good";
  head.innerHTML =
    `<span class="risk-stress-name">${esc(s.label)} ` +
    `<small>(${esc(s.factor)} ${(s.shock_pct ?? 0) > 0 ? "+" : ""}${esc(s.shock_pct)}%)</small></span>` +
    `<span class="risk-stress-impact ${impactCls}">` +
    (s.measurable ? `${(s.nav_impact_pct ?? 0) > 0 ? "+" : ""}${esc(s.nav_impact_pct)}% NAV` : "not measurable") +
    `</span>`;
  card.appendChild(head);
  if (s.note) card.appendChild(el("div", "hint", esc(s.note)));
  if (s.measurable && s.contributions && s.contributions.length) {
    const det = el("details", "risk-contrib");
    det.appendChild(el("summary", undefined, "Per-name contribution"));
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

function heatmap(syms: string[], matrix: CorrMatrix) {
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
function volClass(v: number | null | undefined) {
  if (v == null) return "muted";
  if (v >= 60) return "bad";
  if (v >= 40) return "warn";
  if (v >= 25) return "";
  return "good";
}

function posTable(positions: RiskPosition[]) {
  const vols = positions.map((p) => p.ann_vol_pct).filter((v) => v != null);
  const maxVol = vols.length ? Math.max(...vols) : 0;
  const table = el("table", "risk-pos-table");
  const body = el("tbody");
  let sort: RiskPositionSort = { key: "weight_pct", dir: "desc" };
  let head: HTMLElement;
  const columns: Array<{ key: RiskPositionSortKey; label: string; num?: boolean }> = [
    { key: "symbol", label: "Name" },
    { key: "weight_pct", label: "Weight", num: true },
    { key: "norm_weight_pct", label: "Norm. weight", num: true },
    { key: "ann_vol_pct", label: "Ann. vol" },
  ];

  const drawBody = () => {
    body.innerHTML = "";
    sortRiskPositions(positions, sort).forEach((p) => {
      const v = p.ann_vol_pct;
      const cls = volClass(v);
      const fill = maxVol > 0 && v != null ? Math.max(3, Math.round((v / maxVol) * 100)) : 0;
      const row = el("tr");
      row.innerHTML = `<td class="risk-pos-sym">${esc(p.symbol)}</td>` +
        `<td class="num">${fmtPct1(p.weight_pct)}</td>` +
        `<td class="num muted">${fmtPct1(p.norm_weight_pct)}</td>` +
        `<td class="risk-vol-cell">` +
          `<span class="risk-vol-track"><span class="risk-vol-bar ${cls}" style="width:${fill}%"></span></span>` +
          `<span class="risk-vol-val ${cls}">${fmtPct1(v)}</span>` +
        `</td>`;
      body.appendChild(row);
    });
  };

  const makeHead = () => {
    const thead = el("thead");
    const row = el("tr");
    columns.forEach((column) => {
      const active = sort.key === column.key;
      const th = el(
        "th",
        `${column.num ? "num " : ""}hist-sortable${active ? " active" : ""}`,
      );
      th.dataset.riskSort = column.key;
      th.tabIndex = 0;
      th.setAttribute("role", "button");
      th.setAttribute(
        "aria-sort",
        active ? (sort.dir === "asc" ? "ascending" : "descending") : "none",
      );
      th.title = `Sort by ${column.label}`;
      th.innerHTML = `<span class="hist-sort-lbl">${esc(column.label)}</span>` +
        `<span class="hist-sort-ind">${active ? (sort.dir === "asc" ? "\u2191" : "\u2193") : ""}</span>`;
      const applySort = () => {
        sort = sort.key === column.key
          ? { key: column.key, dir: sort.dir === "asc" ? "desc" : "asc" }
          : { key: column.key, dir: column.key === "symbol" ? "asc" : "desc" };
        const next = makeHead();
        table.replaceChild(next, head);
        head = next;
        drawBody();
        head.querySelector<HTMLElement>(`[data-risk-sort="${column.key}"]`)?.focus();
      };
      th.addEventListener("click", applySort);
      th.addEventListener("keydown", (event) => {
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        applySort();
      });
      row.appendChild(th);
    });
    thead.appendChild(row);
    return thead;
  };

  head = makeHead();
  table.append(head, body);
  drawBody();
  return table;
}

function initRiskControls() {
  initUrlBoundSelect({
    select: $$("#risk-range"),
    param: "range",
    defaultValue: "1y",
    onValue: (v) => { _riskRange = v; },
    reload: loadRisk,
  });
}

export { loadRisk, renderRisk, initRiskControls, corrColor, sortRiskPositions };
