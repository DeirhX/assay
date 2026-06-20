import { expect, test } from "@playwright/test";
import { installApi } from "./_api";

test.describe("app shell + navigation", () => {
  test("boots to the default Ticker view with the top nav present", async ({ page }) => {
    await installApi(page);
    await page.goto("/");

    // The three-item top nav and the default deepdive view render.
    await expect(page.locator('.group[data-group="deepdive"]')).toHaveClass(/active/);
    await expect(page.locator('.group[data-group="portfolio"]')).toBeVisible();
    await expect(page.locator("#view-deepdive")).toHaveClass(/active/);
  });

  test("navigating to Portfolio > Trade activates the trade view", async ({ page }) => {
    await installApi(page);
    await page.goto("/");

    await page.locator('.group[data-group="portfolio"]').click();
    // The portfolio sub-tab bar reveals; pick Trade.
    await page.locator('.subtab[data-view="trade"]').click();

    await expect(page.locator("#view-trade")).toHaveClass(/active/);
    await expect(page.locator('.group[data-group="portfolio"]')).toHaveClass(/active/);
  });
});
