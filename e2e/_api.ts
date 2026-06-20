import type { Page } from "@playwright/test";

// Hermetic API stub for e2e: intercept every /api/** call and serve JSON from a
// per-path map, with benign empty defaults for the chatter the app fires on boot
// (setup probe, login status, task-center poll). Tests pass `overrides` for the
// endpoints whose response shape the journey actually depends on.
const DEFAULTS: Record<string, unknown> = {
  "/api/setup/status": { data: { empty: false } },
  "/api/tasks": { tasks: [] },
  "/api/jobs": { jobs: [] },
  "/api/pplx/status": { logged_in: false, updated_at: null, note: "" },
  "/api/trade/status": {
    trading_enabled: false, authenticated: false, accounts: [], live_allowed: false,
  },
};

export async function installApi(page: Page, overrides: Record<string, unknown> = {}) {
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    const body = path in overrides ? overrides[path] : (path in DEFAULTS ? DEFAULTS[path] : {});
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });
}
