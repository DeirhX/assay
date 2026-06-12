import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "happy-dom",
    // Several view modules attach DOM listeners at import time, so the setup
    // file loads the real index.html shell before any test module is imported.
    setupFiles: ["web/tests/setup.ts"],
    include: ["web/tests/**/*.test.ts"],
  },
});
