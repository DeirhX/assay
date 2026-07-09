// Tests for the "Activity by name" column sorter: direction toggling per column,
// string vs numeric comparison, a stable name tiebreak, and input immutability.
import { describe, expect, it } from "vitest";
import { sortActivityGroups, type ActivitySort } from "../src/history/tables";
import type { ActivityGroup } from "../src/history/data";

// Minimal group factory — only the fields the sorter reads matter.
function g(over: Partial<ActivityGroup>): ActivityGroup {
  return {
    key: over.label || "?", label: "?", is_option: false, opt_count: 0,
    n: 0, buys: 0, sells: 0, bought_base: 0, sold_base: 0,
    net_base_cash_flow: 0, base_realized_pnl: 0,
    currencies: new Set<string>(), currency: "USD", members: [],
    ...over,
  };
}

const sample = (): ActivityGroup[] => [
  g({ label: "AMD", n: 28, bought_base: 2551989, sold_base: 5115736, net_base_cash_flow: 2563748, base_realized_pnl: 3203924, currency: "USD" }),
  g({ label: "GEN", n: 39, bought_base: 44807, sold_base: 8149515, net_base_cash_flow: 8104708, base_realized_pnl: 2501168, currency: "USD" }),
  g({ label: "INTC", n: 21, bought_base: 310734, sold_base: 661306, net_base_cash_flow: 350572, base_realized_pnl: -47755, currency: "USD" }),
  g({ label: "EUR.CZK", n: 33, bought_base: 0, sold_base: 0, net_base_cash_flow: 0, base_realized_pnl: 0, currency: "CZK" }),
];

const order = (rows: ActivityGroup[]) => rows.map((r) => r.label);

describe("sortActivityGroups", () => {
  it("sorts by realized P&L descending / ascending", () => {
    expect(order(sortActivityGroups(sample(), { key: "pnl", dir: "desc" })))
      .toEqual(["AMD", "GEN", "EUR.CZK", "INTC"]);
    expect(order(sortActivityGroups(sample(), { key: "pnl", dir: "asc" })))
      .toEqual(["INTC", "EUR.CZK", "GEN", "AMD"]);
  });

  it("sorts by trades descending (the default column)", () => {
    expect(order(sortActivityGroups(sample(), { key: "trades", dir: "desc" })))
      .toEqual(["GEN", "EUR.CZK", "AMD", "INTC"]);
  });

  it("sorts Name alphabetically", () => {
    expect(order(sortActivityGroups(sample(), { key: "name", dir: "asc" })))
      .toEqual(["AMD", "EUR.CZK", "GEN", "INTC"]);
    expect(order(sortActivityGroups(sample(), { key: "name", dir: "desc" })))
      .toEqual(["INTC", "GEN", "EUR.CZK", "AMD"]);
  });

  it("sorts Ccy as a string, breaking ties by name (dir-independent tiebreak)", () => {
    // Three USD names tie on Ccy; they must stay A→Z among themselves whether the
    // overall direction is asc or desc.
    const asc = sortActivityGroups(sample(), { key: "ccy", dir: "asc" });
    expect(order(asc)).toEqual(["EUR.CZK", "AMD", "GEN", "INTC"]);
    const desc = sortActivityGroups(sample(), { key: "ccy", dir: "desc" });
    // USD block first (desc), still A→Z internally, then CZK.
    expect(order(desc)).toEqual(["AMD", "GEN", "INTC", "EUR.CZK"]);
  });

  it("does not mutate the input array", () => {
    const rows = sample();
    const before = order(rows);
    sortActivityGroups(rows, { key: "pnl", dir: "asc" } as ActivitySort);
    expect(order(rows)).toEqual(before);
  });
});
