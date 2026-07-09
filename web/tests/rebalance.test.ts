// Tests for the rebalance planner's default-amount rule, which is what excludes
// price-gated rows from the staged basket: a "wait" action (set by the backend
// when a locked price trigger blocks the side) seeds the plan input at 0, so a
// gated buy/trim isn't staged unless the human types an override.
import { describe, expect, it } from "vitest";
import { fundingCardHtml, fundingNeededCzk, optionsLine, projectedCash, rebActionClass, rebDefaultDelta } from "../src/rebalance";
import type { FundingCandidate, PendingOptionExposure } from "../src/api-types";

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

describe("optionsLine (pending option-exposure annotation)", () => {
  const base: PendingOptionExposure = {
    long_pct: 3.5, short_pct: 0, net_pct: 3.5, contracts: 2,
    label: "KLAC short 2× 238P", legs: [],
  };

  it("returns null without exposure", () => {
    expect(optionsLine(null)).toBeNull();
    expect(optionsLine(undefined)).toBeNull();
  });

  it("flags a fully covered buy and warns against doubling up", () => {
    const line = optionsLine({ ...base, covers: "full", gap_pct: 2.5, full_suggest_delta_pct: 2.5 })!;
    expect(line.className).toContain("reb-opt-full");
    expect(line.textContent).toContain("Covered by options");
    expect(line.textContent).toContain("hold off");
    expect(line.textContent).toContain("KLAC short 2× 238P");
  });

  it("annotates a partial cover but does not say covered", () => {
    const line = optionsLine({ ...base, covers: "partial", gap_pct: 6, covered_pct: 3.5 })!;
    expect(line.className).toContain("reb-opt-partial");
    expect(line.textContent).toContain("covers 3.5% of a +6% buy");
  });

  it("shows a plain info line when there's exposure but no buy to cover", () => {
    const line = optionsLine(base)!;
    expect(line.className).toContain("reb-opt-info");
    expect(line.textContent).toContain("~3.5% pending");
  });
});

describe("projectedCash (cash-after-plan tile)", () => {
  const cash = { czk: 50_000, nav: 1_000_000, pct_of_nav: 5, target_pct: 5, band_pp: 2, low: 3, high: 7, status: "IN" };

  it("slides with the plan's net CZK and stays green inside the band", () => {
    const p = projectedCash(cash, 10_000)!; // 60k = 6% of NAV, band [3,7]
    expect(p.czk).toBe(60_000);
    expect(p.pct).toBeCloseTo(6);
    expect(p.cls).toBe("good");
  });

  it("goes red under the cash floor and amber above the ceiling", () => {
    expect(projectedCash(cash, -25_000)!.cls).toBe("bad");   // 2.5% < 3
    expect(projectedCash(cash, 30_000)!.cls).toBe("warn");   // 8% > 7
  });

  it("is null when the plan carries no cash block", () => {
    expect(projectedCash(null, 1000)).toBeNull();
    expect(projectedCash(undefined, 0)).toBeNull();
  });
});

describe("fundingNeededCzk (when to offer Fund this plan)", () => {
  const cash = { czk: 60_000, nav: 1_000_000, pct_of_nav: 6, target_pct: 5, band_pp: 2, low: 3, high: 7, status: "IN" };

  it("is zero when trims cover the buys", () => {
    expect(fundingNeededCzk(5_000, cash)).toBe(0);
    expect(fundingNeededCzk(0, cash)).toBe(0);
  });

  it("nets the shortfall against cash headroom above the floor", () => {
    // Short 50k; 30k sits above the 3% floor (60k - 30k) -> need 20k.
    expect(fundingNeededCzk(-50_000, cash)).toBe(20_000);
    // Headroom fully covers a small shortfall.
    expect(fundingNeededCzk(-20_000, cash)).toBe(0);
  });

  it("uses the whole shortfall when there is no cash block", () => {
    expect(fundingNeededCzk(-50_000, null)).toBe(50_000);
  });
});

describe("fundingCardHtml", () => {
  const res = {
    needed_czk: 100_000, covered_czk: 80_000, shortfall_czk: 20_000,
    candidates: [] as never[],
  };
  const applied: FundingCandidate[] = [
    { symbol: "BONDS", source: "funding_order", current_pct: 30, floor_pct: 20, available_czk: 100_000, suggest_czk: 50_000, suggest_pct: -5, tax: { taxable_gain: 1_000, exempt_proceeds: 40_000, has_lots: true } },
    { symbol: "BIG", source: "untargeted", current_pct: 25, floor_pct: null, available_czk: 250_000, suggest_czk: 30_000, suggest_pct: -3, tax: null },
  ];

  it("lists applied trims with their bucket and tax note", () => {
    const html = fundingCardHtml(res, applied);
    expect(html).toContain("2 trims filled in");
    expect(html).toContain("funding order");
    expect(html).toContain("untargeted");
    expect(html).toContain("taxable gain");
    expect(html).toContain("no lot data");
  });

  it("flags a remaining shortfall", () => {
    expect(fundingCardHtml(res, applied)).toContain("short");
    expect(fundingCardHtml({ ...res, shortfall_czk: 0 }, applied)).not.toContain("out of headroom");
  });
});

describe("rebActionClass", () => {
  it("maps a gated 'wait' action to the muted (non-trade) style", () => {
    expect(rebActionClass("wait")).toBe("muted");
    expect(rebActionClass("buy")).toBe("good");
    expect(rebActionClass("trim")).toBe("bad");
  });
});
