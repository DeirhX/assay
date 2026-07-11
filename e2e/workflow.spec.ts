import { expect, test } from "@playwright/test";
import { installApi } from "./_api";

const row = {
  key: "AAPL", name: "AAPL", kind: "target", rule: "accumulate", held: true,
  current_pct: 2, current_czk: 20_000, low: 3, high: 5, mid: 4, status: "BELOW",
  drift_pct: -1, action: "buy", suggest_delta_pct: 1, suggest_delta_czk: 10_000,
  note: null, members: null, interactive: true,
};

const plan = {
  nav: 1_000_000, invested: 1_000_000, currency: "CZK",
  snapshot: "2026-07-10T00:00:00+00:00", as_of: "2026-07-10",
  cash_target_pct: 5, funding_order: [], cash: null,
  rows: [row], untargeted: [], untargeted_pct: 0, provenance: {},
};

const projection = {
  currency: "CZK",
  trades: [{ symbol: "AAPL", delta_czk: 10_000 }],
  summary: {
    bands_in_before: 0, bands_in_after: 1, bands_total: 1,
    net_cash_czk: -10_000, realized_taxable_gain_czk: 0,
  },
  after: { rows: [{ ...row, current_pct: 3, current_czk: 30_000, status: "IN" }] },
  before_status: { AAPL: "BELOW" },
  cash: null, caveats: [],
};

test.describe("rebalance review safety", () => {
  test("simulation stays read-only until Stage orders is explicitly clicked", async ({ page }) => {
    await installApi(page, {
      "/api/rebalance": plan,
      "/api/whatif": projection,
      "/api/trade/basket": {
        trades: projection.trades, revision: "rev-1", reviewed: false,
      },
    });
    let basketPosts = 0;
    page.on("request", (request) => {
      if (new URL(request.url()).pathname === "/api/trade/basket" &&
          request.method() === "POST") basketPosts += 1;
    });

    await page.goto("/?view=rebalance");
    await page.getByRole("button", { name: "Simulate trades" }).click();
    await expect(page.getByRole("button", { name: "Stage 1 order →" })).toBeVisible();
    expect(basketPosts).toBe(0);

    await page.getByRole("button", { name: "Stage 1 order →" }).click();
    await expect(page.getByRole("button", { name: "Orders staged ✓" })).toBeVisible();
    expect(basketPosts).toBe(1);
  });

  test("Trade preview stays locked for an unreviewed queue", async ({ page }) => {
    await installApi(page, {
      "/api/trade/status": {
        trading_enabled: true, authenticated: true, default_account: "DU1",
        accounts: [{ id: "DU1", kind: "paper" }], live_allowed: false,
      },
      "/api/trade/basket": {
        trades: projection.trades, revision: "rev-1", reviewed: false,
      },
    });
    await page.goto("/?view=trade");

    await expect(page.getByRole("tab", { name: "Order review" })).toBeDisabled();
    await expect(page.getByRole("button", { name: "Review target state →" })).toBeVisible();
  });
});
