import { expect, test } from "@playwright/test";
import { installApi } from "./_api";

// The trade desk is the only order-placing surface, so its safety messaging is
// the journey worth pinning end to end through the real built app.
test.describe("trade desk safety states", () => {
  test("explains and blocks when trading is disabled", async ({ page }) => {
    await installApi(page); // default trade/status: disabled + unauthenticated
    await page.goto("/?view=trade");

    await expect(page.locator("#view-trade")).toHaveClass(/active/);
    const banner = page.locator("#trade-banner");
    await expect(banner).toContainText("Trading is disabled");
    await expect(banner).toContainText("Gateway not connected");
  });

  test("shows the connected paper account and an empty order-queue hint", async ({ page }) => {
    await installApi(page, {
      "/api/trade/status": {
        trading_enabled: true, authenticated: true, default_account: "DU1",
        accounts: [{ id: "DU1", kind: "paper" }], live_allowed: false, competing: false,
      },
    });
    await page.goto("/?view=trade");

    await expect(page.locator("#trade-banner")).toContainText("Paper account DU1");
    // No queued orders from the rebalance planner -> the desk refuses gracefully.
    await expect(page.locator("#trade-result")).toContainText("The order queue is empty");
  });
});
