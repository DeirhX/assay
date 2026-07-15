// Fundamental metric tiles + their peer-percentile bars. METRIC_ROWS drives both
// the tile grid (in the composer) and the formatter lookup used by the peer bar.
// peerBar/loadPeerStats decorate already-rendered tiles with a low->high track
// showing where the subject sits among its segment peers. Extracted from
// deepdive.ts; loadPeerStats is best-effort over /api/peer-stats.
import { api, el, esc, fmtB, fmtPct, fmtShares, fmtX } from "../core";

type Fmt = (v: number | null) => string;

interface PeerSegment {
  title: string;
  n: number;
  pct: number;
  median: number | null;
  min: number | null;
  max: number | null;
  members_total?: number;
}

interface PeerMetric {
  aggregate: { pct: number };
  per_segment?: PeerSegment[];
  reliable?: boolean | null;
}

export const METRIC_ROWS: [string, string, Fmt][] = [
  ["market_cap_usd_b", "Market cap", fmtB],
  ["pe_ttm", "P/E (TTM)", fmtX],
  ["pe_fwd", "P/E (fwd)", fmtX],
  ["ps", "P/S", fmtX],
  ["revenue_ttm_usd_b", "Revenue TTM", fmtB],
  ["net_income_ttm_usd_b", "Net income TTM", fmtB],
  ["gross_margin_pct", "Gross margin", (v) => (v == null ? "n/a" : v.toFixed(0) + "%")],
  ["rev_growth_yoy_pct", "Rev growth YoY", fmtPct],
  ["shares_out_b", "Shares out", fmtShares],
];

const METRIC_FMT: Record<string, Fmt> = Object.fromEntries(METRIC_ROWS.map(([k, , f]) => [k, f]));

function ordinal(n: number): string {
  const s = ["th", "st", "nd", "rd"], v = n % 100;
  return n + (s[(v - 20) % 10] || s[v] || s[0]);
}

// Qualitative rank for samples too small to carry a percentile.
function rankWord(pct: number): string {
  if (pct <= 0) return "lowest";
  if (pct >= 1) return "highest";
  if (pct < 0.5) return "below median";
  if (pct > 0.5) return "above median";
  return "at median";
}

// A slim track (low -> high across the segment peers) with the subject's marker
// at its rank-percentile and a centre tick marking the peer median. With one
// segment the caption names it; with several it shows the aggregate and the
// per-segment breakdown lives in the tooltip.
//
// A rank-percentile over a handful of peers is noise (n=3 -> only 0/50/100), so
// when too few segment members have data the backend marks the metric
// unreliable: we then drop the precise-looking "Nth pctile" for an honest rank +
// coverage ("lowest · 3 of 19 peers") and de-emphasize the bar.
export function peerBar(key: string, m: PeerMetric): HTMLElement {
  const fmt: Fmt = METRIC_FMT[key] || ((v) => String(v));
  const pct = Math.max(0, Math.min(1, m.aggregate.pct));
  const segs = m.per_segment || [];
  const multi = segs.length > 1;
  const best = segs.reduce<{ n: number; members_total?: number }>(
    (a, s) => (s.n > a.n ? s : a), segs[0] || { n: 0, members_total: 0 });
  // Backend flag is authoritative; fall back to sample size for older payloads.
  const reliable = m.reliable != null ? m.reliable : best.n >= 5;
  const coverage = best.members_total
    ? `${best.n} of ${best.members_total} peers`
    : `${best.n} peers`;

  let cap: string, tip: string;
  if (reliable) {
    cap = multi
      ? `vs ${segs.length} sectors \u00b7 ${ordinal(Math.round(pct * 100))} pctile`
      : `vs ${segs[0].title} \u00b7 ${ordinal(Math.round(pct * 100))} pctile`;
    tip = segs
      .map((s) => `${s.title}: ${ordinal(Math.round(s.pct * 100))} pctile of ${s.n}` +
        ` (median ${fmt(s.median)}, range ${fmt(s.min)}\u2013${fmt(s.max)})`)
      .join("\n");
  } else {
    cap = `${rankWord(pct)} \u00b7 ${coverage}`;
    tip = "Too few peers have data for a meaningful percentile.\n" + segs
      .map((s) => `${s.title}: ${rankWord(s.pct)} of ${s.n}` +
        (s.members_total ? ` of ${s.members_total}` : "") +
        ` (median ${fmt(s.median)}, range ${fmt(s.min)}\u2013${fmt(s.max)})`)
      .join("\n");
  }

  const wrap = el("div", reliable ? "metric-peer" : "metric-peer sparse");
  wrap.title = tip;
  wrap.innerHTML =
    `<div class="mp-track"><span class="mp-fill" style="width:${(pct * 100).toFixed(1)}%"></span>` +
    `<span class="mp-median"></span>` +
    `<span class="mp-marker" style="left:${(pct * 100).toFixed(1)}%"></span></div>` +
    `<div class="mp-cap">${esc(cap)}</div>`;
  return wrap;
}

export async function loadPeerStats(symbol: string, grid: HTMLElement): Promise<void> {
  let data;
  try { data = await api("/api/peer-stats?symbol=" + encodeURIComponent(symbol)); }
  catch (_e) { return; }  // best-effort enrichment; tiles already rendered
  const metrics = (data && data.metrics) || {};
  if (!Object.keys(metrics).length) return;
  grid.querySelectorAll<HTMLElement>(".metric-cell").forEach((cell) => {
    const key = cell.dataset.metric;
    const m = key ? metrics[key] : null;
    if (m && key) cell.appendChild(peerBar(key, m));
  });
}
