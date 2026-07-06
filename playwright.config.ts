import { defineConfig, devices } from "@playwright/test";

// E2E runs the real SPA (Vite dev server) in a real browser. The Python API is
// NOT booted: each test intercepts /api/** and serves fixtures, so the journeys
// are hermetic, fast, and don't need secrets or the private data submodule.
//
// The port is overridable via E2E_PORT so a worktree's e2e can run on a free
// port without colliding with (or reusing the stale build of) a dev server you
// already have open on the default 5173 elsewhere.
const PORT = process.env.E2E_PORT ? Number(process.env.E2E_PORT) : 5173;
const BASE_URL = `http://127.0.0.1:${PORT}`;

export default defineConfig({
  testDir: "e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? "line" : [["list"]],
  use: {
    baseURL: BASE_URL,
    trace: "on-first-retry",
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
  ],
  webServer: {
    command: `npm run dev -- --port ${PORT} --strictPort`,
    url: BASE_URL,
    reuseExistingServer: !process.env.CI,
    timeout: 60_000,
  },
});
