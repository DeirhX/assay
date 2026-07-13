// Pure plan math for the rebalance planner. No DOM, no fetch — everything here
// is a function of (plan rows, edited deltas, invested base), so the arithmetic
// the planner shows can be unit-tested without rendering a single element.
// rebalance.ts owns the DOM: it reads the inputs, calls computePlan(), and
// paints the results. Keep it that way — if a new number needs to appear in the
// planner, derive it here first.
import type { CashBlock, PlanRow, WhatifTrade } from "./api-types";
import { axisMax, clampPct, onAxis, r1 } from "./weight-axis";

// Band membership tolerance: weights are shown to 2 decimals, so a projection
// within a hundredth of a band edge counts as inside (mirrors the backend).
export const BAND_EPS = 0.01;
// Edits smaller than this are treated as "no trade" everywhere (inputs step in
// tenths of a percent; 0.001 only ever appears via float noise).
export const DELTA_EPS = 0.001;

// r1/clampPct now live in the shared weight-axis module; re-export so existing
// planner call sites keep importing them from here.
export { r1, clampPct };

// An <input type=number> can hold "", "-", or garbage mid-edit; the plan math
// treats all of those as zero rather than poisoning the totals with NaN.
export const parseDelta = (raw: string): number => {
  const d = parseFloat(raw);
  return Number.isFinite(d) ? d : 0;
};

// Weights are percent of the invested book, so size money off invested value
// (not NAV) — that keeps a row's CZK equal to its actual market value.
export const pctToCzk = (pct: number | null | undefined, base: number | null | undefined) =>
  (typeof base === "number" && pct != null ? Math.round((pct / 100) * base) : null);

// Default planned amount: prefill the minimal band-closing trade only for clear
// trim/buy actions. "review" (accumulate over ceiling) and untargeted names are
// judgement calls, so they start at zero — the human decides.
export const rebDefaultDelta = (r: Pick<PlanRow, "action" | "suggest_delta_pct">) =>
  (r.action === "trim" || r.action === "buy" ? r.suggest_delta_pct : 0);

export const inBandAfter = (proj: number, low: number, high: number) =>
  proj >= low - BAND_EPS && proj <= high + BAND_EPS;

// Invert projectWeight() for one edited row while every other planned delta
// stays fixed. A buy changes both the position numerator and the invested-book
// denominator, so target-current is only an approximation.
export function deltaForProjectedWeight(
  projected: number,
  current: number,
  otherDeltas: number,
): number {
  const target = Math.max(0, projected);
  const divisor = 1 - target / 100;
  if (divisor <= DELTA_EPS) return 0;
  return (target * (1 + otherDeltas / 100) - current) / divisor;
}

// One axis for the whole plan: the largest band edge / weight / projection,
// rounded up to a friendly multiple of 5 with a 10% floor (see weight-axis).
export function rebScaleMax(rows: Pick<PlanRow, "high" | "current_pct" | "suggest_delta_pct">[]): number {
  const vals: number[] = [];
  for (const r of rows) {
    vals.push(r.high, r.current_pct, (r.current_pct || 0) + (r.suggest_delta_pct || 0));
  }
  return axisMax(vals);
}

// ---- track geometry ---------------------------------------------------------
// The Position track maps weights onto a shared 0..scaleMax axis; this is the
// shared projection (kept under the planner's own name for its call sites).
export const scalePct = onAxis;

// The current→projected connector bar: spans between the two ticks.
export { connectorGeom } from "./band-viz";

// ---- the edited plan --------------------------------------------------------
// Inputs mirror what the DOM holds (a delta per editable row); results carry
// everything the DOM needs to paint, so recompute() contains no arithmetic.

export interface RowInput { current: number; low: number; high: number; delta: number; }
export interface RowResult { delta: number; proj: number; inBand: boolean; czk: number | null; }

export interface MemberInput { cur: number; target: number; cap: number | null; delta: number; }
export interface MemberResult {
  delta: number;
  proj: number;
  overCap: boolean;   // projected past its member cap
  atTarget: boolean;  // reached its target share (and not over cap)
  czk: number | null;
}

export interface SleeveInput { current: number; low: number; high: number; members: MemberInput[]; }
export interface SleeveResult { sum: number; proj: number; inBand: boolean; members: MemberResult[]; }

export interface PlanTotals {
  raised: number;         // % of book freed by trims (all sections)
  spent: number;          // % of book consumed by buys (all sections)
  net: number;            // raised - spent; negative needs fresh cash
  closed: number;         // interactive target rows projected in-band
  total: number;          // interactive target rows
  raisedCzk: number | null;
  spentCzk: number | null;
  netCzk: number | null;
  fundMax: number;        // shared scale for the freed/needed bars
}

export interface PlanComputation {
  rows: RowResult[];
  sleeves: SleeveResult[];
  untargeted: { delta: number; czk: number | null }[];
  totals: PlanTotals;
}

export function computePlan(
  rows: RowInput[],
  sleeves: SleeveInput[],
  untargetedDeltas: number[],
  base: number | null | undefined,
): PlanComputation {
  let raised = 0, spent = 0, closed = 0;

  const tally = (d: number) => { if (d < 0) raised += -d; else spent += d; };
  rows.forEach((row) => tally(row.delta));
  sleeves.forEach((sleeve) => sleeve.members.forEach((member) => tally(member.delta)));
  untargetedDeltas.forEach(tally);
  const denominator = 1 + (spent - raised) / 100;
  const projectWeight = (current: number, delta: number) =>
    denominator > DELTA_EPS ? Math.max(0, current + delta) / denominator : 0;

  const rowResults = rows.map((r): RowResult => {
    const proj = projectWeight(r.current, r.delta);
    const within = inBandAfter(proj, r.low, r.high);
    if (within) closed += 1;
    return { delta: r.delta, proj, inBand: within, czk: pctToCzk(r.delta, base) };
  });

  const sleeveResults = sleeves.map((s): SleeveResult => {
    let sum = 0;
    const members = s.members.map((m): MemberResult => {
      sum += m.delta;
      const proj = projectWeight(m.cur, m.delta);
      const overCap = m.cap != null && proj > m.cap + BAND_EPS;
      return {
        delta: m.delta,
        proj,
        overCap,
        atTarget: !overCap && proj >= m.target - BAND_EPS,
        czk: pctToCzk(m.delta, base),
      };
    });
    const proj = projectWeight(s.current, sum);
    const within = inBandAfter(proj, s.low, s.high);
    if (within) closed += 1;
    return { sum, proj, inBand: within, members };
  });

  const untargeted = untargetedDeltas.map((d) => {
    return { delta: d, czk: pctToCzk(d, base) };
  });

  const net = raised - spent;
  return {
    rows: rowResults,
    sleeves: sleeveResults,
    untargeted,
    totals: {
      raised, spent, net,
      closed, total: rows.length + sleeves.length,
      raisedCzk: pctToCzk(raised, base),
      spentCzk: pctToCzk(spent, base),
      netCzk: pctToCzk(net, base),
      fundMax: Math.max(raised, spent, 0.01),
    },
  };
}

// The staged basket: every edited amount that survives the noise floor becomes
// a CZK trade. One definition shared by Simulate and the funding "keep the
// user's edits" exclusion, so they can't disagree about what counts as a trade.
export function tradesFrom(
  entries: { symbol: string; delta: number }[],
  base: number | null | undefined,
): WhatifTrade[] {
  const trades: WhatifTrade[] = [];
  for (const e of entries) {
    if (!Number.isFinite(e.delta) || Math.abs(e.delta) < DELTA_EPS) continue;
    const czk = pctToCzk(e.delta, base);
    if (czk == null || czk === 0) continue;
    trades.push({ symbol: e.symbol, delta_czk: czk });
  }
  return trades;
}

// Projected cash after the currently-edited plan: current cash plus the plan's
// net CZK, graded against the cash band (% of NAV). Null = no cash data.
export function projectedCash(cash: CashBlock | null | undefined, netCzk: number | null) {
  if (!cash || typeof cash.nav !== "number" || cash.nav <= 0) return null;
  const czk = cash.czk + (netCzk || 0);
  const pct = (czk / cash.nav) * 100;
  const cls = pct < cash.low - BAND_EPS ? "bad" : pct > cash.high + BAND_EPS ? "warn" : "good";
  return { czk, pct, cls };
}

// How much fresh cash the edited plan still needs: the net shortfall (buys
// minus trims) less whatever cash sits above the cash-band floor. 0 = the plan
// self-funds.
export function fundingNeededCzk(netCzk: number | null, cash: CashBlock | null | undefined): number {
  const shortfall = -(netCzk || 0);
  if (shortfall <= 0) return 0;
  const headroom = cash && typeof cash.nav === "number" && cash.nav > 0
    ? Math.max(0, cash.czk - (cash.low / 100) * cash.nav)
    : 0;
  return Math.max(0, Math.round(shortfall - headroom));
}
