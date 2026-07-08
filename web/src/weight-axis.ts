// Shared weight-axis geometry for the band / position tracks the app draws in
// four places: the rebalance planner (rebalance-model), the trade desk
// (trade-model), the before→after band viz used by the working draft + optimizer
// + strategy (band-viz), and the target-state comparison (targetstate). Each one
// rounded the largest weight up to a friendly 5% multiple (10% floor), mapped
// weights onto that 0..max axis with the same clamp, and rounded inline-style
// percentages to one decimal — every one carrying a private copy that could only
// drift by accident. This is the single source of that geometry. Pure: no DOM,
// no fetch, no state.

// Round to one decimal for inline-style percentages, keeping generated CSS terse
// and stable across renders.
export const r1 = (n: number): number => Math.round(n * 10) / 10;

// Clamp a percentage onto [0, 100].
export const clampPct = (v: number): number => Math.max(0, Math.min(100, v));

// One shared axis for a set of bars so a 0–8% band and a 10–18% band are
// visually comparable rather than each stretched to fill its own row. Round the
// largest value up to a friendly multiple of 5, with a 10% floor so a book of
// small bands still reads. Non-finite/empty inputs floor to 10.
export function axisMax(values: Array<number | null | undefined>): number {
  let max = 0;
  for (const v of values) {
    if (typeof v === "number" && Number.isFinite(v)) max = Math.max(max, v);
  }
  return Math.max(10, Math.ceil(max / 5) * 5);
}

// Project a weight onto the [0, scaleMax] axis as a clamped 0..100 percentage.
export const onAxis = (v: number, scaleMax: number): number => clampPct((v / scaleMax) * 100);
