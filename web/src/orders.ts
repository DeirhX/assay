// Orders is the execution index: one place to see durable trade intent, the
// exact local queue, and live broker orders. It deliberately does not merge
// those stores into fake lifecycle records -- submitted execution-plan items
// cannot be called working/filled until broker-order correlation exists.
import { $, api, esc, fmtCZK, isStaleToken, nextToken, sensitive } from "./core";
import {
  gatewayConnected, gatewayUnavailableReason, getGatewayStatus,
  refreshGatewayStatus,
} from "./gateway";
import {
  countWorkingOrders, isTerminalOrder, observeBrokerState, queueWorkflowView,
  subscribePipelineChanged, updatePipelineChrome,
} from "./pipeline-summary";
import type {
  BrokerCorrelationRecord, BrokerCorrelationState, LiveOrderSummary,
} from "./pipeline-summary";
import { pushNav, setActiveView } from "./shell";
import type {
  ExecutionPlanItem, ExecutionPlanState, QueuedTradeLeg, TradeLeg, TradeQueueState,
} from "./api-types";

export type WorkingState = "loading" | "ready" | "offline" | "error";

export interface OrdersDashboardData {
  plan: ExecutionPlanState;
  queue: TradeQueueState;
  working: LiveOrderSummary[];
  correlations: BrokerCorrelationRecord[];
  workingState: WorkingState;
  workingMessage?: string;
}

export interface PipelineCounts {
  planned: number;
  selected: number;
  deferred: number;
  suggested: number;
  queued: number;
  excluded: number;
  working: number;
}

function plannedItems(plan: ExecutionPlanState): ExecutionPlanItem[] {
  return (plan.items || [])
    .filter((item) => item.status === "selected" || item.status === "deferred")
    .sort((a, b) => {
      if (a.status !== b.status) return a.status === "selected" ? -1 : 1;
      return String(b.updated_at || "").localeCompare(String(a.updated_at || ""));
    });
}

function queueLegs(queue: TradeQueueState): QueuedTradeLeg[] {
  return queue.queue_trades?.length
    ? queue.queue_trades
    : (queue.trades || []).map((trade) => ({ ...trade, included: true }));
}

export function pipelineCounts(data: OrdersDashboardData): PipelineCounts {
  const planned = plannedItems(data.plan);
  const legs = queueLegs(data.queue);
  return {
    planned: planned.length,
    selected: planned.filter((item) => item.status === "selected").length,
    deferred: planned.filter((item) => item.status === "deferred").length,
    suggested: (data.plan.items || []).filter((item) => item.status === "suggested").length,
    queued: legs.filter((leg) => leg.included).length,
    excluded: legs.filter((leg) => !leg.included).length,
    working: countWorkingOrders(data.working),
  };
}

const action = (view: string, label: string, tab = "", cls = "ghost") =>
  `<button class="${cls}" type="button" data-orders-view="${esc(view)}"` +
  (tab ? ` data-orders-tab="${esc(tab)}"` : "") + `>${label}</button>`;

function itemAmount(item: ExecutionPlanItem): string {
  const verb = item.direction === "reduce" ? "Reduce" : "Increase";
  return `${verb} ${sensitive(`${fmtCZK(Math.abs(item.delta_czk || 0))} CZK`, "planned trade size")}`;
}

function plannedHtml(plan: ExecutionPlanState, counts: PipelineCounts): string {
  const rows = plannedItems(plan);
  const body = rows.length
    ? `<div class="orders-list">${rows.slice(0, 8).map((item) => {
        const deferred = item.status === "deferred";
        const when = deferred && item.defer_until ? ` until ${esc(item.defer_until)}` : "";
        return `<div class="orders-row">` +
          `<div class="orders-symbol"><strong>${esc(item.symbol)}</strong>` +
          `<small>${esc(item.source || "manual")}</small></div>` +
          `<div class="orders-intent">${itemAmount(item)}</div>` +
          `<span class="chip ${deferred ? "muted" : "warn"}">${deferred ? `deferred${when}` : "selected"}</span>` +
          `</div>`;
      }).join("")}</div>`
    : `<div class="orders-empty">No deliberately planned trades. Generated suggestions remain advice until you select them.</div>`;
  const suggestion = counts.suggested
    ? `<span class="orders-footnote">${counts.suggested} generated suggestion${counts.suggested === 1 ? "" : "s"} awaiting a decision</span>`
    : `<span class="orders-footnote">No unreviewed suggestions</span>`;
  const next = plan.stale
    ? action("rebalance", "Recheck stale amounts →", "", "primary")
    : action("rebalance", "Build exact orders →", "", "primary");
  return `<section class="orders-card">` +
    `<div class="orders-card-head"><div><span class="orders-step">Intent</span><h3>Planned trades</h3></div>` +
    `<span class="orders-count">${counts.planned}</span></div>` +
    `<p class="orders-card-copy">Trades you selected or deferred, before they become exact broker-ready legs.</p>` +
    body + `<div class="orders-card-actions">${suggestion}${next}</div>` +
    (plan.stale ? `<div class="orders-warning">The execution plan is stale against the current portfolio plan. Recheck amounts before queuing.</div>` : "") +
    `</section>`;
}

function legDescription(leg: TradeLeg): string {
  if (leg.type === "covered_call") {
    return `Sell ${leg.contracts} covered call${leg.contracts === 1 ? "" : "s"} · ${leg.expiry} · strike ${leg.strike}`;
  }
  if (leg.type === "cash_secured_put") {
    return `Sell ${leg.contracts} cash-secured put${leg.contracts === 1 ? "" : "s"} · ${leg.expiry} · strike ${leg.strike}`;
  }
  const delta = leg.delta_czk || 0;
  return `${delta < 0 ? "Sell" : "Buy"} ${sensitive(`${fmtCZK(Math.abs(delta))} CZK`, "queued trade size")}`;
}

function queueHtml(queue: TradeQueueState, counts: PipelineCounts): string {
  const legs = queueLegs(queue);
  const nextView = queueWorkflowView({ ...queue, count: counts.queued });
  const ready = nextView === "trade";
  const body = legs.length
    ? `<div class="orders-list">${legs.slice(0, 8).map((leg) =>
        `<div class="orders-row${leg.included ? "" : " orders-row-excluded"}">` +
          `<div class="orders-symbol"><strong>${esc(leg.symbol)}</strong>` +
          `<small>${esc(leg.route || leg.type || "stock")}</small></div>` +
          `<div class="orders-intent">${legDescription(leg)}</div>` +
          `<span class="chip ${leg.included ? (ready ? "good" : "warn") : "muted"}">` +
          `${leg.included ? (ready ? "approved" : "needs review") : "excluded"}</span></div>`).join("")}</div>`
    : `<div class="orders-empty">The local order queue is empty. Nothing here has been sent to IBKR.</div>`;
  const excluded = counts.excluded
    ? `<span class="orders-footnote">${counts.excluded} excluded leg${counts.excluded === 1 ? "" : "s"} retained for later</span>`
    : `<span class="orders-footnote">${ready ? "Projection approved for this exact revision" : "Not sent to IBKR"}</span>`;
  const next = nextView === "trade"
    ? action("trade", "Open queue & preview →", "basket", "primary")
    : nextView === "target-state"
      ? action("target-state", "Review projected portfolio →", "", "primary")
      : action("rebalance", "Build orders →", "", "primary");
  return `<section class="orders-card">` +
    `<div class="orders-card-head"><div><span class="orders-step">Local</span><h3>Order queue</h3></div>` +
    `<span class="orders-count">${counts.queued}</span></div>` +
    `<p class="orders-card-copy">Exact included legs saved locally. Projection approval is invalidated whenever this queue changes.</p>` +
    body + `<div class="orders-card-actions">${excluded}${next}</div>` +
    (queue.valid === false ? `<div class="orders-warning">The queue contains an invalid stock sell. Fix it before approval or preview.</div>` : "") +
    `</section>`;
}

function workingDescription(order: LiveOrderSummary): string {
  if (order.orderDesc) return esc(order.orderDesc);
  const side = String(order.side || "Order");
  const qty = order.remainingQuantity ?? order.quantity ?? order.totalSize ?? "";
  const type = order.orderType || order.order_type || "";
  const price = order.price == null || order.price === "" ? "" : ` @ ${order.price}`;
  return `${esc(side)} ${esc(String(qty))} ${esc(type)}${esc(price)}`.trim();
}

function workingHtml(data: OrdersDashboardData, counts: PipelineCounts): string {
  const working = data.working.filter((order) => !isTerminalOrder(order));
  const correlations = new Map(
    data.correlations.map((record) => [String(record.broker_order_id), record]),
  );
  let body: string;
  if (data.workingState === "loading") {
    body = `<div class="orders-empty"><span class="spinner"></span> Checking IBKR working orders…</div>`;
  } else if (data.workingState === "offline" || data.workingState === "error") {
    body = `<div class="orders-empty">${esc(data.workingMessage || "Working orders are unavailable.")}</div>`;
  } else if (!working.length) {
    body = `<div class="orders-empty">No working orders at IBKR right now.</div>`;
  } else {
    body = `<div class="orders-list">${working.slice(0, 8).map((order) => {
      const symbol = order.ticker || order.symbol || "—";
      const status = order.status || order.order_status || "working";
      const brokerId = String(order.orderId || order.order_id || "");
      const linked = correlations.get(brokerId);
      const linkNote = linked?.execution_item_ids?.length
        ? ` · ${linked.execution_item_ids.length} linked intent${linked.execution_item_ids.length === 1 ? "" : "s"}`
        : "";
      return `<div class="orders-row"><div class="orders-symbol"><strong>${esc(symbol)}</strong>` +
        `<small>IBKR${linkNote}</small></div><div class="orders-intent">${workingDescription(order)}</div>` +
        `<span class="chip good">${esc(status)}</span></div>`;
    }).join("")}</div>`;
  }
  const recent = data.correlations
    .filter((record) => record.last_event_kind)
    .slice(0, 4);
  const lifecycle = recent.length
    ? `<div class="orders-list">${recent.map((record) => {
        const kind = record.last_event_kind || record.broker_status || "updated";
        const qty = record.total_qty
          ? `${record.filled_qty}/${record.total_qty}`
          : String(record.filled_qty || "");
        return `<div class="orders-row"><div class="orders-symbol"><strong>${esc(record.symbol)}</strong>` +
          `<small>${esc(record.broker_order_id)}</small></div>` +
          `<div class="orders-intent">${esc(record.side)} ${esc(qty)} · linked to ` +
          `${record.execution_item_ids.length} intent${record.execution_item_ids.length === 1 ? "" : "s"}</div>` +
          `<span class="chip ${kind === "filled" ? "good" : kind === "partial" ? "warn" : "bad"}">${esc(kind)}</span></div>`;
      }).join("")}</div>`
    : "";
  const note = data.workingState === "ready"
    ? `<span class="orders-footnote">Live from the Client Portal Gateway</span>`
    : `<span class="orders-footnote">Local plan and queue remain available while IBKR is offline</span>`;
  return `<section class="orders-card">` +
    `<div class="orders-card-head"><div><span class="orders-step">Broker</span><h3>Working at IBKR</h3></div>` +
    `<span class="orders-count">${data.workingState === "ready" ? counts.working : "—"}</span></div>` +
    `<p class="orders-card-copy">Orders accepted by IBKR. These are broker truth; “submitted” plan records are not treated as fills.</p>` +
    body + lifecycle +
    `<div class="orders-card-actions">${note}${action("trade", "Open working orders →", "orders", "primary")}</div>` +
    `</section>`;
}

export function ordersDashboardHtml(data: OrdersDashboardData): string {
  const counts = pipelineCounts(data);
  const inFlight = counts.planned + counts.queued + counts.working;
  return `<div class="orders-summary">` +
      `<div><span>In flight</span><strong>${inFlight}</strong><small>planned + queued + working</small></div>` +
      `<div><span>Planned</span><strong>${counts.planned}</strong><small>${counts.deferred} deferred</small></div>` +
      `<div><span>Queued locally</span><strong>${counts.queued}</strong><small>${queueWorkflowView({ ...data.queue, count: counts.queued }) === "trade" ? "projection approved" : counts.queued ? "review required" : "empty"}</small></div>` +
      `<div><span>Working at IBKR</span><strong>${data.workingState === "ready" ? counts.working : "—"}</strong>` +
      `<small>${data.workingState === "ready" ? "live broker state" : data.workingState}</small></div>` +
    `</div>` +
    `<div class="orders-grid">${plannedHtml(data.plan, counts)}${queueHtml(data.queue, counts)}${workingHtml(data, counts)}</div>` +
    `<div class="orders-history"><div><strong>Completed orders belong in Trade history</strong>` +
      `<span>Fills are reconstructed from the read-only Flex ledger, separate from local intent and broker working state.</span></div>` +
      `${action("history", "Open Trade history →")}</div>`;
}

async function loadLiveOrders(): Promise<{
  orders: LiveOrderSummary[];
  correlations: BrokerCorrelationRecord[];
  state: WorkingState;
  message?: string;
}> {
  let status = getGatewayStatus();
  if (!status) {
    try { status = await refreshGatewayStatus(); } catch { /* rendered as offline */ }
  }
  if (!gatewayConnected(status)) {
    return {
      orders: [],
      correlations: [],
      state: "offline",
      message: gatewayUnavailableReason(status) || "IBKR Gateway is offline.",
    };
  }
  try {
    const data = await api<{
      orders?: LiveOrderSummary[];
      correlations?: BrokerCorrelationState;
    }>(
      "/api/trade/orders", "GET", null, { timeoutMs: 20_000, reportError: false },
    );
    observeBrokerState(data.correlations, false);
    return {
      orders: data.orders || [],
      correlations: data.correlations?.records || [],
      state: "ready",
    };
  } catch (error) {
    return {
      orders: [], correlations: [], state: "error",
      message: `Could not read IBKR working orders: ${(error as Error).message}`,
    };
  }
}

export async function loadOrders(): Promise<void> {
  const body = $("#orders-body");
  const status = $("#orders-status");
  if (!body) return;
  const token = nextToken("orders");
  body.innerHTML = `<div class="status"><span class="spinner"></span> Loading local order pipeline…</div>`;
  if (status) { status.textContent = ""; status.classList.remove("err"); }
  let plan: ExecutionPlanState;
  let queue: TradeQueueState;
  try {
    [plan, queue] = await Promise.all([
      api<ExecutionPlanState>("/api/execution-plan"),
      api<TradeQueueState>("/api/trade/basket"),
    ]);
  } catch (error) {
    if (isStaleToken("orders", token)) return;
    body.innerHTML = "";
    if (status) {
      status.textContent = `Could not load the local order pipeline: ${(error as Error).message}`;
      status.classList.add("err");
    }
    return;
  }
  if (isStaleToken("orders", token)) return;
  const initial: OrdersDashboardData = {
    plan, queue, working: [], correlations: [], workingState: "loading",
  };
  body.innerHTML = ordersDashboardHtml(initial);
  const localCounts = pipelineCounts(initial);
  updatePipelineChrome({
    planned: localCounts.planned,
    queued: localCounts.queued,
  });

  const live = await loadLiveOrders();
  if (isStaleToken("orders", token)) return;
  const complete: OrdersDashboardData = {
    plan, queue, working: live.orders, correlations: live.correlations,
    workingState: live.state, workingMessage: live.message,
  };
  body.innerHTML = ordersDashboardHtml(complete);
  updatePipelineChrome({
    working: live.state === "ready" ? countWorkingOrders(live.orders) : null,
  });
}

let _wired = false;
export function initOrders(): void {
  if (_wired) return;
  _wired = true;
  $("#orders-refresh")?.addEventListener("click", () => void loadOrders());
  $("#view-orders")?.addEventListener("click", (event) => {
    const target = (event.target as HTMLElement).closest<HTMLElement>("[data-orders-view]");
    const view = target?.dataset.ordersView;
    if (!view) return;
    pushNav({ view, tab: target.dataset.ordersTab || "" });
    setActiveView(view);
    window.scrollTo(0, 0);
  });
  subscribePipelineChanged((detail) => {
    if (detail.source === "broker" && $("#view-orders")?.classList.contains("active")) {
      void loadOrders();
    }
  });
}
