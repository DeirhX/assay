import { expect, test } from "@playwright/test";
import { installApi } from "./_api";

test.describe("app shell + navigation", () => {
  test("boots to the Assay Today home with the top nav present", async ({ page }) => {
    await installApi(page);
    await page.goto("/");

    // Assay is the home surface; Plan remains one click away.
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
