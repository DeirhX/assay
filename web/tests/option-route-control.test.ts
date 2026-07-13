import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../src/core", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/core")>()),
  api: vi.fn(),
}));

import { api } from "../src/core";
import {
  createOptionRouteControl,
  loadCompactOptionRoute,
  OptionRouteLoader,
} from "../src/option-route-control";
import type { RebalanceRouteResponse, RebalanceRouteSelection } from "../src/api-types";

const apiMock = vi.mocked(api);

function route(direction: "increase" | "reduce", stageable = true): RebalanceRouteResponse {
  const put = direction === "increase";
  return {
    symbol: "NVDA",
    delta_czk: put ? 230_000 : -230_000,
    direction,
    planned_shares: 100,
    underlying: 100,
    currency: "USD",
    fx_to_base: 23,
    source: stageable ? "ibkr" : "yahoo",
    direct: {
      kind: put ? "buy_shares" : "sell_shares",
      label: put ? "Buy shares" : "Sell shares",
      eligible: true,
      reasons: [],
    },
    option: {
      kind: put ? "cash_secured_put" : "covered_call",
      label: put ? "Sell cash-secured put" : "Sell covered call",
      eligible: true,
      stageable,
      reasons: stageable ? [] : ["Indicative yahoo levels; exact IBKR contract required."],
      contracts: 1,
      assignment_shares: 100,
      share_deviation: 0,
      rounded_up: false,
      available_cash_czk: put ? 1_000_000 : null,
    },
    recommended: put ? "buy_shares" : "sell_shares",
    ladder: [{
      conid: stageable ? 556 : null,
      strike: put ? 93 : 105,
      expiry: "2026-08-07",
      dte: 37,
      premium: 2,
      premium_czk: 4_600,
      effective_entry: put ? 91 : undefined,
      effective_exit: put ? undefined : 107,
      cash_secured_czk: put ? 213_900 : undefined,
      moneyness_pct: put ? -7 : 5,
      premium_yield_annual_pct: 21.2,
      assignment_prob_pct: 25,
      open_interest: 500,
      volume: 50,
      spread_pct: 10,
      liquidity: "ok",
      source: stageable ? "ibkr" : "yahoo",
      estimate: false,
      stageable,
      executable: stageable,
    }],
  };
}

describe("createOptionRouteControl", () => {
  beforeEach(() => apiMock.mockReset());

  it("defaults to stock and loads the full ladder on option click", async () => {
    apiMock.mockResolvedValue(route("increase"));
    const selected = new Map<string, RebalanceRouteSelection>();
    const control = createOptionRouteControl("NVDA", 230_000, selected);
    document.body.innerHTML = "";
    document.body.append(control.controls, control.detail);

    expect(control.controls.className).toContain("route-controls");
    expect(selected.get("NVDA")?.route).toBe("buy_shares");
    const optionBtn = [...control.controls.querySelectorAll("button")]
      .find((node) => node.textContent === "Cash-secured put")!;
    optionBtn.click();
    await vi.waitFor(() => expect(control.detail.querySelector(".reb-route-ladder")).toBeTruthy());
    expect(control.detail.querySelectorAll("th")).toHaveLength(8);

    const use = [...control.detail.querySelectorAll("button")]
      .find((node) => node.textContent === "Use")!;
    use.click();
    expect(selected.get("NVDA")).toMatchObject({
      route: "cash_secured_put",
      conid: 556,
      strike: 93,
    });
  });

  it("cancels in-flight ladder loads when sync changes the amount", async () => {
    let resolveRoute!: (value: RebalanceRouteResponse) => void;
    const pending = new Promise<RebalanceRouteResponse>((resolve) => {
      resolveRoute = resolve;
    });
    apiMock.mockReturnValue(pending);
    const selected = new Map<string, RebalanceRouteSelection>();
    const control = createOptionRouteControl("NVDA", 230_000, selected);
    control.controls.querySelectorAll("button")[1].dispatchEvent(new MouseEvent("click"));
    control.sync(0);
    resolveRoute(route("increase"));
    await pending;
    expect(control.detail.querySelector(".reb-route-ladder")).toBeNull();
  });
});

describe("loadCompactOptionRoute", () => {
  beforeEach(() => apiMock.mockReset());

  it("picks the first stageable rung for the composer summary", async () => {
    apiMock.mockResolvedValue(route("increase"));
    const loader = new OptionRouteLoader();
    const result = await loadCompactOptionRoute(loader, "NVDA", 100_000, "increase");
    expect(result?.eligible).toBe(true);
    expect(result?.selection).toMatchObject({ route: "cash_secured_put", conid: 556 });
    expect(result?.html).toContain("2026-08-07 · 93P");
  });

  it("returns null when a newer load supersedes the request", async () => {
    apiMock.mockResolvedValue(route("increase"));
    const loader = new OptionRouteLoader();
    const first = loadCompactOptionRoute(loader, "NVDA", 100_000, "increase");
    loader.cancel();
    const second = await loadCompactOptionRoute(loader, "NVDA", 200_000, "increase");
    expect(await first).toBeNull();
    expect(second?.eligible).toBe(true);
  });
});
