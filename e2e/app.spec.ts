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
});
