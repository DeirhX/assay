import { expect, test } from "@playwright/test";
import { installApi } from "./_api";

test.describe("app shell + navigation", () => {
  test("boots to the guided Plan view with the top nav present", async ({ page }) => {
    await installApi(page);
    await page.goto("/");

    // The workflow-ordered top nav lands on Plan (the guided strategy flow).
    await expect(page.locator('.group[data-group="strategy"]')).toHaveClass(/active/);
    await expect(page.locator('.group[data-group="rebalance"]')).toBeVisible();
    await expect(page.locator('.group[data-group="portfolio"]')).toBeVisible();
    await expect(page.locator("#view-strategy")).toHaveClass(/active/);
  });

  test("navigating to Rebalance > Trade activates the trade view", async ({ page }) => {
    await installApi(page);
    await page.goto("/");

    await page.locator('.group[data-group="rebalance"]').click();
    // The rebalance sub-tab bar reveals; pick Trade.
    await page.locator('.subtab[data-view="trade"]').click();

    await expect(page.locator("#view-trade")).toHaveClass(/active/);
    await expect(page.locator('.group[data-group="rebalance"]')).toHaveClass(/active/);
  });
});
