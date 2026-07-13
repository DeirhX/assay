// One app-wide authority for the live IBKR Client Portal Gateway session.
// Views subscribe to this store instead of independently polling auth/status.
import { $, api, esc } from "./core";
import type { GatewayStatus } from "./api-types";

const STATUS_POLL_MS = 45_000;
const KEEPALIVE_MS = 60_000;

let current: GatewayStatus | null = null;
let lastError: string | null = null;
let checking = false;
let connectionAction: "checking" | "reconnecting" | null = null;
let actionError: string | null = null;
let panelOpen = false;
let statusInFlight: Promise<GatewayStatus> | null = null;
let pollTimer: ReturnType<typeof setInterval> | null = null;
let keepaliveTimer: ReturnType<typeof setInterval> | null = null;
let started = false;
const listeners = new Set<(status: GatewayStatus | null) => void>();

export function gatewayLoginUrl(base: string | null | undefined): string {
  const raw = String(base || "").replace(/\/v1\/api\/?$/, "") || "https://localhost:5000";
  try {
    const url = new URL(raw);
    // Keep 127.0.0.1 for server-side API calls, but use IBKR's documented
    // localhost host for browser login so certificate and cookies line up.
    if (url.hostname === "127.0.0.1" || url.hostname === "[::1]") {
      url.hostname = "localhost";
    }
    return url.origin;
  } catch {
    return raw;
  }
}

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
  indicator.setAttribute("aria-expanded", panelOpen ? "true" : "false");
  if (connectionAction) {
    indicator.classList.add("checking");
    indicator.textContent = connectionAction === "reconnecting"
      ? "IBKR: reconnecting…"
      : "IBKR: checking…";
    indicator.title = connectionAction === "reconnecting"
      ? "Attempting to reconnect the Client Portal Gateway"
      : "Checking the Client Portal Gateway session";
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

function connectionDetail(status: GatewayStatus): string {
  const id = status.default_account || status.accounts?.[0]?.id || "";
  const account = status.accounts?.find((candidate) => candidate.id === id);
  const accountText = id
    ? `${account?.kind === "paper" ? "Paper" : account?.kind === "live" ? "Live" : "IBKR"} account ${id}`
    : "IBKR session";
  return status.trading_enabled
    ? `${accountText} is ready for market data and order execution.`
    : `${accountText} is connected for market data; order execution is disabled.`;
}

function renderGatewayPanel(): void {
  const panel = $("#gateway-panel");
  const content = $("#gateway-panel-content");
  if (!panel || !content) return;
  panel.hidden = !panelOpen;
  if (!panelOpen) return;

  if (connectionAction) {
    const verb = connectionAction === "reconnecting" ? "Reconnecting" : "Checking connection";
    content.innerHTML =
      `<div class="task-item gateway-task-running">` +
        `<div class="task-item-head"><span class="task-kind">IBKR gateway</span>` +
          `<span class="task-state task-state-running">running</span></div>` +
        `<div class="task-subject">${verb}</div>` +
        `<div class="task-detail"><span class="spinner"></span> ` +
          `${connectionAction === "reconnecting"
            ? "Re-establishing the Client Portal session…"
            : "Confirming the existing Client Portal session…"}</div>` +
      `</div>`;
    return;
  }

  if (gatewayConnected()) {
    content.innerHTML =
      `<div class="task-item task-done">` +
        `<div class="task-item-head"><span class="task-kind">IBKR gateway</span>` +
          `<span class="task-state task-state-done">connected</span></div>` +
        `<div class="task-subject">Connection active</div>` +
        `<div class="task-detail">${connectionDetail(current!)}</div>` +
      `</div>`;
    return;
  }

  const message = current?.reconnect_error || actionError || lastError ||
    "The Client Portal Gateway is offline or not logged in.";
  const loginUrl = gatewayLoginUrl(current?.gateway_base);
  content.innerHTML =
    `<div class="task-item task-error">` +
      `<div class="task-item-head"><span class="task-kind">IBKR gateway</span>` +
        `<span class="task-state task-state-error">offline</span></div>` +
      `<div class="task-subject">Connection unavailable</div>` +
      `<div class="task-detail">${esc(message)}</div>` +
    `</div>` +
    `<div class="gateway-panel-hint">Log in at ` +
      `<a href="${esc(loginUrl)}" target="_blank" rel="noopener">${esc(loginUrl)}</a>, ` +
      `then click the IBKR status again to retry.</div>`;
}

function closeSiblingPanels(): void {
  ["task-panel", "error-panel"].forEach((id) => {
    const panel = document.getElementById(id);
    if (panel) panel.hidden = true;
  });
  document.getElementById("task-indicator")?.setAttribute("aria-expanded", "false");
  document.getElementById("error-indicator")?.setAttribute("aria-expanded", "false");
}

export function toggleGatewayPanel(force?: boolean): void {
  panelOpen = force != null ? force : !panelOpen;
  if (panelOpen) closeSiblingPanels();
  renderGatewayIndicator();
  renderGatewayPanel();
}

export function publishGatewayStatus(status: GatewayStatus): GatewayStatus {
  current = { ...(current || {}), ...status };
  lastError = null;
  checking = false;
  if (gatewayConnected(current)) actionError = null;
  renderGatewayIndicator();
  renderGatewayPanel();
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
    renderGatewayPanel();
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
  connectionAction = "reconnecting";
  actionError = null;
  renderGatewayIndicator();
  renderGatewayPanel();
  try {
    const status = await api<GatewayStatus>(
      "/api/trade/reconnect", "POST", null, { timeoutMs: 30_000, reportError: false },
    );
    const published = publishGatewayStatus(status);
    if (!gatewayConnected(published)) {
      actionError = published.reconnect_error || "IBKR remained offline after the reconnect attempt.";
    }
    return published;
  } catch (error) {
    actionError = error instanceof Error ? error.message : String(error || "Reconnect failed");
    return refreshGatewayStatus().catch(() => current);
  } finally {
    connectionAction = null;
    renderGatewayIndicator();
    renderGatewayPanel();
  }
}

async function indicatorClick(): Promise<void> {
  const indicator = $("#gateway-indicator") as HTMLButtonElement | null;
  toggleGatewayPanel(true);
  if (connectionAction) return;
  if (indicator) indicator.disabled = true;
  try {
    if (gatewayConnected()) {
      connectionAction = "checking";
      actionError = null;
      renderGatewayIndicator();
      renderGatewayPanel();
      const status = await tickleGateway();
      if (!gatewayConnected(status)) {
        actionError = status?.reconnect_error || "IBKR did not confirm an active session.";
      }
    } else {
      await reconnectGateway();
    }
  } catch (error) {
    actionError = error instanceof Error ? error.message : String(error || "Connection check failed");
  } finally {
    connectionAction = null;
    if (indicator) indicator.disabled = false;
    renderGatewayIndicator();
    renderGatewayPanel();
  }
}

export function startGatewayMonitor(): void {
  if (started) return;
  started = true;
  $("#gateway-indicator")?.addEventListener("click", () => void indicatorClick());
  $("#gateway-close")?.addEventListener("click", () => toggleGatewayPanel(false));
  renderGatewayIndicator();
  renderGatewayPanel();
  if (!current) void refreshGatewayStatus().catch(() => undefined);
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
}

// Test-only reset kept explicit so module-level polling state cannot leak between
// isolated UI cases. Production code only ever starts the monitor once.
export function resetGatewayState(): void {
  stopGatewayMonitor();
  current = null;
  lastError = null;
  checking = false;
  connectionAction = null;
  actionError = null;
  panelOpen = false;
  statusInFlight = null;
  listeners.clear();
  renderGatewayIndicator();
  renderGatewayPanel();
}
