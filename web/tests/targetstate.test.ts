// Tests for the Target state view's pure builders: deriving a tradeable basket
// from the plan's suggestions (targets + sleeve members, skipping review/wait),
// pairing before/after rows into a comparison, and the row HTML's now->after
// rendering with change highlighting.
import { describe, expect, it } from "vitest";
import {
  compareRowHtml, compareRows, deriveSuggestionTrades, scaleMaxOf, sourceBanner,
} from "../src/targetstate";
import type { PlanRow, RebalancePlan } from "../src/api-types";

const row = (over: Partial<PlanRow>): PlanRow => ({
  key: over.name || "X", name: "X", kind: "target", rule: "hold", held: true,
  current_pct: 5, current_czk: 5000, low: 4, high: 6, mid: 5, status: "IN",
  drift_pct: 0, action: null, suggest_delta_pct: 0, suggest_delta_czk: 0,
  note: null, members: null, interactive: true, ...over,
});

describe("deriveSuggestionTrades", () => {
  it("takes buy/trim suggestions and sleeve member splits, skipping the rest", () => {
    const plan = {
      rows: [
        row({ name: "BUY1", action: "buy", suggest_delta_czk: 12_000 }),
        row({ name: "TRIM1", action: "trim", suggest_delta_czk: -8_000 }),
        row({ name: "REVIEW", action: "review", suggest_delta_czk: -5_000 }),   // judgement call
        row({ name: "WAIT", action: "wait", suggest_delta_czk: 3_000 }),        // price-gated
        row({ name: "ZERO", action: "buy", suggest_delta_czk: 0 }),
        row({
          name: "sleeve", kind: "sleeve", interactive: false, action: "buy",
          members: [
            { symbol: "MEM1", current_pct: 1, current_czk: 1000, member_action: "buy", suggest_delta_czk: 4_000 },
            { symbol: "MEM2", current_pct: 2, current_czk: 2000, member_action: null, suggest_delta_czk: 0 },
          ],
        }),
      ],
    } as unknown as RebalancePlan;
    expect(deriveSuggestionTrades(plan)).toEqual([
      { symbol: "BUY1", delta_czk: 12_000 },
      { symbol: "TRIM1", delta_czk: -8_000 },
      { symbol: "MEM1", delta_czk: 4_000 },
    ]);
  });
});

describe("projection review gate", () => {
  it("offers approval for an unreviewed queue and Trade only after approval", () => {
    const unreviewed = sourceBanner("basket", 2, {
      trades: [], revision: "rev-1", reviewed: false,
    });
    expect(unreviewed).toContain('data-ts-review="rev-1"');
    expect(unreviewed).not.toContain('data-ts-goto="trade"');

    const reviewed = sourceBanner("basket", 2, {
      trades: [], revision: "rev-1", reviewed: true,
    });
    expect(reviewed).toContain("projection approved");
    expect(reviewed).toContain('data-ts-goto="trade"');
  });
});

describe("compareRows", () => {
  const before = [
    row({ name: "MOVED", current_pct: 8, status: "ABOVE" }),
    row({ name: "STILL", current_pct: 5, status: "IN" }),
  ];
  const after = [
    row({ name: "MOVED", current_pct: 6, status: "IN" }),
    row({ name: "STILL", current_pct: 5, status: "IN" }),
  ];

  it("pairs by name, flags changes, and sorts biggest move first", () => {
    const rows = compareRows(before, after);
    expect(rows[0].name).toBe("MOVED");
    expect(rows[0].changed).toBe(true);
    expect(rows[0].proj).toBe(6);
    expect(rows[0].statusBefore).toBe("ABOVE");
    expect(rows[0].statusAfter).toBe("IN");
    expect(rows[1].changed).toBe(false);
  });

  it("with no after book the projection equals now", () => {
    const rows = compareRows(before, null);
    expect(rows.every((r) => !r.changed && r.proj === r.cur)).toBe(true);
  });
});

describe("compareRowHtml", () => {
  const r = compareRows(
    [row({ name: "MOVED", current_pct: 8, status: "ABOVE" })],
    [row({ name: "MOVED", current_pct: 6, status: "IN" })])[0];

  it("draws both ticks and the now -> after numbers for a changed row", () => {
    const html = compareRowHtml(r, scaleMaxOf([r]));
    expect(html).toContain("reb-cur-mark");
    expect(html).toContain("reb-proj-mark in");
    expect(html).toContain("8.00%");
    expect(html).toContain("6.00%");
    expect(html).toContain("ABOVE");
    expect(html).toContain("IN");
  });

  it("an unchanged row shows a single tick and a single status", () => {
    const still = compareRows([row({ name: "STILL" })], null)[0];
    const html = compareRowHtml(still, 10);
    expect(html).not.toContain("reb-proj-mark");
    expect(html).not.toContain("tstate-arrow");
  });
});
