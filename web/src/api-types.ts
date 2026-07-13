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
  underlying: string;
  expiry: string | null;
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
  quantity: number | null;
  percent_of_nav: number | null;
  broker_percent_of_nav: number | null;
  base_market_value: number | null;
  currency: string | null;
  unrealized_pnl: number | null;
  issuer_country_code: string | null;
  option: OptionExposure | null;
  // Set only on the /api/holdings/live payload: true when this row's mark was
  // refreshed from the live CPAPI feed, false when it's still on the delayed
  // Flex snapshot (options / unmatched names). Absent on the base snapshot.
  live_mark?: boolean;
}

export interface HoldingsPayload {
  net_asset_value: number | null;
  invested_value: number;
  generated_at: string | null;
  sizing_legend: Record<string, number>;
  positions: HoldingPosition[];
}

// GET /api/holdings/live — best-effort live-mark overlay. `available:false` means
// the gateway is down/unauthenticated (or the fetch timed out): keep the snapshot.
export interface HoldingsLiveResponse {
  available: boolean;
  reason?: string;
  as_of?: string;
  coverage?: { live: number; eligible: number; total: number };
  payload?: HoldingsPayload;
}

// ---- live IBKR Client Portal Gateway --------------------------------------
export interface GatewayAccount {
  id: string;
  kind?: "paper" | "live" | string;
  trading_type?: "CASH" | "MRGN" | "PMRGN" | string | null;
  margin?: boolean;
}

export interface GatewayStatus {
  trading_enabled?: boolean;
  live_allowed?: boolean;
  gateway_base?: string | null;
  authenticated?: boolean;
  connected?: boolean;
  competing?: boolean;
  accounts?: GatewayAccount[];
  default_account?: string | null;
  reconnect_error?: string | null;
}

// ---- rebalance plan (GET /api/rebalance) ----------------------------------
export type BandStatus = "BELOW" | "IN" | "ABOVE";
// "wait" is set by the research overlay when a locked price trigger blocks the
// band's suggested side (see PriceGate / serve._apply_price_gate).
export type PlanAction = "trim" | "buy" | "review" | "wait" | null;

// One option leg on an underlying, as the overlay reports it.
export interface OptionLeg {
  right: "C" | "P";
  strike: number;
  contracts: number;
  exercise_pct: number;
  label: string;
}

// Pending option exposure attached to a plan row/member: what the options book
// would add to the stock on assignment/exercise (NOT counted as owned shares).
// `covers` is set only on a buy the bullish exposure already fills — "full"
// downgrades the action and zeroes the pre-staged default (originals kept in
// full_*), "partial" is annotated but left to act.
export interface PendingOptionExposure {
  long_pct: number;
  short_pct: number;
  net_pct: number;
  contracts: number;
  label: string;
  legs: OptionLeg[];
  covers?: "full" | "partial";
  gap_pct?: number;
  covered_pct?: number;
  full_suggest_delta_pct?: number;
  full_suggest_delta_czk?: number | null;
}

export interface LastQuote {
  price: number;
  currency?: string | null;
  source?: string;
  at?: string | null;
}

export interface PlanMember {
  symbol: string;
  current_pct: number;
  current_czk: number | null;
  mark_price?: number;
  mark_currency?: string;
  last_quote?: LastQuote | null;
  // Per-member sleeve advice (sleeve members only; absent on the untargeted bucket):
  // an even split of the sleeve midpoint capped by member_caps, the share of the
  // sleeve's suggested buy/trim allocated to this name, and a 1-based order.
  cap?: number | null;
  target_pct?: number;
  conviction?: string | null;
  suggest_delta_pct?: number;
  suggest_delta_czk?: number | null;
  member_action?: "buy" | "trim" | null;
  order?: number;
  options?: PendingOptionExposure | null;
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
  n_lots_total?: number;
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
  mark_price?: number;
  mark_currency?: string;
  last_quote?: LastQuote | null;
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
  options?: PendingOptionExposure | null;
}

// First-class cash line (rebalance.cash_block): current cash vs the
// cash_target_pct band, measured as % of NAV. Null when the snapshot has no
// cash/NAV data. Informational — cash is never a tradeable row.
export interface CashBlock {
  czk: number;
  nav: number;
  pct_of_nav: number;
  target_pct: number;
  band_pp: number;
  low: number;
  high: number;
  status: string;
}

export interface RebalancePlan {
  as_of: string | null;
  snapshot: string | null;
  nav: number | null;
  invested: number;
  currency: string;
  cash_target_pct: number;
  cash?: CashBlock | null;
  funding_order: string[];
  rows: PlanRow[];
  untargeted: PlanMember[];
  untargeted_pct: number;
  // Per-key lineage and the working-draft banner state (serve._get_rebalance).
  provenance?: Record<string, Provenance | null | undefined>;
  staged?: { has_draft?: boolean; pending?: number; previewing_draft?: boolean } | null;
  execution_plan?: ExecutionPlanState;
}

export type ExecutionPlanStatus =
  | "suggested" | "selected" | "deferred" | "dismissed"
  | "queued" | "submitted" | "superseded";

export interface ExecutionPlanItem {
  id: string;
  symbol: string;
  source: "rebalance" | "ticker" | "exit" | string;
  origin_key?: string;
  plan_fingerprint?: string | null;
  direction: "increase" | "reduce";
  delta_czk: number;
  delta_pct?: number | null;
  desired_weight_pct?: number | null;
  route_policy: "auto_put" | RebalanceExecutionRoute;
  route_selection?: RebalanceRouteSelection | null;
  limit_price?: number | null;
  status: ExecutionPlanStatus;
  defer_until?: string | null;
  dismiss_reason?: string | null;
  queued_leg_id?: string | null;
  created_at?: string;
  updated_at?: string;
}

export interface ExecutionPlanState {
  schema_version: number;
  version: number;
  plan_binding?: {
    fingerprint: string;
    as_of?: string | null;
    snapshot?: string | null;
  } | null;
  items: ExecutionPlanItem[];
  stale?: boolean;
  pending_count?: number;
  pending_binding?: {
    fingerprint: string;
    as_of?: string | null;
    snapshot?: string | null;
  } | null;
  updated_at?: string | null;
}

// ---- funding assistant (POST /api/rebalance/funding) -----------------------
// Deterministic funding suggestions when a plan's buys outrun its trims:
// funding_order first, then untargeted names, each capped at its headroom and
// tax-annotated server-side. Advice — lands as editable plan inputs.
export interface FundingCandidate {
  symbol: string;
  source: "funding_order" | "untargeted";
  current_pct: number;
  floor_pct: number | null;
  available_czk: number;
  suggest_czk: number;
  suggest_pct: number;
  tax?: {
    taxable_gain?: number | null;
    exempt_proceeds?: number | null;
    harvestable_loss?: number | null;
    has_lots?: boolean;
  } | null;
}
export interface FundingResponse {
  needed_czk: number;
  covered_czk: number;
  shortfall_czk: number;
  candidates: FundingCandidate[];
}

// ---- what-if (POST /api/whatif) -------------------------------------------
export interface WhatifTrade {
  symbol: string;
  delta_czk: number;
}

export interface TradeQueueState {
  trades: TradeLeg[];
  queue_trades?: QueuedTradeLeg[];
  excluded_leg_ids?: string[];
  revision: string;
  reviewed: boolean;
  reviewed_at?: string | null;
  valid?: boolean;
  stock_sell_violations?: StockSellViolation[];
  coverage_violations?: CoveredCallCoverageViolation[];
  working_orders_verified?: boolean;
  working_orders_error?: string | null;
}

export interface CoveredCallCoverageViolation {
  symbol: string;
  current_shares: number;
  planned_stock_sell_shares: number;
  working_stock_sell_shares?: number;
  working_stock_order_ids?: string[];
  working_call_order_ids?: string[];
  selected_call_contracts: number;
  held_short_call_contracts: number;
  working_short_call_contracts: number;
  required_shares: number;
  excess_shares: number;
  stock_leg_ids: string[];
  call_leg_ids: string[];
}

export interface StockSellViolation {
  symbol: string;
  held_czk: number;
  requested_sell_czk: number;
  excess_czk: number;
  after_czk: number;
}

export interface WhatifSummary {
  bands_in_before?: number;
  bands_in_after?: number;
  bands_total?: number;
  net_cash_czk?: number;
  realized_taxable_gain_czk?: number;
}

export interface Whatif {
  valid?: boolean;
  stock_sell_violations?: StockSellViolation[];
  summary?: WhatifSummary;
  currency?: string;
  trades?: WhatifTrade[];
  applied_trades?: WhatifTrade[];
  cash?: {
    after?: number | null;
    target?: {
      target_pct: number; low: number; high: number;
      before_pct: number; after_pct: number; status_after: string;
    } | null;
  } | null;
  after?: { rows?: PlanRow[] } | null;
  before_status?: Record<string, string>;
  tax?: { totals?: Record<string, number> } | null;
  risk?: RiskDelta | null;
  caveats?: string[];
}

// Pre-trade risk delta (risk_delta.py). Concentration + effective names are
// always present; the correlation-aware pair only when a price series was
// available (has_correlation).
export interface RiskPair {
  before?: number | null;
  after?: number | null;
  delta?: number | null;
}
export interface RiskDelta {
  top1_pct?: RiskPair;
  top5_pct?: RiskPair;
  effective_names?: RiskPair;
  effective_bets?: RiskPair;
  portfolio_vol_pct?: RiskPair;
  has_correlation?: boolean;
  warnings?: string[];
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

// One entry in the durable Activity feed (GET /api/activity). Either a ticker
// the user opened ("view") or a background task that finished ("task"). Task
// rows carry the same small routing identifiers navForTask reads, so the feed
// can deep-link a finished task back to its result.
export interface ActivityEvent {
  ts: string;
  type: "view" | "task";
  // view
  symbol?: string | null;
  name?: string | null;
  // task
  id?: string | null;
  kind?: string | null;
  state?: JobState | null;
  segment?: string | null;
  run_id?: string | null;
  stem?: string | null;
  artifact_stem?: string | null;
  slug?: string | null;
  error?: string | null;
  message?: string | null;
}

export interface ActivityResponse {
  events: ActivityEvent[];
}

// ---- setup / status (GET /api/setup/status) -------------------------------
export interface DataStatus {
  ready: boolean;
  holdings: { exists: boolean; positions: number };
  target_model: { exists: boolean };
  empty: boolean;
}

export interface PerplexityStatus {
  enabled: boolean;
  logged_in: boolean;
  deep_research_available?: boolean | null;
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

// ---- exit plan (GET /api/exit-plan, POST /api/exit-plan/stage) ------------
// Advisory tax-timed scale-out for unwanted positions. Money is base currency
// (CZK) unless a field says otherwise; limit prices are in the instrument's
// trading currency.
export interface ExitSellNowLot {
  bucket: string;
  shares: number;
  proceeds: number;
  gain: number;
  exempt: boolean;
  days_to_exempt: number | null;
  open_datetime: string | null;
}

export interface ExitDeferLot {
  bucket: string;
  shares: number;
  market_value: number;
  gain: number;
  days_to_exempt: number | null;
  exempt_on: string | null;
  tax_if_sold_now: number;
  note: string;
}

export interface ExitTaxLayers {
  sell_now_czk: number;
  defer_czk: number;
  sell_now_lots: ExitSellNowLot[];
  defer_lots: ExitDeferLot[];
  taxable_gain_now: number;
  exempt_gain_now: number;
  harvested_loss_now: number;
  tax_cost_now: number;
  tax_saved_by_waiting: number;
}

export interface ExitTranche {
  index: number;
  date: string;
  shares: number;
  czk: number;
  limit_price: number | null;
  limit_currency: string | null;
  over_adv_cap: boolean;
}

export interface ExitSchedule {
  tranches: ExitTranche[];
  n: number;
  adv: number | null;
  max_shares_per_day: number | null;
}

// Live quote fields shared by the underlying and option rungs on the Exit overlay.
export interface ExitQuoteFields {
  bid?: number | null;
  ask?: number | null;
  last?: number | null;
  conid?: number | null;
  multiplier?: number | null;
  limit_price?: number | null;
  quote_timestamp?: string | null;
  quote_age_seconds?: number | null;
  quote_fresh?: boolean;
  stageable?: boolean;
  staging_warning?: string | null;
  executable?: boolean;
}

export interface ExitCoveredCall extends ExitQuoteFields {
  type: "covered_call";
  source: string;
  contracts: number;
  expiry: string;
  dte: number;
  strike: number;
  premium: number;
  premium_czk: number;
  effective_exit: number;
  premium_yield_annual_pct: number | null;
  assignment_prob_pct: number | null;
  vol_used: number;
  estimate: boolean;
  assignment_guard?: boolean;
}

export interface ExitCoveredCallRung extends ExitQuoteFields {
  strike: number;
  expiry: string;
  dte: number;
  premium: number;
  premium_czk: number;
  effective_exit: number;
  moneyness_pct: number;
  premium_yield_annual_pct: number;
  assignment_prob_pct: number | null;
  open_interest: number | null;
  volume: number | null;
  spread_pct: number | null;
  liquidity: "ok" | "thin" | "unknown";
  source: string;
  estimate: boolean;
  recommended?: boolean;
}

export interface ExitProtectivePut {
  type: "protective_put";
  source: string;
  contracts: number;
  expiry: string;
  dte: number;
  days_to_exempt: number;
  exempt_on: string;
  put_strike: number;
  put_premium: number;
  put_cost_czk: number;
  protected_floor: number;
  collar_call_strike: number;
  collar_call_premium: number | null;
  net_collar_premium: number | null;
  net_collar_czk: number | null;
  tax_saved_by_waiting_czk: number;
  vol_used: number;
  estimate: boolean;
}

export interface ExitUnderlyingQuote {
  last?: number | null;
  bid?: number | null;
  ask?: number | null;
  source?: string;
  quote_timestamp?: string | null;
}

export interface ExitOptionsOverlay {
  symbol: string;
  underlying: number;
  currency: string | null;
  source: string;
  underlying_quote?: ExitUnderlyingQuote | null;
  covered_call: ExitCoveredCall | null;
  covered_call_ladder: ExitCoveredCallRung[];
  protective_put: ExitProtectivePut | null;
  notes: string[];
}

export type ExitRouteKind = "sell_shares" | "covered_call";

export interface ExitRouteEligibility {
  eligible: boolean;
  reasons: string[];
}

export interface ExitCoveredCallRoute extends ExitRouteEligibility {
  capacity_contracts?: number;
  stageable?: boolean;
  planned_exit_shares?: number;
  assignment_shares?: number;
  share_deviation?: number;
  rounded_up?: boolean;
}

export interface ExitRoutes {
  sell_shares: ExitRouteEligibility;
  covered_call: ExitCoveredCallRoute;
  recommended: ExitRouteKind;
}

export interface ExitPosition {
  symbol: string;
  source: string;
  rule: string | null;
  currency: string | null;
  mark_price: number | null;
  quantity: number;
  current_pct: number;
  current_czk: number;
  end_state: "ceiling" | "stub" | "zero";
  target_pct: number;
  exit_czk: number;
  exit_shares: number;
  sell_now_shares: number;
  tax: ExitTaxLayers;
  schedule: ExitSchedule;
  options?: ExitOptionsOverlay | null;
  routes?: ExitRoutes | null;
}

export interface ExitPlanResponse {
  as_of: string;
  snapshot: string | null;
  currency: string;
  invested: number | null;
  config: {
    horizon_days: number;
    adv_slice_pct: number;
    near_exempt_days: number;
    tax_rate: number;
  };
  positions: ExitPosition[];
  totals: {
    exit_czk: number;
    sell_now_czk: number;
    defer_czk: number;
    tax_cost_now: number;
    tax_saved_by_waiting: number;
  };
}

export type RebalanceExecutionRoute =
  "buy_shares" | "sell_shares" | "covered_call" | "cash_secured_put";

export interface RebalanceOptionRung extends ExitQuoteFields {
  strike: number;
  expiry: string;
  dte: number;
  premium: number;
  premium_czk: number;
  effective_exit?: number;
  effective_entry?: number;
  cash_secured_czk?: number;
  moneyness_pct: number;
  premium_yield_annual_pct: number;
  assignment_prob_pct: number | null;
  open_interest: number | null;
  volume: number | null;
  spread_pct: number | null;
  liquidity: "ok" | "thin" | "unknown";
  source: string;
  estimate: boolean;
}

export interface RebalanceRouteResponse {
  symbol: string;
  delta_czk: number;
  direction: "increase" | "reduce";
  planned_shares: number;
  underlying?: number | null;
  currency?: string | null;
  fx_to_base: number;
  source: string;
  direct: {
    kind: "buy_shares" | "sell_shares";
    label: string;
    eligible: boolean;
    reasons: string[];
  };
  option: {
    kind: "covered_call" | "cash_secured_put";
    label: string;
    eligible: boolean;
    stageable: boolean;
    reasons: string[];
    contracts: number;
    assignment_shares: number;
    share_deviation: number;
    rounded_up: boolean;
    collateral_mode?: "cash" | "margin" | null;
    available_cash_czk?: number | null;
    snapshot_cash_czk?: number | null;
    held_short_put_collateral_czk?: number | null;
  };
  recommended: RebalanceExecutionRoute;
  ladder: RebalanceOptionRung[];
}

export interface RebalanceRouteSelection {
  symbol: string;
  route: RebalanceExecutionRoute;
  collateral_mode?: "cash" | "margin";
  conid?: number;
  expiry?: string;
  strike?: number;
  contracts?: number;
  limit_price?: number;
  execution_item_id?: string;
}

// Canonical staged trade-desk legs (GET/POST basket and Exit staging).
export interface TradeLegProvenance {
  source?: string;
  route?: string;
  plan_as_of?: string;
  plan_snapshot?: string;
  plan_fingerprint?: string;
  tranche_index?: number;
  intended_shares?: number;
  rung?: { conid?: number; expiry?: string; strike?: number };
  intended_assigned_shares?: number;
}

export interface StockTradeLeg {
  type?: "stock";
  symbol: string;
  delta_czk: number;
  leg_id?: string;
  route?: "buy_shares" | "sell_shares";
  estimated_shares?: number;
  share_estimate_price?: number;
  share_estimate_currency?: string | null;
  share_estimate_source?: string | null;
  provenance?: TradeLegProvenance[];
}

export interface CoveredCallTradeLeg {
  type: "covered_call";
  symbol: string;
  leg_id?: string;
  route: "covered_call";
  conid: number;
  expiry: string;
  strike: number;
  contracts: number;
  delta_czk?: number;
  limit_price?: number | null;
  quote_timestamp?: string | null;
  staging_warning?: string | null;
  multiplier?: 100;
  provenance?: TradeLegProvenance[];
}

export interface CashSecuredPutTradeLeg {
  type: "cash_secured_put";
  symbol: string;
  leg_id?: string;
  route: "cash_secured_put";
  conid: number;
  expiry: string;
  strike: number;
  contracts: number;
  delta_czk?: number;
  limit_price?: number | null;
  quote_timestamp?: string | null;
  staging_warning?: string | null;
  multiplier?: 100;
  currency?: string | null;
  fx_to_base?: number | null;
  collateral_mode?: "cash" | "margin";
  provenance?: TradeLegProvenance[];
}

export type TradeLeg = StockTradeLeg | CoveredCallTradeLeg | CashSecuredPutTradeLeg;
export type QueuedTradeLeg = TradeLeg & { included: boolean };

export interface ExitStageResponse {
  staged: boolean;
  basket: TradeLeg[];
  tranche?: ExitTranche | null;
  leg?: TradeLeg | null;
  symbol: string;
}

// ---- error envelope (any non-2xx) -----------------------------------------
export interface ApiError {
  error: string;
}
