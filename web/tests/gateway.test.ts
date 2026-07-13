import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../src/core", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/core")>()),
  api: vi.fn(),
}));

import { api } from "../src/core";
import {
  gatewayConnected, gatewayLoginUrl, gatewayUnavailableReason, publishGatewayStatus,
  reconnectGateway, refreshGatewayStatus, resetGatewayState, startGatewayMonitor,
  tickleGateway,
} from "../src/gateway";

const apiMock = vi.mocked(api);

describe("central IBKR gateway state", () => {
  beforeEach(() => {
    document.body.innerHTML =
      '<button id="gateway-indicator" class="gateway-indicator checking" aria-expanded="false"></button>' +
      '<div id="gateway-panel" hidden><button id="gateway-close"></button>' +
        '<div id="gateway-panel-content"></div></div>' +
      '<div id="task-panel" hidden></div><button id="task-indicator"></button>' +
      '<div id="error-panel" hidden></div><button id="error-indicator"></button>';
    apiMock.mockReset();
    resetGatewayState();
  });

  afterEach(() => resetGatewayState());

  it("renders disconnected state explicitly", () => {
    publishGatewayStatus({
      trading_enabled: false, authenticated: false, connected: false,
    });
    const indicator = document.getElementById("gateway-indicator")!;
    expect(indicator.textContent).toBe("IBKR: offline");
    expect(indicator.classList.contains("offline")).toBe(true);
    expect(gatewayUnavailableReason()).toContain("not connected");
  });

  it("always uses localhost for browser login", () => {
    expect(gatewayLoginUrl("https://localhost:5000/v1/api")).toBe("https://localhost:5000");
    expect(gatewayLoginUrl("https://127.0.0.1:5000/v1/api")).toBe("https://localhost:5000");
    expect(gatewayLoginUrl("https://[::1]:5000/v1/api")).toBe("https://localhost:5000");
    expect(gatewayLoginUrl(null)).toBe("https://localhost:5000");
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

  it("keeps the current page open and reports reconnect progress and failure in its panel", async () => {
    publishGatewayStatus({
      trading_enabled: false, authenticated: false, connected: false,
    });
    let resolve!: (value: {
      trading_enabled: boolean; authenticated: boolean; connected: boolean;
      reconnect_error: string;
    }) => void;
    apiMock.mockReturnValue(new Promise((done) => { resolve = done; }));
    startGatewayMonitor();
    const urlBefore = window.location.href;

    (document.getElementById("gateway-indicator") as HTMLButtonElement).click();

    expect(window.location.href).toBe(urlBefore);
    expect(document.getElementById("gateway-panel")!.hidden).toBe(false);
    expect(document.getElementById("gateway-panel-content")!.textContent).toContain("Reconnecting");
    resolve({
      trading_enabled: false,
      authenticated: false,
      connected: false,
      reconnect_error: "Saved login expired",
    });
    await vi.waitFor(() => {
      expect((document.getElementById("gateway-indicator") as HTMLButtonElement).disabled).toBe(false);
    });
    expect(document.getElementById("gateway-panel-content")!.textContent).toContain("Saved login expired");
    expect(document.querySelector<HTMLAnchorElement>("#gateway-panel-content a")!.href)
      .toBe("https://localhost:5000/");
    expect(window.location.href).toBe(urlBefore);
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
