// Tests for the history page's pure data-shaping helpers: per-day grouping for
// the chart, option-folding for "activity by name", and pagination.
import { describe, expect, it } from "vitest";
import { contractLabel, dayGroups, groupActivity, groupBySector, paginate } from "../src/history";

describe("dayGroups", () => {
  it("folds trades into ascending days and drops dateless rows", () => {
    const groups = dayGroups([
      { date: "2024-03-12", side: "BUY" },
      { date: "2024-03-11", side: "SELL" },
      { date: "2024-03-12", side: "SELL" },
      { side: "BUY" }, // no date -> ignored
    ]);
    expect(groups.map((g: any) => g.date)).toEqual(["2024-03-11", "2024-03-12"]);
    expect(groups[1].trades).toHaveLength(2);
  });

  it("returns empty for empty/garbage input", () => {
    expect(dayGroups([])).toEqual([]);
    expect(dayGroups(null)).toEqual([]);
  });
});

describe("groupActivity", () => {
  const rows = [
    { symbol: "AMD", is_option: false, currency: "USD", n: 5, buys: 3, sells: 2, bought_base: 500, sold_base: 600, net_base_cash_flow: 100, base_realized_pnl: 230 },
    { symbol: "META  240322P00470000", is_option: true, underlying: "META", currency: "USD", n: 2, buys: 1, sells: 1, bought_base: 80, sold_base: 30, net_base_cash_flow: -50, base_realized_pnl: 115 },
    { symbol: "META  240419C00500000", is_option: true, underlying: "META", currency: "USD", n: 1, buys: 0, sells: 1, bought_base: 0, sold_base: 30, net_base_cash_flow: 30, base_realized_pnl: 0 },
  ];

  it("folds option contracts under their underlying and aggregates in base currency", () => {
    const groups = groupActivity(rows);
    const meta = groups.find((g: any) => g.label === "META")!;
    expect(meta.is_option).toBe(true);
    expect(meta.members).toHaveLength(2);
    expect(meta.n).toBe(3);
    expect(meta.buys).toBe(1);
    expect(meta.sells).toBe(2);
    expect(meta.bought_base).toBe(80);
    expect(meta.sold_base).toBe(60);
    expect(meta.net_base_cash_flow).toBe(-20);
    expect(meta.base_realized_pnl).toBe(115);
    expect(meta.currency).toBe("USD");
  });

  it("flags a group whose members trade in different currencies as mixed", () => {
    const mixed = groupActivity([
      { symbol: "X 1", is_option: true, underlying: "X", currency: "USD", n: 1, base_realized_pnl: 1 },
      { symbol: "X 2", is_option: true, underlying: "X", currency: "EUR", n: 1, base_realized_pnl: 1 },
    ]);
    expect(mixed[0].currency).toBe("mixed");
  });

  it("keeps stocks as singleton groups", () => {
    const amd = groupActivity(rows).find((g: any) => g.label === "AMD")!;
    expect(amd.is_option).toBe(false);
    expect(amd.members).toHaveLength(1);
    expect(amd.currency).toBe("USD");
  });

  it("merges a stock and its options under one name (shares leg first)", () => {
    const merged = groupActivity([
      { symbol: "SOFI", is_option: false, currency: "USD", n: 16, buys: 13, sells: 3, net_base_cash_flow: -1784031, base_realized_pnl: 180485 },
      { symbol: "SOFI  240920C00010000", is_option: true, underlying: "SOFI", currency: "USD", n: 21, buys: 8, sells: 13, net_base_cash_flow: 273485, base_realized_pnl: 55334 },
    ]);
    expect(merged).toHaveLength(1);
    const sofi = merged[0];
    expect(sofi.label).toBe("SOFI");
    expect(sofi.members).toHaveLength(2);
    expect(sofi.opt_count).toBe(1);
    expect(sofi.is_option).toBe(true);
    expect(sofi.n).toBe(37);
    expect(sofi.base_realized_pnl).toBe(235819);
    expect(sofi.members[0].is_option).toBe(false); // equity leg leads
    expect(sofi.members[1].is_option).toBe(true);
  });

  it("sorts groups by trade count descending", () => {
    expect(groupActivity(rows).map((g: any) => g.label)).toEqual(["AMD", "META"]);
  });
});

describe("groupBySector", () => {
  const rows = [
    { symbol: "AMD", is_option: false, sector: "Technology", currency: "USD", n: 5, buys: 3, sells: 2, net_base_cash_flow: 100, base_realized_pnl: 200 },
    { symbol: "ASML", is_option: false, sector: "Technology", currency: "EUR", n: 2, buys: 1, sells: 1, net_base_cash_flow: -40, base_realized_pnl: 50 },
    { symbol: "JPM", is_option: false, sector: "Financial Services", currency: "USD", n: 1, buys: 1, sells: 0, net_base_cash_flow: -20, base_realized_pnl: 0 },
    { symbol: "WEIRD0", is_option: false, sector: "", currency: "EUR", n: 3, buys: 2, sells: 1, net_base_cash_flow: 10, base_realized_pnl: -5 },
  ];

  it("aggregates rows into sectors with base-currency sums and a name count", () => {
    const tech = groupBySector(rows).find((g: any) => g.sector === "Technology")!;
    expect(tech.n).toBe(7);
    expect(tech.names).toBe(2);
    expect(tech.net_base_cash_flow).toBe(60);
    expect(tech.base_realized_pnl).toBe(250);
  });

  it("buckets unsectored names under 'Unknown' and sorts it last", () => {
    const order = groupBySector(rows).map((g: any) => g.sector);
    expect(order[order.length - 1]).toBe("Unknown");
    expect(order).toEqual(["Technology", "Financial Services", "Unknown"]);
  });
});

describe("contractLabel", () => {
  it("formats from IBKR's readable label, dropping the ticker", () => {
    expect(contractLabel({ label: "UAA 28JUN24 7 P" })).toBe("7 Put · 28 Jun '24");
    expect(contractLabel({ label: "WBD 30AUG24 7.5 C" })).toBe("7.5 Call · 30 Aug '24");
    expect(contractLabel({ label: "CRWD 09AUG24 310 C" })).toBe("310 Call · 9 Aug '24");
  });

  it("falls back to parsing the OCC symbol", () => {
    expect(contractLabel({ symbol: "UAA   240628P00007000" })).toBe("7 Put · 28 Jun '24");
    expect(contractLabel({ symbol: "SOFI  240920C00010000" })).toBe("10 Call · 20 Sep '24");
  });

  it("strips a leading ticker when nothing parses", () => {
    expect(contractLabel({ label: "VOW3 weird-contract", underlying: "VOW3" })).toBe("weird-contract");
    expect(contractLabel({ symbol: "MYSTERY" })).toBe("MYSTERY");
  });
});

describe("paginate", () => {
  const items = Array.from({ length: 23 }, (_, i) => i);

  it("slices the requested page", () => {
    const pg = paginate(items, 1, 10);
    expect(pg.items).toEqual([0, 1, 2, 3, 4, 5, 6, 7, 8, 9]);
    expect(pg.pages).toBe(3);
    expect(pg.total).toBe(23);
  });

  it("clamps an out-of-range page into bounds", () => {
    expect(paginate(items, 99, 10).page).toBe(3);
    expect(paginate(items, 0, 10).page).toBe(1);
    expect(paginate(items, -5, 10).page).toBe(1);
  });

  it("always reports at least one page, even when empty", () => {
    const pg = paginate([], 1, 10);
    expect(pg.pages).toBe(1);
    expect(pg.items).toEqual([]);
  });
});
