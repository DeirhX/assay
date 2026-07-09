// Interactive price-history card: an SVG "mountain" chart with switchable range
// buttons (1D … Max), each window fetched lazily and cached. Extracted from
// deepdive.ts; pure rendering over /api/price-history -- no app state.
import { api, el, esc, fmtPct, fmtPrice, pctClass } from "../core";

interface HistoryPoint {
  date: string;
  close: unknown;
}

interface PriceHistory {
  points?: HistoryPoint[];
  range?: string;
  interval?: string;
  source?: string;
}

interface ChartRec {
  symbol?: string;
  price_history?: PriceHistory;
}

export const PRICE_RANGES: [string, string][] = [
  ["1d", "1D"], ["1w", "1W"], ["1mo", "1M"], ["3mo", "3M"], ["6mo", "6M"],
  ["1y", "1Y"], ["5y", "5Y"], ["max", "Max"],
];

export function chartSvg(rec: ChartRec, history: PriceHistory):
  { svg: string; sourceLabel: string; lastHtml: string } | null {
  const points = (history.points || [])
    .map((p) => ({ date: p.date, close: Number(p.close) }))
    .filter((p) => p.date && Number.isFinite(p.close));
  if (points.length < 2) return null;

  const width = 760, height = 260;
  const pad = { top: 18, right: 18, bottom: 34, left: 58 };
  const innerW = width - pad.left - pad.right;
  const innerH = height - pad.top - pad.bottom;
  let min = Math.min(...points.map((p) => p.close));
  let max = Math.max(...points.map((p) => p.close));
  if (min === max) {
    min *= 0.98;
    max *= 1.02;
  }
  const buffer = (max - min) * 0.06;
  min -= buffer;
  max += buffer;

  const x = (i: number) => pad.left + (points.length === 1 ? 0 : (i / (points.length - 1)) * innerW);
  const y = (v: number) => pad.top + ((max - v) / (max - min)) * innerH;
  const line = points.map((p, i) => `${x(i).toFixed(1)},${y(p.close).toFixed(1)}`).join(" ");
  const area = `${pad.left},${height - pad.bottom} ${line} ${width - pad.right},${height - pad.bottom}`;
  const first = points[0], last = points[points.length - 1];
  const change = first.close ? (last.close / first.close - 1) * 100 : null;
  const trend = pctClass(change);
  const parseStamp = (value: string) => new Date(value.length > 10 ? value : value + "T00:00:00Z");
  const spanDays = (parseStamp(last.date).getTime() - parseStamp(first.date).getTime()) / 86400000;
  const dateLabel = (value: string) => {
    const d = parseStamp(value);
    if (spanDays < 2) return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
    return spanDays > 420
      ? d.toLocaleDateString(undefined, { month: "short", year: "numeric" })
      : d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  };
  const rangeLabel = [history.range, history.interval].filter(Boolean).join(" / ") || "daily closes";
  const sourceLabel = [history.source || "unknown", rangeLabel, `${points.length} points`].join(" · ");

  const lo = min + buffer, hi = max - buffer;
  const yTicks = 4;  // top, two interior, bottom
  const yAxis = Array.from({ length: yTicks }, (_, i) => {
    const v = hi - (i / (yTicks - 1)) * (hi - lo);
    const yp = y(v).toFixed(1);
    const interior = i > 0 && i < yTicks - 1;
    return (
      (interior ? `<line class="chart-grid" x1="${pad.left}" y1="${yp}" x2="${width - pad.right}" y2="${yp}"></line>` : "") +
      `<text class="chart-label" x="${pad.left - 10}" y="${yp}" text-anchor="end" dominant-baseline="middle">${esc(fmtPrice(v))}</text>`
    );
  }).join("");

  // Evenly spaced x-axis date labels (previously just first + last). Aim for one
  // label per ~120px, snapped to real data points so labels line up with the
  // plotted line. End ticks anchor inward; interior ticks center and get a faint
  // vertical gridline to match the y-axis grid. Set() dedupes when there are
  // fewer points than ticks.
  const xTickCount = Math.min(Math.max(2, Math.round(innerW / 120)), points.length);
  const rawIndices = Array.from(new Set(
    Array.from({ length: xTickCount }, (_, i) =>
      Math.round((i / (xTickCount - 1)) * (points.length - 1))),
  ));
  // Deduping the indices isn't enough: on an intraday range two evenly-spaced
  // points can format to the SAME label (the last two ticks both reading
  // "Jul 8"). Dedupe by the rendered label too, keeping the first tick of each
  // day — but if the right-edge point (the "now" tick) collides with an earlier
  // one, move that tick out to the edge so the latest date anchors the axis once
  // instead of appearing twice.
  const lastIdx = points.length - 1;
  const labelPos = new Map<string, number>();
  const xIndices: number[] = [];
  for (const idx of rawIndices) {
    const label = dateLabel(points[idx].date);
    const prev = labelPos.get(label);
    if (prev === undefined) { labelPos.set(label, xIndices.length); xIndices.push(idx); }
    else if (idx === lastIdx) { xIndices[prev] = idx; }
  }
  const xAxis = xIndices.map((idx) => {
    const xp = x(idx).toFixed(1);
    const anchor = idx === 0 ? "start" : idx === points.length - 1 ? "end" : "middle";
    const interior = idx > 0 && idx < points.length - 1;
    return (
      (interior ? `<line class="chart-grid" x1="${xp}" y1="${pad.top}" x2="${xp}" y2="${height - pad.bottom}"></line>` : "") +
      `<text class="chart-label" x="${xp}" y="${height - 9}" text-anchor="${anchor}">${esc(dateLabel(points[idx].date))}</text>`
    );
  }).join("");

  // Vertical "mountain" gradient: saturated at the price line, fading to nothing
  // at the baseline. A flat-opacity fill (the old approach) reads as a featureless
  // slab whose top — the volatile line — smears into a band; the fade ties the
  // fill to the line and keeps the baseline unambiguous.
  const fillId = "price-area-fill";
  const svg =
    `<svg class="price-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="${esc(rec.symbol)} ${esc(rangeLabel)} price history">` +
      `<defs><linearGradient id="${fillId}" class="chart-fill-grad ${trend}" x1="0" y1="0" x2="0" y2="1">` +
        `<stop class="cf-top" offset="0%"></stop><stop class="cf-bot" offset="100%"></stop>` +
      `</linearGradient></defs>` +
      `<line class="chart-axis" x1="${pad.left}" y1="${height - pad.bottom}" x2="${width - pad.right}" y2="${height - pad.bottom}"></line>` +
      `<line class="chart-axis" x1="${pad.left}" y1="${pad.top}" x2="${pad.left}" y2="${height - pad.bottom}"></line>` +
      yAxis +
      xAxis +
      `<polygon class="chart-area" fill="url(#${fillId})" points="${area}"></polygon>` +
      `<polyline class="chart-line ${trend}" points="${line}"></polyline>` +
      `<circle class="chart-dot" cx="${x(points.length - 1).toFixed(1)}" cy="${y(last.close).toFixed(1)}" r="3.5"></circle>` +
    `</svg>`;
  const lastHtml = `<span>${esc(fmtPrice(last.close))}</span><strong class="${trend}">${esc(fmtPct(change))}</strong>`;
  return { svg, sourceLabel, lastHtml };
}

export function renderPriceChart(rec: ChartRec): HTMLElement | null {
  const stored = rec.price_history || {};
  if (!chartSvg(rec, stored)) return null;

  const card = el("div", "card price-chart-card");
  const head = el("div", "chart-head");
  head.innerHTML =
    `<div><h2 class="section">Price history</h2><div class="chart-source"></div></div>` +
    `<div class="chart-last"></div>`;
  card.appendChild(head);
  const ranges = el("div", "chart-ranges");
  card.appendChild(ranges);
  const body = el("div", "chart-body");
  // The canvas keeps the last drawn chart while a new range loads, so the card's
  // height never changes; a transparent overlay just dims it and shows a spinner.
  const canvas = el("div", "chart-canvas");
  const overlay = el("div", "chart-overlay", `<span class="spinner"></span>`);
  body.appendChild(canvas);
  body.appendChild(overlay);
  card.appendChild(body);

  const srcEl = head.querySelector(".chart-source") as HTMLElement;
  const lastEl = head.querySelector(".chart-last") as HTMLElement;
  const cache: Record<string, PriceHistory> = { "1y": stored };  // stored series is the 1y window; reuse it
  let active = "1y";

  function paint(history: PriceHistory) {
    const drawn = chartSvg(rec, history);
    if (!drawn) {
      canvas.innerHTML = `<div class="hint">No price data for this range.</div>`;
      srcEl.textContent = "";
      lastEl.innerHTML = "";
      return;
    }
    canvas.innerHTML = drawn.svg;
    srcEl.textContent = drawn.sourceLabel;
    lastEl.innerHTML = drawn.lastHtml;
  }

  async function select(key: string, label: string, btn: HTMLElement) {
    active = key;
    [...ranges.children].forEach((b) => b.classList.toggle("active", b === btn));
    if (cache[key]) { paint(cache[key]); return; }
    body.classList.add("loading");  // dim + spinner; chart stays put underneath
    try {
      const ph = await api(`/api/price-history/${encodeURIComponent(rec.symbol || "")}?range=${encodeURIComponent(key)}`);
      cache[key] = ph;
      if (active === key) paint(ph);
    } catch (e) {
      if (active === key) srcEl.innerHTML = `<span class="err">Could not load ${esc(label)}: ${esc((e as Error).message)}</span>`;
    } finally {
      if (active === key) body.classList.remove("loading");
    }
  }

  PRICE_RANGES.forEach(([key, label]) => {
    const btn = el("button", "chart-range" + (key === "1y" ? " active" : ""), esc(label));
    btn.type = "button";
    btn.addEventListener("click", () => select(key, label, btn));
    ranges.appendChild(btn);
  });

  paint(stored);
  return card;
}
