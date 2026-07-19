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

  test("the persistent IBKR indicator attempts reconnect and opens its connection panel", async ({ page }) => {
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

    await expect(page.locator("#gateway-indicator")).toHaveAttribute("aria-expanded", "true");
    await expect(page.locator("#gateway-panel")).toBeVisible();
    await expect(page.locator("#gateway-panel-content")).toContainText("login required");
  });

  test("Trade lands on the pipeline index with the guarded execution flow", async ({ page }) => {
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

  test("Targets lands on Composition; advanced modes stay outside Trade", async ({ page }) => {
    await installApi(page);
    await page.goto("/");

    await page.locator('.group[data-group="strategy"]').click();
    await expect(page.locator("#view-working-draft")).toHaveClass(/active/);
    await expect(page.locator("#composition-panel")).toBeVisible();
    await expect(page.locator('.subtabs[data-group="strategy"]')).toHaveCount(0);
    await expect(page.locator("#flowbar")).toBeHidden();

    await page.locator('#composition-panel [data-shell-view="optimizer"]').click();
    await expect(page.locator("#view-optimizer")).toHaveClass(/active/);
    await expect(page.locator('.group[data-group="strategy"]')).toHaveClass(/active/);
    await expect(page.locator("#flowbar")).toBeHidden();
  });

  test("Research is Topics and Ticker; Activity lives under Portfolio", async ({ page }) => {
    await installApi(page);
    await page.goto("/");

    await page.locator('.group[data-group="research"]').click();
    const researchTabs = page.locator('.subtabs[data-group="research"]');
    await expect(researchTabs).toBeVisible();
    await expect(researchTabs).toContainText("Topics");
    await expect(researchTabs).toContainText("Ticker");
    await expect(researchTabs).not.toContainText("Deep Research");

    await page.locator('.group[data-group="portfolio"]').click();
    const portTabs = page.locator('.subtabs[data-group="portfolio"]');
    await expect(portTabs).toContainText("Activity");
    await expect(portTabs).toContainText("Decisions");
    await expect(page.locator('.utility-tab[data-view="activity"]')).toHaveCount(0);
  });

  test("advanced reductions has an explicit return to order building", async ({ page }) => {
    await installApi(page);
    await page.goto("/?view=rebalance");

    await page.getByRole("button", { name: "Advanced reductions & exits" }).click();
    await expect(page.locator("#view-exit")).toHaveClass(/active/);
    await page.getByRole("button", { name: "← Back to build orders" }).click();
    await expect(page.locator("#view-rebalance")).toHaveClass(/active/);
  });

  test("Journal highlights Portfolio Decisions subtab", async ({ page }) => {
    await installApi(page);
    await page.goto("/?view=journal");

    await expect(page.locator('.group[data-group="portfolio"]')).toHaveClass(/active/);
    await expect(page.locator('.subtab[data-view="journal"]')).toHaveClass(/active/);
    await expect(page.locator("#view-journal")).toHaveClass(/active/);
  });
});
