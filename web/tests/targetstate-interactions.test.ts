import { beforeEach, expect, it, vi } from "vitest";

const { apiMock } = vi.hoisted(() => ({ apiMock: vi.fn() }));
vi.mock("../src/core", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/core")>()),
  api: apiMock,
}));

import { initTargetState, loadTargetState } from "../src/targetstate";

const plan = {
  rows: [],
  currency: "CZK",
  cash: null,
};

const activeQueue = {
  trades: [
    { type: "stock", leg_id: "stock:NVDA", symbol: "NVDA", delta_czk: 100_000 },
    { type: "stock", leg_id: "stock:ARM", symbol: "ARM", delta_czk: 50_000 },
  ],
  queue_trades: [
    {
      type: "stock", leg_id: "stock:NVDA", symbol: "NVDA",
      delta_czk: 100_000, estimated_shares: 50, included: true,
    },
    {
      type: "stock", leg_id: "stock:ARM", symbol: "ARM",
      delta_czk: 50_000, estimated_shares: 25, included: true,
    },
  ],
  excluded_leg_ids: [],
  revision: "active",
  reviewed: false,
  valid: true,
};

const excludedQueue = {
  trades: [
    { type: "stock", leg_id: "stock:ARM", symbol: "ARM", delta_czk: 50_000 },
  ],
  queue_trades: [
    {
      type: "stock", leg_id: "stock:NVDA", symbol: "NVDA",
      delta_czk: 100_000, estimated_shares: 50, included: false,
    },
    {
      type: "stock", leg_id: "stock:ARM", symbol: "ARM",
      delta_czk: 50_000, estimated_shares: 25, included: true,
    },
  ],
  excluded_leg_ids: ["stock:NVDA"],
  revision: "",
  reviewed: false,
  valid: true,
};

beforeEach(() => {
  apiMock.mockReset();
  if (!document.getElementById("tstate-body")) {
    document.body.innerHTML =
      '<section id="view-target-state"><span id="tstate-status"></span>' +
      '<div id="tstate-body"></div><button id="tstate-refresh"></button></section>';
  } else {
    document.getElementById("tstate-body")!.innerHTML = "";
    document.getElementById("tstate-status")!.innerHTML = "";
  }
  initTargetState();
});

it("keeps the current page mounted while an exclusion projection refreshes", async () => {
  let whatifCalls = 0;
  let finishProjection!: (value: unknown) => void;
  const pendingProjection = new Promise((resolve) => { finishProjection = resolve; });
  apiMock.mockImplementation((path: string, method?: string) => {
    if (path === "/api/rebalance") return Promise.resolve(plan);
    if (path === "/api/trade/basket" && method === "POST") {
      return Promise.resolve(excludedQueue);
    }
    if (path === "/api/trade/basket") return Promise.resolve(activeQueue);
    if (path === "/api/whatif") {
      whatifCalls += 1;
      return whatifCalls === 1
        ? Promise.resolve({ trades: activeQueue.trades, after: { rows: [] }, valid: true })
        : pendingProjection;
    }
    return Promise.resolve({});
  });

  await loadTargetState();
  document.querySelector<HTMLButtonElement>('[data-ts-toggle-leg="stock:NVDA"]')!.click();
  await vi.waitFor(() => expect(whatifCalls).toBe(2));

  expect(document.querySelector(".tstate-queue")).toBeTruthy();
  expect(document.getElementById("tstate-body")!.textContent).toContain("NVDA");

  finishProjection({ trades: [], after: { rows: [] }, valid: true });
  await vi.waitFor(() =>
    expect(document.querySelector(".tstate-queue-row")?.classList.contains("excluded"))
      .toBe(true));
});

it("shows working IBKR stock sells and cancels them without leaving Target State", async () => {
  const call = {
    type: "covered_call", leg_id: "covered_call:PYPL:42", symbol: "PYPL",
    conid: 42, expiry: "2026-08-21", strike: 70, contracts: 7,
  };
  const queue = {
    trades: [call],
    queue_trades: [{ ...call, included: true }],
    excluded_leg_ids: [],
    revision: "calls",
    reviewed: false,
    valid: true,
  };
  let conflictChecks = 0;
  apiMock.mockImplementation((path: string, method?: string, body?: unknown) => {
    if (path === "/api/rebalance") return Promise.resolve(plan);
    if (path === "/api/trade/basket") return Promise.resolve(queue);
    if (path === "/api/whatif") {
      return Promise.resolve({ trades: [], after: { rows: [] }, valid: true });
    }
    if (path === "/api/trade/cancel") {
      expect(method).toBe("POST");
      expect(body).toEqual({ order_id: "991" });
      return Promise.resolve({ order_id: "991", status: "cancelled" });
    }
    if (path === "/api/trade/queue-conflicts") {
      conflictChecks += 1;
      return Promise.resolve({
        working_orders_verified: true,
        coverage_violations: conflictChecks === 1 ? [{
          symbol: "PYPL",
          current_shares: 1_000,
          planned_stock_sell_shares: 0,
          working_stock_sell_shares: 400,
          working_stock_order_ids: ["991"],
          selected_call_contracts: 7,
          held_short_call_contracts: 0,
          working_short_call_contracts: 0,
          required_shares: 1_100,
          excess_shares: 100,
          stock_leg_ids: [],
          call_leg_ids: ["covered_call:PYPL:42"],
        }] : [],
      });
    }
    return Promise.resolve({});
  });
  Object.defineProperty(window, "confirm", {
    configurable: true,
    value: vi.fn(() => true),
  });

  await loadTargetState();
  expect(document.getElementById("tstate-body")!.textContent)
    .toContain("400 in working IBKR stock sells");
  expect(document.querySelector("[data-ts-review]")).toBeNull();

  document.querySelector<HTMLButtonElement>("[data-coverage-cancel-order-ids]")!.click();
  await vi.waitFor(() => expect(conflictChecks).toBe(2));
  expect(apiMock).toHaveBeenCalledWith(
    "/api/trade/cancel", "POST", { order_id: "991" },
  );
  expect(document.getElementById("tstate-body")!.textContent)
    .not.toContain("working IBKR stock sells");
});
