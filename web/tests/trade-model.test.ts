// Tests for the pure trade-desk view-model (extracted from trade.ts so the money
// math, band-track geometry, and confirm/result markup are testable without
// mounting the desk). The invariants that matter: money facts split buys/sells
// and find the largest absolute leg, the shared band axis rounds up with a 10%
// floor, the risk panel hides correlation-only metrics without a price series,
// and the placement-result HTML blurs the account id and closes the loop.
import { describe, expect, it } from "vitest";
import {
  basketMoneyFacts, gatewayOrigin, orderBandScopeLabel, placeResultHtml, previewStats, reconciliationTitle, riskPanelHtml,
  weightBandCaption, weightScaleMax,
} from "../src/trade-model";

describe("basketMoneyFacts", () => {
  it("splits gross buys from gross sells and finds the largest absolute leg", () => {
    const f = basketMoneyFacts([
      { symbol: "AAPL", delta_czk: 1000 },
      { symbol: "MSFT", delta_czk: -2500 },
      { symbol: "NVDA", delta_czk: 500 },
    ]);
    expect(f.buy).toBe(1500);
    expect(f.sell).toBe(2500);
    expect(f.largest).toEqual({ symbol: "MSFT", czk: -2500 });
  });

  it("is all-zero on an empty or missing basket", () => {
    expect(basketMoneyFacts([])).toEqual({ buy: 0, sell: 0, largest: null });
    expect(basketMoneyFacts()).toEqual({ buy: 0, sell: 0, largest: null });
  });

  it("coerces non-numeric sizes to zero rather than poisoning the totals", () => {
    const f = basketMoneyFacts([{ symbol: "X", delta_czk: NaN as unknown as number }]);
    expect(f.buy).toBe(0);
    expect(f.sell).toBe(0);
  });

  it("excludes covered-call premium from immediate stock cash totals", () => {
    const f = basketMoneyFacts([
      { symbol: "AMD", delta_czk: -2500 },
      {
        leg_type: "covered_call", leg_id: "covered_call:AMD:2026-08-21:105:C",
        symbol: "AMD", contracts: 1, conid: 999, expiry: "2026-08-21",
        strike: 105, right: "C", limit_price: 2.5, multiplier: 100,
        quote: { bid: 2.4, ask: 2.6, last: 2.5 },
        underlying_quote: { last: 100 }, provenance: [],
      },
    ]);
    expect(f).toEqual({
      buy: 0,
      sell: 2500,
      largest: { symbol: "AMD", czk: -2500 },
    });
  });
});

describe("working-order preview model", () => {
  it("summarizes only residual orders and counts reconciled symbols", () => {
    const stats = previewStats(
      [{ side: "BUY" }, { side: "SELL" }],
      [
        { symbol: "A", side: "BUY", classification: "same_side_partial",
          proposed_qty: 10, residual_qty: 4, residual_delta_czk: 4000 },
        { symbol: "B", side: "SELL", classification: "opposite_side",
          proposed_qty: 3, residual_qty: 0, residual_delta_czk: 0 },
      ],
    );
    expect(stats).toEqual({ buys: 1, sells: 1, adjusted: 2, residualValue: 4000 });
  });

  it("uses concise decision labels for non-placeable rows", () => {
    expect(reconciliationTitle({
      symbol: "A", side: "BUY", classification: "fully_covered",
      proposed_qty: 3, residual_qty: 0,
    })).toBe("Already covered");
    expect(reconciliationTitle({
      symbol: "A", side: "BUY", classification: "opposite_side",
      proposed_qty: 3, residual_qty: 0,
    })).toBe("Resolve opposite order");
  });
});

describe("weightScaleMax", () => {
  it("rounds the largest band edge / weight up to a multiple of 5", () => {
    expect(weightScaleMax([{ high: 7, before_pct: 8.2, after_pct: 6.9 }])).toBe(10);
    expect(weightScaleMax([{ high: 22, before_pct: 18, after_pct: 20 }])).toBe(25);
  });

  it("floors at 10 for a book of small bands", () => {
    expect(weightScaleMax([{ high: 2, before_pct: 1, after_pct: 1.5 }])).toBe(10);
    expect(weightScaleMax([])).toBe(10);
  });
});

describe("weightBandCaption", () => {
  it("reads back inside for an in-band land", () => {
    const cap = weightBandCaption({ low: 5, high: 7, before_pct: 8.2, after_pct: 6.9, status_after: "IN" });
    expect(cap).toContain("8.2% \u2192 6.9%");
    expect(cap).toContain("inside 5–7%");
    expect(cap).not.toContain("out of band");
  });

  it("flags an out-of-band land", () => {
    const cap = weightBandCaption({ low: 5, high: 7, before_pct: 9, after_pct: 8, status_after: "OUT" });
    expect(cap).toContain("out of band");
  });
});

describe("orderBandScopeLabel", () => {
  it("explains when an order moves a combined sleeve band", () => {
    expect(orderBandScopeLabel("ADI", {
      scope: "sleeve", scope_name: "analog", scope_members: ["TXN", "ADI"],
    })).toBe("Analog sleeve (TXN + ADI) · ADI's order moves the combined band");
  });

  it("stays quiet for a standalone target", () => {
    expect(orderBandScopeLabel("AMAT", { scope: "target" })).toBe("");
  });
});

describe("riskPanelHtml", () => {
  it("is empty with no risk data", () => {
    expect(riskPanelHtml(undefined)).toBe("");
  });

  it("hides the correlation-only metrics when there is no price series", () => {
    const html = riskPanelHtml({
      top5_pct: { before: 40, after: 38 },
      effective_names: { before: 8, after: 9 },
      portfolio_vol_pct: { before: 12, after: 11 },
      has_correlation: false,
    });
    expect(html).toContain("Top-5 concentration");
    expect(html).toContain("Effective names");
    expect(html).not.toContain("Portfolio vol");
    expect(html).not.toContain("Effective bets");
  });

  it("shows correlation-aware metrics and promotes warnings when present", () => {
    const html = riskPanelHtml({
      top5_pct: { before: 40, after: 45 },
      portfolio_vol_pct: { before: 12, after: 14 },
      has_correlation: true,
      warnings: ["concentration rising"],
    });
    expect(html).toContain("Portfolio vol");
    expect(html).toContain("concentration rising");
  });
});

describe("gatewayOrigin", () => {
  it("strips the /v1/api suffix to the bare origin", () => {
    expect(gatewayOrigin("https://localhost:5000/v1/api")).toBe("https://localhost:5000");
    expect(gatewayOrigin("https://localhost:5000/v1/api/")).toBe("https://localhost:5000");
  });

  it("falls back to the default gateway when unset", () => {
    expect(gatewayOrigin(null)).toBe("https://127.0.0.1:5000");
    expect(gatewayOrigin(undefined)).toBe("https://127.0.0.1:5000");
  });
});

describe("placeResultHtml (post-placement loop close)", () => {
  const res = {
    kind: "paper",
    account: "DU12345",
    staged_basket_cleared: true,
    placed: [{ order_id: "1" }, { orderId: "2" }, { note: "no id" }],
  };

  it("counts acknowledged orders and names the account", () => {
    const html = placeResultHtml(res);
    expect(html).toContain("2 order(s) acknowledged");
    expect(html).toContain("DU12345");
    expect(html).toContain("trade-bnr paper");
  });

  it("wraps the account id so privacy mode blurs it", () => {
    const html = placeResultHtml(res);
    expect(html).toMatch(/data-sensitive[^>]*>DU12345</);
  });

  it("offers the loop-closing next steps and the cleared-basket notice", () => {
    const html = placeResultHtml(res);
    expect(html).toContain('data-trade-next="resync"');
    expect(html).toContain('data-trade-next="journal"');
    expect(html).toContain("cleared so it can't be placed twice");
  });

  it("collapses the raw response instead of dumping JSON", () => {
    const html = placeResultHtml(res);
    expect(html).toContain("<details");
    expect(html).toContain("Raw IBKR response");
  });

  it("warns when nothing was acknowledged; no cleared note when the basket was kept", () => {
    const html = placeResultHtml({ kind: "paper", account: "DU1", placed: [{}] });
    expect(html).toContain("0 order(s) acknowledged");
    expect(html).toContain("trade-bnr warn");
    expect(html).not.toContain("placed twice");
  });
});
