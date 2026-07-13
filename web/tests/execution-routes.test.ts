import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../src/core", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/core")>()),
  api: vi.fn(),
}));

import { api } from "../src/core";
import type { RebalanceOptionRung, RebalanceRouteResponse } from "../src/api-types";
import {
  buildRouteSelection,
  directRouteFor,
  fetchRebalanceRoute,
  optionRouteFor,
  pickStageableRung,
} from "../src/execution-routes";

const apiMock = vi.mocked(api);

function ladder(over: Partial<RebalanceOptionRung> = {}): RebalanceOptionRung {
  return {
    strike: 100,
    expiry: "2026-08-07",
    dte: 30,
    premium: 2,
    premium_czk: 4600,
    moneyness_pct: 0,
    premium_yield_annual_pct: 20,
    assignment_prob_pct: 25,
    open_interest: 100,
    volume: 10,
    spread_pct: 5,
    liquidity: "ok",
    source: "ibkr",
    estimate: false,
    ...over,
  };
}

describe("execution-routes", () => {
  beforeEach(() => apiMock.mockReset());

  it("maps delta sign to direct and option routes", () => {
    expect(directRouteFor(1000)).toBe("buy_shares");
    expect(directRouteFor(-1000)).toBe("sell_shares");
    expect(optionRouteFor(1000)).toBe("cash_secured_put");
    expect(optionRouteFor(-1000)).toBe("covered_call");
  });

  it("fetches rebalance routes with a 60s timeout", async () => {
    const response = { symbol: "NVDA", ladder: [] } as unknown as RebalanceRouteResponse;
    apiMock.mockResolvedValue(response);
    await expect(fetchRebalanceRoute("NVDA", 230_000)).resolves.toBe(response);
    expect(apiMock).toHaveBeenCalledWith(
      "/api/rebalance/route?symbol=NVDA&delta_czk=230000",
      "GET",
      null,
      { timeoutMs: 60_000 },
    );
  });

  it("picks the first stageable rung with a contract id", () => {
    expect(pickStageableRung([
      ladder({ stageable: false, conid: 1 }),
      ladder({ stageable: true, conid: null }),
      ladder({ stageable: true, conid: 42, strike: 95 }),
    ])?.strike).toBe(95);
    expect(pickStageableRung([ladder({ stageable: false })])).toBeUndefined();
  });

  it("builds direct and option route selections", () => {
    expect(buildRouteSelection({
      symbol: "AMD",
      route: "sell_shares",
      limitPrice: 150,
    })).toEqual({
      symbol: "AMD",
      route: "sell_shares",
      limit_price: 150,
    });
    expect(buildRouteSelection({
      symbol: "NVDA",
      route: "cash_secured_put",
      rung: ladder({ conid: 556, limit_price: 2.5 }),
      contracts: 2,
      executionItemId: "item-1",
    })).toEqual({
      symbol: "NVDA",
      route: "cash_secured_put",
      conid: 556,
      expiry: "2026-08-07",
      strike: 100,
      contracts: 2,
      limit_price: 2.5,
      execution_item_id: "item-1",
    });
  });
});
