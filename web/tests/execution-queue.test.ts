import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../src/core", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/core")>()),
  api: vi.fn(),
}));

import { api, state } from "../src/core";
import type { TradeLeg, TradeQueueState } from "../src/api-types";
import {
  applyStagedBasketFromQueue,
  applyStagedBasketLegs,
  clearStagedBasket,
  normalizeTradeQueueState,
  publishQueueChanged,
  stageRebalanceQueue,
} from "../src/execution-queue";
import { subscribePipelineChanged } from "../src/pipeline-summary";

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

  it("publishes queue changes through the shared pipeline event", () => {
    const handler = vi.fn();
    const unsubscribe = subscribePipelineChanged(handler);
    publishQueueChanged();
    expect(handler).toHaveBeenCalledTimes(1);
    expect(handler).toHaveBeenCalledWith({ source: "queue" });
    unsubscribe();
    publishQueueChanged();
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it("stages rebalance trades via POST /api/rebalance/stage", async () => {
    const saved: TradeQueueState = {
      trades: [{ symbol: "NVDA", delta_czk: 1000 }],
      revision: "stage-rev",
      reviewed: false,
    };
    apiMock.mockResolvedValue(saved);
    const handler = vi.fn();
    subscribePipelineChanged(handler);

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
