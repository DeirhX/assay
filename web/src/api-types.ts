// Hand-written contract for the JSON the Python server (tools/serve.py) returns.
//
// There is no codegen / OpenAPI here on purpose: the API is tiny and stdlib-only.
// These types document the shapes the SPA depends on and let typed call sites
// use `api<T>(...)` from ./core for real checking, e.g.
//
//   import type { HoldingsPayload } from "./api-types";
//   const h = await api<HoldingsPayload>("/api/holdings");
//
// They are intentionally permissive where the backend is loose (optional/nullable
// fields), and `[key: string]: unknown` index signatures leave room for fields a
// given view does not read. Keep these in sync with the serve.py handlers and the
// payload builders in portfolio.py / rebalance.py / jobs.py.

// ---- holdings (GET /api/holdings) -----------------------------------------
export interface OptionExposure {
  right: "C" | "P";
  strike: number;
  contracts: number;
  multiplier: number;
  notional_base: number;
  exercise_pct: number;
}

export interface HoldingPosition {
  symbol: string;
  provider_symbol: string;
  researchable: boolean;
  description: string | null;
  asset_class: string | null;
  percent_of_nav: number | null;
  broker_percent_of_nav: number | null;
  base_market_value: number | null;
  currency: string | null;
  unrealized_pnl: number | null;
  issuer_country_code: string | null;
  option: OptionExposure | null;
}

export interface HoldingsPayload {
  net_asset_value: number | null;
  invested_value: number;
  generated_at: string | null;
  sizing_legend: Record<string, number>;
  positions: HoldingPosition[];
}

// ---- rebalance plan (GET /api/rebalance) ----------------------------------
export type BandStatus = "BELOW" | "IN" | "ABOVE";
export type PlanAction = "trim" | "buy" | "review" | null;

export interface PlanMember {
  symbol: string;
  current_pct: number;
  current_czk: number | null;
}

export interface PlanRow {
  key: string;
  name: string;
  kind: "target" | "sleeve";
  rule: string;
  held: boolean;
  current_pct: number;
  current_czk: number | null;
  low: number;
  high: number;
  mid: number;
  status: BandStatus;
  drift_pct: number;
  action: PlanAction;
  suggest_delta_pct: number;
  suggest_delta_czk: number | null;
  note: string | null;
  members: PlanMember[] | null;
  interactive: boolean;
}

export interface RebalancePlan {
  as_of: string | null;
  snapshot: string | null;
  nav: number | null;
  invested: number;
  currency: string;
  cash_target_pct: number;
  funding_order: string[];
  rows: PlanRow[];
  untargeted: PlanMember[];
  untargeted_pct: number;
}

// ---- jobs (GET /api/deep-job, POST run/import/login/analyze/qa) -----------
export type JobState =
  | "queued"
  | "running"
  | "done"
  | "error"
  | "cancelled"
  | "needs_login";

export interface Job {
  id: string;
  kind: string;
  state: JobState;
  message: string;
  segment?: string | null;
  date?: string | null;
  result?: Record<string, unknown> | null;
  artifact?: { stem?: string; [key: string]: unknown } | null;
  error?: string | null;
  cancelled: boolean;
  updated_at?: string | null;
}

// ---- setup / status (GET /api/setup/status) -------------------------------
export interface DataStatus {
  ready: boolean;
  holdings: { exists: boolean; positions: number };
  target_model: { exists: boolean };
  empty: boolean;
}

export interface PerplexityStatus {
  logged_in: boolean;
  updated_at?: string | null;
  note?: string;
}

export interface SetupStatus {
  llm: Record<string, unknown>;
  perplexity: PerplexityStatus;
  ibkr: Record<string, unknown>;
  data: DataStatus;
  environment: {
    sec_user_agent: boolean;
    fmp_api_key: boolean;
    pplx_profile_dir: string;
  };
}

// ---- segments + deep runs -------------------------------------------------
export interface SegmentSummary {
  name: string;
  title: string;
  kind: string;
  status: string;
  overlap_allowed: boolean;
  count: number;
  cached: boolean;
  cached_at: string | null;
}

export interface DeepRun {
  stem: string;
  files: Record<string, string>;
  segment: string;
  date: string;
  title: string;
  source_count: number;
  source_url: string;
  generated_at: string;
  has_review: boolean;
  has_proposal: boolean;
  change_count: number;
  blocked_symbols: string[];
}

// ---- error envelope (any non-2xx) -----------------------------------------
export interface ApiError {
  error: string;
}
