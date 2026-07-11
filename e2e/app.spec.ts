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

  test("the persistent IBKR indicator attempts reconnect and opens Trade", async ({ page }) => {
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

    await expect(page.locator("#view-trade")).toHaveClass(/active/);
    await expect(page.locator("#trade-banner")).toContainText("Gateway not connected");
  });

  test("navigating to Rebalance > Trade activates the trade view", async ({ page }) => {
    await installApi(page);
    await page.goto("/");

    await page.locator('.group[data-group="rebalance"]').click();
    // The review gate appears before the execution surface in the subnav.
    const order = await page.locator('.subtabs[data-group="rebalance"] [data-view]').evaluateAll(
      (nodes) => nodes.map((n) => (n as HTMLElement).dataset.view),
    );
    expect(order.indexOf("target-state")).toBeLessThan(order.indexOf("trade"));
    await page.locator('.subtab[data-view="trade"]').click();

    await expect(page.locator("#view-trade")).toHaveClass(/active/);
    await expect(page.locator('.group[data-group="rebalance"]')).toHaveClass(/active/);
  });

  test("Journal keeps the Activity utility navigation highlighted", async ({ page }) => {
    await installApi(page);
    await page.goto("/?view=journal");

    await expect(page.locator('.utility-tab[data-view="activity"]')).toHaveClass(/active/);
    await expect(page.locator("#view-journal")).toHaveClass(/active/);
  });
});
