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
// "wait" is set by the research overlay when a locked price trigger blocks the
// band's suggested side (see PriceGate / serve._apply_price_gate).
export type PlanAction = "trim" | "buy" | "review" | "wait" | null;

export interface PlanMember {
  symbol: string;
  current_pct: number;
  current_czk: number | null;
}

// One tranche of a locked ladder as the overlay reports it to the planner: a
// concrete trigger price and its signed distance from the current price.
export interface GateTranche {
  price?: number | null;
  distance_pct?: number | null;
}

// Per-row price-trigger evaluation attached by the rebalance overlay when the
// row's symbol has a locked level. `blocked_action` is set (and PlanRow.action
// downgraded to "wait") when the current price isn't favorable for that side.
// The ladder internals (totals/live/next/fraction) let the planner read out how
// much of the move the price unlocks; they're absent on a plain single level.
export interface PriceGate {
  buy_below: number | null;
  trim_above: number | null;
  current: number | null;
  currency: string;
  price_known: boolean;
  blocks_buy: boolean;
  blocks_trim: boolean;
  blocked_action?: "buy" | "trim";
  buy_total?: number;
  trim_total?: number;
  buy_live?: number;
  trim_live?: number;
  next_buy?: GateTranche | null;
  next_trim?: GateTranche | null;
  applied_fraction?: number | null;
  partial?: boolean;
}

// Independent research context attached to a plan row (decision support only;
// it never changes the trade math).
export interface ResearchInfo {
  data_quality?: string;
  thesis_action?: string;
  // Which way the (free-text) thesis verdict leans, classified server-side so the
  // add/trim vocabulary lives in exactly one place (tools/rebalance_overlay.py).
  thesis_lean?: "add" | "trim" | "neutral";
  thesis_summary?: string;
  momentum_3m_pct?: number;
  as_of?: string | null;
}

// One realized-tax lot under a trim row (Czech 3-year aware).
export interface TaxLot {
  bucket?: string;
  open_datetime?: string;
  days_to_exempt?: number | null;
  proceeds?: number | null;
  gain?: number;
}

export interface TaxInfo {
  has_lots?: boolean;
  lots?: TaxLot[];
  totals?: Record<string, number>;
  raised?: number | null;
  currency?: string;
  n_lots_used?: number;
  requested?: number | null;
  shortfall?: number;
}

// Lineage of a band's target: where it came from (a pin, a research run, a
// hand-set legacy value), surfaced as a small badge on the name cell.
export interface Provenance {
  source?: string;
  stance?: string;
  rationale?: string;
  set_at?: string;
  conviction?: string;
  run_id?: string;
  segment?: string;
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
  // Decision-support overlays (added in-place by serve._attach_research_overlay
  // and tax_lots.enrich_plan); absent on rows with no dossier / level / lots.
  price_gate?: PriceGate | null;
  research?: ResearchInfo | null;
  research_conflict?: boolean;
  tax?: TaxInfo | null;
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
  // Per-key lineage and the working-draft banner state (serve._get_rebalance).
  provenance?: Record<string, Provenance | null | undefined>;
  staged?: { has_draft?: boolean; pending?: number; previewing_draft?: boolean } | null;
}

// ---- what-if (POST /api/whatif) -------------------------------------------
export interface WhatifTrade {
  symbol: string;
  delta_czk: number;
}

export interface WhatifSummary {
  bands_in_before?: number;
  bands_in_after?: number;
  bands_total?: number;
  net_cash_czk?: number;
  realized_taxable_gain_czk?: number;
}

export interface Whatif {
  summary?: WhatifSummary;
  currency?: string;
  trades?: WhatifTrade[];
  cash?: { after?: number | null } | null;
  after?: { rows?: PlanRow[] } | null;
  before_status?: Record<string, string>;
  tax?: { totals?: Record<string, number> } | null;
  caveats?: string[];
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
  // Routing identifiers the Task Center uses to deep-link a finished task back
  // to its result view (see navForTask in tasks.ts). Populated per kind.
  symbol?: string | null;
  stem?: string | null;
  run_id?: string | null;
  // Set on a child job (deep-research spawned by a strategy run) so the Task
  // Center folds it into the parent strategy card instead of double-listing.
  parent_run_id?: string | null;
  source_url?: string | null;
  result?: Record<string, unknown> | null;
  artifact?: { stem?: string; [key: string]: unknown } | null;
  error?: string | null;
  cancelled: boolean;
  created_at?: string | null;
  updated_at?: string | null;
}

// One entry in the central Task Center feed (GET /api/jobs). Same shape as a
// single-job poll; named separately so call sites read intentionally.
export type JobListing = Job;

export interface JobsResponse {
  jobs: JobListing[];
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

// ---- price levels (GET /api/price-levels, POST lock/clear) ----------------
// A human-confirmed, valuation-anchored ladder (instrument currency). A
// fair-value anchor plus buy/trim tranche ladders; buy_below/trim_above mirror
// the outermost tranche of each side for back-compat. A legacy single level
// reads as a 1-tranche ladder. Provenance carried in `source` for audit.
export interface PriceLevelSource {
  kind?: string;
  stem?: string;
  suggested?: unknown;
}

// One tranche of a ladder: a concrete price (what the gate/orders use), the
// margin vs fair value that produced it (intent, for staleness detection), and
// the size fraction it unlocks.
export interface PriceTranche {
  price: number;
  size_pct: number;
  discount_pct?: number | null;
  premium_pct?: number | null;
}

export interface PriceLevel {
  symbol: string;
  currency: string;
  fair_value?: number | null;
  buy_ladder?: PriceTranche[];
  trim_ladder?: PriceTranche[];
  buy_below: number | null;
  trim_above: number | null;
  locked_at?: string;
  status?: string;
  source?: PriceLevelSource;
}

// GET /api/price-levels -> all locked levels keyed by provider symbol.
export interface PriceLevelsResponse {
  levels: Record<string, PriceLevel>;
}

// ---- error envelope (any non-2xx) -----------------------------------------
export interface ApiError {
  error: string;
}
