// The rebalance-group flow bar: one persistent strip that turns five sibling
// sub-tabs into a legible pipeline —
//
//   ① Current book → ② Plan changes → ③ Orders → ④ Target state
//
// Each stage shows live counts (positions + working orders, suggested actions +
// drafted changes, staged + working orders, bands in reach) and clicks through
// to the view that owns it, so "where am I and what's next" never needs
// exploration. Fed by one cached /api/overview call plus a best-effort
// /api/trade/orders count (absent when the gateway is off — the bar simply
// omits the chip rather than nagging).
import { $, api, esc } from "./core";
import { pushNav, setActiveView } from "./shell";

// ---- data ------------------------------------------------------------------
interface FlowOverview {
  snapshot?: { exists?: boolean; positions?: number; age_days?: number | null; stale?: boolean } | null;
  plan?: { rows?: number; out_of_band?: number; actionable?: number; gates_open?: number;
           cash?: { status?: string } | null } | null;
  draft?: { pending?: number } | null;
  staged_basket?: { count?: number } | null;
}
export interface FlowData {
  ov: FlowOverview | null;
  working: number | null;  // null = unknown (gateway off/unreachable), not zero
}

let _cache: FlowData | null = null;
let _cacheAt = 0;
const TTL_MS = 15_000;

async function fetchFlowData(): Promise<FlowData> {
  if (_cache && Date.now() - _cacheAt < TTL_MS) return _cache;
  let ov: FlowOverview | null = null;
  let working: number | null = null;
  try { ov = await api<FlowOverview>("/api/overview"); } catch { /* sections degrade */ }
  try {
    const res = await api<{ orders?: unknown[] }>("/api/trade/orders");
    working = Array.isArray(res.orders) ? res.orders.length : 0;
  } catch { /* trading disabled / gateway down -> unknown */ }
  _cache = { ov, working };
  _cacheAt = Date.now();
  return _cache;
}

// Force the next render to refetch (e.g. after placing/cancelling an order).
export function invalidateFlowData(): void { _cacheAt = 0; }

// ---- pure builders (exported for tests) -------------------------------------
// Which pipeline stage a view belongs to. `holdings`/`setup` are stage 1 (the
// current book) even though they live in the portfolio group, so the bar stays
// put — and highlights the right step — when you click through to "Current book".
export function stageForView(view: string): 1 | 2 | 3 | 4 {
  if (view === "holdings" || view === "setup") return 1;
  if (view === "trade") return 3;
  if (view === "target-state") return 4;
  return 2;  // rebalance / optimizer / working-draft / exit: deciding the changes
}

// Non-rebalance-group views that still belong to the pipeline and so keep the
// flow bar visible. Only the "Current book" target (holdings) — the other
// portfolio views (history, risk, tax, …) aren't pipeline steps.
const FLOW_VIEWS = new Set(["holdings"]);

const plural = (n: number, s: string) => `${n} ${s}${n === 1 ? "" : "s"}`;
const ago = (d: number | null | undefined) =>
  d == null ? "" : d === 0 ? "synced today" : d === 1 ? "synced yesterday" : `synced ${d}d ago`;

interface Stage { n: 1 | 2 | 3 | 4; label: string; sub: string; tone: "ok" | "warn" | "muted"; view: string; title: string }

export function flowStages(d: FlowData): Stage[] {
  const snap = d.ov?.snapshot || {};
  const plan = d.ov?.plan;
  const draft = d.ov?.draft || {};
  const basket = d.ov?.staged_basket || {};

  const workingBit = d.working ? ` · ${plural(d.working, "working order")}` : "";
  const s1: Stage = snap.exists
    ? { n: 1, label: "Current book", view: "holdings", tone: snap.stale ? "warn" : "ok",
        sub: `${plural(snap.positions || 0, "position")} · ${ago(snap.age_days)}${workingBit}`,
        title: "Your holdings snapshot" + (d.working ? " plus unfilled orders at IBKR" : "") + " — the ground truth every suggestion is computed from" }
    : { n: 1, label: "Current book", view: "setup", tone: "warn", sub: "no holdings yet",
        title: "Connect your data to begin" };

  const actionable = plan?.actionable || 0;
  const pending = draft.pending || 0;
  const bits2 = [];
  if (actionable) bits2.push(`${actionable} suggested`);
  if (pending) bits2.push(`${pending} drafted`);
  if (plan?.gates_open) bits2.push(`${plural(plan.gates_open, "gate")} triggered`);
  const s2: Stage = { n: 2, label: "Plan changes", view: "rebalance",
    tone: actionable || pending ? "warn" : "muted",
    sub: bits2.join(" · ") || (plan ? "nothing to do" : "no plan yet"),
    title: "Suggested trades from your target bands, plus anything drafted in the Optimizer / Working draft / Exit planner" };

  const staged = basket.count || 0;
  const bits3 = [];
  if (staged) bits3.push(`${staged} staged`);
  if (d.working) bits3.push(`${d.working} working`);
  const s3: Stage = { n: 3, label: "Orders", view: "trade",
    tone: staged ? "warn" : "muted",
    sub: bits3.join(" · ") || "nothing staged",
    title: "Preview the staged basket through IBKR and place it, order by confirmed order" };

  const rows = plan?.rows || 0;
  const inBand = rows - (plan?.out_of_band || 0);
  const cashOff = plan?.cash && plan.cash.status && plan.cash.status !== "IN";
  const s4: Stage = { n: 4, label: "Target state", view: "target-state",
    tone: rows && inBand === rows && !cashOff ? "ok" : "muted",
    sub: rows ? `${inBand}/${rows} bands in${cashOff ? " · cash off target" : ""}` : "—",
    title: "Current vs projected book, side by side — where placing what's on the table lands you" };

  return [s1, s2, s3, s4];
}

export function flowBarHtml(d: FlowData, activeStage: number): string {
  return flowStages(d).map((s) =>
    `<button type="button" class="flow-stage flow-${s.tone}${s.n === activeStage ? " active" : ""}"` +
    ` data-flow-view="${esc(s.view)}" title="${esc(s.title)}">` +
    `<span class="flow-dot">${s.n}</span>` +
    `<span class="flow-text"><span class="flow-label">${esc(s.label)}</span>` +
    `<span class="flow-sub">${esc(s.sub)}</span></span>` +
    `</button>`).join(`<span class="flow-link" aria-hidden="true"></span>`);
}

// ---- DOM wiring --------------------------------------------------------------
let _wired = false;
export function initFlowBar(): void {
  if (_wired) return;
  _wired = true;
  const host = $("#flowbar");
  if (!host) return;
  host.addEventListener("click", (e) => {
    const b = (e.target as HTMLElement).closest<HTMLElement>("[data-flow-view]");
    if (!b || !b.dataset.flowView) return;
    pushNav({ view: b.dataset.flowView });
    setActiveView(b.dataset.flowView);
  });
}

// Show the bar on rebalance-group views plus the pipeline's "Current book"
// target (holdings), so clicking stage 1 doesn't drop the guide (the shell calls
// this on every view switch); render instantly from cache, then refresh once the
// fetch lands.
export function updateFlowBar(view: string, group: string): void {
  const host = $("#flowbar");
  if (!host) return;
  if (group !== "rebalance" && !FLOW_VIEWS.has(view)) { host.hidden = true; return; }
  host.hidden = false;
  const stage = stageForView(view);
  if (_cache) host.innerHTML = flowBarHtml(_cache, stage);
  void fetchFlowData().then((d) => {
    // Only paint if we're still on a rebalance view (fetch may outlive a nav).
    if (!host.hidden) host.innerHTML = flowBarHtml(d, stage);
  });
}
