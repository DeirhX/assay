import type { ExitQuoteFields } from "./api-types";

export const EXECUTION_QUOTE_MAX_AGE_MS = 120_000;
type QuotePresentationFields = Pick<ExitQuoteFields, "quote_fresh" | "stageable">;
export type QuotePresentationState = "fresh" | "stale" | "indicative";

export function quoteTimestampIsStale(timestamp: string | null | undefined): boolean {
  const quoteTime = timestamp ? new Date(timestamp).getTime() : NaN;
  const localAge = Date.now() - quoteTime;
  return !Number.isFinite(quoteTime)
    || localAge < -10_000
    || localAge > EXECUTION_QUOTE_MAX_AGE_MS;
}

export function rungQuoteIsStale(r: Pick<ExitQuoteFields, "quote_fresh" | "quote_timestamp">): boolean {
  return r.quote_fresh === false || quoteTimestampIsStale(r.quote_timestamp);
}

export function quoteBidAskValid(
  bid: number | null | undefined,
  ask: number | null | undefined,
): boolean {
  return typeof bid === "number" && typeof ask === "number"
    && bid > 0 && ask > 0 && bid <= ask;
}

export function quoteBidAskCrossed(
  bid: number | null | undefined,
  ask: number | null | undefined,
): boolean {
  return typeof bid === "number" && typeof ask === "number"
    && bid > 0 && ask > 0 && bid > ask;
}

export function quoteBidAskMissing(
  bid: number | null | undefined,
  ask: number | null | undefined,
): boolean {
  return bid == null || ask == null || bid <= 0 || ask <= 0;
}

export function quoteFreshnessCaption(
  rung: QuotePresentationFields,
): string {
  const state = quotePresentationState(rung);
  if (state === "fresh") return "fresh";
  if (state === "stale") return "stale / no quote";
  return state;
}

export function quotePresentationState(rung: QuotePresentationFields): QuotePresentationState {
  if (rung.quote_fresh) return "fresh";
  if (rung.stageable) return "stale";
  return "indicative";
}

export function quoteFreshnessLabel(rung: QuotePresentationFields): string {
  const state = quotePresentationState(rung);
  if (state === "fresh") return "Fresh quote";
  if (state === "stale") return "Quote needed at preview";
  return "Indicative only";
}

export function formatQuoteSourceLabel(source: string): string {
  return source.replace(/_/g, " ");
}

export function quoteSourceChipClass(source: string): "good" | "muted" {
  return source === "ibkr" ? "good" : "muted";
}

export function liquidityChipClass(
  liquidity: "ok" | "thin" | "unknown",
): "good" | "warn" | "muted" {
  if (liquidity === "ok") return "good";
  if (liquidity === "thin") return "warn";
  return "muted";
}

export function isLiveQuoteSource(source: string, estimate: boolean): boolean {
  return !estimate && (source === "ibkr" || source === "alpaca" || source === "yahoo");
}
