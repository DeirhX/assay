import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../src/core", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/core")>()),
  api: vi.fn(),
}));

import { api, state } from "../src/core";
import type { TradeLeg, TradeQueueState } from "../src/api-types";
import {
  QUEUE_CHANGED_EVENT,
  applyStagedBasketFromQueue,
  applyStagedBasketLegs,
  clearStagedBasket,
  normalizeTradeQueueState,
  publishQueueChanged,
  stageRebalanceQueue,
  subscribeQueueChanged,
} from "../src/execution-queue";

const apiMock = vi.mocked(api);

describe("execution-queue", () => {
  beforeEach(() => {
    apiMock.mockReset();
    state.stagedBasket = [];
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("normalizes missing queue_trades and revision fields", () => {
    const normalized = normalizeTradeQueueState({
      trades: [{ symbol: "NVDA", delta_czk: 1000 }],
      revision: "",
      reviewed: false,
    });
    expect(normalized.queue_trades).toEqual([
      { symbol: "NVDA", delta_czk: 1000, included: true },
    ]);
    expect(normalized.excluded_leg_ids).toEqual([]);
    expect(normalized.reviewed).toBe(false);
    expect(normalized.reviewed_at).toBeNull();
  });

  it("applies staged basket legs from a queue response", () => {
    applyStagedBasketFromQueue({
      trades: [{ symbol: "AMD", delta_czk: -500 }],
      revision: "rev-1",
      reviewed: true,
      reviewed_at: "2026-07-01",
    });
    expect(state.stagedBasket).toEqual([{ symbol: "AMD", delta_czk: -500 }]);
  });

  it("applies and clears staged basket legs directly", () => {
    const legs: TradeLeg[] = [{ symbol: "AAPL", delta_czk: 250 }];
    applyStagedBasketLegs(legs);
    expect(state.stagedBasket).toEqual(legs);
    clearStagedBasket();
    expect(state.stagedBasket).toEqual([]);
  });

  it("publishes and subscribes to queue-changed", () => {
    const handler = vi.fn();
    const unsubscribe = subscribeQueueChanged(handler);
    publishQueueChanged();
    expect(handler).toHaveBeenCalledTimes(1);
    unsubscribe();
    publishQueueChanged();
    expect(handler).toHaveBeenCalledTimes(1);
    expect(QUEUE_CHANGED_EVENT).toBe("assay:queue-changed");
  });

  it("stages rebalance trades via POST /api/rebalance/stage", async () => {
    const saved: TradeQueueState = {
      trades: [{ symbol: "NVDA", delta_czk: 1000 }],
      revision: "stage-rev",
      reviewed: false,
    };
    apiMock.mockResolvedValue(saved);
    const handler = vi.fn();
    subscribeQueueChanged(handler);

    const result = await stageRebalanceQueue({
      trades: [{ symbol: "NVDA", delta_czk: 1000 }],
      selections: [{ symbol: "NVDA", route: "buy_shares" }],
      mode: "append",
      source: "ticker",
    });

    expect(apiMock).toHaveBeenCalledWith("/api/rebalance/stage", "POST", {
      trades: [{ symbol: "NVDA", delta_czk: 1000 }],
      selections: [{ symbol: "NVDA", route: "buy_shares" }],
      mode: "append",
      source: "ticker",
    });
    expect(result).toBe(saved);
    expect(state.stagedBasket).toEqual(saved.trades);
    expect(handler).toHaveBeenCalledTimes(1);
  });
});
