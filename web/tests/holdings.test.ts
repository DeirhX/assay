import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../src/core", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/core")>()),
  api: vi.fn(),
}));

import { api } from "../src/core";
import { groupHoldingPositions, loadHoldings } from "../src/holdings";
import type { HoldingPosition } from "../src/api-types";

const apiMock = vi.mocked(api);

const position = (over: Partial<HoldingPosition>): HoldingPosition => ({
  symbol: "PYPL",
  provider_symbol: "PYPL",
  researchable: true,
  description: "PayPal",
  asset_class: "STK",
  quantity: 1300,
  percent_of_nav: 4,
  broker_percent_of_nav: 4,
  base_market_value: 1_200_000,
  currency: "USD",
  unrealized_pnl: 0,
  issuer_country_code: "US",
  option: null,
  ...over,
});

describe("holdings grouping", () => {
  it("combines stock and every option leg under one underlying row", () => {
    const groups = groupHoldingPositions([
      position({}),
      position({
        symbol: "PYPL  260814C00047000",
        provider_symbol: "PYPL  260814C00047000",
        researchable: false,
        asset_class: "OPT",
        quantity: -7,
        percent_of_nav: -0.1,
        base_market_value: -30_000,
        option: {
          underlying: "PYPL",
          expiry: "2026-08-14",
          right: "C",
          strike: 47,
          contracts: -7,
          multiplier: 100,
          notional_base: 700_000,
          exercise_pct: -2.3,
        },
      }),
      position({
        symbol: "PYPL  260821P00045000",
        provider_symbol: "PYPL  260821P00045000",
        researchable: false,
        asset_class: "OPT",
        quantity: 1,
        percent_of_nav: 0.02,
        base_market_value: 5_000,
        option: {
          underlying: "PYPL",
          expiry: "2026-08-21",
          right: "P",
          strike: 45,
          contracts: 1,
          multiplier: 100,
          notional_base: 100_000,
          exercise_pct: -0.3,
        },
      }),
    ]);

    expect(groups).toHaveLength(1);
    expect(groups[0].symbol).toBe("PYPL");
    expect(groups[0].stocks).toHaveLength(1);
    expect(groups[0].options).toHaveLength(2);
    expect(groups[0].optionExercisePct).toBeCloseTo(-2.6);
    expect(groups[0].baseMarketValue).toBe(1_175_000);
  });
});

describe("holdings live-data provenance", () => {
  beforeEach(() => {
    document.body.innerHTML =
      '<div id="hold-status"></div><div id="hold-synced"></div>' +
      '<div id="hold-gateway-notice"></div><div id="hold-result"></div>';
    apiMock.mockReset();
  });

  it("keeps the Flex snapshot and explains a missing gateway overlay", async () => {
    apiMock.mockImplementation((path: string) => {
      if (path === "/api/holdings") {
        return Promise.resolve({
          net_asset_value: 1_000_000,
          invested_value: 900_000,
          generated_at: "2026-07-10T10:00:00Z",
          sizing_legend: {},
          positions: [],
        });
      }
      if (path === "/api/holdings/live") {
        return Promise.resolve({
          available: false,
          reason: "gateway session is not authenticated",
        });
      }
      return Promise.resolve({});
    });

    await loadHoldings();
    await vi.waitFor(() => {
      expect(document.getElementById("hold-gateway-notice")!.textContent)
        .toContain("Showing the Flex snapshot");
    });
    expect(document.getElementById("hold-gateway-notice")!.textContent)
      .toContain("not authenticated");
    expect(document.getElementById("hold-result")!.textContent)
      .toContain("Net asset value");
  });
});
