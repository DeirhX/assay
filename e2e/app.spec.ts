import { expect, test } from "@playwright/test";
import { installApi } from "./_api";

test.describe("app shell + navigation", () => {
  test("boots to the Today command center with the primary workflow present", async ({ page }) => {
    await installApi(page);
    await page.goto("/");

    await expect(page.locator('.group[data-group="today"]')).toHaveClass(/active/);
    await expect(page.locator('.group[data-group="strategy"]')).toBeVisible();
    await expect(page.locator('.group[data-group="rebalance"]')).toBeVisible();
    await expect(page.locator('.group[data-group="portfolio"]')).toBeVisible();
    await expect(page.locator("#view-today")).toHaveClass(/active/);
    await expect(page.locator("#gateway-indicator")).toBeVisible();
    await expect(page.locator("#gateway-indicator")).toContainText("IBKR: offline");
  });

  test("the persistent IBKR indicator attempts reconnect and shows recovery", async ({ page }) => {
    await installApi(page, {
      "/api/trade/status": {
        trading_enabled: false, authenticated: false, connected: false, accounts: [],
      },
      "/api/trade/reconnect": {
        trading_enabled: false, authenticated: false, connected: false, accounts: [],
        reconnect_error: "login required",
      },
    });
    await page.goto("/");
    await expect(page.locator("#gateway-indicator")).toContainText("IBKR: offline");

    const reconnect = page.waitForRequest(
      (request) => new URL(request.url()).pathname === "/api/trade/reconnect",
    );
    await page.locator("#gateway-indicator").click();
    await reconnect;

    await expect(page.locator("#gateway-panel")).toBeVisible();
    await expect(page.locator("#gateway-panel-content")).toContainText("Connection unavailable");
    await expect(page.locator("#gateway-panel-content")).toContainText("login required");
  });

  test("Orders lands on the pipeline index with the guarded execution flow", async ({ page }) => {
    await installApi(page);
    await page.goto("/");

    await page.locator('.group[data-group="rebalance"]').click();
    await expect(page.locator("#view-orders")).toHaveClass(/active/);
    await expect(page.locator("#orders-body")).toContainText("Planned trades");
    await expect(page.locator("#flowbar")).toBeVisible();
    await expect(page.locator('.subtabs[data-group="rebalance"]')).toHaveCount(0);
    // The impact gate appears before the execution surface in the workflow.
    const order = await page.locator("#flowbar [data-flow-view]").evaluateAll(
      (nodes) => nodes.map((n) => (n as HTMLElement).dataset.flowView),
    );
    expect(order.indexOf("target-state")).toBeLessThan(order.indexOf("trade"));
    await page.locator('#flowbar [data-flow-view="trade"]').click();

    await expect(page.locator("#view-trade")).toHaveClass(/active/);
    await expect(page.locator('.group[data-group="rebalance"]')).toHaveClass(/active/);
  });

  test("target-model tools live under Plan, outside the execution workflow", async ({ page }) => {
    await installApi(page);
    await page.goto("/");

    await page.locator('.group[data-group="strategy"]').click();
    const planTabs = page.locator('.subtabs[data-group="strategy"]');
    await expect(planTabs).toBeVisible();
    await expect(planTabs).toContainText("Guided plan");
    await expect(planTabs).toContainText("Optimizer");
    await expect(planTabs).toContainText("Pending model changes");
    await expect(page.locator("#flowbar")).toBeHidden();

    await planTabs.locator('[data-view="optimizer"]').click();
    await expect(page.locator("#view-optimizer")).toHaveClass(/active/);
    await expect(page.locator('.group[data-group="strategy"]')).toHaveClass(/active/);
  });

  test("advanced reductions has an explicit return to order building", async ({ page }) => {
    await installApi(page);
    await page.goto("/?view=rebalance");

    await page.getByRole("button", { name: "Advanced reductions & exits" }).click();
    await expect(page.locator("#view-exit")).toHaveClass(/active/);
    await page.getByRole("button", { name: "← Back to build orders" }).click();
    await expect(page.locator("#view-rebalance")).toHaveClass(/active/);
  });

  test("Journal keeps the Activity utility navigation highlighted", async ({ page }) => {
    await installApi(page);
    await page.goto("/?view=journal");

    await expect(page.locator('.utility-tab[data-view="activity"]')).toHaveClass(/active/);
    await expect(page.locator("#view-journal")).toHaveClass(/active/);
  });
});
