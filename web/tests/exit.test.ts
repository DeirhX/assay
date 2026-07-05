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

  it("shows an empty state when nothing needs exiting", async () => {
    apiMock.mockResolvedValue(planFixture({ positions: [] }));
    await loadExit();
    await flush();
    expect(document.querySelector("#exit-body .empty-state")).toBeTruthy();
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
});
