// Shared band-shift visualization: the horizontal "before (ghost) → after
// (solid, colour-coded)" track used by the working draft and the optimizer
// preview. Extracted from staging.ts so both surfaces speak the same visual
// language on one shared axis. Pure string/markup helpers; no DOM, no fetch.
import { esc } from "./core";
import { axisMax, onAxis, r1 } from "./weight-axis";

export type Band = { low?: number; high?: number; rule?: string; sleeve?: string } | null;

// The minimal row shape the band graphics need: a change kind plus the two
// bands. Both the staging DiffRow and the optimizer's rows satisfy this.
export interface BandRow {
  change: "added" | "modified" | "removed";
  before: Band;
  after: Band;
}

// Turn the model's terse rule verbs into plain words -- the single source of
// truth for rule wording across the planner, staging, optimizer, and strategy
// surfaces (they used to each carry their own near-identical map, and drifted:
// `do_not_add` read "don't add" in some and "hold, don't add" in others). Unknown
// verbs just get their underscores spaced out.
const RULE_WORDS: Record<string, string> = {
  accumulate: "accumulate",
  hold: "hold",
  wait: "wait",
  buy: "buy",
  avoid: "avoid",
  reduce: "reduce",
  trim_only: "trim only",
  do_not_add: "hold, don't add",
};
export const ruleWord = (r?: string): string => (r ? RULE_WORDS[r] || r.replace(/_/g, " ") : "");

export type RuleTone = "good" | "bad" | "warn" | "hold";
const RULE_TONES: Record<string, RuleTone> = {
  accumulate: "good", buy: "good",
  reduce: "bad", trim_only: "bad", avoid: "bad",
  wait: "warn",
  hold: "hold", do_not_add: "hold",
};
export const ruleTone = (r?: string | null): RuleTone => RULE_TONES[r || ""] || "hold";

export function bandText(b: Band): string {
  if (!b) return "—";
  const lo = typeof b.low === "number" ? b.low : "?";
  const hi = typeof b.high === "number" ? b.high : "?";
  const sleeve = b.sleeve ? ` · ${esc(b.sleeve)}` : "";
  return `${lo}–${hi}% ${esc(ruleWord(b.rule))}${sleeve}`;
}

export const midOf = (b: Band): number | null => {
  if (!b) return null;
  const lo = typeof b.low === "number" ? b.low : null;
  const hi = typeof b.high === "number" ? b.high : null;
  if (lo != null && hi != null) return (lo + hi) / 2;
  return lo != null ? lo : hi;
};

// One shared axis for every bar in the list so a 0–8% band and a 10–18% band
// are visually comparable, not each stretched to fill its own row (see
// weight-axis: round up to a friendly multiple of 5, 10% floor).
export function scaleMaxFor(rows: BandRow[]): number {
  const vals: Array<number | null | undefined> = [];
  for (const r of rows) {
    for (const b of [r.before, r.after]) {
      if (b) vals.push(b.high, b.low);
    }
  }
  return axisMax(vals);
}

// Project a band onto the [0, scaleMax] axis as left/width/mid percentages.
function bandSeg(b: Band, scaleMax: number): { left: number; width: number; mid: number } | null {
  if (!b) return null;
  let lo = typeof b.low === "number" ? b.low : null;
  let hi = typeof b.high === "number" ? b.high : null;
  if (lo == null && hi == null) return null;
  if (lo == null) lo = hi as number;
  if (hi == null) hi = lo as number;
  const left = onAxis(lo, scaleMax);
  const right = onAxis(hi, scaleMax);
  const width = Math.max(2, right - left); // keep a hairline band visible
  return { left, width, mid: (left + right) / 2 };
}

// Plain-language direction of a change, so a user reads "trimmed" instead of
// decoding "10–12% → 0–7.7%".
export function directionTag(r: BandRow): { label: string; tone: "ok" | "warn" | "bad" } {
  if (r.change === "added") return { label: "new", tone: "ok" };
  if (r.change === "removed") return { label: "dropped", tone: "bad" };
  const a = midOf(r.before);
  const b = midOf(r.after);
  if (a != null && b != null) {
    if (b > a + 0.05) return { label: "raised", tone: "ok" };
    if (b < a - 0.05) return { label: "trimmed", tone: "warn" };
  }
  if ((r.before && r.before.rule) !== (r.after && r.after.rule)) return { label: "rule change", tone: "warn" };
  return { label: "tweaked", tone: "warn" };
}

// The headline graphic: a horizontal track showing where this name's target sits
// before (ghost) and after (solid, colour-coded by direction) on the shared
// axis, so a trim, a raise, a brand-new band or a drop all read at a glance.
export function bandBar(r: BandRow, scaleMax: number, opts?: { axis?: boolean }): string {
  const dir = directionTag(r);
  const before = bandSeg(r.before, scaleMax);
  const after = bandSeg(r.after, scaleMax);
  const ghostTone = r.change === "removed" ? "bad" : "neutral";
  const ghost = before
    ? `<span class="band-seg band-ghost tone-${ghostTone}" style="left:${r1(before.left)}%;width:${r1(before.width)}%"></span>`
    : "";
  const live = after
    ? `<span class="band-seg band-live tone-${dir.tone}" style="left:${r1(after.left)}%;width:${r1(after.width)}%"></span>`
    : "";
  let conn = "";
  if (before && after && Math.abs(after.mid - before.mid) > 0.5) {
    const a = Math.min(before.mid, after.mid);
    const w = Math.abs(after.mid - before.mid);
    conn = `<span class="band-conn tone-${dir.tone}" style="left:${r1(a)}%;width:${r1(w)}%"></span>`;
  }
  const afterMark = after
    ? `<span class="band-mark tone-${dir.tone}" style="left:${r1(after.mid)}%"></span>`
    : "";
  const label = `${bandText(r.before)} to ${bandText(r.after)}`;
  // The axis (0%…max labels) is worth showing once per list, not per row; callers
  // rendering many rows can drop it and show a single shared legend instead.
  const axis = opts?.axis === false ? "" : `<div class="band-axis"><span>0%</span><span>${scaleMax}%</span></div>`;
  return `<div class="band-viz">
    <div class="band-track" role="img" aria-label="${esc(label)}">${ghost}${conn}${live}${afterMark}</div>
    ${axis}
  </div>`;
}

// ---- current → projected position track ------------------------------------
// The horizontal weight track shared by the rebalance planner (draggable),
// target-state comparison (static), and trade-desk band preview (static). Same
// .reb-* styling everywhere so a trim, a raise, and an in-band land read
// identically across surfaces.

/** Stable class names the draggable planner queries after paint. */
export const POSITION_TRACK_SEL = {
  track: "reb-track",
  zone: "reb-zone",
  conn: "reb-conn",
  curMark: "reb-cur-mark",
  projMark: "reb-proj-mark",
  axis: "reb-axis",
} as const;

export type PositionTrackRole = "img" | "group";

export interface PositionTrackBand {
  low: number;
  high: number;
}

export interface PositionTrackSpec {
  scaleMax: number;
  band: PositionTrackBand;
  current: number | null;
  projected: number | null;
  ariaLabel: string;
  opts?: {
    /** `group` for draggable planner rows; `img` for read-only surfaces. */
    role?: PositionTrackRole;
    /** When false, omit the projected tick (target-state unchanged rows). */
    showProjected?: boolean;
    /** Colour the connector: auto infers buy/sell from current vs projected. */
    connTone?: "none" | "auto" | "buy" | "sell";
    /** When false, omit the current→projected connector (unchanged target-state rows). */
    showConn?: boolean;
    inBand?: boolean;
    showAxis?: boolean;
    currentTitle?: string;
    projectedTitle?: string;
  };
}

/** Axis max for a set of position tracks (band edges + current + projected). */
export function positionTrackScaleMax(
  values: Array<number | null | undefined>,
): number {
  return axisMax(values);
}

/** Band zone geometry on the shared axis. */
export function bandZoneGeom(
  band: PositionTrackBand,
  scaleMax: number,
): { left: number; width: number } {
  const low = typeof band.low === "number" ? band.low : 0;
  const high = typeof band.high === "number" ? band.high : low;
  const zL = onAxis(low, scaleMax);
  return { left: zL, width: Math.max(1.5, onAxis(high, scaleMax) - zL) };
}

/** Connector bar between current and projected ticks (0..100 axis percentages). */
export const connectorGeom = (curP: number, projP: number) =>
  ({ left: Math.min(curP, projP), width: Math.abs(projP - curP) });

function connToneClass(
  tone: "none" | "auto" | "buy" | "sell" | undefined,
  cur: number | null,
  proj: number | null,
): string {
  if (tone === "buy") return " buy";
  if (tone === "sell") return " sell";
  if (tone === "auto" && cur != null && proj != null) {
    return proj > cur ? " buy" : proj < cur ? " sell" : "";
  }
  return "";
}

/**
 * Build the position track markup. Returns the track HTML plus stable selectors
 * for imperative updates (rebalance planner drag/recompute).
 */
export function positionTrackHtml(spec: PositionTrackSpec): {
  html: string;
  refs: typeof POSITION_TRACK_SEL;
  geom: { curP: number | null; projP: number | null };
} {
  const o = spec.opts || {};
  const role = o.role || "img";
  const showProj = o.showProjected !== false && spec.projected != null;
  const zone = bandZoneGeom(spec.band, spec.scaleMax);
  const curP = spec.current != null ? onAxis(spec.current, spec.scaleMax) : null;
  const projP = spec.projected != null ? onAxis(spec.projected, spec.scaleMax) : null;
  const connCls = connToneClass(o.connTone ?? "none", spec.current, spec.projected);
  const showConn = o.showConn ?? (curP != null && projP != null && showProj);
  const connGeom = curP != null && projP != null ? connectorGeom(curP, projP) : null;
  const conn = showConn && connGeom
    ? `<span class="${POSITION_TRACK_SEL.conn}${connCls}" style="left:${r1(connGeom.left)}%;width:${r1(connGeom.width)}%"></span>`
    : "";
  const curTitle = o.currentTitle
    ?? (spec.current != null ? `current ${spec.current.toFixed(2)}%` : "");
  const curMark = curP != null
    ? `<span class="${POSITION_TRACK_SEL.curMark}" style="left:${r1(curP)}%"` +
      (curTitle ? ` title="${esc(curTitle)}"` : "") + `></span>`
    : "";
  const inBand = o.inBand !== false;
  const projTitle = o.projectedTitle
    ?? (spec.projected != null ? `projected ${spec.projected.toFixed(2)}%` : "");
  const projMark = showProj && projP != null
    ? `<span class="${POSITION_TRACK_SEL.projMark} ${inBand ? "in" : "out"}" style="left:${r1(projP)}%"` +
      (projTitle ? ` title="${esc(projTitle)}"` : "") + `></span>`
    : "";
  const track =
    `<div class="${POSITION_TRACK_SEL.track}" role="${role}" aria-label="${esc(spec.ariaLabel)}">` +
    `<span class="${POSITION_TRACK_SEL.zone}" style="left:${r1(zone.left)}%;width:${r1(zone.width)}%"></span>` +
    conn + curMark + projMark +
    `</div>`;
  const axis = o.showAxis
    ? `<div class="${POSITION_TRACK_SEL.axis}"><span>0%</span><span>${spec.scaleMax}%</span></div>`
    : "";
  return {
    html: track + axis,
    refs: POSITION_TRACK_SEL,
    geom: { curP, projP },
  };
}
