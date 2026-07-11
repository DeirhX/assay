// Pure view-model for the trade desk. No DOM, no fetch, no timers, no module
// state — every function here is a deterministic function of its arguments
// (data in, number/string/HTML-fragment out), so the money math, the band-track
// geometry, and the confirm/result markup can be unit-tested without mounting a
// single element. trade.ts owns the DOM: it fetches, holds `_status`/`_preview`,
// wires events, and calls these to build the strings it paints. Keep it that
// way — if a new figure or fragment needs to appear on the desk, derive it here
// first so it stays testable.
import { esc, sensitive } from "./core";
import type {
  CoveredCallTradeLeg,
  StockTradeLeg,
  TradeLeg,
  TradeLegProvenance,
} from "./api-types";
import { axisMax, onAxis, r1 } from "./weight-axis";

// r1 now lives in the shared weight-axis module; re-export for the desk's own
// call sites.
export { r1 };

export const sideTag = (side: string) =>
  `<span class="trade-side ${side === "BUY" ? "buy" : "sell"}">${esc(side)}</span>`;

export type TradeInstrumentType = "stock" | "covered_call" | "cash_secured_put";

type DisplayProvenance = Omit<TradeLegProvenance, "rung"> & {
  // Read-only tolerance for queue rows persisted before the canonical contract.
  plan_timestamp?: string;
  tranche?: number;
  rung?: number | TradeLegProvenance["rung"];
};

type OptionPreviewFields = Omit<
  Partial<CoveredCallTradeLeg>,
  "conid" | "multiplier" | "provenance"
> & {
  instrument_type?: TradeInstrumentType;
  conid?: number | string;
  multiplier?: number;
  right?: string;
  current_shares?: number;
  coverage_shares?: number;
  if_assigned_shares?: number;
  premium_credit?: number;
  cash_secured_czk?: number;
  currency?: string | null;
  provenance?: DisplayProvenance | DisplayProvenance[];
  coverage_ok?: boolean;
  coverage_capacity_contracts?: number;
  coverage_working_contracts?: number;
};

export type WorkingOrderPreview = OptionPreviewFields & {
  order_id?: string;
  side?: string;
  remaining_qty?: number;
  filled_qty?: number;
  status?: string;
  order_type?: string;
  price?: number | null;
  tif?: string;
};

export type OrderReconciliation = OptionPreviewFields & {
  symbol: string;
  side: string;
  classification: "none" | "same_side_partial" | "fully_covered" | "opposite_side" | "coverage_blocked" | "oversell_blocked" | "quote_blocked";
  proposed_qty: number;
  working_same_qty?: number;
  working_qty?: number;
  residual_qty: number;
  current_position_qty?: number;
  projected_position_qty?: number;
  requested_projected_position_qty?: number;
  oversell_excess_qty?: number;
  block_reason?: "quote_invalid" | "quote_stale" | "limit_invalid" | string;
  proposed_delta_czk?: number;
  working_delta_czk?: number;
  residual_delta_czk?: number;
  effective_delta_czk?: number;
  working?: WorkingOrderPreview[];
  next_step?: string;
  placeable?: boolean;
};

export function tradeInstrumentType(
  leg: { instrument_type?: TradeInstrumentType; type?: TradeInstrumentType } | null | undefined,
): TradeInstrumentType {
  if (leg?.instrument_type === "covered_call" || leg?.type === "covered_call") {
    return "covered_call";
  }
  if (leg?.instrument_type === "cash_secured_put" || leg?.type === "cash_secured_put") {
    return "cash_secured_put";
  }
  return "stock";
}

export function isCoveredCallLeg(
  leg: { instrument_type?: TradeInstrumentType; type?: TradeInstrumentType } | null | undefined,
): boolean {
  return tradeInstrumentType(leg) === "covered_call";
}

export function isStockLeg(
  leg: { instrument_type?: TradeInstrumentType; type?: TradeInstrumentType } | null | undefined,
): boolean {
  return tradeInstrumentType(leg) === "stock";
}

export function isOptionLeg(
  leg: { instrument_type?: TradeInstrumentType; type?: TradeInstrumentType } | null | undefined,
): boolean {
  return tradeInstrumentType(leg) !== "stock";
}

export function optionRightLabel(right?: string | null): string {
  const r = String(right || "").toUpperCase();
  if (r === "C" || r === "CALL") return "Call";
  if (r === "P" || r === "PUT") return "Put";
  return right ? String(right) : "Option";
}

export function optionContractLabel(leg: {
  symbol?: string;
  expiry?: string;
  strike?: number;
  right?: string;
}): string {
  const sym = String(leg.symbol || "").trim();
  const strike = typeof leg.strike === "number" ? leg.strike : null;
  const expiry = String(leg.expiry || "").trim();
  const right = optionRightLabel(leg.right);
  const parts = [sym, expiry, strike != null ? String(strike) : "", right].filter(Boolean);
  return parts.join(" ");
}

export function coveredCallActionLabel(): string {
  return "Sell to open";
}

export function contractsLabel(contracts?: number | null): string {
  const n = Number(contracts);
  if (!Number.isFinite(n) || n <= 0) return "contracts";
  return `${n} contract${n === 1 ? "" : "s"}`;
}

export function assignmentProjectionLabel(
  currentShares?: number | null,
  ifAssignedShares?: number | null,
): string {
  const cur = Number(currentShares);
  const after = Number(ifAssignedShares);
  const curText = Number.isFinite(cur) ? `${cur} shares` : "current shares";
  const afterText = Number.isFinite(after) ? `${after} if assigned` : "if assigned";
  return `${curText} \u2192 ${afterText}`;
}

export function coverageCheckLabel(
  coverageShares?: number | null,
  contracts?: number | null,
  multiplier = 100,
): string {
  const covered = Number(coverageShares);
  const mult = Number(multiplier) || 100;
  const need = (Number(contracts) || 0) * mult;
  if (!Number.isFinite(covered) || need <= 0) return "Coverage check";
  return covered >= need
    ? `Covered: ${covered} shares for ${contractsLabel(contracts)}`
    : `Uncovered: ${covered} shares available, ${need} required`;
}

export function premiumCreditLabel(czk?: number | null, currency = "CZK"): string {
  const n = Number(czk);
  if (!Number.isFinite(n) || n === 0) return `No premium credit (${currency})`;
  return `Premium credit: ${n.toLocaleString(undefined, { maximumFractionDigits: 0 })} ${currency}`;
}

export function provenanceLabel(
  provenance?: DisplayProvenance | DisplayProvenance[] | null,
): string {
  if (!provenance) return "";
  const row = Array.isArray(provenance) ? provenance[provenance.length - 1] : provenance;
  if (!row) return "";
  const bits: string[] = [];
  if (row.route) bits.push(String(row.route).replace(/_/g, " "));
  const tranche = row.tranche ?? row.tranche_index;
  if (tranche != null) bits.push(`tranche ${tranche}`);
  if (typeof row.rung === "number") bits.push(`rung ${row.rung}`);
  else if (row.rung && typeof row.rung === "object") {
    const contract = [
      row.rung.expiry,
      row.rung.strike != null
        ? `${row.rung.strike} ${row.route === "cash_secured_put" ? "put" : "call"}`
        : "",
    ].filter(Boolean).join(" · ");
    if (contract) bits.push(contract);
  }
  if (row.intended_assigned_shares != null) {
    bits.push(`${row.intended_assigned_shares} shares if assigned`);
  }
  return bits.join(" \u00b7 ");
}

export function residualStockValueCzk(ctx: OrderReconciliation): number {
  if (!isStockLeg(ctx)) return 0;
  const d = ctx.residual_delta_czk;
  return typeof d === "number" && Number.isFinite(d) ? Math.abs(d) : 0;
}

export function reconciliationTitle(c: OrderReconciliation): string {
  if (c.classification === "coverage_blocked") return "Coverage blocked";
  if (c.classification === "oversell_blocked") return "Sell exceeds position";
  if (c.classification === "quote_blocked") return "Waiting for IBKR quote";
  if (isCoveredCallLeg(c)) {
    if (c.classification === "opposite_side") return "Resolve opposite option order";
    if (c.classification === "fully_covered") return "Option order already working";
    if (c.classification === "same_side_partial") return "Reduced by working option order";
    return "New covered call";
  }
  if (tradeInstrumentType(c) === "cash_secured_put") {
    if (c.classification === "opposite_side") return "Resolve opposite put order";
    if (c.classification === "fully_covered") return "Put order already working";
    if (c.classification === "same_side_partial") return "Reduced by working put order";
    return "New cash-secured put";
  }
  if (c.classification === "opposite_side") return "Resolve opposite order";
  if (c.classification === "fully_covered") return "Already covered";
  if (c.classification === "same_side_partial") return "Reduced by working order";
  return "New order";
}

export function previewStats(
  orders: Array<{ side?: string; instrument_type?: TradeInstrumentType }> = [],
  contexts: OrderReconciliation[] = [],
): { buys: number; sells: number; adjusted: number; residualValue: number } {
  return {
    buys: orders.filter((o) => o.side === "BUY").length,
    sells: orders.filter((o) => o.side === "SELL").length,
    adjusted: contexts.filter((c) => c.classification !== "none").length,
    residualValue: contexts.reduce((sum, c) => sum + residualStockValueCzk(c), 0),
  };
}

// Per-order band context (from the server's what-if recompute): where this
// name's weight sits before/after the trade, relative to its target band.
export interface OrderBand {
  low?: number | null;
  high?: number | null;
  before_pct?: number | null;
  after_pct?: number | null;
  status_after?: string | null;
  scope?: "target" | "sleeve";
  scope_name?: string;
  scope_members?: string[];
}

export function orderBandScopeLabel(sym: string, band: OrderBand): string {
  if (band.scope !== "sleeve") return "";
  const name = String(band.scope_name || "sleeve").replace(/[-_]+/g, " ");
  const title = name.replace(/\b\w/g, (c) => c.toUpperCase());
  const members = (band.scope_members || []).join(" + ");
  return `${title} sleeve${members ? ` (${members})` : ""} · ${sym}'s order moves the combined band`;
}

// Shared axis max across the previewed names' bands, rounded to a friendly
// multiple of 5 (10% floor) so every track is comparable on one scale.
export function weightScaleMax(bands: OrderBand[]): number {
  const vals: Array<number | null | undefined> = [];
  for (const b of bands) vals.push(b.high, b.before_pct, b.after_pct);
  return axisMax(vals);
}

// A compact "weight moving within its band" track: the band as a fixed zone, a
// muted current tick, and a bright projected tick coloured by whether the trade
// lands the name inside its band (green) or leaves it out (amber). Reuses the
// rebalance planner's .reb-* track styling so the two surfaces read identically.
export function weightBandTrackHtml(sym: string, b: OrderBand, scaleMax: number): string {
  const low = typeof b.low === "number" ? b.low : 0;
  const high = typeof b.high === "number" ? b.high : low;
  const toP = (v: number) => onAxis(v, scaleMax);
  const before = typeof b.before_pct === "number" ? b.before_pct : null;
  const after = typeof b.after_pct === "number" ? b.after_pct : null;
  const inBand = String(b.status_after || "").toUpperCase() === "IN";
  const zL = toP(low), zW = Math.max(1.5, toP(high) - zL);
  const dir = before != null && after != null && after < before ? "sell" : "buy";
  const conn = before != null && after != null
    ? `<span class="reb-conn ${dir}" style="left:${r1(Math.min(toP(before), toP(after)))}%;width:${r1(Math.abs(toP(after) - toP(before)))}%"></span>`
    : "";
  const curMark = before != null
    ? `<span class="reb-cur-mark" style="left:${r1(toP(before))}%" title="current ${before.toFixed(2)}%"></span>` : "";
  const projMark = after != null
    ? `<span class="reb-proj-mark ${inBand ? "in" : "out"}" style="left:${r1(toP(after))}%" title="after ${after.toFixed(2)}%"></span>` : "";
  const aria = `${sym}: ${before != null ? before.toFixed(1) + "%" : "?"} to ${after != null ? after.toFixed(1) + "%" : "?"} vs band ${low.toFixed(1)}–${high.toFixed(1)}%`;
  return `<div class="reb-track" role="img" aria-label="${esc(aria)}">` +
    `<span class="reb-zone" style="left:${r1(zL)}%;width:${r1(zW)}%"></span>${conn}${curMark}${projMark}</div>`;
}

// The caption under a band track: "8.2% → 6.9% · back inside 5–7%" (or a red
// "still out of band" flag), tying the order back to its reason at confirm time.
export function weightBandCaption(b: OrderBand): string {
  const before = typeof b.before_pct === "number" ? `${b.before_pct.toFixed(1)}%` : "?";
  const after = typeof b.after_pct === "number" ? `${b.after_pct.toFixed(1)}%` : "?";
  const low = typeof b.low === "number" ? b.low : null;
  const high = typeof b.high === "number" ? b.high : null;
  const band = low != null && high != null ? `${low}–${high}%` : "band";
  const inBand = String(b.status_after || "").toUpperCase() === "IN";
  const verdict = inBand
    ? `<span class="trade-band-ok">inside ${esc(band)}</span>`
    : `<span class="trade-band-bad">\u26a0 out of band (${esc(band)})</span>`;
  return `<span class="trade-band-move">${esc(before)} \u2192 ${esc(after)}</span> \u00b7 ${verdict}`;
}

// Before/after/delta for one risk metric, as risk_delta.py emits it.
export interface RiskPair {
  before?: number | null;
  after?: number | null;
  delta?: number | null;
}

// The pre-trade risk delta from the local what-if. Concentration + effective
// names are always present; the correlation-aware pair only when the server had
// a price series (has_correlation).
export interface RiskDelta {
  top1_pct?: RiskPair;
  top5_pct?: RiskPair;
  effective_names?: RiskPair;
  effective_bets?: RiskPair;
  portfolio_vol_pct?: RiskPair;
  has_correlation?: boolean;
  warnings?: string[];
}

// One before -> after risk figure with a coloured delta chip. `higherIsWorse`
// tints a rise red (concentration/vol) and a fall green; effective-names/bets
// flip it (more diversification is better).
export function riskMetricHtml(
  label: string, pair: RiskPair | undefined, unit: string, higherIsWorse: boolean,
): string {
  if (!pair || typeof pair.before !== "number" || typeof pair.after !== "number") return "";
  const d = typeof pair.delta === "number" ? pair.delta : pair.after - pair.before;
  const worse = higherIsWorse ? d > 0 : d < 0;
  const tone = Math.abs(d) < 0.05 ? "flat" : worse ? "bad" : "good";
  const arrow = d > 0 ? "\u2191" : d < 0 ? "\u2193" : "";
  const sign = d > 0 ? "+" : "";
  return `<div class="trade-risk-cell">` +
    `<span class="trade-risk-label">${esc(label)}</span>` +
    `<span class="trade-risk-move">${esc(pair.before.toFixed(unit === "" ? 1 : 1))}${esc(unit)}` +
    ` \u2192 ${esc(pair.after.toFixed(1))}${esc(unit)}</span>` +
    `<span class="trade-risk-delta ${tone}">${esc(arrow)} ${esc(sign + d.toFixed(1))}${esc(unit)}</span>` +
    `</div>`;
}

// The basket-level risk panel: what this trade does to concentration and
// diversification, with any threshold breaches promoted to loud pre-flight
// warnings. risk.py is a destination view; this brings the same lens to the
// decision itself.
export function riskPanelHtml(risk: RiskDelta | undefined): string {
  if (!risk) return "";
  const cells = [
    riskMetricHtml("Top-5 concentration", risk.top5_pct, "%", true),
    riskMetricHtml("Top-1 name", risk.top1_pct, "%", true),
    riskMetricHtml("Effective names", risk.effective_names, "", false),
    risk.has_correlation ? riskMetricHtml("Effective bets", risk.effective_bets, "", false) : "",
    risk.has_correlation ? riskMetricHtml("Portfolio vol", risk.portfolio_vol_pct, "%", true) : "",
  ].filter(Boolean).join("");
  if (!cells && !(risk.warnings || []).length) return "";
  const warns = (risk.warnings || [])
    .map((w) => `<div class="trade-warn">\u26a0 ${esc(w)}</div>`).join("");
  return `<div class="trade-risk">` +
    `<div class="trade-risk-head">Risk impact of this order queue</div>` +
    `<div class="trade-risk-grid">${cells}</div>${warns}</div>`;
}

export function gatewayOrigin(base: string | null | undefined) {
  return String(base || "").replace(/\/v1\/api\/?$/, "") || "https://127.0.0.1:5000";
}

// Buy/sell gross and the single largest trade, from the token-bound basket —
// the CZK the human actually reasoned about (orders carry shares, not CZK). Used
// for the last-mile confirmation modal.
export function basketMoneyFacts(trades?: TradeLeg[]): {
  buy: number; sell: number; largest: { symbol: string; czk: number } | null;
} {
  let buy = 0, sell = 0;
  let largest: { symbol: string; czk: number } | null = null;
  for (const t of trades || []) {
    if (!isStockLeg(t)) continue;
    const d = Number((t as StockTradeLeg).delta_czk) || 0;
    if (d >= 0) buy += d; else sell += -d;
    if (!largest || Math.abs(d) > Math.abs(largest.czk)) largest = { symbol: t.symbol, czk: d };
  }
  return { buy, sell, largest };
}

export interface PlaceResult {
  placed?: Array<Record<string, any>>;
  kind?: string;
  account?: string;
  staged_basket_cleared?: boolean;
}

// Pure HTML for the placement-outcome card: an acknowledgement banner, the
// loop-closing next steps (resync holdings, log the decision), and the raw
// IBKR response tucked into a collapsed drawer instead of a wall of JSON.
export function placeResultHtml(res: PlaceResult): string {
  const placed = res.placed || [];
  const ok = placed.filter((o) => o && (o.order_id || o.orderId || o.order_status)).length;
  const banner = `<div class="trade-bnr ${ok ? "paper" : "warn"}">` +
    `${ok} order(s) acknowledged by IBKR on ${esc(res.kind)} account ${sensitive(esc(res.account), "account id")}.</div>`;
  const cleared = res.staged_basket_cleared
    ? `<span class="muted">The order queue was cleared so it can't be placed twice.</span>` : "";
  const next = `<div class="trade-next">
    <div class="subhead">Close the loop</div>
    <ol class="trade-next-list">
      <li><strong>Resync holdings</strong> so the planner works from your new positions, not the pre-trade snapshot.
        <button class="ghost" type="button" data-trade-next="resync">Resync from IBKR</button></li>
      <li><strong>Log the decision</strong> while the reasoning is fresh — outcomes get scored later.
        <button class="ghost" type="button" data-trade-next="journal">Log to journal</button></li>
    </ol>
    ${cleared}
  </div>`;
  const raw = `<details class="trade-raw-det"><summary>Raw IBKR response</summary>` +
    `<pre class="trade-raw">${esc(JSON.stringify(placed, null, 2))}</pre></details>`;
  return banner + next + raw;
}
