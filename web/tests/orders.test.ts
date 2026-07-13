import { describe, expect, it } from "vitest";
import {
  ordersDashboardHtml, pipelineCounts, type OrdersDashboardData,
} from "../src/orders";
import { isTerminalOrder } from "../src/pipeline-summary";

const data = (over: Partial<OrdersDashboardData> = {}): OrdersDashboardData => ({
  plan: {
    schema_version: 1,
    version: 3,
    items: [
      { id: "a", symbol: "AMD", source: "rebalance", direction: "increase",
        delta_czk: 20_000, route_policy: "buy_shares", status: "selected" },
      { id: "b", symbol: "ARM", source: "ticker", direction: "reduce",
        delta_czk: -10_000, route_policy: "sell_shares", status: "deferred",
        defer_until: "2026-08-01" },
      { id: "c", symbol: "NVDA", source: "rebalance", direction: "increase",
        delta_czk: 5_000, route_policy: "buy_shares", status: "suggested" },
      { id: "d", symbol: "TSM", source: "rebalance", direction: "increase",
        delta_czk: 5_000, route_policy: "buy_shares", status: "queued" },
    ],
  },
  queue: {
    trades: [{ symbol: "TSM", delta_czk: 5_000 }],
    queue_trades: [
      { symbol: "TSM", delta_czk: 5_000, included: true },
      { symbol: "ASML", delta_czk: 2_000, included: false },
    ],
    revision: "abc",
    reviewed: true,
    valid: true,
  },
  working: [
    { ticker: "MSFT", side: "BUY", quantity: 2, status: "Submitted", price: 400 },
    { ticker: "GOOG", side: "SELL", quantity: 1, status: "Filled", price: 200 },
  ],
  workingState: "ready",
  ...over,
});

describe("pipelineCounts", () => {
  it("keeps planned, queued, excluded, and broker-working stages distinct", () => {
    expect(pipelineCounts(data())).toEqual({
      planned: 2,
      selected: 1,
      deferred: 1,
      suggested: 1,
      queued: 1,
      excluded: 1,
      working: 1,
    });
  });

  it("does not count terminal broker rows as working", () => {
    expect(isTerminalOrder({ status: "Filled" })).toBe(true);
    expect(isTerminalOrder({ order_status: "Cancelled" })).toBe(true);
    expect(isTerminalOrder({ status: "Submitted" })).toBe(false);
  });
});

describe("ordersDashboardHtml", () => {
  it("renders the complete pipeline and routes each stage to its canonical workspace", () => {
    const html = ordersDashboardHtml(data());
    expect(html).toContain("Planned trades");
    expect(html).toContain("Order queue");
    expect(html).toContain("Working at IBKR");
    expect(html).toContain("projection approved");
    expect(html).toContain('data-orders-view="rebalance"');
    expect(html).toContain('data-orders-view="trade" data-orders-tab="orders"');
    expect(html).toContain('data-orders-view="history"');
  });

  it("keeps local intent visible when IBKR is offline", () => {
    const html = ordersDashboardHtml(data({
      working: [],
      workingState: "offline",
      workingMessage: "IBKR Client Portal Gateway is not connected.",
    }));
    expect(html).toContain("AMD");
    expect(html).toContain("TSM");
    expect(html).toContain("IBKR Client Portal Gateway is not connected.");
    expect(html).toContain("Local plan and queue remain available");
  });

  it("does not present submitted plan records as broker truth", () => {
    const html = ordersDashboardHtml(data({
      plan: {
        schema_version: 1, version: 1,
        items: [{ id: "s", symbol: "META", source: "rebalance", direction: "increase",
          delta_czk: 10_000, route_policy: "buy_shares", status: "submitted" }],
      },
      working: [],
    }));
    expect(html).not.toContain("<strong>META</strong>");
    expect(html).toContain("“submitted” plan records are not treated as fills");
  });

  it("makes stale intent revalidation the planned-trades action", () => {
    const html = ordersDashboardHtml(data({
      plan: { ...data().plan, stale: true },
    }));
    expect(html).toContain("Recheck stale amounts");
    expect(html).toContain("execution plan is stale");
  });
});
