// Tests for the LLM prompt export: weight-based, privacy-safe (no CZK), sleeve
// grouping, an options overlay from OCC legs, and a per-ticker focus block.
import { describe, expect, it } from "vitest";
import { buildPortfolioPrompt } from "../src/prompt-export";
import type { HoldingsPayload, RebalancePlan } from "../src/api-types";

const holdings = (): HoldingsPayload => ({
  net_asset_value: 1000,
  invested_value: 900,
  generated_at: "2026-06-29T10:00:00Z",
  sizing_legend: {},
  positions: [
    // base_market_value/unrealized_pnl drive uPnL%: (mv - pnl) is cost.
    { symbol: "NVDA", provider_symbol: "NVDA", researchable: true, description: null,
      asset_class: "STK", quantity: 10, percent_of_nav: 12.34, broker_percent_of_nav: null,
      base_market_value: 110, currency: "USD", unrealized_pnl: 10, issuer_country_code: null, option: null },
    { symbol: "KLAC", provider_symbol: "KLAC", researchable: true, description: null,
      asset_class: "STK", quantity: 2, percent_of_nav: 2.0, broker_percent_of_nav: null,
      base_market_value: 100, currency: "USD", unrealized_pnl: -5, issuer_country_code: null, option: null },
    { symbol: "UNH", provider_symbol: "UNH", researchable: true, description: null,
      asset_class: "STK", quantity: 3, percent_of_nav: 6.0, broker_percent_of_nav: null,
      base_market_value: 60, currency: "USD", unrealized_pnl: 0, issuer_country_code: null, option: null },
    // A short put on KLAC -> bullish (+) exercise exposure.
    { symbol: "KLAC  260717P00238000", provider_symbol: "KLAC", researchable: false, description: "KLAC 17JUL26 238 P",
      asset_class: "OPT", quantity: -2, percent_of_nav: 0.1, broker_percent_of_nav: null,
      base_market_value: -100, currency: "USD", unrealized_pnl: 0, issuer_country_code: null,
      option: { underlying: "KLAC", expiry: "2026-07-17", right: "P", strike: 238, contracts: -2, multiplier: 100, notional_base: 1000, exercise_pct: 3.5 } },
  ],
});

const plan = (): RebalancePlan => ({
  as_of: "2026-06-29", snapshot: null, nav: 1000, invested: 900, currency: "CZK",
  cash_target_pct: 5, funding_order: [], untargeted: [], untargeted_pct: 0,
  provenance: { NVDA: { conviction: "high" }, KLAC: { conviction: "medium" } },
  rows: [
    { key: "NVDA", name: "NVDA", kind: "target", rule: "hold", held: true, current_pct: 12.34,
      current_czk: null, low: 10, high: 12, mid: 11, status: "ABOVE", drift_pct: 0, action: null,
      suggest_delta_pct: 0, suggest_delta_czk: null, note: "Trim on strength.", members: null, interactive: true,
      price_gate: { buy_below: 100, trim_above: 180, current: 150 } as any },
    { key: "[semis-equipment]", name: "semis-equipment", kind: "sleeve", rule: "accumulate", held: false,
      current_pct: 2, current_czk: null, low: 5, high: 7, mid: 6, status: "BELOW", drift_pct: 0,
      action: "buy", suggest_delta_pct: 3, suggest_delta_czk: null, note: null, interactive: false,
      members: [{ symbol: "KLAC", current_pct: 2, current_czk: null }] },
  ],
});

describe("buildPortfolioPrompt", () => {
  it("emits a weight/uPnL table with sleeve grouping and no absolute values", () => {
    const md = buildPortfolioPrompt(holdings(), plan());
    expect(md).toContain("weights = % of invested book");
    // NVDA: 12.34%, uPnL = 10/(110-10) = +10.0%, hold (high), band 10–12, no sleeve.
    expect(md).toMatch(/\| NVDA \| 12\.34% \| \+10\.0% \| hold \(high\) \| 10\u201312 \| \u2014 \|/);
    // KLAC is a sleeve member -> inherits accumulate + the sleeve name.
    expect(md).toMatch(/\| KLAC \| 2\.00% \|.*accumulate \(medium\) \| 5\u20137 \| semis-equipment \|/);
    // UNH is untargeted -> no stance/band/sleeve.
    expect(md).toMatch(/\| UNH \| 6\.00% \| \+0\.0% \| \u2014 \| \u2014 \| \u2014 \|/);
    // Privacy: no CZK amounts anywhere.
    expect(md).not.toMatch(/CZK/);
    // The option row must not appear as an equity line.
    expect(md).not.toMatch(/\| KLAC  260717P/);
  });

  it("summarizes options exposure per underlying", () => {
    const md = buildPortfolioPrompt(holdings(), plan());
    expect(md).toContain("## Options overlay");
    expect(md).toMatch(/\*\*KLAC\*\*: short 2\u00d7 238P \u2192 net \+3\.5% underlying/);
  });

  it("adds a focus block with stance, options, sleeve peers and a question scaffold", () => {
    const md = buildPortfolioPrompt(holdings(), plan(), "NVDA");
    expect(md).toContain("## Focus: NVDA");
    expect(md).toContain("Weight 12.34%");
    expect(md).toContain("Stance: **hold**, band 10\u201312, conviction high");
    expect(md).toContain("Standing note: Trim on strength.");
    expect(md).toContain("Locked price levels: buy \u2264 100, trim \u2265 180");
    expect(md).toContain("## Question");
    expect(md).toContain("<your question about NVDA here>");
  });

  it("works without a plan (weights + options only)", () => {
    const md = buildPortfolioPrompt(holdings(), null);
    expect(md).toMatch(/\| NVDA \| 12\.34% \| \+10\.0% \| \u2014 \| \u2014 \| \u2014 \|/);
    expect(md).toContain("## Options overlay");
  });
});
