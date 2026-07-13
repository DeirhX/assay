import type { ExitQuoteFields } from "./api-types";

export const EXECUTION_QUOTE_MAX_AGE_MS = 120_000;

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
  rung: Pick<ExitQuoteFields, "quote_fresh" | "stageable">,
): string {
  if (rung.quote_fresh) return "fresh";
  if (rung.stageable) return "stale / no quote";
  return "indicative";
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
