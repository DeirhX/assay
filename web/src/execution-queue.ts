import { api, state } from "./core";
import type {
  QueuedTradeLeg, RebalanceRouteSelection, TradeLeg, TradeQueueState,
} from "./api-types";
import { publishPipelineChanged } from "./pipeline-summary";

export function publishQueueChanged(): void {
  publishPipelineChanged({ source: "queue" });
}

export function normalizeTradeQueueState(saved: TradeQueueState): TradeQueueState {
  const trades = Array.isArray(saved.trades) ? saved.trades : [];
  const queueTrades: QueuedTradeLeg[] = Array.isArray(saved.queue_trades)
    ? saved.queue_trades
    : trades.map((trade) => ({ ...trade, included: true }));
  return {
    ...saved,
    trades,
    queue_trades: queueTrades,
    excluded_leg_ids: saved.excluded_leg_ids || [],
    revision: saved.revision || "",
    reviewed: !!saved.reviewed,
    reviewed_at: saved.reviewed_at || null,
  };
}

export function applyStagedBasketFromQueue(saved: TradeQueueState): void {
  state.stagedBasket = normalizeTradeQueueState(saved).trades.slice();
}

export function applyStagedBasketLegs(legs: TradeLeg[]): void {
  state.stagedBasket = legs.slice();
}

export function clearStagedBasket(): void {
  state.stagedBasket = [];
}

export interface StageRebalanceQueueRequest {
  trades: Array<{ symbol: string; delta_czk: number }>;
  selections: RebalanceRouteSelection[];
  mode: "append" | "replace";
  source?: string;
}

export async function stageRebalanceQueue(
  body: StageRebalanceQueueRequest,
): Promise<TradeQueueState> {
  const saved = await api<TradeQueueState>("/api/rebalance/stage", "POST", body);
  applyStagedBasketFromQueue(saved);
  publishQueueChanged();
  return saved;
}
