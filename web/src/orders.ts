// Orders is the execution index: one place to see durable trade intent, the
// exact local queue, and live broker orders. It deliberately does not merge
// those stores into fake lifecycle records -- submitted execution-plan items
// cannot be called working/filled until broker-order correlation exists.
import { $, api, esc, fmtCZK, isStaleToken, nextToken, sensitive } from "./core";
import {
  gatewayConnected, gatewayUnavailableReason, getGatewayStatus,
  refreshGatewayStatus, subscribeGatewayStatus,
} from "./gateway";
import { pushNav, setActiveView } from "./shell";
import type {
  ExecutionPlanItem, ExecutionPlanState, QueuedTradeLeg, TradeLeg, TradeQueueState,
} from "./api-types";

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
}

export type WorkingState = "loading" | "ready" | "offline" | "error";

export interface OrdersDashboardData {
  plan: ExecutionPlanState;
  queue: TradeQueueState;
  working: LiveOrderSummary[];
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

const TERMINAL_STATUS = /^(filled|cancelled|canceled|expired|rejected|apicancelled)$/i;

export function isTerminalOrder(order: LiveOrderSummary): boolean {
  return TERMINAL_STATUS.test(String(order.status || order.order_status || "").trim());
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
    working: data.working.filter((order) => !isTerminalOrder(order)).length,
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
  return `<section class="orders-card">` +
    `<div class="orders-card-head"><div><span class="orders-step">Intent</span><h3>Planned trades</h3></div>` +
    `<span class="orders-count">${counts.planned}</span></div>` +
    `<p class="orders-card-copy">Trades you selected or deferred, before they become exact broker-ready legs.</p>` +
    body + `<div class="orders-card-actions">${suggestion}${action("rebalance", "Build exact orders →", "", "primary")}</div>` +
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
  const body = legs.length
    ? `<div class="orders-list">${legs.slice(0, 8).map((leg) =>
        `<div class="orders-row${leg.included ? "" : " orders-row-excluded"}">` +
          `<div class="orders-symbol"><strong>${esc(leg.symbol)}</strong>` +
          `<small>${esc(leg.route || leg.type || "stock")}</small></div>` +
          `<div class="orders-intent">${legDescription(leg)}</div>` +
          `<span class="chip ${leg.included ? (queue.reviewed ? "good" : "warn") : "muted"}">` +
          `${leg.included ? (queue.reviewed ? "approved" : "needs review") : "excluded"}</span></div>`).join("")}</div>`
    : `<div class="orders-empty">The local order queue is empty. Nothing here has been sent to IBKR.</div>`;
  const excluded = counts.excluded
    ? `<span class="orders-footnote">${counts.excluded} excluded leg${counts.excluded === 1 ? "" : "s"} retained for later</span>`
    : `<span class="orders-footnote">${queue.reviewed && counts.queued ? "Projection approved for this exact revision" : "Not sent to IBKR"}</span>`;
  const next = counts.queued
    ? queue.reviewed
      ? action("trade", "Open queue & preview →", "basket", "primary")
      : action("target-state", "Review projected portfolio →", "", "primary")
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
      return `<div class="orders-row"><div class="orders-symbol"><strong>${esc(symbol)}</strong>` +
        `<small>IBKR</small></div><div class="orders-intent">${workingDescription(order)}</div>` +
        `<span class="chip good">${esc(status)}</span></div>`;
    }).join("")}</div>`;
  }
  const note = data.workingState === "ready"
    ? `<span class="orders-footnote">Live from the Client Portal Gateway</span>`
    : `<span class="orders-footnote">Local plan and queue remain available while IBKR is offline</span>`;
  return `<section class="orders-card">` +
    `<div class="orders-card-head"><div><span class="orders-step">Broker</span><h3>Working at IBKR</h3></div>` +
    `<span class="orders-count">${data.workingState === "ready" ? counts.working : "—"}</span></div>` +
    `<p class="orders-card-copy">Orders accepted by IBKR. These are broker truth; “submitted” plan records are not treated as fills.</p>` +
    body + `<div class="orders-card-actions">${note}${action("trade", "Open working orders →", "orders", "primary")}</div>` +
    `</section>`;
}

export function ordersDashboardHtml(data: OrdersDashboardData): string {
  const counts = pipelineCounts(data);
  const inFlight = counts.planned + counts.queued + counts.working;
  return `<div class="orders-summary">` +
      `<div><span>In flight</span><strong>${inFlight}</strong><small>planned + queued + working</small></div>` +
      `<div><span>Planned</span><strong>${counts.planned}</strong><small>${counts.deferred} deferred</small></div>` +
      `<div><span>Queued locally</span><strong>${counts.queued}</strong><small>${data.queue.reviewed ? "projection approved" : counts.queued ? "review required" : "empty"}</small></div>` +
      `<div><span>Working at IBKR</span><strong>${data.workingState === "ready" ? counts.working : "—"}</strong>` +
      `<small>${data.workingState === "ready" ? "live broker state" : data.workingState}</small></div>` +
    `</div>` +
    `<div class="orders-grid">${plannedHtml(data.plan, counts)}${queueHtml(data.queue, counts)}${workingHtml(data, counts)}</div>` +
    `<div class="orders-history"><div><strong>Completed orders belong in Trade history</strong>` +
      `<span>Fills are reconstructed from the read-only Flex ledger, separate from local intent and broker working state.</span></div>` +
      `${action("history", "Open Trade history →")}</div>`;
}

const _badge = { planned: 0, queued: 0, working: null as number | null };
let _badgeLiveInFlight: Promise<void> | null = null;

function paintOrdersBadge(): void {
  const badge = $("#orders-count");
  const count = _badge.planned + _badge.queued + (_badge.working || 0);
  const breakdown = [
    `${_badge.planned} planned`,
    `${_badge.queued} queued`,
    _badge.working == null ? "IBKR count unavailable" : `${_badge.working} working`,
  ];
  if (badge) {
    badge.textContent = String(count);
    badge.hidden = count === 0;
    badge.title = breakdown.join(" · ");
  }
  // Today paints independently from the gateway monitor. If it is mounted,
  // enrich its local planned+queued count with the asynchronously loaded IBKR
  // count without making the initial cockpit request wait on the gateway.
  const pulse = $("#today-orders-inflight");
  if (pulse) {
    const value = pulse.querySelector("strong");
    const note = pulse.querySelector("small");
    if (value) value.textContent = String(count);
    if (note) note.textContent = breakdown.join(" · ");
    pulse.classList.toggle("today-pulse-warn", count > 0);
  }
}

export function setOrdersBadgeLocal(planned: number, queued: number): void {
  _badge.planned = Math.max(0, planned || 0);
  _badge.queued = Math.max(0, queued || 0);
  paintOrdersBadge();
}

function setOrdersBadgeWorking(orders: LiveOrderSummary[] | null): void {
  _badge.working = orders == null ? null : orders.filter((order) => !isTerminalOrder(order)).length;
  paintOrdersBadge();
}

async function refreshLiveBadge(): Promise<void> {
  if (_badgeLiveInFlight) return _badgeLiveInFlight;
  _badgeLiveInFlight = (async () => {
    const status = getGatewayStatus();
    if (!gatewayConnected(status)) {
      setOrdersBadgeWorking(null);
      return;
    }
    try {
      const data = await api<{ orders?: LiveOrderSummary[] }>(
        "/api/trade/orders", "GET", null, { timeoutMs: 20_000, reportError: false },
      );
      setOrdersBadgeWorking(data.orders || []);
    } catch {
      setOrdersBadgeWorking(null);
    }
  })().finally(() => { _badgeLiveInFlight = null; });
  return _badgeLiveInFlight;
}

export async function refreshOrdersBadge(): Promise<void> {
  try {
    const overview = await api<{
      execution_plan?: { planned?: number };
      staged_basket?: { count?: number };
    }>("/api/overview", "GET", null, { reportError: false });
    setOrdersBadgeLocal(
      overview.execution_plan?.planned || 0,
      overview.staged_basket?.count || 0,
    );
  } catch {
    // Navigation remains usable without a badge.
  }
  await refreshLiveBadge();
}

async function loadLiveOrders(): Promise<{
  orders: LiveOrderSummary[];
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
      state: "offline",
      message: gatewayUnavailableReason(status) || "IBKR Gateway is offline.",
    };
  }
  try {
    const data = await api<{ orders?: LiveOrderSummary[] }>(
      "/api/trade/orders", "GET", null, { timeoutMs: 20_000, reportError: false },
    );
    return { orders: data.orders || [], state: "ready" };
  } catch (error) {
    return { orders: [], state: "error", message: `Could not read IBKR working orders: ${(error as Error).message}` };
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
    plan, queue, working: [], workingState: "loading",
  };
  body.innerHTML = ordersDashboardHtml(initial);
  const localCounts = pipelineCounts(initial);
  setOrdersBadgeLocal(localCounts.planned, localCounts.queued);

  const live = await loadLiveOrders();
  if (isStaleToken("orders", token)) return;
  const complete: OrdersDashboardData = {
    plan, queue, working: live.orders, workingState: live.state, workingMessage: live.message,
  };
  body.innerHTML = ordersDashboardHtml(complete);
  setOrdersBadgeWorking(live.state === "ready" ? live.orders : null);
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
  document.addEventListener("orders-local-summary", (event) => {
    const detail = (event as CustomEvent<{ planned?: number; queued?: number }>).detail || {};
    setOrdersBadgeLocal(detail.planned || 0, detail.queued || 0);
  });
  subscribeGatewayStatus((status) => {
    if (!gatewayConnected(status)) setOrdersBadgeWorking(null);
    else void refreshLiveBadge();
  });
  void refreshOrdersBadge();
}
