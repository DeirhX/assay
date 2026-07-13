import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../src/core", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/core")>()),
  api: vi.fn(),
}));

import { api, state } from "../src/core";
import { executionRouteChoices, renderWhatif } from "../src/rebalance";
import type {
  RebalanceRouteResponse, RebalanceRouteSelection, WhatifTrade,
  Whatif,
} from "../src/api-types";

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

describe("rebalance execution route choices", () => {
  beforeEach(() => apiMock.mockReset());

  it("defaults both directions to stock and selects an exact CSP rung lazily", async () => {
    apiMock.mockResolvedValue(route("increase"));
    const trades: WhatifTrade[] = [
      { symbol: "NVDA", delta_czk: 230_000 },
      { symbol: "AMD", delta_czk: -100_000 },
    ];
    const selected = new Map<string, RebalanceRouteSelection>();
    const host = executionRouteChoices(trades, selected);
    document.body.innerHTML = "";
    document.body.appendChild(host);

    expect(selected.get("NVDA")?.route).toBe("buy_shares");
    expect(selected.get("AMD")?.route).toBe("sell_shares");
    expect(host.textContent).toContain("Put option");
    expect(host.textContent).toContain("Covered call");
    expect(host.querySelectorAll(".reb-route-row-detail:not([hidden])")).toHaveLength(0);

    const button = [...host.querySelectorAll("button")]
      .find((node) => node.textContent === "Put option")!;
    button.click();
    await vi.waitFor(() => expect(host.textContent).toContain("Sell cash-secured put"));
    expect(apiMock).toHaveBeenCalledWith(
      expect.stringContaining("/api/rebalance/route?"),
      "GET",
      null,
      { timeoutMs: 60_000 },
    );
    const use = [...host.querySelectorAll("button")]
      .find((node) => node.textContent === "Use contract")!;
    use.click();
    expect(selected.get("NVDA")).toMatchObject({
      route: "cash_secured_put",
      conid: 556,
      strike: 93,
      contracts: 1,
    });
    expect(host.textContent).toContain("Selected ✓");
  });

  it("shows fallback rungs but does not allow staging them", async () => {
    apiMock.mockResolvedValue(route("reduce", false));
    const selected = new Map<string, RebalanceRouteSelection>();
    const host = executionRouteChoices(
      [{ symbol: "NVDA", delta_czk: -230_000 }],
      selected,
    );
    document.body.innerHTML = "";
    document.body.appendChild(host);
    host.querySelector<HTMLButtonElement>("button:nth-of-type(2)")!.click();
    await vi.waitFor(() => expect(host.textContent).toContain("Indicative"));
    const indicative = [...host.querySelectorAll("button")]
      .find((node) => node.textContent === "Indicative only") as HTMLButtonElement;
    expect(indicative.disabled).toBe(true);
    expect(selected.get("NVDA")?.route).toBe("sell_shares");
    expect(host.textContent).toContain("exact IBKR contract required");
  });

  it("stages the simulated trades and selected option rung in one request", async () => {
    apiMock.mockImplementation((path: string) => {
      if (path === "/api/rebalance/stage") {
        return Promise.resolve({
          trades: [{
            type: "cash_secured_put",
            route: "cash_secured_put",
            symbol: "NVDA",
            conid: 556,
            expiry: "2026-08-07",
            strike: 93,
            contracts: 1,
          }],
          revision: "mixed-rev",
          reviewed: false,
        });
      }
      return Promise.resolve({});
    });
    document.body.innerHTML = '<div id="reb-whatif"></div>';
    const selected = new Map<string, RebalanceRouteSelection>([[
      "NVDA",
      {
        symbol: "NVDA",
        route: "cash_secured_put",
        conid: 556,
        expiry: "2026-08-07",
        strike: 93,
        contracts: 1,
      },
    ]]);
    renderWhatif({
      currency: "CZK",
      trades: [{ symbol: "NVDA", delta_czk: 230_000 }],
      summary: {},
      before_status: {},
      after: { rows: [] },
      caveats: [],
    } as unknown as Whatif, selected);
    expect(document.getElementById("reb-whatif")!.textContent).toContain("Cash-secured put");
    const stage = [...document.querySelectorAll("button")]
      .find((node) => node.textContent?.startsWith("Add 1 order to queue"))!;
    stage.click();
    await vi.waitFor(() => expect(apiMock).toHaveBeenCalledWith(
      "/api/rebalance/stage",
      "POST",
      {
        trades: [{ symbol: "NVDA", delta_czk: 230_000 }],
        selections: [{
          symbol: "NVDA",
          route: "cash_secured_put",
          conid: 556,
          expiry: "2026-08-07",
          strike: 93,
          contracts: 1,
        }],
        mode: "append",
      },
    ));
    expect(state.stagedBasket[0].type).toBe("cash_secured_put");
  });

  it("stages an unrelated option and offers to reconcile an older coverage conflict", async () => {
    apiMock.mockImplementation((path: string) => {
      if (path === "/api/rebalance/stage") {
        return Promise.resolve({
          trades: [{
            type: "covered_call", symbol: "EEFT", conid: 777,
            expiry: "2026-08-07", strike: 105, contracts: 1,
          }],
          revision: "with-conflict",
          reviewed: false,
          coverage_violations: [{
            symbol: "PYPL",
            current_shares: 1_000,
            planned_stock_sell_shares: 619,
            selected_call_contracts: 7,
            held_short_call_contracts: 0,
            working_short_call_contracts: 0,
            required_shares: 1_319,
            excess_shares: 319,
            stock_leg_ids: ["stock:PYPL"],
            call_leg_ids: ["covered_call:PYPL:900"],
          }],
        });
      }
      if (path === "/api/trade/basket") {
        return Promise.resolve({
          trades: [{
            type: "covered_call", symbol: "EEFT", conid: 777,
            expiry: "2026-08-07", strike: 105, contracts: 1,
          }],
          revision: "reconciled",
          reviewed: false,
        });
      }
      return Promise.resolve({});
    });
    document.body.innerHTML = '<div id="reb-whatif"></div>';
    renderWhatif({
      currency: "CZK",
      trades: [{ symbol: "EEFT", delta_czk: -230_000 }],
      summary: {},
      before_status: {},
      after: { rows: [] },
      caveats: [],
    } as unknown as Whatif, new Map([[
      "EEFT",
      {
        symbol: "EEFT", route: "covered_call", conid: 777,
        expiry: "2026-08-07", strike: 105, contracts: 1,
      },
    ]]));

    [...document.querySelectorAll("button")]
      .find((node) => node.textContent?.startsWith("Add 1 order to queue"))!
      .click();
    await vi.waitFor(() => expect(document.body.textContent).toContain(
      "New orders were added. Reconcile older covered-call plans before review.",
    ));
    expect(document.body.textContent).toContain("319 shares over capacity");
    expect(
      [...document.querySelectorAll<HTMLButtonElement>("button")]
        .find((node) => node.textContent === "Reconcile coverage before review")!
        .disabled,
    ).toBe(true);

    [...document.querySelectorAll("button")]
      .find((node) => node.textContent === "Keep calls · exclude share sale")!
      .click();
    await vi.waitFor(() => expect(apiMock).toHaveBeenCalledWith(
      "/api/trade/basket",
      "POST",
      { toggle_leg_id: "stock:PYPL", included: false },
    ));
    expect(document.body.textContent).toContain("PYPL reconciled");
    expect(document.body.textContent).toContain("Review projected portfolio →");
  });

  it("can explicitly replace prior rebalance orders instead of appending", async () => {
    apiMock.mockResolvedValue({
      trades: [{ type: "stock", symbol: "NVDA", delta_czk: 230_000 }],
      revision: "replace-rev",
      reviewed: false,
    });
    document.body.innerHTML = '<div id="reb-whatif"></div>';
    renderWhatif({
      currency: "CZK",
      trades: [{ symbol: "NVDA", delta_czk: 230_000 }],
      summary: {},
      before_status: {},
      after: { rows: [] },
      caveats: [],
    } as unknown as Whatif);

    const replace = document.querySelector<HTMLInputElement>(
      'input[name="reb-queue-mode"][value="replace"]',
    )!;
    replace.click();
    const action = [...document.querySelectorAll("button")]
      .find((node) => node.textContent?.startsWith("Replace rebalance orders with 1 order"))!;
    action.click();

    await vi.waitFor(() => expect(apiMock).toHaveBeenCalledWith(
      "/api/rebalance/stage",
      "POST",
      expect.objectContaining({ mode: "replace" }),
    ));
  });
});
