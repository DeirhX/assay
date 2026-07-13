import { $, $$, apiLoad, el, esc, statTile } from "./core";
import { fmtMoneyCcy, fmtSignedPct2 } from "./display/format";
import { analyticsSection, caveatBanner, metaStrip } from "./display/chrome";
import { navFromUrl, replaceViewState } from "./shell";

// ---- process attribution ---------------------------------------------------
// The only view that measures the *process* instead of a position: actual
// time-weighted return vs two skill-free baselines (never-rebalanced, benchmark),
// flow-neutralized and FX-clean. Local endpoint, so its types live here.

interface CurvePoint { date: string; value: number }
interface AttrPayload {
  as_of?: string | null;
  base?: string;
  range?: string;
  benchmark?: string;
  start?: string;
  twr?: { actual?: number | null; hold?: number | null; benchmark?: number | null };
  curves?: { actual?: CurvePoint[]; hold?: CurvePoint[]; benchmark?: CurvePoint[] };
  flows_total?: number;
  caveats?: string[];
  enough_data?: boolean;
}

// Each curve gets a stable label, order, and colour class (defined in style.css).
const SERIES: { key: "actual" | "hold" | "benchmark"; label: string; cls: string }[] = [
  { key: "actual", label: "Actual", cls: "attr-actual" },
  { key: "hold", label: "Never rebalanced", cls: "attr-hold" },
  { key: "benchmark", label: "Benchmark", cls: "attr-bench" },
];

let _attrRange = "1y";
let _attrBenchmark = "SPY";

async function loadAttribution() {
  await apiLoad<AttrPayload>({
    path: `/api/attribution?range=${encodeURIComponent(_attrRange)}&benchmark=${encodeURIComponent(_attrBenchmark)}`,
    status: $("#attr-status"),
    clear: [$("#attr-result")],
    loading: "Attributing the process (pulling benchmark + held-name prices)…",
    errorLabel: "Could not compute attribution",
    render: renderAttribution,
  });
}

function renderAttribution(r: AttrPayload) {
  const out = $$("#attr-result");
  out.innerHTML = "";
  const base = r.base || "CZK";

  // Caveats first, always — a thin ledger or a missing price makes a curve lie.
  const banner = caveatBanner(r.caveats || []);
  if (banner) out.appendChild(banner);

  if (!r.enough_data) {
    out.appendChild(el("div", "hint", "Not enough portfolio history yet to attribute the process."));
    return;
  }

  out.appendChild(metaStrip([
    `window ${esc(r.range || "?")}`,
    `${esc(r.start || "?")} → ${esc(r.as_of || "?")}`,
    `benchmark ${esc(r.benchmark || "?")}`,
    `net flows ${esc(fmtMoneyCcy(r.flows_total, base))}`,
  ]));

  const twr = r.twr || {};
  const actual = twr.actual;
  const stats = el("div", "risk-stats");
  stats.appendChild(statTile("Actual TWR", fmtSignedPct2(actual), {
    family: "risk-stat",
    cls: actual == null ? "muted" : actual >= 0 ? "good" : "bad",
    title: "Time-weighted return over the window. Deposits and withdrawals are removed, " +
      "so this is what the invested koruna actually earned — not what the account balance did.",
  }));
  stats.appendChild(deltaTile("vs never rebalanced", actual, twr.hold,
    "Actual TWR minus the return of freezing the book at the window start and never trading. " +
    "Positive means your trading beat sitting on your hands."));
  stats.appendChild(deltaTile(`vs ${r.benchmark || "benchmark"}`, actual, twr.benchmark,
    "Actual TWR minus putting the same koruna into the benchmark instead. " +
    "Positive means you beat just buying the index."));
  out.appendChild(stats);

  const chart = growthChart(r);
  if (chart) {
    const sec = analyticsSection(
      "Growth of 100, same flows",
      "Each line starts at 100 and receives the identical deposits; they diverge only on " +
      "what the money was invested in. Foreign prices are converted day-by-day.",
    );
    sec.appendChild(legend(r));
    sec.appendChild(chart);
    out.appendChild(sec);
  }
}

// Actual minus a counterfactual, in percentage points; green when the process won.
function deltaTile(label: string, actual: number | null | undefined, other: number | null | undefined, title: string) {
  const d = actual != null && other != null ? actual - other : null;
  return statTile(label, d == null ? "n/a" : (d >= 0 ? "+" : "") + d.toFixed(2) + " pp", {
    family: "risk-stat",
    cls: d == null ? "muted" : d >= 0 ? "good" : "bad",
    title,
  });
}

function legend(r: AttrPayload) {
  const wrap = el("div", "attr-legend");
  SERIES.forEach((s) => {
    if (!(r.curves || {})[s.key]?.length) return;
    const item = el("span", "attr-legend-item");
    item.innerHTML = `<span class="attr-swatch ${s.cls}"></span>${esc(s.label)}`;
    wrap.appendChild(item);
  });
  return wrap;
}

// A compact multi-line SVG chart: every curve normalized to 100 at its first
// point (so they share a y-scale), x by shared date order. No dependencies.
function growthChart(r: AttrPayload): SVGElement | null {
  const curves = r.curves || {};
  const ref = curves.actual;
  if (!ref || ref.length < 2) return null;
  const dateX = new Map<string, number>();
  ref.forEach((p, i) => dateX.set(p.date, i));
  const xMax = ref.length - 1;

  type Norm = { cls: string; pts: [number, number][] };
  const series: Norm[] = [];
  let yMin = Infinity;
  let yMax = -Infinity;
  for (const s of SERIES) {
    const c = curves[s.key];
    if (!c || c.length < 2 || !c[0].value) continue;
    const base0 = c[0].value;
    const pts: [number, number][] = [];
    for (const p of c) {
      const x = dateX.get(p.date);
      if (x == null) continue;
      const y = (p.value / base0) * 100;
      pts.push([x, y]);
      if (y < yMin) yMin = y;
      if (y > yMax) yMax = y;
    }
    if (pts.length >= 2) series.push({ cls: s.cls, pts });
  }
  if (!series.length || !isFinite(yMin) || !isFinite(yMax)) return null;
  if (yMax - yMin < 1e-6) { yMin -= 1; yMax += 1; }

  const W = 640;
  const H = 220;
  const pad = { l: 44, r: 12, t: 10, b: 22 };
  const sx = (x: number) => pad.l + (x / xMax) * (W - pad.l - pad.r);
  const sy = (y: number) => pad.t + (1 - (y - yMin) / (yMax - yMin)) * (H - pad.t - pad.b);

  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("class", "attr-chart");
  svg.setAttribute("role", "img");

  // Baseline at 100 (break-even for the seeded capital) when it's in range.
  if (yMin <= 100 && yMax >= 100) {
    const base = document.createElementNS(NS, "line");
    base.setAttribute("x1", String(sx(0)));
    base.setAttribute("x2", String(sx(xMax)));
    base.setAttribute("y1", String(sy(100)));
    base.setAttribute("y2", String(sy(100)));
    base.setAttribute("class", "attr-baseline");
    svg.appendChild(base);
  }
  // y-axis end labels (min/max normalized level).
  [yMax, yMin].forEach((v) => {
    const t = document.createElementNS(NS, "text");
    t.setAttribute("x", "4");
    t.setAttribute("y", String(sy(v) + 4));
    t.setAttribute("class", "attr-axis");
    t.textContent = v.toFixed(0);
    svg.appendChild(t);
  });

  for (const s of series) {
    const path = document.createElementNS(NS, "polyline");
    path.setAttribute("points", s.pts.map(([x, y]) => `${sx(x).toFixed(1)},${sy(y).toFixed(1)}`).join(" "));
    path.setAttribute("class", `attr-line ${s.cls}`);
    svg.appendChild(path);
  }
  return svg;
}

function initAttributionControls() {
  const rng = $<HTMLSelectElement & { _wired?: boolean }>("#attr-range");
  const nav = navFromUrl();
  if (rng) {
    _attrRange = nav.range && Array.from(rng.options).some((o) => o.value === nav.range)
      ? nav.range : "1y";
    rng.value = _attrRange;
  }
  if (rng && !rng._wired) {
    rng._wired = true;
    rng.addEventListener("change", () => {
      _attrRange = rng.value || "1y";
      replaceViewState({ range: _attrRange === "1y" ? "" : _attrRange });
      loadAttribution();
    });
  }
  const bench = $<HTMLSelectElement & { _wired?: boolean }>("#attr-benchmark");
  if (bench) {
    _attrBenchmark = nav.benchmark && Array.from(bench.options).some((o) => o.value === nav.benchmark)
      ? nav.benchmark : "SPY";
    bench.value = _attrBenchmark;
  }
  if (bench && !bench._wired) {
    bench._wired = true;
    bench.addEventListener("change", () => {
      _attrBenchmark = bench.value || "SPY";
      replaceViewState({ benchmark: _attrBenchmark === "SPY" ? "" : _attrBenchmark });
      loadAttribution();
    });
  }
}

export { loadAttribution, renderAttribution, initAttributionControls };
