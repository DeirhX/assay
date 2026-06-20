import { defineConfig, devices } from "@playwright/test";

// E2E runs the real SPA (Vite dev server) in a real browser. The Python API is
// NOT booted: each test intercepts /api/** and serves fixtures, so the journeys
// are hermetic, fast, and don't need secrets or the private data submodule.
export default defineConfig({
  testDir: "e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? "line" : [["list"]],
  use: {
    baseURL: "http://127.0.0.1:5173",
    trace: "on-first-retry",
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
  ],
  webServer: {
    command: "npm run dev",
    url: "http://127.0.0.1:5173",
    reuseExistingServer: !process.env.CI,
    timeout: 60_000,
  },
});
