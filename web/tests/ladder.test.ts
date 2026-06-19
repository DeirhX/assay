// Tests for the pure valuation-ladder math (src/ladder.ts): margin<->price
// conversion, size normalization, active-fraction grading, staleness, and the
// per-tranche match-check. These mirror the backend normalization in
// tools/price_levels.py so the deep-dive editor previews exactly what locks.
import { describe, expect, it } from "vitest";
import {
  activeFraction,
  fairValueStale,
  laddersMatch,
  marginFromPrice,
  normalizeSizes,
  priceFromMargin,
  sizeSum,
  sortLadder,
} from "../src/ladder";

describe("priceFromMargin / marginFromPrice", () => {
  it("derives a buy price below fair value and round-trips", () => {
    expect(priceFromMargin(400, 0.2, "buy")).toBe(320);
    expect(marginFromPrice(400, 320, "buy")).toBeCloseTo(0.2, 6);
  });

  it("derives a trim price above fair value and round-trips", () => {
    expect(priceFromMargin(400, 0.25, "trim")).toBe(500);
    expect(marginFromPrice(400, 500, "trim")).toBeCloseTo(0.25, 6);
  });

  it("returns null on missing or non-positive inputs", () => {
    expect(priceFromMargin(null, 0.2, "buy")).toBeNull();
    expect(priceFromMargin(400, null, "buy")).toBeNull();
    expect(marginFromPrice(0, 320, "buy")).toBeNull();
  });
});

describe("normalizeSizes / sizeSum", () => {
  it("scales explicit sizes to sum to 1", () => {
    const out = normalizeSizes([2, 2]);
    expect(out).toEqual([0.5, 0.5]);
  });

  it("splits missing sizes over the remaining headroom", () => {
    const out = normalizeSizes([0.6, null]);
    expect(out[0]).toBeCloseTo(0.6, 6);
    expect(out[1]).toBeCloseTo(0.4, 6);
    expect(out.reduce((a, b) => a + b, 0)).toBeCloseTo(1, 6);
  });

  it("splits equally when all sizes are missing", () => {
    const out = normalizeSizes([null, null, null]);
    out.forEach((s) => expect(s).toBeCloseTo(1 / 3, 6));
  });

  it("sizeSum treats missing/invalid as zero", () => {
    expect(sizeSum([0.5, null, 0.3])).toBeCloseTo(0.8, 6);
  });
});

describe("sortLadder", () => {
  it("sorts buys descending (shallowest discount first) and trims ascending", () => {
    const buy = sortLadder([{ price: 320 }, { price: 360 }], "buy");
    expect(buy.map((t) => t.price)).toEqual([360, 320]);
    const trim = sortLadder([{ price: 600 }, { price: 500 }], "trim");
    expect(trim.map((t) => t.price)).toEqual([500, 600]);
  });
});

describe("activeFraction", () => {
  const buy = [
    { price: 360, size_pct: 0.5 },
    { price: 320, size_pct: 0.3 },
    { price: 280, size_pct: 0.2 },
  ];
  it("is zero above all buy tranches", () => {
    expect(activeFraction(buy, 380, "buy")).toBe(0);
  });
  it("accumulates as price falls through tranches", () => {
    expect(activeFraction(buy, 340, "buy")).toBeCloseTo(0.5, 6);
    expect(activeFraction(buy, 300, "buy")).toBeCloseTo(0.8, 6);
    expect(activeFraction(buy, 100, "buy")).toBeCloseTo(1, 6);
  });
  it("accumulates as price rises through trim tranches", () => {
    const trim = [{ price: 500, size_pct: 0.6 }, { price: 600, size_pct: 0.4 }];
    expect(activeFraction(trim, 450, "trim")).toBe(0);
    expect(activeFraction(trim, 550, "trim")).toBeCloseTo(0.6, 6);
    expect(activeFraction(trim, 650, "trim")).toBeCloseTo(1, 6);
  });
  it("is zero when the price is unknown", () => {
    expect(activeFraction(buy, null, "buy")).toBe(0);
  });
});

describe("fairValueStale", () => {
  it("flags drift beyond the tolerance", () => {
    expect(fairValueStale(400, 440)).toBe(true); // +10%
    expect(fairValueStale(400, 405)).toBe(false); // ~1%
  });
  it("is false when either value is missing", () => {
    expect(fairValueStale(null, 440)).toBe(false);
    expect(fairValueStale(400, null)).toBe(false);
  });
});

describe("laddersMatch", () => {
  it("matches identical price ladders", () => {
    expect(laddersMatch([{ price: 360 }, { price: 320 }], [{ price: 360 }, { price: 320 }])).toBe(true);
  });
  it("differs on length or price", () => {
    expect(laddersMatch([{ price: 360 }], [{ price: 360 }, { price: 320 }])).toBe(false);
    expect(laddersMatch([{ price: 360 }], [{ price: 361 }])).toBe(false);
  });
});
