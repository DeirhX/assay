import { $, api, esc, fmtPct, fmtWeight, loadError, pctClass, relAge, sensitive, spinner } from "./core";
import { loadCachedSegment, loadSegmentList } from "./segment";
import { prefillDirection } from "./strategy";
import { navFromUrl, pushNav, replaceViewState, setActiveView } from "./shell";

// ---- leaderboard: rank every cached segment by "promise" ------------------
// The "which segment is hottest, and am I in it?" screen. Renders entirely from
// the cached-only /api/segments/leaderboard payload; sorting is client-side, so
// switching the sort never re-hits the network. Pure builders (rankSegments,
// exposureGaps, leaderboardTileHtml) are exported for vitest.

export interface LeaderboardRow {
  segment: string;
  title: string;
  member_count: number;
  momentum_3m_med: number | null;
  momentum_12m_med: number | null;
  breadth_3m: number | null;
  val_growth_med: number | null;
  val_growth_coverage: number;
  exposure_pct: number;
  held_count: number;
  cached_at: string | null;
  age_days: number | null;
  stale: boolean;
  overlap_allowed: boolean;
  score: number;
}

export interface LeaderboardPayload {
  segments: LeaderboardRow[];
  overlap: boolean;
  as_of: string;
  stale_days: number;
}

export type SortMode = "promise" | "momentum" | "breadth" | "gap";

export interface GapOpts {
  // Absolute exposure thresholds (percent of invested book). A segment is only
  // flagged if BOTH its promise quartile AND its exposure clear the bar.
  underweightPct?: number;
  overweightPct?: number;
}

const SORTS: { mode: SortMode; label: string }[] = [
  { mode: "promise", label: "Promise" },
  { mode: "momentum", label: "Momentum" },
  { mode: "breadth", label: "Breadth" },
  { mode: "gap", label: "Exposure gap" },
];

// Defaults for the actionable callouts. Kept liberal so a genuinely lopsided
// position surfaces, while a book that already tracks promise stays quiet.
const UNDERWEIGHT_PCT = 5;
const OVERWEIGHT_PCT = 15;

const _num = (v: number | null | undefined): number =>
  typeof v === "number" && Number.isFinite(v) ? v : -Infinity;

// Min-max normalize a metric across the set to 0..1 for the composite "gap"
// axis. Missing values map to 0 so an unmeasured segment sinks rather than NaNs.
function _normalize(rows: LeaderboardRow[], key: keyof LeaderboardRow): Record<string, number> {
  const vals = rows
    .map((r) => r[key])
    .filter((v): v is number => typeof v === "number" && Number.isFinite(v));
  const out: Record<string, number> = {};
  if (!vals.length) return out;
  const lo = Math.min(...vals);
  const span = Math.max(...vals) - lo || 1;
  rows.forEach((r) => {
    const v = r[key];
    if (typeof v === "number" && Number.isFinite(v)) out[r.segment] = (v - lo) / span;
  });
  return out;
}

// Order the tiles for a given sort. "gap" ranks by promise-minus-exposure, so
// hot-but-unowned floats to the top and cold-but-owned sinks to the bottom --
// both actionable ends in one axis. Never mutates the input.
export function rankSegments(rows: LeaderboardRow[], mode: SortMode): LeaderboardRow[] {
  const copy = rows.slice();
  if (mode === "momentum") {
    copy.sort((a, b) => _num(b.momentum_3m_med) - _num(a.momentum_3m_med));
  } else if (mode === "breadth") {
    copy.sort((a, b) => _num(b.breadth_3m) - _num(a.breadth_3m));
  } else if (mode === "gap") {
    const ns = _normalize(rows, "score");
    const ne = _normalize(rows, "exposure_pct");
    const gap = (r: LeaderboardRow) => (ns[r.segment] ?? 0) - (ne[r.segment] ?? 0);
    copy.sort((a, b) => gap(b) - gap(a));
  } else {
    copy.sort((a, b) => b.score - a.score || _num(b.momentum_3m_med) - _num(a.momentum_3m_med));
  }
  return copy;
}

// The two actionable mismatches: top-quartile promise you barely own, and
// bottom-quartile promise you're heavy in. Quartile is by score; the exposure
// threshold gates it to genuinely lopsided cases.
export function exposureGaps(rows: LeaderboardRow[], opts: GapOpts = {}): { hot: LeaderboardRow[]; cold: LeaderboardRow[] } {
  const underweight = opts.underweightPct ?? UNDERWEIGHT_PCT;
  const overweight = opts.overweightPct ?? OVERWEIGHT_PCT;
  const scored = rows.filter((r) => typeof r.score === "number").slice().sort((a, b) => b.score - a.score);
  if (!scored.length) return { hot: [], cold: [] };
  const q = Math.max(1, Math.ceil(scored.length / 4));
  const top = new Set(scored.slice(0, q).map((r) => r.segment));
  const bottom = new Set(scored.slice(-q).map((r) => r.segment));
  const hot = scored.filter((r) => top.has(r.segment) && r.exposure_pct < underweight);
  const cold = scored.filter((r) => bottom.has(r.segment) && r.exposure_pct > overweight);
  return { hot, cold };
}

function staleDot(row: LeaderboardRow): string {
  const age = relAge(row.cached_at);
  if (row.stale) {
    return `<span class="lb-dot lb-dot-stale" title="cached ${esc(age || "long ago")} — stale, run a fresh pull"></span>`;
  }
  return `<span class="lb-dot lb-dot-fresh" title="cached ${esc(age || "recently")}"></span>`;
}

function metricCell(k: string, v: number | null, sorted = false): string {
  return `<div class="lb-metric${sorted ? " is-sort" : ""}"><span class="lb-k">${esc(k)}</span>` +
    `<span class="lb-v ${pctClass(v)}">${fmtPct(v)}</span></div>`;
}

// Exposure is a NAV-derived figure, so it renders through sensitive(); segment
// momentum/breadth are public market data and stay visible under privacy mode.
function exposureChip(row: LeaderboardRow, sorted = false): string {
  const owned = row.held_count > 0
    ? `${sensitive(fmtWeight(row.exposure_pct), "segment exposure")} across ${row.held_count}/${row.member_count}`
    : `<span class="muted">not held</span> · ${row.member_count} names`;
  return `<span class="lb-exposure${sorted ? " is-sort" : ""}">${owned}</span>`;
}

export function leaderboardTileHtml(row: LeaderboardRow, rank: number, sort: SortMode = "promise"): string {
  const breadthPct = row.breadth_3m == null ? null : Math.round(row.breadth_3m * 100);
  const breadthBar = breadthPct == null
    ? `<span class="muted">breadth n/a</span>`
    : `<div class="lb-breadth" title="${breadthPct}% of members up over 3M">` +
        `<div class="lb-breadth-fill" style="width:${breadthPct}%"></div></div>` +
        `<span class="lb-breadth-label">${breadthPct}% up</span>`;
  const breadthRowCls = "lb-breadth-row" + (sort === "breadth" ? " is-sort" : "");
  const valGrowth = row.val_growth_med == null
    ? ""
    : `<div class="lb-valgrowth" title="median forward P/E per point of revenue growth, ${row.val_growth_coverage} of ${row.member_count} names">` +
        `P/E÷growth ${row.val_growth_med.toFixed(2)}</div>`;
  return `<button class="lb-tile${row.stale ? " lb-tile-stale" : ""}" type="button" ` +
      `data-segment="${esc(row.segment)}" ` +
      `title="promise score ${row.score} = rank(3M momentum) + rank(breadth); ordering, not a forecast">` +
    `<div class="lb-tile-head">` +
      `<span class="lb-rank">#${rank}</span>` +
      `<span class="lb-title">${esc(row.title)}</span>` +
      staleDot(row) +
    `</div>` +
    `<div class="lb-metrics">${metricCell("3M", row.momentum_3m_med, sort === "momentum")}${metricCell("12M", row.momentum_12m_med)}</div>` +
    `<div class="${breadthRowCls}">${breadthBar}</div>` +
    `<div class="lb-foot">${exposureChip(row, sort === "gap")}${valGrowth}</div>` +
  `</button>`;
}

function calloutHtml(kind: "hot" | "cold", rows: LeaderboardRow[]): string {
  if (!rows.length) return "";
  const heading = kind === "hot" ? "Hot but underweight" : "Cold but overweight";
  const hint = kind === "hot"
    ? "top-promise segments you barely own"
    : "weak-promise segments you're heavy in";
  const items = rows.map((r) => {
    const dir = kind === "hot" ? `add exposure to ${r.title}` : `reduce exposure to ${r.title}`;
    return `<div class="lb-callout-row">` +
      `<span class="lb-callout-name">${esc(r.title)}</span>` +
      `<span class="lb-callout-meta">score ${r.score} · 3M <span class="${pctClass(r.momentum_3m_med)}">${fmtPct(r.momentum_3m_med)}</span> · own ${sensitive(fmtWeight(r.exposure_pct), "segment exposure")}</span>` +
      `<span class="lb-callout-actions">` +
        `<button class="linklike" type="button" data-segment="${esc(r.segment)}">Open segment</button>` +
        `<button class="linklike" type="button" data-plan="${esc(dir)}">Explore in Plan →</button>` +
      `</span>` +
    `</div>`;
  }).join("");
  return `<div class="lb-callout lb-callout-${kind}"><div class="lb-callout-head">` +
    `<strong>${heading}</strong><span class="hint">${hint}</span></div>${items}</div>`;
}

let _payload: LeaderboardPayload | null = null;
let _sort: SortMode = "promise";

function draw(): void {
  const body = $("#lb-body");
  if (!body || !_payload) return;
  const rows = _payload.segments;
  if (!rows.length) {
    body.innerHTML = `<div class="empty-state">No cached segments yet. Pull a segment from ` +
      `<button class="linklike" type="button" data-segment="">Segments</button> to rank it here.</div>`;
    return;
  }
  const gaps = exposureGaps(rows);
  const ordered = rankSegments(rows, _sort);
  const toolbar = SORTS.map((s) =>
    `<button class="lb-sort${s.mode === _sort ? " active" : ""}" type="button" data-sort="${s.mode}">` +
      `${esc(s.label)}${s.mode === _sort ? " \u2193" : ""}</button>`
  ).join("");
  // With few segments the promise/momentum/breadth orders often coincide, so a
  // caption + emphasized metric make it clear WHICH axis is active even when the
  // tile order doesn't visibly change.
  const activeLabel = (SORTS.find((s) => s.mode === _sort)?.label || "promise").toLowerCase();
  const rankedBy = `<span class="lb-rankedby hint">ranked by ${esc(activeLabel)}</span>`;
  const overlapNote = _payload.overlap
    ? `<span class="lb-overlap hint" title="a name can belong to several segments, so exposures don't sum to 100%">segments overlap</span>`
    : "";
  const prev = tileRects(body);
  body.innerHTML =
    calloutHtml("hot", gaps.hot) +
    calloutHtml("cold", gaps.cold) +
    `<div class="lb-toolbar"><div class="lb-sorts">${toolbar}</div>${rankedBy}${overlapNote}</div>` +
    `<div class="lb-grid">${ordered.map((r, i) => leaderboardTileHtml(r, i + 1, _sort)).join("")}</div>`;
  flipReorder(body, prev);
}

// Snapshot each tile's viewport position, keyed by segment, for a FLIP animation.
function tileRects(body: HTMLElement): Map<string, DOMRect> {
  const m = new Map<string, DOMRect>();
  body.querySelectorAll<HTMLElement>(".lb-tile[data-segment]").forEach((t) => {
    m.set(t.dataset.segment || "", t.getBoundingClientRect());
  });
  return m;
}

// Animate tiles from their previous positions to the new ones so a re-sort is
// unmistakable even when only a couple of rows swap. No-op under reduced motion
// or in environments without layout (jsdom reports zero-size rects).
function flipReorder(body: HTMLElement, prev: Map<string, DOMRect>): void {
  if (!prev.size || typeof requestAnimationFrame !== "function") return;
  if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return;
  body.querySelectorAll<HTMLElement>(".lb-tile[data-segment]").forEach((t) => {
    const old = prev.get(t.dataset.segment || "");
    if (!old) return;
    const now = t.getBoundingClientRect();
    const dx = old.left - now.left;
    const dy = old.top - now.top;
    if (!dx && !dy) return;
    t.style.transition = "none";
    t.style.transform = `translate(${dx}px, ${dy}px)`;
    requestAnimationFrame(() => {
      t.style.transition = "transform 260ms cubic-bezier(.2,.7,.3,1)";
      t.style.transform = "";
      t.addEventListener("transitionend", () => { t.style.transition = ""; }, { once: true });
    });
  });
}

async function openSegment(slug: string): Promise<void> {
  if (!slug) { setActiveView("segment"); pushNav({ view: "segment" }); await loadSegmentList(); return; }
  await loadSegmentList();
  await loadCachedSegment(slug, { push: true });
}

function exploreInPlan(direction: string): void {
  pushNav({ view: "strategy" });
  setActiveView("strategy");
  prefillDirection(direction);
}

// Delegated click/sort wiring, (re)bound to the stable #lb-body after each draw.
function wire(): void {
  const body = $("#lb-body");
  if (!body || body.dataset.wired) return;
  body.dataset.wired = "1";
  body.addEventListener("click", (e) => {
    const t = e.target as HTMLElement;
    const sortBtn = t.closest<HTMLElement>(".lb-sort");
    if (sortBtn) {
      _sort = sortBtn.dataset.sort as SortMode;
      replaceViewState({ sort: _sort === "promise" ? "" : _sort });
      draw();
      return;
    }
    const plan = t.closest<HTMLElement>("[data-plan]");
    if (plan) { exploreInPlan(plan.dataset.plan || ""); return; }
    const seg = t.closest<HTMLElement>("[data-segment]");
    if (seg) { void openSegment(seg.dataset.segment || ""); }
  });
}

export async function loadLeaderboard(): Promise<void> {
  const requested = navFromUrl().sort;
  _sort = (["promise", "momentum", "breadth", "gap"] as string[]).includes(requested)
    ? requested as SortMode : "promise";
  const status = $("#lb-status");
  const body = $("#lb-body");
  wire();
  if (status) { status.classList.remove("err"); status.innerHTML = `${spinner()} Ranking cached segments…`; }
  try {
    _payload = await api<LeaderboardPayload>("/api/segments/leaderboard");
    if (status) {
      const n = _payload.segments.length;
      status.textContent = n
        ? `${n} cached segment${n === 1 ? "" : "s"} · ranked from cached pulls only`
        : "";
    }
    draw();
  } catch (e) {
    if (body) body.innerHTML = "";
    loadError(status, "Couldn't load the leaderboard", e);
  }
}
