import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../src/core", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/core")>()),
  api: vi.fn(),
}));

import { api } from "../src/core";
import { loadHoldings } from "../src/holdings";

const apiMock = vi.mocked(api);

describe("holdings live-data provenance", () => {
  beforeEach(() => {
    document.body.innerHTML =
      '<div id="hold-status"></div><div id="hold-synced"></div>' +
      '<div id="hold-gateway-notice"></div><div id="hold-result"></div>';
    apiMock.mockReset();
  });

  it("keeps the Flex snapshot and explains a missing gateway overlay", async () => {
    apiMock.mockImplementation((path: string) => {
      if (path === "/api/holdings") {
        return Promise.resolve({
          net_asset_value: 1_000_000,
          invested_value: 900_000,
          generated_at: "2026-07-10T10:00:00Z",
          sizing_legend: {},
          positions: [],
        });
      }
      if (path === "/api/holdings/live") {
        return Promise.resolve({
          available: false,
          reason: "gateway session is not authenticated",
        });
      }
      return Promise.resolve({});
    });

    await loadHoldings();
    await vi.waitFor(() => {
      expect(document.getElementById("hold-gateway-notice")!.textContent)
        .toContain("Showing the Flex snapshot");
    });
    expect(document.getElementById("hold-gateway-notice")!.textContent)
      .toContain("not authenticated");
    expect(document.getElementById("hold-result")!.textContent)
      .toContain("Net asset value");
  });
});
