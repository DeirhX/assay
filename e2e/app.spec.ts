import { expect, test } from "@playwright/test";
import { installApi } from "./_api";

test.describe("app shell + navigation", () => {
  test("boots to the Assay overview with the top nav present", async ({ page }) => {
    await installApi(page);
    await page.goto("/");

    // A bare URL lands on the app-level Assay cockpit; workflow tools remain in
    // the top navigation.
    await expect(page.locator("#brand-home")).toHaveClass(/active/);
    await expect(page.locator('.group[data-group="strategy"]')).toBeVisible();
    await expect(page.locator('.group[data-group="rebalance"]')).toBeVisible();
    await expect(page.locator('.group[data-group="portfolio"]')).toBeVisible();
    await expect(page.locator("#view-today")).toHaveClass(/active/);
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
