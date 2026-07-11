import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../src/core", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/core")>()),
  api: vi.fn(),
}));

import { api } from "../src/core";
import {
  gatewayConnected, gatewayUnavailableReason, publishGatewayStatus,
  reconnectGateway, refreshGatewayStatus, resetGatewayState, tickleGateway,
} from "../src/gateway";

const apiMock = vi.mocked(api);

describe("central IBKR gateway state", () => {
  beforeEach(() => {
    document.body.innerHTML =
      '<button id="gateway-indicator" class="gateway-indicator checking"></button>';
    apiMock.mockReset();
    resetGatewayState();
  });

  it("renders disconnected state explicitly", () => {
    publishGatewayStatus({
      trading_enabled: false, authenticated: false, connected: false,
    });
    const indicator = document.getElementById("gateway-indicator")!;
    expect(indicator.textContent).toBe("IBKR: offline");
    expect(indicator.classList.contains("offline")).toBe(true);
    expect(gatewayUnavailableReason()).toContain("not connected");
  });

  it("distinguishes connected market data from enabled order execution", () => {
    publishGatewayStatus({
      trading_enabled: false,
      authenticated: true,
      connected: true,
      accounts: [{ id: "U1", kind: "live" }],
      default_account: "U1",
    });
    expect(gatewayConnected()).toBe(true);
    expect(document.getElementById("gateway-indicator")!.textContent).toBe("IBKR: data");
    expect(document.getElementById("gateway-indicator")!.title).toContain("execution is disabled");
  });

  it("deduplicates simultaneous status requests", async () => {
    let resolve!: (value: {
      authenticated: boolean; connected: boolean;
    }) => void;
    apiMock.mockReturnValue(new Promise((done) => { resolve = done; }));
    const first = refreshGatewayStatus();
    const second = refreshGatewayStatus();
    expect(apiMock).toHaveBeenCalledTimes(1);
    resolve({ authenticated: true, connected: true });
    await expect(first).resolves.toMatchObject({ authenticated: true });
    await expect(second).resolves.toMatchObject({ authenticated: true });
  });

  it("reconnects a read-only data session while trading remains disabled", async () => {
    apiMock.mockResolvedValue({
      trading_enabled: false, authenticated: true, connected: true,
    });
    await reconnectGateway();
    expect(apiMock).toHaveBeenCalledWith(
      "/api/trade/reconnect", "POST", null,
      { timeoutMs: 30_000, reportError: false },
    );
    expect(gatewayConnected()).toBe(true);
  });

  it("keeps an authenticated data-only session alive", async () => {
    publishGatewayStatus({
      trading_enabled: false, authenticated: true, connected: true,
    });
    apiMock.mockResolvedValue({
      trading_enabled: false, authenticated: true, connected: true,
    });
    await tickleGateway();
    expect(apiMock).toHaveBeenCalledWith(
      "/api/trade/tickle", "GET", null,
      { timeoutMs: 10_000, reportError: false },
    );
  });
});
