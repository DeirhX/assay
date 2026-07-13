import type { TradeLeg, TradeLegProvenance } from "./api-types";
import type { OrderBand, OrderReconciliation, RiskDelta } from "./trade-model";

export interface TradeOrder {
  instrument_type?: "stock" | "covered_call" | "cash_secured_put";
  leg_id?: string;
  symbol?: string;
  conid?: string | number;
  side?: string;
  quantity?: number | string;
  orderType?: string;
  price?: number | null;
  tif?: string;
  expiry?: string;
  strike?: number;
  right?: string;
  multiplier?: number;
  contracts?: number;
  current_shares?: number;
  coverage_shares?: number;
  if_assigned_shares?: number;
  premium_credit?: number;
  cash_secured_czk?: number;
  collateral_mode?: "cash" | "margin";
  currency?: string | null;
  provenance?: TradeLegProvenance[];
}

interface ImpactValue {
  amount?: string | number | ImpactValue;
  after?: string | number;
  commission?: string | number;
}

export interface IbkrImpact {
  amount?: string | number | ImpactValue;
  initial?: ImpactValue;
  maintenance?: ImpactValue;
  commission?: string | number;
}

export interface TradePreview {
  is_paper?: boolean;
  live_allowed?: boolean;
  account?: string;
  kind?: string;
  warnings?: string[];
  options_only?: string[];
  preview_ttl_s?: number;
  orders?: TradeOrder[];
  proposed_orders?: TradeOrder[];
  order_context?: OrderReconciliation[];
  working_orders_available?: boolean;
  working_orders_error?: string | null;
  placement_blocked?: boolean;
  ibkr_preview?: IbkrImpact | IbkrImpact[];
  trades?: TradeLeg[];
  effective_trades?: Array<{ symbol: string; delta_czk: number }>;
  residual_trades?: Array<{ symbol: string; delta_czk: number }>;
  token?: string;
  snapshot_age_days?: number | null;
  snapshot_stale?: boolean;
  stale_after_days?: number;
  order_bands?: Record<string, OrderBand>;
  local_whatif?: { risk?: RiskDelta } | null;
}

export interface Quote {
  last?: number | null;
  bid?: number | null;
  ask?: number | null;
}

export interface LiveOrder {
  orderId?: string | number;
  order_id?: string | number;
  ticker?: string;
  symbol?: string;
  conid?: string | number;
  side?: string;
  totalSize?: number | string;
  quantity?: number | string;
  remainingQuantity?: number | string;
  filledQuantity?: number | string;
  status?: string;
  order_status?: string;
  orderType?: string;
  order_type?: string;
  price?: number | string | null;
  tif?: string;
  timeInForce?: string;
  orderDesc?: string;
  lastExecutionTime_r?: number;
  quote?: Quote;
  avg_cost?: number;
}

export interface PegState {
  order_id: string;
  state?: string;
  reprices?: number;
  price?: number | null;
  message?: string;
  side?: string;
  symbol?: string;
  bound?: number;
  tick?: number;
}
