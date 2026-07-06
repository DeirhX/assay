// Shared band-shift visualization: the horizontal "before (ghost) → after
// (solid, colour-coded)" track used by the working draft and the optimizer
// preview. Extracted from staging.ts so both surfaces speak the same visual
// language on one shared axis. Pure string/markup helpers; no DOM, no fetch.
import { esc } from "./core";

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
// are visually comparable, not each stretched to fill its own row. Round up to a
// friendly multiple of 5, with a 10% floor so a book of small bands still reads.
export function scaleMaxFor(rows: BandRow[]): number {
  let max = 0;
  for (const r of rows) {
    for (const b of [r.before, r.after]) {
      if (b && typeof b.high === "number") max = Math.max(max, b.high);
      if (b && typeof b.low === "number") max = Math.max(max, b.low);
    }
  }
  return Math.max(10, Math.ceil(max / 5) * 5);
}

// Project a band onto the [0, scaleMax] axis as left/width/mid percentages.
function bandSeg(b: Band, scaleMax: number): { left: number; width: number; mid: number } | null {
  if (!b) return null;
  let lo = typeof b.low === "number" ? b.low : null;
  let hi = typeof b.high === "number" ? b.high : null;
  if (lo == null && hi == null) return null;
  if (lo == null) lo = hi as number;
  if (hi == null) hi = lo as number;
  const clamp = (v: number) => Math.max(0, Math.min(100, (v / scaleMax) * 100));
  const left = clamp(lo);
  const right = clamp(hi);
  const width = Math.max(2, right - left); // keep a hairline band visible
  return { left, width, mid: (left + right) / 2 };
}

const r1 = (n: number) => Math.round(n * 10) / 10;

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
