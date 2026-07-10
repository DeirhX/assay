// Tests for the Exit planner view: it must render the tax-layering summary and a
// per-tranche scale-out schedule, and "Stage →" must POST the exact tranche to
// /api/exit-plan/stage (server re-derives size; we only send symbol/index/cfg)
// and mirror the returned basket into shared state for the Trade desk.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { apiMock } = vi.hoisted(() => ({ apiMock: vi.fn() }));
vi.mock("../src/core", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/core")>();
  return { ...actual, api: apiMock };
});

import { state } from "../src/core";
import type { ExitPlanResponse } from "../src/api-types";
import { loadExit } from "../src/exit";

const flush = async () => {
  for (let i = 0; i < 6; i++) await Promise.resolve();
  await new Promise((r) => setTimeout(r, 0));
};

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

function coveredCallPlan(): ExitPlanResponse {
  const plan = planFixture();
  plan.positions[0].options = {
    symbol: "EXITME",
    underlying: 101.25,
    currency: "USD",
    source: "ibkr",
    available_covered_shares: 700,
    available_contracts: 7,
    route_contracts: 7,
    route_assigned_shares: 700,
    working_orders_checked: false,
    covered_call: null,
    covered_call_ladder: [{
      strike: 110, expiry: "2026-08-21", dte: 51,
      premium: 2.5, premium_czk: 4000, effective_exit: 112.5,
      moneyness_pct: 8.6, premium_yield_annual_pct: 16.2,
      assignment_prob_pct: 24, open_interest: 500, volume: 40,
      spread_pct: 8, liquidity: "ok", source: "ibkr", estimate: false,
      recommended: true, executable: true, conid: 12345,
      bid: 2.4, ask: 2.6, last: 2.5, quote_at: new Date().toISOString(),
      multiplier: 100,
      underlying_quote: { conid: 500, last: 101.25, bid: 101.2, ask: 101.3, quote_at: new Date().toISOString() },
    }],
    protective_put: null,
    notes: [],
  };
  return plan;
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

    // Summary stat strip carries the sell-now / deferred / tax-saved headline.
    const summary = document.querySelector("#exit-summary")!.textContent || "";
    expect(summary).toContain("Sell now");
    expect(summary).toContain("Tax saved by waiting");

    // Schedule renders one row per tranche.
    const rows = document.querySelectorAll("#exit-body table.exit-sched tbody tr");
    expect(rows).toHaveLength(2);

    // The deferred near-exempt lot surfaces with its wait note.
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
    // Primary CTA stages the first slice.
    const cta = document.querySelector<HTMLButtonElement>("#exit-body .exit-reco-cta");
    expect(cta).toBeTruthy();
    expect(cta!.textContent).toContain("Stage first slice");

    // The detail sections live inside a <details> that is closed by default.
    const details = document.querySelector<HTMLDetailsElement>("#exit-body details.exit-details");
    expect(details).toBeTruthy();
    expect(details!.open).toBe(false);
    expect(details!.querySelector("table.exit-sched")).toBeTruthy();       // schedule moved inside
    expect(details!.querySelector(".exit-posbar")).toBeTruthy();           // tax bar moved inside
  });

  it("the headline CTA stages the first tranche", async () => {
    apiMock.mockImplementation((path: string) => {
      if (path.startsWith("/api/exit-plan?")) return Promise.resolve(planFixture());
      if (path === "/api/exit-plan/stage") {
        return Promise.resolve({ staged: true, symbol: "EXITME", basket: [], tranche: null });
      }
      return Promise.resolve({ orders: [] });
    });
    await loadExit();
    await flush();
    document.querySelector<HTMLButtonElement>("#exit-body .exit-reco-cta")!.click();
    await flush();
    expect(apiMock).toHaveBeenCalledWith(
      "/api/exit-plan/stage", "POST",
      expect.objectContaining({ symbol: "EXITME", index: 1, cfg: expect.any(Object) }),
    );
  });

  it("shows an empty state when nothing needs exiting", async () => {
    apiMock.mockResolvedValue(planFixture({ positions: [] }));
    await loadExit();
    await flush();
    expect(document.querySelector("#exit-body .empty-state")).toBeTruthy();
  });

  it("a partial reduce renders a Keep segment sized to the kept remainder", async () => {
    // Fixture keeps 30k of an 800k position — the bar must NOT read as a full exit.
    apiMock.mockResolvedValue(planFixture());
    await loadExit();
    await flush();
    const segs = [...document.querySelectorAll<HTMLElement>("#exit-body .exit-posbar-seg")];
    const keep = segs.find((s) => s.classList.contains("keep"));
    expect(keep).toBeTruthy();
    // keep = (800k-770k)/800k = 3.75% of the position.
    expect(keep!.style.width).toBe("3.8%");
    // Sell-now slice is only half the position, never full.
    const sellNow = segs.find((s) => s.classList.contains("good"))!;
    expect(sellNow.style.width).toBe("50.0%");
    // Header spells out the partial nature.
    const body = document.querySelector("#exit-body")!.textContent || "";
    expect(body).toContain("keeping 4%");
  });

  it("a full exit keeps nothing (no keep segment)", async () => {
    const full = planFixture();
    full.positions[0].end_state = "zero";
    full.positions[0].exit_czk = 800_000;      // sell the whole position
    await apiMock.mockResolvedValue(full);
    await loadExit();
    await flush();
    const keep = document.querySelector("#exit-body .exit-posbar-seg.keep");
    expect(keep).toBeFalsy();
    const body = document.querySelector("#exit-body")!.textContent || "";
    expect(body).toContain("full exit — nothing kept");
  });

  it("stages a tranche to the trade desk with only symbol/index/cfg", async () => {
    apiMock.mockImplementation((path: string) => {
      if (path.startsWith("/api/exit-plan?")) return Promise.resolve(planFixture());
      if (path === "/api/exit-plan/stage") {
        return Promise.resolve({
          staged: true, symbol: "EXITME",
          basket: [{ symbol: "EXITME", delta_czk: -200_000 }],
          tranche: { index: 1, date: "2026-07-01", shares: 2000, czk: 200_000, limit_price: 100, limit_currency: "USD", over_adv_cap: true },
        });
      }
      return Promise.resolve({ orders: [] }); // trade desk status/etc after nav
    });

    await loadExit();
    await flush();

    const stageBtn = [...document.querySelectorAll<HTMLButtonElement>("#exit-body .exit-stage-btn")][0];
    expect(stageBtn).toBeTruthy();
    stageBtn.click();
    await flush();

    expect(apiMock).toHaveBeenCalledWith(
      "/api/exit-plan/stage", "POST",
      expect.objectContaining({ symbol: "EXITME", index: 1, cfg: expect.any(Object) }),
    );
    // The returned basket is mirrored into shared state for the Trade desk.
    expect(state.stagedBasket).toEqual([{ symbol: "EXITME", delta_czk: -200_000 }]);
  });

  it("offers a live-quoted covered-call route and stages only its rung index", async () => {
    const plan = coveredCallPlan();
    apiMock.mockImplementation((path: string) => {
      if (path.startsWith("/api/exit-plan?")) return Promise.resolve(plan);
      if (path === "/api/exit-plan/stage-call") {
        return Promise.resolve({
          staged: true, symbol: "EXITME", rung: plan.positions[0].options!.covered_call_ladder[0],
          leg: {}, basket: [],
        });
      }
      return Promise.resolve({ orders: [] });
    });
    await loadExit();
    await flush();

    const route = [...document.querySelectorAll<HTMLButtonElement>(".exit-route-tab")]
      .find((b) => b.textContent === "Covered-call exit")!;
    expect(route).toBeTruthy();
    route.click();
    const panel = document.querySelector<HTMLElement>('[data-exit-route="covered_call"]')!;
    expect(panel.textContent).toContain("Underlying last 101.25 USD");
    expect(panel.textContent).toContain("Bid (sell)");
    expect(panel.textContent).toContain("Ask (buy)");
    expect(panel.textContent).toContain("7 contracts available before working orders");
    expect(panel.textContent).toContain("700 shares available to cover calls");
    expect(panel.textContent).toContain("Assignment is not guaranteed");

    panel.querySelector<HTMLButtonElement>(".exit-stage-call")!.click();
    await flush();
    expect(apiMock).toHaveBeenCalledWith(
      "/api/exit-plan/stage-call", "POST",
      expect.objectContaining({ symbol: "EXITME", rung_index: 0, cfg: expect.any(Object) }),
      { timeoutMs: 60_000 },
    );
  });

  it("shows unavailable option quotes honestly and disables staging", async () => {
    const plan = coveredCallPlan();
    const rung = plan.positions[0].options!.covered_call_ladder[0];
    Object.assign(rung, {
      executable: false, bid: null, ask: null, last: null,
      source: "black_scholes", estimate: true, conid: undefined,
    });
    apiMock.mockResolvedValue(plan);
    await loadExit();
    await flush();
    [...document.querySelectorAll<HTMLButtonElement>(".exit-route-tab")]
      .find((b) => b.textContent === "Covered-call exit")!.click();

    const panel = document.querySelector<HTMLElement>('[data-exit-route="covered_call"]')!;
    expect(panel.textContent).toContain("No executable covered call");
    expect(panel.textContent).toContain("Unavailable");
    expect(panel.textContent).toContain("—");
    expect(panel.querySelector<HTMLButtonElement>(".exit-stage-call")!.disabled).toBe(true);
  });
});
