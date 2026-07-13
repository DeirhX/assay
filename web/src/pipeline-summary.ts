// One authority for execution-pipeline counts and invalidation. Mutating views
// publish a single event; this module refreshes the persistent header/Today
// chrome, while the flowbar subscribes to the same event for its own cache.
import { $, api } from "./core";
import {
  gatewayConnected, getGatewayStatus, subscribeGatewayStatus,
} from "./gateway";

export interface LiveOrderSummary {
  orderId?: string | number;
  order_id?: string | number;
  ticker?: string;
  symbol?: string;
  side?: string;
  totalSize?: number | string;
  quantity?: number | string;
  remainingQuantity?: number | string;
  filledQuantity?: number | string;
  status?: string;
  order_status?: string;
  orderType?: string;
  order_type?: string;
  price?: number | string | null;
  tif?: string;
  timeInForce?: string;
  orderDesc?: string;
  cOID?: string;
}

export interface BrokerCorrelationRecord {
  key: string;
  leg_id: string;
  execution_item_ids: string[];
  symbol: string;
  side: string;
  quantity: number;
  cOID: string;
  broker_order_id: string;
  broker_status: string;
  filled_qty: number;
  total_qty: number;
  terminal: boolean;
  last_event_kind?: "partial" | "filled" | "cancelled" | "rejected" | null;
  last_event_at?: string | null;
  placed_at?: string | null;
  updated_at?: string | null;
}

export interface BrokerCorrelationSummary {
  active: number;
  partial: number;
  recent_filled: number;
  recent_failed: number;
  updated_at?: string | null;
}

export interface BrokerCorrelationState {
  records: BrokerCorrelationRecord[];
  summary: BrokerCorrelationSummary;
  updated_at?: string | null;
}

export interface PipelineChromeCounts {
  planned: number;
  queued: number;
  working: number | null;
}

export interface PipelineChangeDetail {
  source?: "queue" | "plan" | "review" | "overview" | "broker";
  planned?: number;
  queued?: number;
}

export const PIPELINE_CHANGED_EVENT = "assay:pipeline-changed";
const TERMINAL_STATUS = /^(filled|cancelled|canceled|expired|rejected|inactive|apicancelled)$/i;
const counts: PipelineChromeCounts = { planned: 0, queued: 0, working: null };
let localRefresh: Promise<void> | null = null;
let workingRefresh: Promise<void> | null = null;
let wired = false;
let correlationStamp: string | null = null;

export function isTerminalOrder(order: LiveOrderSummary): boolean {
  return TERMINAL_STATUS.test(String(order.status || order.order_status || "").trim());
}

export function countWorkingOrders(
  orders: LiveOrderSummary[] | null | undefined,
): number {
  return (orders || []).filter((order) => !isTerminalOrder(order)).length;
}

export function queueWorkflowView(
  basket: { count?: number; reviewed?: boolean; valid?: boolean } | null | undefined,
): "trade" | "target-state" | null {
  if (!basket?.count) return null;
  return basket.reviewed && basket.valid !== false ? "trade" : "target-state";
}

export function paintPipelineChrome(): void {
  const total = counts.planned + counts.queued + (counts.working || 0);
  const breakdown = [
    `${counts.planned} planned`,
    `${counts.queued} queued`,
    counts.working == null ? "IBKR count unavailable" : `${counts.working} working`,
  ];
  const badge = $("#orders-count");
  if (badge) {
    badge.textContent = String(total);
    badge.hidden = total === 0;
    badge.title = breakdown.join(" · ");
  }
  const pulse = $("#today-orders-inflight");
  if (pulse) {
    const value = pulse.querySelector("strong");
    const note = pulse.querySelector("small");
    if (value) value.textContent = String(total);
    if (note) note.textContent = breakdown.join(" · ");
    pulse.classList.toggle("today-pulse-warn", total > 0);
  }
}

export function updatePipelineChrome(next: Partial<PipelineChromeCounts>): void {
  if (next.planned != null) counts.planned = Math.max(0, next.planned || 0);
  if (next.queued != null) counts.queued = Math.max(0, next.queued || 0);
  if ("working" in next) counts.working = next.working == null
    ? null
    : Math.max(0, next.working);
  paintPipelineChrome();
}

export function publishPipelineChanged(detail: PipelineChangeDetail = {}): void {
  window.dispatchEvent(new CustomEvent<PipelineChangeDetail>(
    PIPELINE_CHANGED_EVENT,
    { detail },
  ));
}

export function subscribePipelineChanged(
  handler: (detail: PipelineChangeDetail) => void,
): () => void {
  const listener = (event: Event) =>
    handler((event as CustomEvent<PipelineChangeDetail>).detail || {});
  window.addEventListener(PIPELINE_CHANGED_EVENT, listener);
  return () => window.removeEventListener(PIPELINE_CHANGED_EVENT, listener);
}

export function observeBrokerState(
  correlations: BrokerCorrelationState | null | undefined,
  notify = true,
): void {
  const stamp = correlations?.updated_at || correlations?.summary?.updated_at || null;
  if (!stamp || stamp === correlationStamp) return;
  const previous = correlationStamp;
  correlationStamp = stamp;
  if (notify && previous) publishPipelineChanged({ source: "broker" });
}

export async function refreshPipelineChrome(
  { local = true, working = true }: { local?: boolean; working?: boolean } = {},
): Promise<void> {
  const requests: Promise<void>[] = [];
  if (local) {
    if (!localRefresh) {
      localRefresh = (async () => {
        try {
          const overview = await api<{
            execution_plan?: { planned?: number };
            staged_basket?: { count?: number };
          }>("/api/overview", "GET", null, { reportError: false });
          updatePipelineChrome({
            planned: overview.execution_plan?.planned || 0,
            queued: overview.staged_basket?.count || 0,
          });
        } catch {
          // Persistent navigation remains usable without counts.
        }
      })().finally(() => { localRefresh = null; });
    }
    requests.push(localRefresh);
  }
  if (working) {
    if (!gatewayConnected(getGatewayStatus())) {
      updatePipelineChrome({ working: null });
    } else {
      if (!workingRefresh) {
        workingRefresh = (async () => {
          try {
            const data = await api<{
              orders?: LiveOrderSummary[];
              correlations?: BrokerCorrelationState;
            }>(
              "/api/trade/orders", "GET", null,
              { timeoutMs: 20_000, reportError: false },
            );
            updatePipelineChrome({ working: countWorkingOrders(data.orders) });
            observeBrokerState(data.correlations);
          } catch {
            updatePipelineChrome({ working: null });
          }
        })().finally(() => { workingRefresh = null; });
      }
      requests.push(workingRefresh);
    }
  }
  await Promise.all(requests);
}

export function initPipelineSummary(): void {
  if (wired) return;
  wired = true;
  subscribePipelineChanged((detail) => {
    if (detail.planned != null || detail.queued != null) {
      updatePipelineChrome({ planned: detail.planned, queued: detail.queued });
    }
    void refreshPipelineChrome({
      local: detail.planned == null && detail.queued == null,
      working: detail.source === "broker",
    });
  });
  subscribeGatewayStatus((status) => {
    if (!gatewayConnected(status)) updatePipelineChrome({ working: null });
    else void refreshPipelineChrome({ local: false, working: true });
  });
  void refreshPipelineChrome({ local: true, working: false });
  window.setInterval(() => {
    if (gatewayConnected(getGatewayStatus())) {
      void refreshPipelineChrome({ local: false, working: true });
    }
  }, 30_000);
}
