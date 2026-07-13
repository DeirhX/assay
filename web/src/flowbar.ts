// The execution flow bar keeps one persistent, unambiguous pipeline —
//
//   current-book input → ① Build orders → ② Review impact → ③ Preview & place
//
// Target-model design lives under Plan. Current holdings are an input, not a
// task, and the projected outcome remains the explicit pre-trade safety gate.
import { $, api, esc } from "./core";
import { subscribeQueueChanged } from "./execution-queue";
import { gatewayConnected, refreshGatewayStatus } from "./gateway";
import { pushNav, setActiveView } from "./shell";
import type { GatewayStatus, HoldingsPayload } from "./api-types";

// ---- data ------------------------------------------------------------------
interface FlowOverview {
  snapshot?: { exists?: boolean; positions?: number; age_days?: number | null; stale?: boolean } | null;
  plan?: { rows?: number; out_of_band?: number; actionable?: number; gates_open?: number;
           cash?: { status?: string } | null } | null;
  draft?: { pending?: number } | null;
  staged_basket?: {
    count?: number; buys?: number; sells?: number;
    conditional_buys?: number; conditional_reductions?: number;
  } | null;
}
export interface FlowData {
  ov: FlowOverview | null;
  working: number | null;  // null = unknown (gateway off/unreachable), not zero
  gateway?: GatewayStatus | null;
}

let _cache: FlowData | null = null;
let _cacheAt = 0;
let _activeView = "";
let _activeGroup = "";
const TTL_MS = 15_000;

async function fetchFlowData(): Promise<FlowData> {
  if (_cache && Date.now() - _cacheAt < TTL_MS) return _cache;
  let ov: FlowOverview | null = null;
  let working: number | null = null;
  let gateway: GatewayStatus | null = null;
  try { ov = await api<FlowOverview>("/api/overview"); } catch { /* sections degrade */ }
  try {
    gateway = await refreshGatewayStatus();
    if (gatewayConnected(gateway)) {
      const res = await api<{ orders?: unknown[] }>("/api/trade/orders");
      working = Array.isArray(res.orders) ? res.orders.length : 0;
    }
  } catch { /* gateway down / orders read failed -> unknown */ }
  _cache = { ov, working, gateway };
  _cacheAt = Date.now();
  return _cache;
}

// Force the next render to refetch (e.g. after placing/cancelling an order).
export function invalidateFlowData(): void { _cacheAt = 0; }

// ---- pure builders (exported for tests) -------------------------------------
// Which execution stage owns a view. The contextual Exit tool remains part of
// order construction; Optimizer and pending model changes are no longer here.
export function stageForView(view: string): 1 | 2 | 3 {
  if (view === "target-state") return 2;
  if (view === "trade") return 3;
  return 1;  // rebalance / exit
}

const plural = (n: number, s: string) => `${n} ${s}${n === 1 ? "" : "s"}`;
const ago = (d: number | null | undefined) =>
  d == null ? "" : d === 0 ? "synced today" : d === 1 ? "synced yesterday" : `synced ${d}d ago`;

interface Stage { n: 1 | 2 | 3; label: string; sub: string; tone: "ok" | "warn" | "muted"; view: string; title: string }

export function flowStages(d: FlowData): Stage[] {
  const plan = d.ov?.plan;
  const basket = d.ov?.staged_basket || {};

  const actionable = plan?.actionable || 0;
  const staged = basket.count || 0;
  const buildBits = [];
  if (staged) buildBits.push(`${staged} queued`);
  if (actionable) buildBits.push(`${actionable} suggested`);
  if (plan?.gates_open) buildBits.push(`${plural(plan.gates_open, "gate")} triggered`);
  const s1: Stage = { n: 1, label: "Build orders", view: "rebalance",
    tone: staged || actionable ? "warn" : "muted",
    sub: buildBits.join(" · ") || (plan ? "nothing to do" : "no target model"),
    title: "Choose amounts and stock or option routes, then add the exact result to the order queue" };

  const rows = plan?.rows || 0;
  const inBand = rows - (plan?.out_of_band || 0);
  const cashOff = plan?.cash && plan.cash.status && plan.cash.status !== "IN";
  const s2: Stage = { n: 2, label: "Review impact", view: "target-state",
    tone: rows && inBand === rows && !cashOff ? "ok" : "muted",
    sub: staged
      ? `${inBand}/${rows} bands in${cashOff ? " · cash off target" : ""}`
      : "waiting for queued orders",
    title: "Approve the portfolio projected from the exact order queue" };

  const placeBits = [];
  if (staged) placeBits.push(`${staged} queued`);
  if (d.working) placeBits.push(`${d.working} working`);
  if (d.working == null) {
    placeBits.push(gatewayConnected(d.gateway || null) ? "IBKR orders unavailable" : "IBKR offline");
  }
  const s3: Stage = { n: 3, label: "Preview & place", view: "trade",
    tone: staged ? "warn" : "muted",
    sub: placeBits.join(" · ") || "nothing queued",
    title: "Preview the approved queue through IBKR and place confirmed orders" };

  return [s1, s2, s3];
}

export function flowBarHtml(d: FlowData, activeStage: number): string {
  const snap = d.ov?.snapshot || {};
  const currentTone = snap.exists && !snap.stale ? "ok" : "warn";
  const currentSub = snap.exists
    ? `${plural(snap.positions || 0, "position")} · ${ago(snap.age_days)}`
    : "connect holdings";
  const inputAction = !snap.exists
    ? `<button type="button" class="flow-input-action" data-flow-view="setup">Connect holdings →</button>`
    : snap.stale
      ? `<button type="button" class="flow-input-action" data-flow-refresh>Refresh holdings</button>`
      : `<button type="button" class="flow-input-action" data-flow-book>View positions ↗</button>`;
  const input = `<div class="flow-input flow-${currentTone}"` +
    ` title="The holdings snapshot used to calculate every order and projection">` +
    `<span>Using</span><strong>Current book</strong><small>${esc(currentSub)}</small>${inputAction}</div>` +
    `<span class="flow-input-link" aria-hidden="true">→</span>`;
  return input + flowStages(d).map((s) =>
    `<button type="button" class="flow-stage flow-${s.tone}${s.n === activeStage ? " active" : ""}"` +
    ` data-flow-view="${esc(s.view)}" title="${esc(s.title)}">` +
    `<span class="flow-dot">${s.n}</span>` +
    `<span class="flow-text"><span class="flow-label">${esc(s.label)}</span>` +
    `<span class="flow-sub">${esc(s.sub)}</span></span>` +
    `</button>`).join(`<span class="flow-link" aria-hidden="true"></span>`);
}

// ---- DOM wiring --------------------------------------------------------------
let _bookDrawer: HTMLElement | null = null;
let _bookDrawerKeyHandler: ((event: KeyboardEvent) => void) | null = null;

function closeBookDrawer(): void {
  if (!_bookDrawer) return;
  const trigger = _bookDrawer.dataset.triggerId
    ? document.getElementById(_bookDrawer.dataset.triggerId)
    : null;
  _bookDrawer.remove();
  _bookDrawer = null;
  if (_bookDrawerKeyHandler) document.removeEventListener("keydown", _bookDrawerKeyHandler);
  _bookDrawerKeyHandler = null;
  trigger?.focus();
}

function bookRows(h: HoldingsPayload): string {
  const rows = (h.positions || [])
    .slice()
    .sort((a, b) => Math.abs(b.percent_of_nav || 0) - Math.abs(a.percent_of_nav || 0));
  if (!rows.length) return `<div class="book-drawer-empty">No positions in this snapshot.</div>`;
  return rows.map((p) => {
    const optionPct = p.option?.exercise_pct;
    const weight = p.asset_class === "OPT" && optionPct != null
      ? `${optionPct < 0 ? "↓" : "↑"}${Math.abs(optionPct).toFixed(1)}% if exercised`
      : p.percent_of_nav == null ? "—" : `${p.percent_of_nav.toFixed(2)}%`;
    const label = p.asset_class === "OPT" ? (p.description || p.symbol) : p.symbol;
    return `<div class="book-drawer-row">` +
      `<span><strong>${esc(label)}</strong>${p.asset_class === "OPT" ? `<small>option exposure</small>` : ""}</span>` +
      `<span>${esc(weight)}</span></div>`;
  }).join("");
}

async function openBookDrawer(trigger: HTMLElement): Promise<void> {
  closeBookDrawer();
  if (!trigger.id) trigger.id = `flow-book-${Date.now()}`;
  const overlay = document.createElement("div");
  overlay.className = "book-drawer-overlay";
  overlay.dataset.triggerId = trigger.id;
  overlay.innerHTML =
    `<aside class="book-drawer" role="dialog" aria-modal="true" aria-labelledby="book-drawer-title">` +
      `<div class="book-drawer-head"><div><span>Workflow input</span>` +
      `<h2 id="book-drawer-title">Current book</h2></div>` +
      `<button type="button" class="ghost book-drawer-close" aria-label="Close current book">×</button></div>` +
      `<div class="book-drawer-body"><div class="status"><span class="spinner"></span> Loading positions…</div></div>` +
      `<div class="book-drawer-actions">` +
      `<button type="button" class="ghost" data-book-full>Open full Portfolio ↗</button></div>` +
    `</aside>`;
  document.body.appendChild(overlay);
  _bookDrawer = overlay;
  const close = () => closeBookDrawer();
  overlay.querySelector(".book-drawer-close")?.addEventListener("click", close);
  overlay.addEventListener("click", (event) => { if (event.target === overlay) close(); });
  overlay.querySelector("[data-book-full]")?.addEventListener("click", () => {
    close();
    pushNav({ view: "holdings" });
    setActiveView("holdings");
  });
  const onKey = (event: KeyboardEvent) => {
    if (event.key !== "Escape" || _bookDrawer !== overlay) return;
    close();
  };
  _bookDrawerKeyHandler = onKey;
  document.addEventListener("keydown", onKey);
  (overlay.querySelector(".book-drawer-close") as HTMLButtonElement | null)?.focus();
  try {
    const h = await api<HoldingsPayload>("/api/holdings");
    if (_bookDrawer !== overlay) return;
    const body = overlay.querySelector(".book-drawer-body") as HTMLElement;
    const count = (h.positions || []).length;
    body.innerHTML =
      `<div class="book-drawer-summary"><strong>${plural(count, "position")}</strong>` +
      `<span>Weights from the snapshot currently driving this rebalance.</span></div>` +
      `<div class="book-drawer-list">${bookRows(h)}</div>`;
  } catch (error) {
    if (_bookDrawer !== overlay) return;
    const body = overlay.querySelector(".book-drawer-body") as HTMLElement;
    body.innerHTML = `<div class="status err">Could not load positions: ${esc((error as Error).message)}</div>`;
  }
}

async function refreshBook(button: HTMLButtonElement): Promise<void> {
  const previous = button.textContent || "Refresh holdings";
  button.disabled = true;
  button.textContent = "Starting refresh…";
  try {
    await api("/api/holdings/sync", "POST", {});
    button.textContent = "Refresh started ✓";
    button.title = "The read-only holdings refresh is running in the background";
    invalidateFlowData();
  } catch (error) {
    button.textContent = "Refresh failed";
    button.title = (error as Error).message;
    button.disabled = false;
    window.setTimeout(() => { button.textContent = previous; }, 2500);
  }
}

let _wired = false;
export function initFlowBar(): void {
  if (_wired) return;
  _wired = true;
  subscribeQueueChanged(() => {
    invalidateFlowData();
    if (_activeView) updateFlowBar(_activeView, _activeGroup);
  });
  const host = $("#flowbar");
  if (!host) return;
  host.addEventListener("click", (e) => {
    const book = (e.target as HTMLElement).closest<HTMLElement>("[data-flow-book]");
    if (book) { void openBookDrawer(book); return; }
    const refresh = (e.target as HTMLElement).closest<HTMLButtonElement>("[data-flow-refresh]");
    if (refresh) { void refreshBook(refresh); return; }
    const b = (e.target as HTMLElement).closest<HTMLElement>("[data-flow-view]");
    if (!b || !b.dataset.flowView) return;
    pushNav({ view: b.dataset.flowView });
    setActiveView(b.dataset.flowView);
  });
}

// The execution bar replaces rebalance subtabs and is shown only while building,
// reviewing, or placing orders. Current book opens in-place so partially edited
// order amounts survive; the drawer's explicit Portfolio action is the escape.
export function updateFlowBar(view: string, group: string): void {
  _activeView = view;
  _activeGroup = group;
  const host = $("#flowbar");
  if (!host) return;
  if (group !== "rebalance") { host.hidden = true; return; }
  host.hidden = false;
  const stage = stageForView(view);
  if (_cache) host.innerHTML = flowBarHtml(_cache, stage);
  void fetchFlowData().then((d) => {
    // Only paint if we're still on a rebalance view (fetch may outlive a nav).
    if (!host.hidden && _activeGroup === "rebalance") {
      host.innerHTML = flowBarHtml(d, stageForView(_activeView));
    }
  });
}
