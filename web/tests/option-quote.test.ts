import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  EXECUTION_QUOTE_MAX_AGE_MS,
  formatQuoteSourceLabel,
  isLiveQuoteSource,
  liquidityChipClass,
  quoteBidAskCrossed,
  quoteBidAskMissing,
  quoteBidAskValid,
  quoteFreshnessCaption,
  quoteFreshnessLabel,
  quotePresentationState,
  quoteSourceChipClass,
  quoteTimestampIsStale,
  rungQuoteIsStale,
} from "../src/option-quote";

describe("option-quote", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-13T10:00:00.000Z"));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("treats missing, future-skewed, and aged timestamps as stale", () => {
    expect(quoteTimestampIsStale(null)).toBe(true);
    expect(quoteTimestampIsStale("not-a-date")).toBe(true);
    expect(quoteTimestampIsStale("2026-07-13T10:00:20.000Z")).toBe(true);
    const fresh = new Date(Date.now() - 30_000).toISOString();
    expect(quoteTimestampIsStale(fresh)).toBe(false);
    const aged = new Date(Date.now() - EXECUTION_QUOTE_MAX_AGE_MS - 1).toISOString();
    expect(quoteTimestampIsStale(aged)).toBe(true);
  });

  it("combines quote_fresh with timestamp staleness", () => {
    const fresh = new Date(Date.now() - 30_000).toISOString();
    expect(rungQuoteIsStale({ quote_fresh: true, quote_timestamp: fresh })).toBe(false);
    expect(rungQuoteIsStale({ quote_fresh: false, quote_timestamp: fresh })).toBe(true);
  });

  it("classifies bid/ask health", () => {
    expect(quoteBidAskValid(2.4, 2.6)).toBe(true);
    expect(quoteBidAskMissing(null, 2.6)).toBe(true);
    expect(quoteBidAskCrossed(2.7, 2.5)).toBe(true);
  });

  it("formats freshness, source, and liquidity labels for ladder cells", () => {
    expect(quoteFreshnessCaption({ quote_fresh: true, stageable: true })).toBe("fresh");
    expect(quoteFreshnessCaption({ quote_fresh: false, stageable: true })).toBe("stale / no quote");
    expect(quoteFreshnessCaption({ quote_fresh: false, stageable: false })).toBe("indicative");
    expect(quotePresentationState({ quote_fresh: false, stageable: true })).toBe("stale");
    expect(quoteFreshnessLabel({ quote_fresh: true, stageable: true })).toBe("Fresh quote");
    expect(quoteFreshnessLabel({ quote_fresh: false, stageable: true })).toBe("Quote needed at preview");
    expect(quoteFreshnessLabel({ quote_fresh: false, stageable: false })).toBe("Indicative only");
    expect(formatQuoteSourceLabel("yahoo_finance")).toBe("yahoo finance");
    expect(quoteSourceChipClass("ibkr")).toBe("good");
    expect(quoteSourceChipClass("yahoo")).toBe("muted");
    expect(liquidityChipClass("ok")).toBe("good");
    expect(liquidityChipClass("thin")).toBe("warn");
    expect(liquidityChipClass("unknown")).toBe("muted");
  });

  it("distinguishes live chain sources from modeled premiums", () => {
    expect(isLiveQuoteSource("ibkr", false)).toBe(true);
    expect(isLiveQuoteSource("yahoo", false)).toBe(true);
    expect(isLiveQuoteSource("ibkr", true)).toBe(false);
  });
});
