// Tests for the Exit planner view: tax-layering summary, scale-out schedule,
// execution-route controls (sell shares vs covered-call), and staging payloads.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { apiMock } = vi.hoisted(() => ({ apiMock: vi.fn() }));
vi.mock("../src/core", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/core")>();
  return { ...actual, api: apiMock };
});

import { state } from "../src/core";
import type { ExitCoveredCallRung, ExitPlanResponse } from "../src/api-types";
import { loadExit } from "../src/exit";
import { resetGatewayState } from "../src/gateway";

const flush = async () => {
  for (let i = 0; i < 6; i++) await Promise.resolve();
  await new Promise((r) => setTimeout(r, 0));
};

function ccRung(over: Partial<ExitCoveredCallRung> = {}): ExitCoveredCallRung {
  return {
    strike: 110, expiry: "2026-08-15", dte: 45, premium: 2.5, premium_czk: 57_500,
    effective_exit: 112.5, moneyness_pct: 4.8, premium_yield_annual_pct: 18.2,
    assignment_prob_pct: 28, open_interest: 1200, volume: 340, spread_pct: 3.1,
    liquidity: "ok", source: "ibkr", estimate: false,
    bid: 2.4, ask: 2.6, last: 2.5, conid: 12345678, multiplier: 100,
    limit_price: 2.5, quote_timestamp: new Date().toISOString(),
    quote_fresh: true, executable: true,
    recommended: true,
    ...over,
  };
}

function planFixture(over: Partial<ExitPlanResponse> = {}): ExitPlanResponse {
  return {
    as_of: "2026-07-01",
    snapshot: null,
    currency: "CZK",
    invested: 1_000_000,
    config: { horizon_days: 10, adv_slice_pct: 0.12, near_exempt_days: 120, tax_rate: 0.15 },
    positions: [
      {
        symbol: "EXITME", source: "trim", rule: "reduce", currency: "USD",
        mark_price: 100, quantity: 8000, current_pct: 80, current_czk: 800_000,
        end_state: "ceiling", target_pct: 3, exit_czk: 770_000, exit_shares: 7700,
        sell_now_shares: 4000,
        tax: {
          sell_now_czk: 400_000, defer_czk: 370_000,
          sell_now_lots: [], defer_lots: [
            { bucket: "taxable_gain", shares: 3700, market_value: 370_000, gain: 277_500,
              days_to_exempt: 90, exempt_on: "2026-09-29", tax_if_sold_now: 41_625,
              note: "wait until 2026-09-29 to sell tax-free" },
          ],
          taxable_gain_now: 0, exempt_gain_now: 200_000, harvested_loss_now: 50_000,
          tax_cost_now: 0, tax_saved_by_waiting: 41_625,
        },
        schedule: {
          n: 2, adv: 5000, max_shares_per_day: 600,
          tranches: [
            { index: 1, date: "2026-07-01", shares: 2000, czk: 200_000, limit_price: 100, limit_currency: "USD", over_adv_cap: true },
            { index: 2, date: "2026-07-10", shares: 2000, czk: 200_000, limit_price: 102, limit_currency: "USD", over_adv_cap: true },
          ],
        },
        options: null,
      },
    ],
    totals: { exit_czk: 770_000, sell_now_czk: 400_000, defer_czk: 370_000, tax_cost_now: 0, tax_saved_by_waiting: 41_625 },
    ...over,
  };
}

function routeFixture(): ExitPlanResponse {
  const base = planFixture();
  base.positions[0].routes = {
    sell_shares: { eligible: true, reasons: [] },
    covered_call: { eligible: true, reasons: [], capacity_contracts: 10 },
    recommended: "sell_shares",
  };
  base.positions[0].options = {
    symbol: "EXITME", underlying: 105, currency: "USD", source: "ibkr",
    underlying_quote: { last: 105.25, bid: 105.2, ask: 105.3, source: "ibkr", quote_timestamp: new Date().toISOString() },
    covered_call: null,
    covered_call_ladder: [
      ccRung(),
      ccRung({ strike: 115, recommended: false, bid: 1.1, ask: 1.3, last: 1.2, conid: 87654321, limit_price: 1.2, executable: true }),
      ccRung({ strike: 120, recommended: false, estimate: true, executable: false, bid: null, ask: null, conid: null, source: "black_scholes" }),
    ],
    protective_put: null,
    notes: [],
  };
  return base;
}

beforeEach(() => {
  apiMock.mockReset();
  resetGatewayState();
  state.stagedBasket = [];
  ["#exit-summary", "#exit-body", "#exit-status"].forEach((s) => {
    const n = document.querySelector(s);
    if (n) n.innerHTML = "";
  });
  const ctl = document.querySelector<HTMLElement>("#exit-controls");
  if (ctl) { ctl.innerHTML = ""; delete ctl.dataset.wired; }
});

afterEach(() => vi.restoreAllMocks());

describe("Exit planner rendering", () => {
  it("explains when fallback option levels are caused by a disconnected gateway", async () => {
    const fallback = routeFixture();
    fallback.positions[0].options!.source = "yahoo";
    apiMock.mockImplementation((path: string) => {
      if (path === "/api/trade/status") {
        return Promise.resolve({
          trading_enabled: false, authenticated: false, connected: false,
        });
      }
      if (path.startsWith("/api/exit-plan?")) return Promise.resolve(fallback);
      return Promise.resolve({});
    });
    await loadExit();
    await flush();

    const notice = document.querySelector("#exit-gateway-notice")!.textContent || "";
    expect(notice).toContain("Live IBKR option data unavailable");
    expect(notice).toContain("not connected");
    expect(notice).toContain("exact contracts");
  });

  it("distinguishes connected IBKR fallback data from disconnection", async () => {
    const fallback = routeFixture();
    fallback.positions[0].options!.source = "yahoo";
    apiMock.mockImplementation((path: string) => {
      if (path === "/api/trade/status") {
        return Promise.resolve({
          trading_enabled: false, authenticated: true, connected: true,
        });
      }
      if (path.startsWith("/api/exit-plan?")) return Promise.resolve(fallback);
      return Promise.resolve({});
    });
    await loadExit();
    await flush();

    const notice = document.querySelector("#exit-gateway-notice")!.textContent || "";
    expect(notice).toContain("IBKR is connected");
    expect(notice).toContain("Fallback levels shown from yahoo");
    expect(notice).not.toContain("not connected");
  });

  it("renders the tax-layering summary and a scale-out schedule", async () => {
    apiMock.mockResolvedValue(planFixture());
    await loadExit();
    await flush();

    const summary = document.querySelector("#exit-summary")!.textContent || "";
    expect(summary).toContain("Sell now");
    expect(summary).toContain("Tax saved by waiting");

    const rows = document.querySelectorAll("#exit-body table.exit-sched tbody tr");
    expect(rows).toHaveLength(2);

    const body = document.querySelector("#exit-body")!.textContent || "";
    expect(body).toContain("wait until 2026-09-29");
  });

  it("renders the base plan before live option enrichment finishes", async () => {
    let resolveOptions!: (value: ExitPlanResponse) => void;
    const options = new Promise<ExitPlanResponse>((resolve) => {
      resolveOptions = resolve;
    });
    let exitCalls = 0;
    apiMock.mockImplementation((path: string) => {
      if (path.startsWith("/api/exit-plan?")) {
        exitCalls += 1;
        return exitCalls === 1 ? Promise.resolve(planFixture()) : options;
      }
      return Promise.resolve({});
    });

    const loading = loadExit();
    await flush();

    const exitPaths = apiMock.mock.calls
      .map((call) => String(call[0]))
      .filter((path) => path.startsWith("/api/exit-plan?"));
    expect(exitPaths[0]).toContain("with_options=0");
    expect(exitPaths[1]).toContain("with_options=1");
    expect(document.querySelector("#exit-body")!.textContent).toContain("Reduce to 3.00%");
    expect(document.querySelector("#exit-status")!.textContent).toContain("loading live option routes");

    resolveOptions(routeFixture());
    await loading;
    await flush();

    expect(document.querySelector("#exit-body")!.textContent).toContain("Covered-call exit");
    expect(document.querySelector("#exit-status")!.textContent).toBe("");
  });

  it("leads with a plain recommendation and hides the math in a collapsed expander", async () => {
    apiMock.mockResolvedValue(planFixture());
    await loadExit();
    await flush();

    const reco = document.querySelector("#exit-body .exit-reco");
    expect(reco).toBeTruthy();
    const recoText = reco!.textContent || "";
    expect(recoText).toContain("Reduce to 3.00%");
    expect(recoText).toContain("Keeps 4% of the position");
    const cta = document.querySelector<HTMLButtonElement>("#exit-body .exit-reco-cta");
    expect(cta).toBeTruthy();
    expect(cta!.textContent).toContain("Add first slice");

    const details = document.querySelector<HTMLDetailsElement>("#exit-body details.exit-details");
    expect(details).toBeTruthy();
    expect(details!.open).toBe(false);
    expect(details!.querySelector("table.exit-sched")).toBeTruthy();
    expect(details!.querySelector(".exit-posbar")).toBeTruthy();
  });

  it("shows whole shares and compact two-significant-digit currency estimates", async () => {
    const data = planFixture();
    data.positions[0].exit_shares = 695.54;
    data.positions[0].exit_czk = 658_160;
    data.totals.exit_czk = 1_200_000;
    apiMock.mockResolvedValue(data);
    await loadExit();
    await flush();

    const text = document.querySelector("#exit-body .exit-reco-lead")!.textContent || "";
    expect(text.replace(/\s/g, "")).toContain("sell696sh(660KCZK)");
    expect(document.querySelector("#exit-summary")!.textContent).toMatch(/1[.,]2M CZK/);
  });

  it("the headline CTA stages the first tranche with route sell_shares", async () => {
    apiMock.mockImplementation((path: string) => {
      if (path.startsWith("/api/exit-plan?")) return Promise.resolve(planFixture());
      if (path === "/api/exit-plan/stage") {
        return Promise.resolve({
          staged: true, symbol: "EXITME", basket: [],
          tranche: { index: 1, date: "2026-07-01", shares: 2000, czk: 200_000, limit_price: 100, limit_currency: "USD", over_adv_cap: true },
        });
      }
      return Promise.resolve({ orders: [] });
    });
    await loadExit();
    await flush();
    document.querySelector<HTMLButtonElement>("#exit-body .exit-reco-cta")!.click();
    await flush();
    expect(apiMock).toHaveBeenCalledWith(
      "/api/exit-plan/stage", "POST",
      expect.objectContaining({ symbol: "EXITME", route: "sell_shares", index: 1, cfg: expect.any(Object) }),
    );
  });

  it("shows an empty state when nothing needs exiting", async () => {
    apiMock.mockResolvedValue(planFixture({ positions: [] }));
    await loadExit();
    await flush();
    expect(document.querySelector("#exit-body .empty-state")).toBeTruthy();
  });

  it("a partial reduce renders a Keep segment sized to the kept remainder", async () => {
    apiMock.mockResolvedValue(planFixture());
    await loadExit();
    await flush();
    const segs = [...document.querySelectorAll<HTMLElement>("#exit-body .exit-posbar-seg")];
    const keep = segs.find((s) => s.classList.contains("keep"));
    expect(keep).toBeTruthy();
    expect(keep!.style.width).toBe("3.8%");
    const sellNow = segs.find((s) => s.classList.contains("good"))!;
    expect(sellNow.style.width).toBe("50.0%");
    const body = document.querySelector("#exit-body")!.textContent || "";
    expect(body).toContain("keeping 4%");
  });

  it("a full exit keeps nothing (no keep segment)", async () => {
    const full = planFixture();
    full.positions[0].end_state = "zero";
    full.positions[0].exit_czk = 800_000;
    await apiMock.mockResolvedValue(full);
    await loadExit();
    await flush();
    const keep = document.querySelector("#exit-body .exit-posbar-seg.keep");
    expect(keep).toBeFalsy();
    const body = document.querySelector("#exit-body")!.textContent || "";
    expect(body).toContain("full exit — nothing kept");
  });

  it("stages a schedule tranche with route sell_shares", async () => {
    apiMock.mockImplementation((path: string) => {
      if (path.startsWith("/api/exit-plan?")) return Promise.resolve(planFixture());
      if (path === "/api/exit-plan/stage") {
        return Promise.resolve({
          staged: true, symbol: "EXITME",
          basket: [{ type: "stock", symbol: "EXITME", delta_czk: -200_000, route: "sell_shares" }],
          tranche: { index: 1, date: "2026-07-01", shares: 2000, czk: 200_000, limit_price: 100, limit_currency: "USD", over_adv_cap: true },
        });
      }
      return Promise.resolve({ orders: [] });
    });

    await loadExit();
    await flush();

    const stageBtn = [...document.querySelectorAll<HTMLButtonElement>("#exit-body .exit-stage-btn")][0];
    expect(stageBtn).toBeTruthy();
    stageBtn.click();
    await flush();

    expect(apiMock).toHaveBeenCalledWith(
      "/api/exit-plan/stage", "POST",
      expect.objectContaining({ symbol: "EXITME", route: "sell_shares", index: 1, cfg: expect.any(Object) }),
    );
    expect(state.stagedBasket).toEqual([{ type: "stock", symbol: "EXITME", delta_czk: -200_000, route: "sell_shares" }]);
  });
});

describe("Exit execution routes", () => {
  it("shows route controls and underlying last with source", async () => {
    apiMock.mockResolvedValue(routeFixture());
    await loadExit();
    await flush();

    const body = document.querySelector("#exit-body")!.textContent || "";
    expect(body).toContain("Sell shares");
    expect(body).toContain("Covered-call exit");
    expect(body).toContain("Underlying last");
    expect(body).toMatch(/105[,.]25/);
    expect(body).toContain("ibkr");

    const routes = document.querySelectorAll("#exit-body .exit-route-btn");
    expect(routes.length).toBeGreaterThanOrEqual(2);
    expect(document.querySelector("#exit-body .exit-route-btn.active")!.textContent).toContain("Sell shares");
  });

  it("switches to a covered-call ladder with bid/ask, annual yield, and minimum credit", async () => {
    apiMock.mockResolvedValue(routeFixture());
    await loadExit();
    await flush();

    const ccBtn = [...document.querySelectorAll<HTMLButtonElement>("#exit-body .exit-route-btn")]
      .find((b) => b.textContent?.includes("Covered-call"))!;
    ccBtn.click();
    await flush();

    const body = document.querySelector("#exit-body")!.textContent || "";
    expect(body).toContain("Conditional exit");
    expect(body).toContain("if assigned");
    expect(body).toContain("Assignment size");
    expect(body).toContain("Safety checks");
    expect(body).toContain("Revalidated twice");

    const headers = document.querySelector("#exit-body table.exit-ladder-exec thead")!.textContent || "";
    expect(headers).toContain("Bid (sell)");
    expect(headers).toContain("Ask (buy)");
    expect(headers).toContain("Yield p.a.");
    expect(headers).toContain("Min credit");
    expect(headers).not.toContain("Last");
    expect(headers).toContain("Assignment");
    expect(headers).toContain("Action");
    expect(document.querySelector<HTMLTableCellElement>(
      "#exit-body table.exit-ladder-exec thead th[title*='Minimum premium']",
    )).toBeTruthy();

    const rows = document.querySelectorAll("#exit-body table.exit-ladder-exec tbody tr");
    expect(rows).toHaveLength(3);
    expect(rows[0].textContent).toMatch(/2[,.]4/);
    expect(rows[0].textContent).toMatch(/2[,.]6/);
    expect(rows[0].textContent).toContain("18.2%");
  });

  it("explains bounded whole-contract rounding", async () => {
    const data = routeFixture();
    data.positions[0].routes!.covered_call = {
      ...data.positions[0].routes!.covered_call,
      capacity_contracts: 1,
      planned_exit_shares: 89,
      assignment_shares: 100,
      share_deviation: 11,
      rounded_up: true,
    };
    apiMock.mockResolvedValue(data);
    await loadExit();
    await flush();

    const ccBtn = [...document.querySelectorAll<HTMLButtonElement>("#exit-body .exit-route-btn")]
      .find((b) => b.textContent?.includes("Covered-call"))!;
    ccBtn.click();
    await flush();

    const body = document.querySelector("#exit-body")!.textContent || "";
    expect(body).toContain("89 → 100 shares");
    expect(body).toContain("+11 shares");
    expect(body).toContain("within 15% guardrail");
  });

  it("sorts the covered-call ladder by any decision column", async () => {
    apiMock.mockResolvedValue(routeFixture());
    await loadExit();
    await flush();

    const ccBtn = [...document.querySelectorAll<HTMLButtonElement>("#exit-body .exit-route-btn")]
      .find((b) => b.textContent?.includes("Covered-call"))!;
    ccBtn.click();
    await flush();

    const bidSort = document.querySelector<HTMLButtonElement>(
      "#exit-body table.exit-ladder-exec button[data-sort='bid']",
    )!;
    const strikes = () => [...document.querySelectorAll<HTMLTableRowElement>(
      "#exit-body table.exit-ladder-exec tbody tr",
    )].map((row) => row.cells[0].textContent || "");

    bidSort.click();
    expect(strikes()[0]).toMatch(/110/);
    expect(strikes()[1]).toMatch(/115/);
    expect(strikes()[2]).toMatch(/120/); // missing bid stays last
    expect(document.querySelector(
      "#exit-body table.exit-ladder-exec button[data-sort='bid']",
    )!.closest("th")!.getAttribute("aria-sort")).toBe("descending");

    document.querySelector<HTMLButtonElement>(
      "#exit-body table.exit-ladder-exec button[data-sort='bid']",
    )!.click();
    expect(strikes()[0]).toMatch(/115/);
    expect(strikes()[1]).toMatch(/110/);
    expect(strikes()[2]).toMatch(/120/);
  });

  it("keeps an indicative covered-call route selectable while staging is unavailable", async () => {
    const data = routeFixture();
    data.positions[0].routes!.covered_call.stageable = false;
    data.positions[0].routes!.covered_call.reasons = [
      "Indicative covered-call levels are available; staging needs a live IBKR quote.",
    ];
    data.positions[0].options!.covered_call_ladder.forEach((rung) => {
      rung.executable = false;
      rung.quote_fresh = false;
    });
    apiMock.mockResolvedValue(data);
    await loadExit();
    await flush();

    const ccBtn = [...document.querySelectorAll<HTMLButtonElement>("#exit-body .exit-route-btn")]
      .find((b) => b.textContent?.includes("Covered-call"))!;
    expect(ccBtn.disabled).toBe(false);
    ccBtn.click();
    await flush();

    const body = document.querySelector("#exit-body")!.textContent || "";
    expect(body).toContain("Indicative covered-call levels are available");
    expect(document.querySelectorAll(".exit-stage-cc-btn")).toHaveLength(0);
    expect(document.querySelector("table.exit-ladder-exec")).toBeTruthy();
  });

  it("summarizes an unavailable covered-call route without bloating the button row", async () => {
    const data = routeFixture();
    data.positions[0].routes!.covered_call.eligible = false;
    data.positions[0].routes!.covered_call.reasons = [
      "The planned 24-share exit is too far from one 100-share option contract.",
      "Indicative covered-call levels from Black Scholes are available; staging needs an exact IBKR contract with a live two-sided quote.",
    ];
    apiMock.mockResolvedValue(data);
    await loadExit();
    await flush();

    const why = document.querySelector<HTMLElement>("#exit-body .exit-route-why")!;
    expect(why.textContent).toBe("Needs about 100 shares; this exit is 24.");
    expect(why.title).toContain("Indicative covered-call levels");
    const labels = [...document.querySelectorAll<HTMLElement>("#exit-body .exit-route-btn")]
      .map((button) => button.textContent);
    expect(labels).toEqual(["Sell shares", "Covered-call exit"]);
  });

  it("stages an executable covered-call rung with the full payload", async () => {
    apiMock.mockImplementation((path: string) => {
      if (path.startsWith("/api/exit-plan?")) return Promise.resolve(routeFixture());
      if (path === "/api/exit-plan/stage") {
        return Promise.resolve({
          staged: true, symbol: "EXITME",
          basket: [{
            type: "covered_call", symbol: "EXITME", route: "covered_call",
            conid: 12345678, expiry: "2026-08-15", strike: 110, contracts: 10,
          }],
        });
      }
      return Promise.resolve({ orders: [] });
    });
    await loadExit();
    await flush();

    const ccBtn = [...document.querySelectorAll<HTMLButtonElement>("#exit-body .exit-route-btn")]
      .find((b) => b.textContent?.includes("Covered-call"))!;
    ccBtn.click();
    await flush();

    const stageBtn = document.querySelector<HTMLButtonElement>("#exit-body .exit-stage-cc-btn")!;
    expect(stageBtn).toBeTruthy();
    stageBtn.click();
    await flush();

    expect(apiMock).toHaveBeenCalledWith(
      "/api/exit-plan/stage", "POST",
      expect.objectContaining({
        symbol: "EXITME",
        route: "covered_call",
        conid: 12345678,
        expiry: "2026-08-15",
        strike: 110,
        contracts: 10,
        cfg: expect.any(Object),
      }),
    );
    expect(state.stagedBasket[0]).toMatchObject({ type: "covered_call", route: "covered_call" });
  });

  it("shows blocked reasons instead of a stage button for non-executable rungs", async () => {
    apiMock.mockResolvedValue(routeFixture());
    await loadExit();
    await flush();

    const ccBtn = [...document.querySelectorAll<HTMLButtonElement>("#exit-body .exit-route-btn")]
      .find((b) => b.textContent?.includes("Covered-call"))!;
    ccBtn.click();
    await flush();

    const blocked = document.querySelectorAll("#exit-body .exit-rung-blocked");
    expect(blocked.length).toBeGreaterThan(0);
    const blockedText = [...blocked].map((n) => n.textContent).join(" ");
    expect(blockedText).toMatch(/Not executable|Modeled premium|Missing bid\/ask|No contract id/);

    const stageBtns = document.querySelectorAll("#exit-body .exit-stage-cc-btn");
    expect(stageBtns.length).toBe(2);
  });

  it("warns but allows staging a known contract with no current bid/ask", async () => {
    const data = routeFixture();
    const rung = data.positions[0].options!.covered_call_ladder[2];
    rung.conid = 99988877;
    rung.stageable = true;
    rung.executable = false;
    rung.bid = null;
    rung.ask = null;
    rung.staging_warning =
      "No live bid/ask right now. Staging is allowed, but placement remains blocked.";
    apiMock.mockResolvedValue(data);
    await loadExit();
    await flush();

    const ccBtn = [...document.querySelectorAll<HTMLButtonElement>("#exit-body .exit-route-btn")]
      .find((b) => b.textContent?.includes("Covered-call"))!;
    ccBtn.click();
    await flush();

    const rows = document.querySelectorAll("#exit-body table.exit-ladder-exec tbody tr");
    const warningRow = [...rows].find((row) => row.textContent?.includes("No live bid/ask"))!;
    expect(warningRow).toBeTruthy();
    expect(warningRow.querySelector(".exit-rung-warning")!.textContent).toContain(
      "placement remains blocked",
    );
    expect(warningRow.querySelector(".exit-stage-cc-btn")).toBeTruthy();
  });

  it("offers a whole-instrument refresh when a displayed quote ages out", async () => {
    const data = routeFixture();
    const rung = data.positions[0].options!.covered_call_ladder![0];
    rung.stageable = true;
    rung.quote_fresh = false;
    rung.quote_timestamp = new Date(Date.now() - 121_000).toISOString();
    rung.staging_warning =
      "The displayed quote is stale. Staging will refresh it from IBKR before calculating a limit price.";
    const refreshed = routeFixture();
    apiMock.mockImplementation((path: string) => Promise.resolve(
      path === "/api/exit-plan/refresh-options" ? refreshed : data,
    ));
    await loadExit();
    await flush();

    const ccBtn = [...document.querySelectorAll<HTMLButtonElement>("#exit-body .exit-route-btn")]
      .find((b) => b.textContent?.includes("Covered-call"))!;
    ccBtn.click();
    await flush();

    const firstRow = document.querySelector("#exit-body table.exit-ladder-exec tbody tr")!;
    expect(firstRow.textContent).toContain("displayed quote is stale");
    expect(firstRow.textContent).toContain("Refresh & add");
    expect(firstRow.querySelector(".exit-stage-cc-btn")).toBeTruthy();

    const refreshBtn = document.querySelector<HTMLButtonElement>("#exit-body .exit-cc-refresh-btn")!;
    expect(refreshBtn.textContent).toContain("Refresh all EXITME quotes");
    refreshBtn.click();
    await flush();

    expect(apiMock).toHaveBeenLastCalledWith(
      "/api/exit-plan/refresh-options",
      "POST",
      expect.objectContaining({ symbol: "EXITME", cfg: expect.any(Object) }),
      { timeoutMs: 60_000 },
    );
    expect(document.querySelector("#exit-body .exit-cc-refresh-btn")).toBeFalsy();
    expect(document.querySelector("#exit-status")!.textContent).toContain(
      "EXITME underlying and option quotes refreshed",
    );
  });

  it("labels protective puts as analysis-only in details", async () => {
    const data = routeFixture();
    data.positions[0].options!.protective_put = {
      type: "protective_put", source: "black_scholes", contracts: 10,
      expiry: "2026-09-29", dte: 90, days_to_exempt: 90, exempt_on: "2026-09-29",
      put_strike: 95, put_premium: 3.5, put_cost_czk: 80_500, protected_floor: 95,
      collar_call_strike: 115, collar_call_premium: 1.2, net_collar_premium: 2.3,
      net_collar_czk: 52_900, tax_saved_by_waiting_czk: 41_625, vol_used: 0.35, estimate: true,
    };
    apiMock.mockResolvedValue(data);
    await loadExit();
    await flush();

    const details = document.querySelector<HTMLDetailsElement>("#exit-body details.exit-details")!;
    details.open = true;
    await flush();

    const optHead = details.querySelector(".exit-options .exit-h3")!.textContent || "";
    expect(optHead).toContain("analysis");
    expect(optHead).toContain("not placeable");
    expect(details.textContent).toContain("Protective put");
    expect(details.querySelector(".exit-ladder-exec")).toBeFalsy();
  });
});
