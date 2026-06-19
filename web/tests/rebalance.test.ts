// Tests for the rebalance planner's default-amount rule, which is what excludes
// price-gated rows from the staged basket: a "wait" action (set by the backend
// when a locked price trigger blocks the side) seeds the plan input at 0, so a
// gated buy/trim isn't staged unless the human types an override.
import { describe, expect, it } from "vitest";
import { rebActionClass, rebDefaultDelta } from "../src/rebalance";

describe("rebDefaultDelta", () => {
  it("prefills the band-closing amount for clear buy/trim actions", () => {
    expect(rebDefaultDelta({ action: "buy", suggest_delta_pct: 1.5 })).toBe(1.5);
    expect(rebDefaultDelta({ action: "trim", suggest_delta_pct: -2.0 })).toBe(-2.0);
  });

  it("zeroes a price-gated 'wait' row so it is excluded from the staged basket", () => {
    // The backend downgraded the action to wait; the suggested band delta is
    // still present, but the default amount must be 0 (not staged by default).
    expect(rebDefaultDelta({ action: "wait", suggest_delta_pct: 1.5 })).toBe(0);
  });

  it("zeroes review/none rows too (judgement calls)", () => {
    expect(rebDefaultDelta({ action: "review", suggest_delta_pct: 3 })).toBe(0);
    expect(rebDefaultDelta({ action: null, suggest_delta_pct: 0 })).toBe(0);
  });
});

describe("rebActionClass", () => {
  it("maps a gated 'wait' action to the muted (non-trade) style", () => {
    expect(rebActionClass("wait")).toBe("muted");
    expect(rebActionClass("buy")).toBe("good");
    expect(rebActionClass("trim")).toBe("bad");
  });
});
