// The shared weight-axis geometry: the rebalance planner, trade desk, band viz,
// and target-state comparison all round the largest weight up to a friendly 5%
// multiple (10% floor) and map weights onto that axis identically. These lock
// that shared behaviour so the four consumers can't drift apart again.
import { describe, expect, it } from "vitest";
import { axisMax, clampPct, onAxis, r1 } from "../src/weight-axis";

describe("axisMax", () => {
  it("rounds the largest value up to a multiple of 5", () => {
    expect(axisMax([7, 8.2, 6.9])).toBe(10);
    expect(axisMax([22, 18, 20])).toBe(25);
    expect(axisMax([12])).toBe(15);
  });
  it("floors at 10 for small or empty inputs", () => {
    expect(axisMax([2, 1, 1.5])).toBe(10);
    expect(axisMax([])).toBe(10);
    expect(axisMax([0])).toBe(10);
  });
  it("ignores null/undefined/NaN entries", () => {
    expect(axisMax([null, undefined, NaN, 22])).toBe(25);
    expect(axisMax([null, undefined])).toBe(10);
  });
});

describe("onAxis", () => {
  it("maps a weight onto the 0..scaleMax axis as a clamped percentage", () => {
    expect(onAxis(5, 20)).toBe(25);
    expect(onAxis(20, 20)).toBe(100);
  });
  it("clamps out-of-range values to [0, 100]", () => {
    expect(onAxis(-1, 20)).toBe(0);
    expect(onAxis(30, 20)).toBe(100);
  });
});

describe("r1 / clampPct", () => {
  it("r1 rounds to one decimal", () => {
    expect(r1(33.333)).toBe(33.3);
    expect(r1(2.05)).toBeCloseTo(2.1, 5);
  });
  it("clampPct bounds to [0, 100]", () => {
    expect(clampPct(-5)).toBe(0);
    expect(clampPct(150)).toBe(100);
    expect(clampPct(42.5)).toBe(42.5);
  });
});
