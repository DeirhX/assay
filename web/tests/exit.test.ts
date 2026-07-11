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
    market_data_availability: "RpB", market_data_timeline: "real_time",
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
    covered_call: {
      eligible: true, reasons: [], capacity_contracts: 10,
      assigned_shares: 1000, unresolved_exit_shares: 6700,
      overtrim_shares: 0, post_assignment_pct: 70,
      target_low_pct: 1, target_high_pct: 3, reaches_target_band: false,
    },
    recommended: "sell_shares",
  };
  base.positions[0].options = {
    symbol: "EXITME", underlying: 105, currency: "USD", source: "ibkr",
    underlying_quote: {
      last: 105.25, bid: 105.2, ask: 105.3, source: "ibkr",
      quote_timestamp: new Date().toISOString(),
      market_data_availability: "RpB", market_data_timeline: "real_time",
    },
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
    expect(cta!.textContent).toContain("Stage first slice");

    const details = document.querySelector<HTMLDetailsElement>("#exit-body details.exit-details");
    expect(details).toBeTruthy();
    expect(details!.open).toBe(false);
    expect(details!.querySelector("table.exit-sched")).toBeTruthy();
    expect(details!.querySelector(".exit-posbar")).toBeTruthy();
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
    expect(body).toContain("real-time");

    const routes = document.querySelectorAll("#exit-body .exit-route-btn");
    expect(routes.length).toBeGreaterThanOrEqual(2);
    expect(document.querySelector("#exit-body .exit-route-btn.active")!.textContent).toContain("Sell shares");
  });

  it("switches to covered-call ladder with bid/ask/last columns", async () => {
    apiMock.mockResolvedValue(routeFixture());
    await loadExit();
    await flush();

    const ccBtn = [...document.querySelectorAll<HTMLButtonElement>("#exit-body .exit-route-btn")]
      .find((b) => b.textContent?.includes("Covered-call"))!;
    ccBtn.click();
    await flush();

    const body = document.querySelector("#exit-body")!.textContent || "";
    expect(body).toContain("conditional");
    expect(body).toContain("if assigned");
    expect(body).toContain("planned exit shares remain deterministic work");
    expect(body).toContain("projected position is 70.00%");

    const headers = document.querySelector("#exit-body table.exit-ladder-exec thead")!.textContent || "";
    expect(headers).toContain("Bid (sell)");
    expect(headers).toContain("Ask (buy)");
    expect(headers).toContain("Last");
    expect(headers).toContain("Limit credit");
    expect(headers).toContain("Assignment");
    expect(headers).toContain("Action");

    const rows = document.querySelectorAll("#exit-body table.exit-ladder-exec tbody tr");
    expect(rows).toHaveLength(3);
    expect(rows[0].textContent).toMatch(/2[,.]4/);
    expect(rows[0].textContent).toMatch(/2[,.]6/);
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

  it("expires executable rung actions locally after the two-minute quote window", async () => {
    const data = routeFixture();
    data.positions[0].options!.covered_call_ladder![0].quote_timestamp =
      new Date(Date.now() - 121_000).toISOString();
    apiMock.mockResolvedValue(data);
    await loadExit();
    await flush();

    const ccBtn = [...document.querySelectorAll<HTMLButtonElement>("#exit-body .exit-route-btn")]
      .find((b) => b.textContent?.includes("Covered-call"))!;
    ccBtn.click();
    await flush();

    const firstRow = document.querySelector("#exit-body table.exit-ladder-exec tbody tr")!;
    expect(firstRow.textContent).toContain("Quote is stale");
    expect(firstRow.querySelector(".exit-stage-cc-btn")).toBeNull();
  });

  it("shows frozen closing quotes but never enables staging", async () => {
    const data = routeFixture();
    const rung = data.positions[0].options!.covered_call_ladder![0];
    rung.market_data_availability = "ZpB";
    rung.market_data_timeline = "frozen";
    rung.executable = false;
    apiMock.mockResolvedValue(data);
    await loadExit();
    await flush();

    const ccBtn = [...document.querySelectorAll<HTMLButtonElement>("#exit-body .exit-route-btn")]
      .find((b) => b.textContent?.includes("Covered-call"))!;
    ccBtn.click();
    await flush();

    const firstRow = document.querySelector("#exit-body table.exit-ladder-exec tbody tr")!;
    expect(firstRow.textContent).toContain("frozen close");
    expect(firstRow.querySelector(".exit-stage-cc-btn")).toBeNull();
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
