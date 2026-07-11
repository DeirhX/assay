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
      "/api/rebalance/stage": {
        trades: projection.trades, revision: "rev-1", reviewed: false,
      },
    });
    let stagePosts = 0;
    page.on("request", (request) => {
      if (new URL(request.url()).pathname === "/api/rebalance/stage" &&
          request.method() === "POST") stagePosts += 1;
    });

    await page.goto("/?view=rebalance");
    await page.getByRole("button", { name: "Simulate trades" }).click();
    await expect(page.getByRole("button", { name: "Stage 1 order →" })).toBeVisible();
    expect(stagePosts).toBe(0);

    await page.getByRole("button", { name: "Stage 1 order →" }).click();
    await expect(page.getByRole("button", { name: "Orders staged ✓" })).toBeVisible();
    expect(stagePosts).toBe(1);
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

  test("simulate → choose CSP → stage → approve → unlock Trade preview", async ({ page }) => {
    const csp = {
      type: "cash_secured_put",
      route: "cash_secured_put",
      leg_id: "cash_secured_put:AAPL:556",
      symbol: "AAPL",
      conid: 556,
      expiry: "2026-08-21",
      strike: 190,
      contracts: 1,
      multiplier: 100,
      limit_price: 2.1,
      currency: "USD",
      fx_to_base: 23,
      provenance: [{
        source: "rebalance_routes",
        route: "cash_secured_put",
        intended_assigned_shares: 100,
      }],
    };
    const responses: Record<string, unknown> = {
      "/api/rebalance": plan,
      "/api/whatif": projection,
      "/api/rebalance/route": {
        symbol: "AAPL", delta_czk: 10_000, direction: "increase",
        planned_shares: 100, underlying: 200, currency: "USD", fx_to_base: 23,
        source: "ibkr",
        direct: { kind: "buy_shares", label: "Buy shares", eligible: true, reasons: [] },
        option: {
          kind: "cash_secured_put", label: "Sell cash-secured put",
          eligible: true, stageable: true, reasons: [], contracts: 1,
          assignment_shares: 100, share_deviation: 0, rounded_up: false,
          available_cash_czk: 1_000_000,
        },
        recommended: "buy_shares",
        ladder: [{
          conid: 556, strike: 190, expiry: "2026-08-21", dte: 37,
          premium: 2.1, premium_czk: 4_830, effective_entry: 187.9,
          cash_secured_czk: 437_000, moneyness_pct: -5,
          premium_yield_annual_pct: 10.9, assignment_prob_pct: 25,
          open_interest: 500, volume: 50, spread_pct: 5, liquidity: "ok",
          source: "ibkr", estimate: false, stageable: true, executable: true,
          bid: 2, ask: 2.2, quote_fresh: true,
        }],
      },
      "/api/rebalance/stage": {
        staged: true, basket: [csp], trades: [csp],
        routes: [{ symbol: "AAPL", route: "cash_secured_put" }],
        revision: "rev-csp", reviewed: false,
      },
      "/api/trade/basket": {
        trades: [csp], revision: "rev-csp", reviewed: false,
      },
      "/api/trade/basket/review": {
        trades: [csp], revision: "rev-csp", reviewed: true,
      },
      "/api/trade/status": {
        trading_enabled: true, authenticated: true, connected: true,
        default_account: "DU1", accounts: [{ id: "DU1", kind: "paper" }],
        live_allowed: false,
      },
      "/api/trade/orders": { orders: [] },
      "/api/spark": { spark: {} },
    };
    page.on("request", (request) => {
      if (
        new URL(request.url()).pathname === "/api/trade/basket/review"
        && request.method() === "POST"
      ) {
        responses["/api/trade/basket"] = {
          trades: [csp], revision: "rev-csp", reviewed: true,
        };
      }
    });
    await installApi(page, responses);
    await page.goto("/?view=rebalance");
    await page.getByRole("button", { name: "Simulate trades" }).click();
    await page.getByRole("button", { name: "Check cash-secured puts" }).click();
    await expect(page.getByText("Sell cash-secured put", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Use" }).click();
    await page.getByRole("button", { name: "Stage 1 order →" }).click();
    await page.getByRole("button", { name: "Review target state →" }).click();

    await expect(page.locator("#view-target-state")).toHaveClass(/active/);
    await expect(page.locator("#tstate-body")).toContainText("AAPL +100 shares");
    await page.getByRole("button", { name: "Approve this projection →" }).click();
    await page.getByRole("button", { name: "Open Trade desk →" }).click();

    await expect(page.locator("#view-trade")).toHaveClass(/active/);
    await expect(page.locator(".trade-basket-option")).toContainText("190 put");
    await expect(page.getByRole("tab", { name: "Order review" })).toBeEnabled();
    await expect(page.getByRole("tab", { name: "Order review" }))
      .toHaveAttribute("title", "Preview the staged orders through IBKR");
  });
});
