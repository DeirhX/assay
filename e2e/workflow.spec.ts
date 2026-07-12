import { expect, test } from "@playwright/test";
import { installApi } from "./_api";

const row = {
  key: "AAPL", name: "AAPL", kind: "target", rule: "accumulate", held: true,
  current_pct: 2, current_czk: 20_000, low: 3, high: 5, mid: 4, status: "BELOW",
  drift_pct: -1, action: "buy", suggest_delta_pct: 1, suggest_delta_czk: 10_000,
  mark_price: 190, mark_currency: "USD",
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

const putRoute = ({
  eligible = true,
  stageable = true,
  margin = true,
  reasons = [],
  ladder,
}: {
  eligible?: boolean;
  stageable?: boolean;
  margin?: boolean;
  reasons?: string[];
  ladder?: Record<string, unknown>[];
} = {}) => ({
  symbol: "AAPL", delta_czk: 10_000, direction: "increase",
  planned_shares: 100, underlying: 200, currency: "USD", fx_to_base: 23,
  source: "ibkr",
  direct: { kind: "buy_shares", label: "Buy shares", eligible: true, reasons: [] },
  option: {
    kind: "cash_secured_put",
    label: margin ? "Sell put (margin)" : "Sell cash-secured put",
    eligible, stageable, reasons,
    contracts: eligible ? 1 : 0,
    assignment_shares: eligible ? 100 : 0,
    share_deviation: 0,
    rounded_up: false,
    collateral_mode: margin ? "margin" : "cash",
    available_cash_czk: margin ? null : 1_000_000,
  },
  recommended: "buy_shares",
  ladder: ladder ?? (eligible ? [{
    conid: stageable ? 556 : null,
    strike: 190,
    expiry: "2026-08-21",
    dte: 37,
    premium: 2.1,
    premium_czk: 4_830,
    effective_entry: 187.9,
    cash_secured_czk: 437_000,
    moneyness_pct: -5,
    premium_yield_annual_pct: 10.9,
    assignment_prob_pct: 25,
    open_interest: 500,
    volume: 50,
    spread_pct: 5,
    liquidity: stageable ? "ok" : "thin",
    source: stageable ? "ibkr" : "yahoo",
    estimate: !stageable,
    stageable,
    executable: stageable,
    bid: stageable ? 2 : null,
    ask: stageable ? 2.2 : null,
    quote_fresh: stageable,
  }] : []),
});

test.describe("rebalance review safety", () => {
  test("impact preview stays read-only until orders are explicitly queued", async ({ page }) => {
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
    let stageMode = "";
    page.on("request", (request) => {
      if (new URL(request.url()).pathname === "/api/rebalance/stage" &&
          request.method() === "POST") {
        stagePosts += 1;
        stageMode = request.postDataJSON().mode;
      }
    });

    await page.goto("/?view=rebalance");
    await page.getByRole("button", { name: "Preview impact" }).click();
    await expect(page.getByRole("button", { name: "Add 1 order to queue →" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Close impact preview" })).toBeVisible();
    await page.getByRole("button", { name: "Close impact preview" }).click();
    await expect(page.locator("#reb-whatif")).toBeEmpty();
    await page.getByRole("button", { name: "Preview impact" }).click();
    await expect(page.getByRole("button", { name: "Add 1 order to queue →" })).toBeVisible();
    expect(stagePosts).toBe(0);

    await page.getByRole("button", { name: "Add 1 order to queue →" }).click();
    await expect(page.getByRole("button", { name: "Orders added ✓" })).toBeVisible();
    expect(stagePosts).toBe(1);
    expect(stageMode).toBe("append");
    await page.getByRole("button", { name: "Add another trade" }).click();
    await expect(page.locator("#reb-whatif")).toBeEmpty();
    await expect(page.getByRole("button", { name: "Preview impact" })).toBeVisible();
  });

  test("dragging the projected marker updates the trade percentage", async ({ page }) => {
    await installApi(page, {
      "/api/rebalance": plan,
      "/api/trade/basket": { trades: [], revision: "", reviewed: false },
    });
    await page.goto("/?view=rebalance");

    const targetRow = page.locator(".reb-data-row").filter({ hasText: "AAPL" });
    const marker = targetRow.getByRole("slider", { name: "Projected portfolio weight" });
    const track = targetRow.locator(".reb-track");
    const input = targetRow.locator(".reb-plan-input");
    await expect(marker).toHaveAttribute("aria-valuenow", "3");
    await marker.scrollIntoViewIfNeeded();

    const markerBox = await marker.boundingBox();
    const trackBox = await track.boundingBox();
    expect(markerBox).not.toBeNull();
    expect(trackBox).not.toBeNull();
    const scaleMax = Number(await marker.getAttribute("aria-valuemax"));
    const desiredProjected = Math.round(scaleMax * 0.75 * 10) / 10;

    await page.mouse.move(markerBox!.x + markerBox!.width / 2, markerBox!.y + markerBox!.height / 2);
    await page.mouse.down();
    await page.mouse.move(trackBox!.x + trackBox!.width * 0.75, trackBox!.y + trackBox!.height / 2);
    await page.mouse.up();

    const expectedDelta = (desiredProjected - row.current_pct) / (1 - desiredProjected / 100);
    expect(Number(await input.inputValue())).toBeCloseTo(expectedDelta, 1);
    expect(Number(await marker.getAttribute("aria-valuenow"))).toBeCloseTo(desiredProjected, 1);
  });

  test("Current book opens in a drawer without discarding edited amounts", async ({ page }) => {
    await installApi(page, {
      "/api/rebalance": plan,
      "/api/overview": {
        snapshot: { exists: true, positions: 1, age_days: 0, stale: false },
        plan: { rows: 1, out_of_band: 1, actionable: 1 },
        staged_basket: { count: 0 },
      },
      "/api/trade/basket": { trades: [], revision: "", reviewed: false },
      "/api/holdings": {
        net_asset_value: 1_000_000,
        invested_value: 1_000_000,
        generated_at: "2026-07-11T00:00:00Z",
        sizing_legend: {},
        positions: [{
          symbol: "AAPL", provider_symbol: "AAPL", researchable: true,
          description: "Apple", asset_class: "STK", quantity: 100,
          percent_of_nav: 2, broker_percent_of_nav: 2, base_market_value: 20_000,
          currency: "USD", unrealized_pnl: 0, issuer_country_code: "US", option: null,
        }],
      },
    });
    await page.goto("/?view=rebalance");

    const input = page.locator(".reb-data-row").filter({ hasText: "AAPL" }).locator(".reb-plan-input");
    await input.fill("2.2");
    await page.getByRole("button", { name: "View positions ↗" }).click();

    const drawer = page.getByRole("dialog", { name: "Current book" });
    await expect(drawer).toBeVisible();
    await expect(drawer).toContainText("AAPL");
    await expect(page.locator("#view-rebalance")).toHaveClass(/active/);
    await expect(input).toHaveValue("2.2");

    await page.getByRole("button", { name: "Close current book" }).click();
    await expect(drawer).toHaveCount(0);
    await expect(input).toHaveValue("2.2");
  });

  test("including a buy defaults to shares instead of silently trying a put", async ({ page }) => {
    const item = {
      id: "rebalance:AAPL",
      symbol: "AAPL",
      source: "rebalance",
      direction: "increase",
      delta_czk: 10_000,
      delta_pct: 1,
      desired_weight_pct: 3,
      route_policy: "auto_put",
      route_selection: null,
      status: "suggested",
    };
    const executionPlan = {
      schema_version: 1,
      version: 1,
      items: [item],
    };
    const requests: Record<string, unknown>[] = [];
    let routeRequests = 0;
    page.on("request", (request) => {
      if (new URL(request.url()).pathname === "/api/rebalance/route") routeRequests += 1;
    });
    await installApi(page, {
      "/api/rebalance": { ...plan, execution_plan: executionPlan },
      "/api/trade/basket": { trades: [], revision: "", reviewed: false },
    });
    await page.route("**/api/execution-plan", async (route) => {
      const body = route.request().postDataJSON() as {
        changes?: Record<string, unknown>;
      };
      requests.push(body);
      Object.assign(item, body.changes || {});
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify(executionPlan),
      });
    });

    await page.goto("/?view=rebalance");
    const aapl = page.locator(".reb-data-row").filter({ hasText: "AAPL" });
    const idleBackground = await aapl.evaluate((node) => getComputedStyle(node).backgroundImage);
    await aapl.locator(".reb-execute-toggle").click();

    await expect(aapl.locator("select.reb-route-select")).toHaveValue("direct");
    await expect.poll(
      () => aapl.evaluate((node) => getComputedStyle(node).backgroundImage),
    ).not.toBe(idleBackground);
    await expect(aapl.locator(".reb-limit-input")).toHaveValue("190");
    await expect(aapl.locator(".reb-limit-field")).toContainText("recommended");
    await expect.poll(() => requests.length).toBeGreaterThanOrEqual(2);
    expect(requests).toEqual(expect.arrayContaining([
      expect.objectContaining({ changes: expect.objectContaining({ status: "selected" }) }),
      expect.objectContaining({
        changes: expect.objectContaining({ route_policy: "buy_shares", limit_price: 190 }),
      }),
    ]));
    expect(routeRequests).toBe(0);

    const limit = aapl.locator(".reb-limit-input");
    await limit.fill("195");
    await limit.press("Tab");
    await expect(aapl.locator(".reb-limit-field")).toContainText("custom");
    await expect.poll(() => requests.some(
      (request) => (request.changes as Record<string, unknown>)?.limit_price === 195,
    )).toBe(true);

    await limit.fill("");
    await limit.press("Tab");
    await expect(aapl.locator(".reb-limit-field")).toContainText("market");
    await expect.poll(() => requests.some(
      (request) => (request.changes as Record<string, unknown>)?.limit_price === null,
    )).toBe(true);

    await aapl.getByRole("button", { name: "Exclude" }).click();
    await expect(aapl.getByText("Include trade", { exact: true })).toBeVisible();
    await expect(aapl.locator("select.reb-route-select")).toBeHidden();
    await expect.poll(
      () => aapl.evaluate((node) => getComputedStyle(node).backgroundImage),
    ).toBe(idleBackground);
    expect(requests).toEqual(expect.arrayContaining([
      expect.objectContaining({ changes: expect.objectContaining({ status: "deferred" }) }),
    ]));

    await aapl.locator(".reb-execute-toggle").click();
    await expect(aapl.locator("select.reb-route-select")).toHaveValue("direct");
    await expect(aapl.getByRole("button", { name: "Exclude" })).toBeVisible();
    await expect.poll(
      () => aapl.evaluate((node) => getComputedStyle(node).backgroundImage),
    ).not.toBe(idleBackground);
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
    await expect(page.getByRole("button", { name: "Review projected portfolio →" })).toBeVisible();
  });

  test("margin account offers a short put without the cash-collateral blocker", async ({ page }) => {
    const route = putRoute();
    route.ladder.push({
      ...route.ladder[0],
      conid: 557,
      strike: 185,
      effective_entry: 182.6,
      premium_yield_annual_pct: 8.7,
      assignment_prob_pct: 19,
      bid: 1.5,
      ask: 1.7,
    });
    await installApi(page, {
      "/api/rebalance": plan,
      "/api/trade/basket": { trades: [], revision: "", reviewed: false },
      "/api/rebalance/route": route,
    });
    await page.goto("/?view=rebalance");
    const aapl = page.locator(".reb-data-row").filter({ hasText: "AAPL" });
    await aapl.locator("select.reb-route-select").selectOption("option");

    await expect(aapl.getByText("Conditional entry")).toBeVisible();
    await expect(aapl.locator(".reb-option-contract")).toHaveCount(2);
    await expect(aapl).toContainText("Bid / ask");
    await expect(aapl).toContainText("Effective entry");
    await expect(aapl).toContainText("Assignment notional");
    await expect(aapl).toContainText("IBKR validates margin at preview");
    await expect(aapl).not.toContainText("Cash-secured put unavailable");

    await aapl.getByRole("button", { name: "Close" }).click();
    await expect(aapl.locator(".reb-route-row-detail")).toBeHidden();
    await aapl.locator("select.reb-route-select").selectOption("option");

    const use = aapl.getByRole("button", { name: "Use contract" });
    await use.nth(1).click();
    await expect(aapl.locator("select.reb-route-select")).toHaveValue("option");
    await expect(aapl.getByRole("button", { name: "Selected ✓" }))
      .toHaveClass(/active/);

    await aapl.locator("select.reb-route-select").selectOption("direct");
    await expect(aapl.locator(".reb-route-row-detail")).toBeHidden();
    await expect(aapl.locator("select.reb-route-select")).toHaveValue("direct");
  });

  test("one ticker explains unavailable and indicative option routes", async ({ page }) => {
    const unavailable = putRoute({
      eligible: false,
      margin: false,
      reasons: ["No uncommitted snapshot cash is available to secure a put."],
    });
    let response = unavailable;
    await installApi(page, {
      "/api/rebalance": plan,
      "/api/trade/basket": { trades: [], revision: "", reviewed: false },
    });
    await page.route("**/api/rebalance/route?*", async (route) => {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify(response),
      });
    });
    await page.goto("/?view=rebalance");
    const aapl = page.locator(".reb-data-row").filter({ hasText: "AAPL" });
    const select = aapl.locator("select.reb-route-select");
    await select.selectOption("option");

    await expect(aapl).toContainText("Cash-secured put unavailable");
    await expect(aapl).toContainText("choose Buy shares");
    await aapl.getByRole("button", { name: "Close" }).click();
    await expect(aapl.locator(".reb-route-row-detail")).toBeHidden();

    response = putRoute({
      stageable: false,
      reasons: ["Indicative levels are available; staging needs an exact IBKR contract."],
    });
    await select.selectOption("option");
    await expect(aapl.locator(".reb-option-contract")).toHaveCount(1);
    await expect(aapl.getByRole("button", { name: "Indicative only" })).toBeDisabled();
    await expect(aapl).toContainText("thin liquidity");
    await expect(aapl).toContainText("Indicative levels are available");
  });

  test("one ticker surfaces option loading failures and can close them", async ({ page }) => {
    await installApi(page, {
      "/api/rebalance": plan,
      "/api/trade/basket": { trades: [], revision: "", reviewed: false },
    });
    await page.route("**/api/rebalance/route?*", async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 250));
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ error: "IBKR option service unavailable" }),
      });
    });
    await page.goto("/?view=rebalance");
    const aapl = page.locator(".reb-data-row").filter({ hasText: "AAPL" });
    await aapl.locator("select.reb-route-select").selectOption("option");

    await expect(aapl).toContainText("loading strikes and quotes");
    await expect(aapl).toContainText("Could not load option routes");
    await expect(aapl).toContainText("IBKR option service unavailable");
    await aapl.getByRole("button", { name: "Close" }).click();
    await expect(aapl.locator(".reb-route-row-detail")).toBeHidden();
  });

  test("editing one ticker flips routes and exit navigation preserves its symbol", async ({ page }) => {
    await installApi(page, {
      "/api/rebalance": plan,
      "/api/trade/basket": { trades: [], revision: "", reviewed: false },
      "/api/exit-plan": { positions: [], generated_at: "2026-07-12T00:00:00Z" },
    });
    await page.goto("/?view=rebalance");
    const aapl = page.locator(".reb-data-row").filter({ hasText: "AAPL" });
    const input = aapl.locator(".reb-plan-input");
    await input.fill("-1");
    await input.press("Tab");

    await expect(aapl.locator("select.reb-route-select").locator('option[value="direct"]'))
      .toHaveText("Sell shares");
    await expect(aapl.locator("select.reb-route-select").locator('option[value="option"]'))
      .toHaveText("Covered call…");
    await aapl.locator("select.reb-route-select").selectOption("exit");
    await expect(page).toHaveURL(/view=exit.*ticker=AAPL/);
  });

  test("choose CSP in the plan → simulate → stage → approve → unlock Trade preview", async ({ page }) => {
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
    const aapl = page.locator(".reb-data-row").filter({ hasText: "AAPL" });
    await aapl.locator("select.reb-route-select").selectOption("option");
    await expect(aapl.getByText("Conditional entry")).toBeVisible();
    await page.getByRole("button", { name: "Use contract" }).click();
    await page.getByRole("button", { name: "Preview impact" }).click();
    await expect(page.locator("#reb-whatif")).toContainText("Cash-secured put");
    await page.getByRole("button", { name: "Add 1 order to queue →" }).click();
    await page.getByRole("button", { name: "Review projected portfolio →" }).click();

    await expect(page.locator("#view-target-state")).toHaveClass(/active/);
    await expect(page.locator("#tstate-body")).toContainText("AAPL +100 shares");
    await page.getByRole("button", { name: "Approve order queue →" }).click();
    await page.getByRole("button", { name: "Preview & place →" }).click();

    await expect(page.locator("#view-trade")).toHaveClass(/active/);
    await expect(page.locator(".trade-basket-option")).toContainText("190 put");
    await expect(page.getByRole("tab", { name: "Order review" })).toBeEnabled();
    await expect(page.getByRole("tab", { name: "Order review" }))
      .toHaveAttribute("title", "Preview the queued orders through IBKR");
  });
});
