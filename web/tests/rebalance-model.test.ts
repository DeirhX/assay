// Tests for the pure rebalance plan model (extracted from renderRebalance so
// the planner's arithmetic is testable without a DOM). The invariants that
// matter: raised/spent/net tally every section, band membership uses the same
// ±0.01 tolerance the backend shows, member caps flag but never block, and the
// staged-trades builder drops noise and zero-CZK entries.
import { describe, expect, it } from "vitest";
import {
  computePlan, connectorGeom, inBandAfter, parseDelta, rebScaleMax, scalePct, tradesFrom,
} from "../src/rebalance-model";

describe("parseDelta", () => {
  it("treats a mid-edit input ('', '-', garbage) as zero", () => {
    expect(parseDelta("")).toBe(0);
    expect(parseDelta("-")).toBe(0);
    expect(parseDelta("abc")).toBe(0);
  });
  it("parses signed decimals", () => {
    expect(parseDelta("-2.5")).toBe(-2.5);
    expect(parseDelta("0.1")).toBe(0.1);
  });
});

describe("inBandAfter", () => {
  it("counts a projection within a hundredth of an edge as inside", () => {
    expect(inBandAfter(2.995, 3, 6)).toBe(true);   // 0.005 under the floor
    expect(inBandAfter(6.005, 3, 6)).toBe(true);   // 0.005 over the ceiling
    expect(inBandAfter(2.98, 3, 6)).toBe(false);
    expect(inBandAfter(6.02, 3, 6)).toBe(false);
  });
});

describe("rebScaleMax (shared position axis)", () => {
  it("rounds the largest band edge / weight / projection up to a multiple of 5", () => {
    const rows = [
      { high: 8, current_pct: 6, suggest_delta_pct: 0 },
      { high: 4, current_pct: 11, suggest_delta_pct: 2 }, // projection 13 wins
    ];
    expect(rebScaleMax(rows)).toBe(15);
  });
  it("floors at 10% so a book of small bands still reads", () => {
    expect(rebScaleMax([{ high: 2, current_pct: 1, suggest_delta_pct: 0 }])).toBe(10);
    expect(rebScaleMax([])).toBe(10);
  });
});

describe("track geometry", () => {
  it("maps weights onto the axis and clamps to 0..100", () => {
    expect(scalePct(5, 20)).toBe(25);
    expect(scalePct(-1, 20)).toBe(0);
    expect(scalePct(30, 20)).toBe(100);
  });
  it("spans the connector between current and projected regardless of direction", () => {
    expect(connectorGeom(25, 40)).toEqual({ left: 25, width: 15 });
    expect(connectorGeom(40, 25)).toEqual({ left: 25, width: 15 });
  });
});

describe("computePlan", () => {
  const BASE = 1_000_000; // invested book in CZK

  it("projects each target row and grades band membership", () => {
    const comp = computePlan(
      [
        { current: 2, low: 3, high: 6, delta: 1.5 },   // 3.5 -> in band
        { current: 8, low: 3, high: 6, delta: 0 },     // stays above
      ],
      [], [], BASE);
    expect(comp.rows[0]).toMatchObject({ inBand: true, czk: 15_000 });
    expect(comp.rows[0].proj).toBeCloseTo(3.5 / 1.015);
    expect(comp.rows[1].proj).toBeCloseTo(8 / 1.015);
    expect(comp.totals.closed).toBe(1);
    expect(comp.totals.total).toBe(2);
  });

  it("tallies raised (trims) and spent (buys) across all three sections", () => {
    const comp = computePlan(
      [{ current: 5, low: 3, high: 6, delta: 1 }],                       // buy 1%
      [{ current: 4, low: 3, high: 6, members: [
        { cur: 2, target: 3, cap: null, delta: 0.5 },                    // buy 0.5%
        { cur: 2, target: 1, cap: null, delta: -0.25 },                  // trim 0.25%
      ] }],
      [-2],                                                              // untargeted trim 2%
      BASE);
    const t = comp.totals;
    expect(t.spent).toBeCloseTo(1.5);
    expect(t.raised).toBeCloseTo(2.25);
    expect(t.net).toBeCloseTo(0.75);
    expect(t.netCzk).toBe(7_500);
    expect(t.fundMax).toBeCloseTo(2.25); // shared bar scale = max(raised, spent)
  });

  it("aggregates sleeve members into the sleeve's projected band position", () => {
    const comp = computePlan([], [
      { current: 4, low: 3, high: 6, members: [
        { cur: 2, target: 3, cap: 3, delta: 1.2 },   // 3.2 > cap 3 -> flagged
        { cur: 2, target: 2.5, cap: null, delta: 0.6 }, // 2.6 >= target -> atTarget
      ] },
    ], [], BASE);
    const s = comp.sleeves[0];
    expect(s.sum).toBeCloseTo(1.8);
    expect(s.proj).toBeCloseTo(5.8 / 1.018);
    expect(s.inBand).toBe(true);
    expect(s.members[0].overCap).toBe(true);
    expect(s.members[0].atTarget).toBe(false); // over cap never reads as "good"
    expect(s.members[1]).toMatchObject({ overCap: false, atTarget: true });
  });

  it("uses one final invested denominator across targets and sleeves", () => {
    const comp = computePlan(
      [{ current: 10, low: 8, high: 10, delta: -1 }],
      [{ current: 2, low: 3, high: 5, members: [
        { cur: 2, target: 3, cap: null, delta: 2 },
      ] }],
      [],
      BASE,
    );
    // Net buy is +1% of the original book, so every after-weight uses 101%.
    expect(comp.rows[0].proj).toBeCloseTo(9 / 1.01);
    expect(comp.sleeves[0].proj).toBeCloseTo(4 / 1.01);
    expect(comp.totals.total).toBe(2);
    expect(comp.totals.closed).toBe(2);
  });

  it("sizes CZK off the invested base, and returns null money without one", () => {
    const withBase = computePlan([{ current: 5, low: 3, high: 6, delta: -2 }], [], [], BASE);
    expect(withBase.rows[0].czk).toBe(-20_000);
    const noBase = computePlan([{ current: 5, low: 3, high: 6, delta: -2 }], [], [], null);
    expect(noBase.rows[0].czk).toBeNull();
    expect(noBase.totals.netCzk).toBeNull();
  });
});

describe("tradesFrom (staged basket)", () => {
  const BASE = 1_000_000;

  it("keeps every edited amount as a CZK trade, in order", () => {
    const trades = tradesFrom(
      [{ symbol: "AAA", delta: 1 }, { symbol: "BBB", delta: -0.5 }], BASE);
    expect(trades).toEqual([
      { symbol: "AAA", delta_czk: 10_000 },
      { symbol: "BBB", delta_czk: -5_000 },
    ]);
  });

  it("drops noise-floor deltas and zero-CZK entries", () => {
    expect(tradesFrom([{ symbol: "DUST", delta: 0.0005 }], BASE)).toEqual([]);
    expect(tradesFrom([{ symbol: "NOBASE", delta: 1 }], null)).toEqual([]);
    // A delta that rounds to 0 CZK is not a trade either.
    expect(tradesFrom([{ symbol: "TINY", delta: 0.002 }], 100)).toEqual([]);
  });
});
