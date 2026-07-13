import { api } from "./core";
import type {
  RebalanceExecutionRoute, RebalanceOptionRung, RebalanceRouteResponse,
  RebalanceRouteSelection,
} from "./api-types";

export function directRouteFor(deltaCzk: number): RebalanceExecutionRoute {
  return deltaCzk >= 0 ? "buy_shares" : "sell_shares";
}

export function optionRouteFor(deltaCzk: number): RebalanceExecutionRoute {
  return deltaCzk >= 0 ? "cash_secured_put" : "covered_call";
}

export async function fetchRebalanceRoute(
  symbol: string,
  deltaCzk: number,
): Promise<RebalanceRouteResponse> {
  const query = new URLSearchParams({ symbol, delta_czk: String(deltaCzk) });
  return api<RebalanceRouteResponse>(
    `/api/rebalance/route?${query.toString()}`,
    "GET",
    null,
    { timeoutMs: 60_000 },
  );
}

export function pickStageableRung(
  ladder: RebalanceOptionRung[],
): RebalanceOptionRung | undefined {
  return ladder.find((candidate) => candidate.stageable && candidate.conid);
}

export function buildRouteSelection(params: {
  symbol: string;
  route: RebalanceExecutionRoute;
  rung?: Pick<RebalanceOptionRung, "conid" | "expiry" | "strike" | "limit_price">;
  contracts?: number;
  limitPrice?: number;
  executionItemId?: string;
  collateralMode?: "cash" | "margin";
}): RebalanceRouteSelection {
  const {
    symbol, route, rung, contracts, limitPrice, executionItemId, collateralMode,
  } = params;
  if (rung?.conid) {
    return {
      symbol,
      route,
      conid: Number(rung.conid),
      expiry: rung.expiry,
      strike: rung.strike,
      contracts,
      ...(collateralMode ? { collateral_mode: collateralMode } : {}),
      ...(typeof rung.limit_price === "number" ? { limit_price: rung.limit_price } : {}),
      ...(executionItemId ? { execution_item_id: executionItemId } : {}),
    };
  }
  return {
    symbol,
    route,
    ...(limitPrice ? { limit_price: limitPrice } : {}),
    ...(executionItemId ? { execution_item_id: executionItemId } : {}),
  };
}
