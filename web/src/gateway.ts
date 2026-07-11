// One app-wide authority for the live IBKR Client Portal Gateway session.
// Views subscribe to this store instead of independently polling auth/status.
import { $, api } from "./core";
import type { GatewayStatus } from "./api-types";

const STATUS_POLL_MS = 45_000;
const KEEPALIVE_MS = 60_000;

let current: GatewayStatus | null = null;
let lastError: string | null = null;
let checking = false;
let reconnecting = false;
let statusInFlight: Promise<GatewayStatus> | null = null;
let pollTimer: ReturnType<typeof setInterval> | null = null;
let keepaliveTimer: ReturnType<typeof setInterval> | null = null;
let started = false;
let openTrade: (() => void) | null = null;
const listeners = new Set<(status: GatewayStatus | null) => void>();

export function gatewayConnected(status: GatewayStatus | null = current): boolean {
  return !!status?.authenticated && status.connected !== false;
}

export function getGatewayStatus(): GatewayStatus | null {
  return current;
}

export function gatewayUnavailableReason(status: GatewayStatus | null = current): string | null {
  if (!status) {
    return lastError
      ? "IBKR Gateway status could not be reached."
      : "Checking the IBKR Gateway connection…";
  }
  if (status.competing) return "Another IBKR session is competing for this login.";
  if (!status.authenticated || status.connected === false) {
    return "IBKR Client Portal Gateway is not connected or logged in.";
  }
  return null;
}

function accountKind(status: GatewayStatus): string {
  const id = status.default_account || status.accounts?.[0]?.id;
  return status.accounts?.find((account) => account.id === id)?.kind || "";
}

export function renderGatewayIndicator(): void {
  const indicator = $("#gateway-indicator");
  if (!indicator) return;
  indicator.classList.remove("checking", "offline", "connected", "paper", "live", "competing");
  if (reconnecting) {
    indicator.classList.add("checking");
    indicator.textContent = "IBKR: reconnecting…";
    indicator.title = "Attempting to reconnect the Client Portal Gateway";
    return;
  }
  if (checking && !current) {
    indicator.classList.add("checking");
    indicator.textContent = "IBKR: checking…";
    indicator.title = "Checking the Client Portal Gateway";
    return;
  }
  if (!gatewayConnected()) {
    indicator.classList.add(current?.competing ? "competing" : "offline");
    indicator.textContent = current?.competing ? "IBKR: competing" : "IBKR: offline";
    indicator.title = gatewayUnavailableReason() || "IBKR Gateway is unavailable";
    return;
  }
  const kind = accountKind(current!);
  if (!current!.trading_enabled) {
    indicator.classList.add("connected");
    indicator.textContent = "IBKR: data";
    indicator.title = "Gateway connected for live data; order execution is disabled";
  } else if (kind === "live") {
    indicator.classList.add("live");
    indicator.textContent = "IBKR: LIVE";
    indicator.title = "Connected to a live IBKR account";
  } else if (kind === "paper") {
    indicator.classList.add("paper");
    indicator.textContent = "IBKR: paper";
    indicator.title = "Connected to an IBKR paper account";
  } else {
    indicator.classList.add("connected");
    indicator.textContent = "IBKR: connected";
    indicator.title = "Connected to the IBKR Client Portal Gateway";
  }
}

export function publishGatewayStatus(status: GatewayStatus): GatewayStatus {
  current = { ...(current || {}), ...status };
  lastError = null;
  checking = false;
  renderGatewayIndicator();
  listeners.forEach((listener) => listener(current));
  return current;
}

export function subscribeGatewayStatus(
  listener: (status: GatewayStatus | null) => void,
  emitCurrent = true,
): () => void {
  listeners.add(listener);
  if (emitCurrent) listener(current);
  return () => listeners.delete(listener);
}

export async function refreshGatewayStatus(): Promise<GatewayStatus> {
  if (statusInFlight) return statusInFlight;
  checking = true;
  renderGatewayIndicator();
  statusInFlight = api<GatewayStatus>(
    "/api/trade/status", "GET", null, { timeoutMs: 15_000, reportError: false },
  ).then(publishGatewayStatus).catch((error: Error) => {
    lastError = error.message;
    checking = false;
    renderGatewayIndicator();
    listeners.forEach((listener) => listener(current));
    throw error;
  }).finally(() => {
    statusInFlight = null;
  });
  return statusInFlight;
}

export async function tickleGateway(): Promise<GatewayStatus | null> {
  if (!gatewayConnected()) return refreshGatewayStatus().catch(() => current);
  try {
    const status = await api<GatewayStatus>(
      "/api/trade/tickle", "GET", null, { timeoutMs: 10_000, reportError: false },
    );
    return publishGatewayStatus(status);
  } catch {
    // A full status read distinguishes a transient tickle failure from logout.
    return refreshGatewayStatus().catch(() => current);
  }
}

export async function reconnectGateway(): Promise<GatewayStatus | null> {
  reconnecting = true;
  renderGatewayIndicator();
  try {
    const status = await api<GatewayStatus>(
      "/api/trade/reconnect", "POST", null, { timeoutMs: 30_000, reportError: false },
    );
    return publishGatewayStatus(status);
  } catch {
    return refreshGatewayStatus().catch(() => current);
  } finally {
    reconnecting = false;
    renderGatewayIndicator();
  }
}

async function indicatorClick(): Promise<void> {
  const indicator = $("#gateway-indicator") as HTMLButtonElement | null;
  if (indicator) indicator.disabled = true;
  try {
    if (gatewayConnected()) await tickleGateway();
    else await reconnectGateway();
  } finally {
    if (indicator) indicator.disabled = false;
    openTrade?.();
  }
}

export function startGatewayMonitor(onOpenTrade: () => void): void {
  openTrade = onOpenTrade;
  if (started) return;
  started = true;
  $("#gateway-indicator")?.addEventListener("click", () => void indicatorClick());
  renderGatewayIndicator();
  void refreshGatewayStatus().catch(() => undefined);
  pollTimer = setInterval(() => void refreshGatewayStatus().catch(() => undefined), STATUS_POLL_MS);
  keepaliveTimer = setInterval(() => void tickleGateway(), KEEPALIVE_MS);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) void tickleGateway();
  });
}

export function stopGatewayMonitor(): void {
  if (pollTimer) clearInterval(pollTimer);
  if (keepaliveTimer) clearInterval(keepaliveTimer);
  pollTimer = null;
  keepaliveTimer = null;
  started = false;
  openTrade = null;
}

// Test-only reset kept explicit so module-level polling state cannot leak between
// isolated UI cases. Production code only ever starts the monitor once.
export function resetGatewayState(): void {
  stopGatewayMonitor();
  current = null;
  lastError = null;
  checking = false;
  reconnecting = false;
  statusInFlight = null;
  listeners.clear();
  renderGatewayIndicator();
}
