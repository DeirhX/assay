// Tests for the pure trade-desk view-model (extracted from trade.ts so the money
// math, band-track geometry, and confirm/result markup are testable without
// mounting the desk). The invariants that matter: money facts split buys/sells
// and find the largest absolute leg, the shared band axis rounds up with a 10%
// floor, the risk panel hides correlation-only metrics without a price series,
// and the placement-result HTML blurs the account id and closes the loop.
import { describe, expect, it } from "vitest";
import {
  assignmentProjectionLabel,
  basketMoneyFacts,
  contractsLabel,
  coveredCallActionLabel,
  coverageCheckLabel,
  isCoveredCallLeg,
  optionContractLabel,
  orderBandScopeLabel,
  placeResultHtml,
  premiumCreditLabel,
  previewStats,
  provenanceLabel,
  reconciliationTitle,
  residualStockValueCzk,
  riskPanelHtml,
  tradeInstrumentType,
  weightBandCaption,
  weightScaleMax,
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

  it("ignores covered-call legs in CZK buy/sell totals", () => {
    const f = basketMoneyFacts([
      { symbol: "AAPL", delta_czk: 1000 },
      {
        type: "covered_call", symbol: "AAPL", route: "covered_call", leg_id: "cc-1",
        contracts: 1, conid: 12345, expiry: "2026-08-21", strike: 250,
      },
      {
        type: "cash_secured_put", symbol: "NVDA", route: "cash_secured_put",
        contracts: 1, conid: 12346, expiry: "2026-08-21", strike: 150,
      },
      { symbol: "MSFT", delta_czk: -2500 },
    ]);
    expect(f.buy).toBe(1000);
    expect(f.sell).toBe(2500);
    expect(f.largest).toEqual({ symbol: "MSFT", czk: -2500 });
  });

  it("behaves identically for legacy stock legs without instrument_type", () => {
    const legacy = basketMoneyFacts([
      { symbol: "AAPL", delta_czk: 1000 },
      { symbol: "MSFT", delta_czk: -2500 },
    ]);
    const explicit = basketMoneyFacts([
      { symbol: "AAPL", delta_czk: 1000, type: "stock" },
      { symbol: "MSFT", delta_czk: -2500, type: "stock" },
    ]);
    expect(explicit).toEqual(legacy);
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
    expect(reconciliationTitle({
      symbol: "A", side: "SELL", classification: "oversell_blocked",
      proposed_qty: 12, residual_qty: 0,
    })).toBe("Sell exceeds position");
    expect(reconciliationTitle({
      symbol: "A", side: "SELL", instrument_type: "covered_call",
      classification: "quote_blocked", proposed_qty: 1, residual_qty: 0,
    })).toBe("Waiting for IBKR quote");
  });

  it("excludes covered-call contexts from residual stock value totals", () => {
    const stats = previewStats(
      [{ side: "SELL", instrument_type: "covered_call" }],
      [
        {
          symbol: "NVDA", side: "SELL", instrument_type: "covered_call",
          classification: "none", proposed_qty: 1, residual_qty: 1,
          contracts: 1, premium_credit: 4200,
        },
        {
          symbol: "AMD", side: "BUY", instrument_type: "stock",
          classification: "none", proposed_qty: 10, residual_qty: 10,
          residual_delta_czk: 5000,
        },
      ],
    );
    expect(stats.residualValue).toBe(5000);
  });

  it("does not treat absent residual_delta_czk as zero stock value for options", () => {
    expect(residualStockValueCzk({
      symbol: "NVDA", side: "SELL", instrument_type: "covered_call",
      classification: "none", proposed_qty: 1, residual_qty: 1,
    })).toBe(0);
    expect(residualStockValueCzk({
      symbol: "AMD", side: "BUY", instrument_type: "stock",
      classification: "none", proposed_qty: 10, residual_qty: 10,
    })).toBe(0);
    expect(residualStockValueCzk({
      symbol: "AMD", side: "BUY", instrument_type: "stock",
      classification: "none", proposed_qty: 10, residual_qty: 10,
      residual_delta_czk: 1200,
    })).toBe(1200);
  });

  it("uses option-specific reconciliation titles", () => {
    expect(reconciliationTitle({
      symbol: "NVDA", side: "SELL", instrument_type: "covered_call",
      classification: "none", proposed_qty: 1, residual_qty: 1,
    })).toBe("New covered call");
    expect(reconciliationTitle({
      symbol: "NVDA", side: "SELL", instrument_type: "cash_secured_put",
      classification: "none", proposed_qty: 1, residual_qty: 1,
    })).toBe("New short put");
    expect(reconciliationTitle({
      symbol: "NVDA", side: "SELL", instrument_type: "covered_call",
      classification: "fully_covered", proposed_qty: 1, residual_qty: 0,
    })).toBe("Option order already working");
  });
});

describe("option leg helpers", () => {
  it("classifies instrument types with stock as the default", () => {
    expect(tradeInstrumentType({})).toBe("stock");
    expect(tradeInstrumentType({ instrument_type: "stock" })).toBe("stock");
    expect(tradeInstrumentType({ instrument_type: "covered_call" })).toBe("covered_call");
    expect(tradeInstrumentType({ instrument_type: "cash_secured_put" })).toBe("cash_secured_put");
    expect(isCoveredCallLeg({ instrument_type: "covered_call" })).toBe(true);
  });

  it("formats contract, coverage, assignment, and provenance copy", () => {
    expect(coveredCallActionLabel()).toBe("Sell to open");
    expect(optionContractLabel({
      symbol: "NVDA", expiry: "20260417", strike: 180, right: "C",
    })).toBe("NVDA 20260417 180 Call");
    expect(contractsLabel(1)).toBe("1 contract");
    expect(contractsLabel(2)).toBe("2 contracts");
    expect(assignmentProjectionLabel(100, 0)).toBe("100 shares \u2192 0 if assigned");
    expect(coverageCheckLabel(100, 1)).toBe("Covered: 100 shares for 1 contract");
    expect(coverageCheckLabel(50, 1)).toBe("Uncovered: 50 shares available, 100 required");
    expect(premiumCreditLabel(4200)).toMatch(/Premium credit: 4.200 CZK/);
    expect(provenanceLabel({
      route: "covered_call", tranche: 2, rung: 1, intended_assigned_shares: 100,
    })).toContain("covered call");
    expect(provenanceLabel({
      route: "covered_call", tranche: 2, rung: 1, intended_assigned_shares: 100,
    })).toContain("100 shares if assigned");
    expect(provenanceLabel({
      route: "covered_call",
      rung: { expiry: "2026-08-21", strike: 105, conid: 555 },
    })).toContain("2026-08-21 · 105 call");
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

  it("makes a partial-basket failure impossible to mistake for full success", () => {
    const html = placeResultHtml({
      kind: "paper",
      account: "DU1",
      placed: [{ order_id: "1" }],
      placement_incomplete: true,
      warnings: ["Review working orders before rebuilding the remainder."],
    });
    expect(html).toContain("Placement stopped early");
    expect(html).toContain("trade-bnr bad");
    expect(html).toContain("Review working orders");
  });
});
