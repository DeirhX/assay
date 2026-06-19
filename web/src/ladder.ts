// Pure valuation-ladder math, shared by the deep-dive editor and any preview.
// No DOM here so it can be unit-tested directly (tests/ladder.test.ts) and so it
// mirrors the backend normalization in tools/price_levels.py: the editor should
// preview exactly what will lock. A "side" is "buy" (accumulate as price falls)
// or "trim" (lighten as price rises).

export type Side = "buy" | "trim";

export interface Tranche {
  price: number | null;
  size_pct: number | null;
  // Discount (buy) or premium (trim) vs fair value, as a decimal (0.2 = 20%).
  margin_pct?: number | null;
}

function round4(v: number): number {
  return Math.round(v * 1e4) / 1e4;
}

// price = fair * (1 - discount) for a buy, fair * (1 + premium) for a trim.
export function priceFromMargin(fair: number | null, marginPct: number | null, side: Side): number | null {
  if (fair == null || marginPct == null || !isFinite(fair) || !isFinite(marginPct) || fair <= 0) return null;
  const p = side === "buy" ? fair * (1 - marginPct) : fair * (1 + marginPct);
  return isFinite(p) && p > 0 ? round4(p) : null;
}

// The inverse: the margin vs fair value implied by an absolute price.
export function marginFromPrice(fair: number | null, price: number | null, side: Side): number | null {
  if (fair == null || price == null || fair <= 0 || !isFinite(fair) || !isFinite(price)) return null;
  const m = side === "buy" ? (fair - price) / fair : (price - fair) / fair;
  return isFinite(m) ? round4(m) : null;
}

// Normalize raw size fractions to sum to 1, splitting missing/invalid sizes over
// the remaining headroom. Mirrors _build_ladder in price_levels.py.
export function normalizeSizes(sizes: (number | null)[]): number[] {
  if (!sizes.length) return [];
  const out = sizes.map((s) => (typeof s === "number" && isFinite(s) && s > 0 ? s : null)) as (number | null)[];
  const missing = out.map((s, i) => (s == null ? i : -1)).filter((i) => i >= 0);
  if (missing.length) {
    const known = out.reduce((a: number, s) => a + (s ?? 0), 0);
    const remaining = 1 - known;
    const share = remaining > 1e-9 ? remaining / missing.length : 1 / out.length;
    for (const i of missing) out[i] = share;
  }
  const total = out.reduce((a: number, s) => a + (s as number), 0);
  if (total > 0 && Math.abs(total - 1) > 1e-9) {
    return (out as number[]).map((s) => s / total);
  }
  return out as number[];
}

// Sum of provided sizes (treating missing/invalid as 0) — for the live "should
// sum to ~100%" hint in the editor.
export function sizeSum(sizes: (number | null)[]): number {
  return sizes.reduce((a: number, s) => a + (typeof s === "number" && isFinite(s) ? s : 0), 0);
}

// Sort a ladder for display and locking: buy by price descending (shallowest
// discount triggers first as price falls), trim ascending. Returns a new array.
export function sortLadder<T extends { price: number | null }>(tranches: T[], side: Side): T[] {
  return tranches.slice().sort((a, b) => {
    const pa = a.price == null ? Number.NEGATIVE_INFINITY : a.price;
    const pb = b.price == null ? Number.NEGATIVE_INFINITY : b.price;
    return side === "buy" ? pb - pa : pa - pb;
  });
}

// Cumulative active size at the current price (0..1). Mirrors evaluate(): a buy
// tranche is live when price <= its level, a trim tranche when price >= it.
export function activeFraction(tranches: Tranche[], current: number | null, side: Side): number {
  if (current == null || !isFinite(current)) return 0;
  let f = 0;
  for (const t of tranches) {
    if (t.price == null) continue;
    const live = side === "buy" ? current <= t.price : current >= t.price;
    if (live) f += t.size_pct ?? 0;
  }
  return Math.min(1, f);
}

// Whether a locked fair value has drifted from the latest analysis fair value by
// more than `tol` (default 2%) — drives the editor's "re-anchor" staleness banner.
export function fairValueStale(locked: number | null, latest: number | null, tol = 0.02): boolean {
  if (locked == null || latest == null || locked <= 0 || !isFinite(locked) || !isFinite(latest)) return false;
  return Math.abs(latest - locked) / locked > tol;
}

// Per-tranche comparison for the match-check: same count and each price within a
// cent. Compares already-sorted ladders.
export function laddersMatch(a: { price: number | null }[], b: { price: number | null }[]): boolean {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) {
    const pa = a[i].price, pb = b[i].price;
    if (pa == null || pb == null) {
      if (pa !== pb) return false;
      continue;
    }
    if (Math.abs(pa - pb) > 0.005) return false;
  }
  return true;
}
