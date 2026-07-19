// The "Today" cockpit: the portfolio group's front door. It reads the one
// aggregate /api/overview payload and renders two lanes — where the portfolio
// process stands (snapshot → plan → draft → basket → journal) and where the
// research funnel stands (basket triage, unresearched queue, segment caches) —
// plus a single next-step banner so the loop always shows its next door.
// Pure HTML builders below are exported for tests; DOM wiring stays in
// initOverview()/loadOverview() (same import-cycle discipline as the peers).
import { starHtml } from "./basket";
import { $, api, esc, fmtCZK, relAge, sensitive } from "./core";
import { tickerAnchorHtml } from "./analyses/linkify";
import { runHoldingsSync } from "./holdings-sync";
import {
  publishPipelineChanged, queueWorkflowView, subscribePipelineChanged,
} from "./pipeline-summary";
import { pushNav, setActiveView } from "./shell";
import { openTicker } from "./ticker-nav";
import { loadCachedSegment } from "./segment";
import type { ActivityEvent, ActivityResponse } from "./api-types";

// ---- payload shapes (GET /api/overview) ------------------------------------
interface SnapshotSum {
  exists: boolean;
  generated_at?: string | null;
  age_days?: number | null;
  stale?: boolean;
  positions: number;
}
interface PlanSum {
  rows: number;
  out_of_band: number;
  buy: number;
  trim: number;
  review: number;
  actionable: number;
  conflicts: number;
  gates_waiting: number;
  gates_open: number;
  untargeted: number;
  untargeted_pct?: number | null;
  cash?: { pct_of_nav: number; target_pct: number; low: number; high: number; status: string } | null;
}
interface DriftBySymbol { symbol: string; net_qty: number; buys: number; sells: number }
interface DriftSum {
  checked: boolean;
  stale_vs_ledger: boolean;
  n_trades_after: number;
  last_trade_at?: string | null;
  by_symbol: DriftBySymbol[];
}
interface DraftSum { has_draft: boolean; pending: number }
interface ExecutionPlanSum {
  planned: number; selected: number; deferred: number; suggested: number;
  queued: number; submitted: number; stale?: boolean; updated_at?: string | null;
}
interface StagedBasketSum {
  count: number; buys: number; sells: number; total_abs_czk: number;
  conditional_buys?: number; conditional_reductions?: number; option_legs?: number;
  reviewed?: boolean; valid?: boolean;
}
interface BrokerOrdersSum {
  active: number; partial: number; recent_filled: number; recent_failed: number;
  updated_at?: string | null;
}
interface JournalSum { total: number; pending_outcomes: number; oldest_pending_days?: number | null; review_due: number }
interface PickRow { symbol: string; tier?: string; segment?: string | null; age_days?: number | null }
interface QueueRow { symbol: string; score: number; segment?: string | null; decision?: string | null }
interface StaleSegment { name: string; title?: string; age_days: number }
interface ResearchSum {
  basket: { count: number; unresearched_count: number; aging_count: number; unresearched: PickRow[] };
  segments: { total: number; cached: number; stale: StaleSegment[]; stale_count: number };
  queue: QueueRow[];
}
interface NextStep { id: string; view: string; label: string; reason: string; symbol?: string }
export interface AutomationTask {
  name: string;
  label: string;
  enabled: boolean;
  last_run?: string | null;
  last_result?: string | null;
  age_days?: number | null;
  next_eligible?: string | null;
}
export interface AutomationSum { enabled: boolean; any_ran: boolean; tasks: AutomationTask[] }
export interface AttributionVerdictSum {
  exists: boolean;
  as_of?: string | null;
  range?: string;
  benchmark?: string;
  actual_pct?: number | null;
  vs_hold_pp?: number | null;
  vs_benchmark_pp?: number | null;
  updated_at?: string | null;
  age_days?: number | null;
  stale?: boolean;
}
export interface Overview {
  generated_at?: string;
  snapshot: SnapshotSum;
  drift?: DriftSum | null;
  plan?: PlanSum | null;
  draft: DraftSum;
  execution_plan?: ExecutionPlanSum;
  staged_basket: StagedBasketSum;
  broker_orders?: BrokerOrdersSum;
  journal: JournalSum;
  attribution?: AttributionVerdictSum;
  research: ResearchSum;
  automation?: AutomationSum;
  next_step: NextStep;
}

interface AttentionItem {
  id: string;
  tone: "bad" | "warn" | "info";
  title: string;
  detail: string;
  view: string;
  action: string;
}

// ---- tiny shared bits -------------------------------------------------------
const tlink = (sym: string) => tickerAnchorHtml(sym, { bold: true });
const goBtn = (view: string, label: string, cls = "ghost") =>
  `<button class="${cls}" type="button" data-goto="${esc(view)}">${label}</button>`;
const agoText = (days: number | null | undefined) =>
  days == null ? "age unknown" : days === 0 ? "today" : days === 1 ? "yesterday" : `${days} days ago`;

function card(tone: "ok" | "warn" | "bad" | "muted", title: string, chip: string,
              body: string, actions: string): string {
  return `<div class="today-card today-${tone}">` +
    `<div class="today-card-head"><span class="today-card-title">${title}</span>${chip}</div>` +
    `<div class="today-card-body">${body}</div>` +
    (actions ? `<div class="today-card-actions">${actions}</div>` : "") +
    `</div>`;
}

// ---- next-step banner -------------------------------------------------------
export function nextStepHtml(step: NextStep): string {
  const urgent = [
    "setup", "resync", "drift-resync", "commit-draft", "place-basket",
    "stale-plan", "planned-orders", "gates-open",
  ].includes(step.id);
  const tone = step.id === "all-clear" ? "ok" : urgent ? "warn" : "info";
  const go = step.id === "all-clear" ? "" :
    `<button class="primary" type="button" data-goto="${esc(step.view)}"` +
    (step.symbol ? ` data-ticker="${esc(step.symbol)}"` : "") +
    `>${esc(step.label)} →</button>`;
  return `<div class="today-next today-next-${tone}">` +
    `<div class="today-next-text"><span class="today-next-kicker">Next step</span>` +
    `<strong>${esc(step.label)}</strong><p>${esc(step.reason)}</p></div>${go}</div>`;
}

// ---- portfolio lane ---------------------------------------------------------
const taskOf = (a: AutomationSum | undefined, name: string): AutomationTask | undefined =>
  a?.tasks?.find((t) => t.name === name);
// Deterministic, tz-safe calendar date for "next check by …" copy.
const onDay = (iso: string | null | undefined) => (iso ? ` by ${esc(iso.slice(0, 10))}` : "");

// "3 NVDA sold, 5 AMD bought" — plain-language summary of what the ledger shows
// moving since the snapshot, so the drift warning names the culprit.
function driftMoves(rows: DriftBySymbol[]): string {
  return rows.slice(0, 4).map((r) => {
    const q = Math.abs(r.net_qty);
    const verb = r.net_qty < 0 ? "sold" : "bought";
    return `${q % 1 === 0 ? q : q.toFixed(2)} ${esc(r.symbol)} ${verb}`;
  }).join(", ");
}

export function snapshotCard(s: SnapshotSum, auto?: AutomationSum, drift?: DriftSum | null): string {
  if (!s.exists) {
    return card("bad", "Holdings snapshot", `<span class="chip bad">missing</span>`,
      `No broker snapshot yet — every portfolio view below needs one.`,
      goBtn("setup", "Open Setup"));
  }
  const resync = taskOf(auto, "holdings-resync");
  const armed = !!(auto?.enabled && resync?.enabled);
  // Ledger drift is a harder failure than calendar-staleness: the snapshot may be
  // young yet already behind a fill, so it drives the card tone red on its own.
  const behindLedger = !!drift?.stale_vs_ledger;
  const tone = behindLedger ? "bad" : s.stale ? "warn" : "ok";
  const chip = `<span class="chip ${tone === "ok" ? "good" : tone}">synced ${esc(agoText(s.age_days))}</span>`;
  let body = `${s.positions} position${s.positions === 1 ? "" : "s"} on file.`;
  if (behindLedger) {
    const moves = driftMoves(drift!.by_symbol || []);
    body += ` <span class="today-warn-text">Behind the trade ledger: ${drift!.n_trades_after} execution`
      + `${drift!.n_trades_after === 1 ? "" : "s"} postdate this snapshot`
      + `${moves ? ` (${moves})` : ""} — resync before sizing any trade.</span>`;
  } else if (s.stale && armed) {
    body += ` <span class="today-auto-text">Stale, but auto-resync is armed — next check${onDay(resync?.next_eligible)}.</span>`;
  } else if (s.stale) {
    body += ` <span class="today-warn-text">Plan math below is computed from this stale snapshot.</span>`;
  } else if (armed) {
    body += ` <span class="today-auto-text">Auto-resync armed — next check${onDay(resync?.next_eligible)}.</span>`;
  }
  const actions =
    `<button class="ghost" type="button" data-action="resync">Resync from IBKR</button>` +
    goBtn("holdings", "Positions →") +
    (armed ? "" : goBtn("setup", "Enable auto-refresh"));
  return card(tone, "Holdings snapshot", chip, body, actions);
}

export function planCard(p: PlanSum | null | undefined): string {
  if (!p) {
    return card("muted", "Standing plan", "",
      `No target model yet. Set allocation segments under Targets → Composition.`,
      goBtn("working-draft", "Composition →", "primary"));
  }
  const tone = p.actionable ? "warn" : "ok";
  const chip = p.actionable
    ? `<span class="chip warn">${p.actionable} action${p.actionable === 1 ? "" : "s"} suggested</span>`
    : `<span class="chip good">all in band</span>`;
  const bits = [];
  bits.push(`<strong>${p.out_of_band}</strong> of ${p.rows} targeted name${p.rows === 1 ? "" : "s"} outside its band` +
    (p.actionable ? ` — ${p.buy} buy, ${p.trim} trim, ${p.review} review.` : `.`));
  if (p.gates_open) bits.push(`<span class="today-warn-text">${p.gates_open} price trigger${p.gates_open === 1 ? "" : "s"} met and actionable.</span>`);
  if (p.gates_waiting) bits.push(`${p.gates_waiting} price gate${p.gates_waiting === 1 ? "" : "s"} still waiting.`);
  if (p.conflicts) bits.push(`<span class="today-warn-text">${p.conflicts} plan-vs-thesis conflict${p.conflicts === 1 ? "" : "s"}.</span>`);
  if (p.cash && p.cash.status !== "IN") {
    bits.push(`<span class="today-warn-text">Cash is ${p.cash.pct_of_nav.toFixed(1)}% of NAV, ` +
      `${p.cash.status === "BELOW" ? "under" : "over"} its ${p.cash.low}–${p.cash.high}% band (target ${p.cash.target_pct}%).</span>`);
  }
  if (p.untargeted) bits.push(`${p.untargeted} held name${p.untargeted === 1 ? "" : "s"} (${(p.untargeted_pct ?? 0).toFixed(1)}%) ride with no band.`);
  // One door: actionable drift goes to Trade; otherwise Composition for ratios.
  const action = p.actionable
    ? goBtn("orders", "Trade →", "primary")
    : goBtn("working-draft", "Targets →");
  return card(tone, "Standing plan", chip, bits.join(" "), action);
}

export function draftCard(d: DraftSum): string {
  if (!d.pending) return "";
  return card("warn", "Composition draft",
    `<span class="chip warn">${d.pending} pending</span>`,
    `Proposed target-band changes — applying them updates the model, not current holdings.`,
    goBtn("working-draft", "Review draft →", "primary"));
}

export function stagedBasketCard(b: StagedBasketSum): string {
  if (!b.count) return "";
  const nextView = queueWorkflowView(b);
  const ready = nextView === "trade";
  const conditional = [
    b.conditional_buys
      ? `${b.conditional_buys} short put${b.conditional_buys === 1 ? "" : "s"}`
      : "",
    b.conditional_reductions
      ? `${b.conditional_reductions} covered call${b.conditional_reductions === 1 ? "" : "s"}`
      : "",
  ].filter(Boolean).join(", ");
  return card("warn", "Order queue",
    `<span class="chip ${ready ? "good" : "warn"}">${b.count} trade${b.count === 1 ? "" : "s"}${ready ? " · approved" : ""}</span>`,
    `${b.buys} share buy${b.buys === 1 ? "" : "s"}, ` +
    `${b.sells} share sell${b.sells === 1 ? "" : "s"}` +
    `${conditional ? ` · conditional: ${conditional}` : ""} · ` +
    `${sensitive(`${fmtCZK(b.total_abs_czk)} CZK`, "queued direct-share size")} direct-share value — queued, not yet placed.`,
    goBtn(
      nextView || "target-state",
      ready ? "Preview & place →" : "Review projected portfolio →",
      "primary",
    ));
}

export function journalCard(j: JournalSum): string {
  if (!j.pending_outcomes) return "";
  const overdue = j.review_due > 0;
  return card(overdue ? "warn" : "muted", "Decision journal",
    `<span class="chip ${overdue ? "warn" : "muted"}">${j.pending_outcomes} unscored</span>`,
    `${j.pending_outcomes} decision${j.pending_outcomes === 1 ? "" : "s"} without a recorded outcome` +
    (j.oldest_pending_days != null ? ` (oldest ${agoText(j.oldest_pending_days)})` : "") +
    `. Scoring them is what makes the calibration loop honest.`,
    goBtn("journal", "Journal →"));
}

export function attributionCard(a: AttributionVerdictSum | null | undefined): string {
  if (!a || !a.exists) {
    return card("muted", "Process attribution", "",
      `Has your trading beaten doing nothing? Compute the flow-neutralized return against ` +
      `never-rebalancing and the benchmark.`,
      goBtn("attribution", "Attribution →"));
  }
  const sign = (x: number) => (x >= 0 ? "+" : "") + x.toFixed(2);
  const range = a.range || "the window";
  const bench = a.benchmark || "benchmark";
  const vh = a.vs_hold_pp;
  const vb = a.vs_benchmark_pp;
  const deltas = [vh, vb].filter((x): x is number => typeof x === "number");
  const worst = deltas.length ? Math.min(...deltas) : null;
  const tone = worst == null ? "muted" : worst >= 0 ? "ok" : "bad";
  const chip = worst == null ? ""
    : worst >= 0 ? `<span class="chip good">earning its keep</span>`
    : `<span class="chip warn">under review</span>`;
  const bits: string[] = [];
  bits.push(`Actual TWR <strong>${a.actual_pct == null ? "n/a" : sign(a.actual_pct) + "%"}</strong> over ${esc(range)}.`);
  if (vh != null) bits.push(`${vh >= 0 ? "Beating" : "Trailing"} never-rebalancing by ${sign(vh)} pp.`);
  if (vb != null) bits.push(`${vb >= 0 ? "Beating" : "Trailing"} ${esc(bench)} by ${sign(vb)} pp.`);
  if (a.stale) bits.push(`<span class="today-warn-text">Verdict is ${agoText(a.age_days)} — reopen Attribution to refresh.</span>`);
  return card(tone, "Process attribution", chip, bits.join(" "), goBtn("attribution", "Attribution →"));
}

// ---- research lane ----------------------------------------------------------
export function basketTriageCard(r: ResearchSum): string {
  const b = r.basket;
  if (!b.count) {
    return card("muted", "Shortlist triage", "",
      `Your watchlist is empty. Star (☆) names from Explore, Deep Research, or a ticker dossier to build the funnel.`,
      goBtn("analyses", "Research →"));
  }
  if (!b.unresearched_count) {
    return card("ok", "Shortlist triage", `<span class="chip good">all researched</span>`,
      `All ${b.count} watchlist pick${b.count === 1 ? "" : "s"} have a saved analysis.`,
      goBtn("basket", "Watchlist →"));
  }
  const rows = b.unresearched.map((p) =>
    `<span class="today-pick">${tlink(p.symbol)}` +
    `<span class="chip ${p.tier === "curious" ? "muted" : "good"}">${esc(p.tier || "want")}</span>` +
    (p.age_days != null && p.age_days > 30 ? `<small class="today-age">${p.age_days}d</small>` : "") +
    `</span>`).join(" ");
  return card(b.aging_count ? "warn" : "muted", "Shortlist triage",
    `<span class="chip ${b.aging_count ? "warn" : "muted"}">${b.unresearched_count} unresearched</span>`,
    `${b.unresearched_count} of ${b.count} pick${b.count === 1 ? "" : "s"} have no saved analysis` +
    (b.aging_count ? ` — ${b.aging_count} sitting for 30+ days` : "") + `: ${rows}`,
    goBtn("basket", "Watchlist →"));
}

export function queueCard(r: ResearchSum): string {
  if (!r.queue.length) return "";
  const rows = r.queue.map((q) =>
    `<div class="today-queue-row">` +
    starHtml(q.symbol, "segment", { tier: "curious", segment: q.segment || undefined }) +
    tlink(q.symbol) +
    `<span class="score-pill">${esc(q.score)}</span>` +
    (q.segment ? `<small class="muted">${esc(q.segment)}</small>` : "") +
    `</div>`).join("");
  return card("muted", "Research queue",
    `<span class="chip muted">${r.queue.length} candidates</span>`,
    `Highest-scoring names from your segment work that nobody has held, starred, or analysed yet:` +
    `<div class="today-queue">${rows}</div>`, "");
}

export function segmentsCard(r: ResearchSum): string {
  const s = r.segments;
  if (!s.total) return "";
  if (!s.stale_count) {
    return card("ok", "Segment universes", `<span class="chip good">${s.cached} cached</span>`,
      `${s.total} segment${s.total === 1 ? "" : "s"} defined, ${s.cached} with a fresh peer pull.`, "");
  }
  const rows = s.stale.map((seg) =>
    `<button class="ghost today-seg" type="button" data-segment="${esc(seg.name)}">` +
    `${esc(seg.title || seg.name)} <small class="today-age">${seg.age_days}d old</small></button>`).join(" ");
  return card("warn", "Segment universes",
    `<span class="chip warn">${s.stale_count} stale</span>`,
    `These peer pulls are over 45 days old — the comparison tables have drifted: ${rows}`, "");
}

// ---- daily command center --------------------------------------------------
// The primary recommendation already owns the loudest CTA. This queue contains
// only the next three *other* exceptions, in workflow-risk order, so the home
// view reads as a decision surface instead of a catalogue of subsystems.
export function attentionItems(v: Overview): AttentionItem[] {
  const rows: AttentionItem[] = [];
  if (!v.snapshot.exists) {
    rows.push({ id: "setup", tone: "bad", title: "Holdings are unavailable",
      detail: "Portfolio decisions have no broker snapshot underneath them.", view: "setup", action: "Set up" });
  } else if (v.snapshot.stale) {
    rows.push({ id: "resync", tone: "bad", title: "Holdings snapshot is stale",
      detail: `Last synced ${agoText(v.snapshot.age_days)}; sizing may no longer match the account.`, view: "holdings", action: "Resync" });
  } else if (v.drift?.stale_vs_ledger) {
    rows.push({ id: "drift-resync", tone: "bad", title: "Trades postdate the snapshot",
      detail: `${v.drift.n_trades_after} execution${v.drift.n_trades_after === 1 ? "" : "s"} are missing from the current book.`, view: "holdings", action: "Resync" });
  }
  if (v.draft.pending) rows.push({ id: "commit-draft", tone: "warn", title: "Target-model changes need a decision",
    detail: `${v.draft.pending} target change${v.draft.pending === 1 ? "" : "s"} remain unapplied.`, view: "working-draft", action: "Review" });
  if (v.staged_basket.count) {
    const nextView = queueWorkflowView(v.staged_basket) || "target-state";
    const ready = nextView === "trade";
    rows.push({ id: "place-basket", tone: "warn", title: ready ? "Orders are approved" : "Orders are queued",
      detail: ready
        ? `${v.staged_basket.count} order${v.staged_basket.count === 1 ? "" : "s"} are ready for IBKR preview.`
        : `${v.staged_basket.count} order${v.staged_basket.count === 1 ? "" : "s"} need a projected-portfolio review.`,
      view: nextView, action: ready ? "Preview & place" : "Review impact" });
  }
  if (v.execution_plan?.stale && v.execution_plan.planned) rows.push({
    id: "stale-plan", tone: "warn", title: "Planned trades are stale",
    detail: `${v.execution_plan.planned} planned trade${v.execution_plan.planned === 1 ? "" : "s"} no longer match the current portfolio plan.`,
    view: "rebalance", action: "Recheck amounts",
  });
  if (v.broker_orders?.partial) rows.push({
    id: "partial-fill", tone: "warn", title: "Orders partially filled",
    detail: `${v.broker_orders.partial} broker order${v.broker_orders.partial === 1 ? "" : "s"} filled only in part.`,
    view: "orders", action: "Track fills",
  });
  if (v.broker_orders?.recent_failed) rows.push({
    id: "broker-failed", tone: "warn", title: "Broker orders failed or were cancelled",
    detail: `${v.broker_orders.recent_failed} correlated order${v.broker_orders.recent_failed === 1 ? "" : "s"} need review; failed intent is reopened automatically.`,
    view: "orders", action: "Review",
  });
  if (v.execution_plan?.planned) rows.push({ id: "planned-orders", tone: "warn", title: "Trades remain planned",
    detail: `${v.execution_plan.planned} selected or deferred trade${v.execution_plan.planned === 1 ? "" : "s"} have not reached the order queue.`, view: "orders", action: "Open pipeline" });
  if (v.plan?.gates_open) rows.push({ id: "gates-open", tone: "warn", title: "Price levels have triggered",
    detail: `${v.plan.gates_open} locked level${v.plan.gates_open === 1 ? " is" : "s are"} actionable.`, view: "rebalance", action: "Review" });
  if (v.plan?.actionable) rows.push({ id: "rebalance", tone: "warn", title: "The portfolio is outside plan",
    detail: `${v.plan.actionable} name${v.plan.actionable === 1 ? "" : "s"} have suggested actions.`, view: "rebalance", action: "Review" });
  if (v.research.basket.unresearched_count) rows.push({ id: "research-picks", tone: "info", title: "Shortlist needs research",
    detail: `${v.research.basket.unresearched_count} pick${v.research.basket.unresearched_count === 1 ? "" : "s"} have no saved analysis.`, view: "basket", action: "Triage" });
  if (v.journal.review_due) rows.push({ id: "journal", tone: "info", title: "Decision outcomes are due",
    detail: `${v.journal.review_due} journal entr${v.journal.review_due === 1 ? "y is" : "ies are"} ready to score.`, view: "journal", action: "Score" });
  if (v.research.segments.stale_count) rows.push({ id: "segments", tone: "info", title: "Segment data has aged",
    detail: `${v.research.segments.stale_count} peer universe${v.research.segments.stale_count === 1 ? " is" : "s are"} stale.`, view: "leaderboard", action: "Inspect" });
  return rows.filter((row) => row.id !== v.next_step.id).slice(0, 3);
}

function attentionHtml(v: Overview): string {
  const rows = attentionItems(v);
  return `<section class="today-section today-attention">` +
    `<div class="today-section-head"><h3>Needs attention</h3><span class="muted">${rows.length ? "after the next step" : "nothing else is pressing"}</span></div>` +
    (rows.length
      ? `<div class="today-attention-list">${rows.map((r) =>
          `<div class="today-attention-row today-attention-${r.tone}">` +
            `<span class="today-attention-mark"></span><div class="today-attention-copy">` +
            `<strong>${esc(r.title)}</strong><span>${esc(r.detail)}</span></div>` +
            goBtn(r.view, esc(r.action)) + `</div>`).join("")}</div>`
      : `<div class="today-clear">No secondary exceptions. The dashboard can shut up for a minute.</div>`) +
    `</section>`;
}

export function pulseHtml(v: Overview): string {
  const plan = v.plan;
  const cash = plan?.cash;
  const planned = v.execution_plan?.planned || 0;
  const brokerActive = v.broker_orders?.active || 0;
  const inFlight = planned + v.staged_basket.count + brokerActive;
  const staleNote = v.execution_plan?.stale && planned ? " · planned amounts stale" : "";
  const stat = (label: string, value: string, note: string, tone = "", view = "", id = "") => {
    const tag = view ? "button" : "div";
    return `<${tag}${id ? ` id="${esc(id)}"` : ""}${view ? ` type="button" data-goto="${esc(view)}"` : ""} ` +
      `class="today-pulse-stat${view ? " today-pulse-link" : ""}${tone ? ` today-pulse-${tone}` : ""}">` +
      `<span class="today-pulse-label">${esc(label)}</span><strong>${esc(value)}</strong><small>${esc(note)}</small></${tag}>`;
  };
  return `<section class="today-section today-pulse">` +
    `<div class="today-section-head"><h3>Portfolio pulse</h3>${goBtn("holdings", "Positions →")}</div>` +
    `<div class="today-pulse-grid">` +
      stat("Holdings", `${v.snapshot.positions} positions`, v.snapshot.exists ? `synced ${agoText(v.snapshot.age_days)}` : "snapshot missing", !v.snapshot.exists || v.snapshot.stale ? "warn" : "") +
      stat("Plan", plan ? `${plan.actionable} actions` : "not configured", plan && !plan.actionable ? "all targeted names in band" : `${plan?.out_of_band || 0} outside band`, plan?.actionable ? "warn" : "") +
      stat("Cash", cash ? `${cash.pct_of_nav.toFixed(1)}%` : "n/a", cash ? `${cash.low}–${cash.high}% band` : "no cash target", cash && cash.status !== "IN" ? "warn" : "") +
      stat("In flight", `${inFlight}`, inFlight ? `${planned} planned · ${v.staged_basket.count} queued · ${brokerActive} working${staleNote}` : "no planned, queued, or working trades", inFlight ? "warn" : "", "orders", "today-orders-inflight") +
    `</div></section>`;
}

function eventLabel(ev: ActivityEvent): string {
  if (ev.type === "view") return `Viewed ${(ev.symbol || "ticker").toUpperCase()}`;
  const kind = String(ev.kind || "task").replace(/[-_]+/g, " ");
  return `${kind.charAt(0).toUpperCase()}${kind.slice(1)} ${ev.state || "finished"}`;
}

export function recentActivityHtml(events: ActivityEvent[], since: string | null): string {
  const sinceMs = since ? Date.parse(since) : Date.now() - 24 * 60 * 60 * 1000;
  const recent = events.filter((e) => {
    const stamp = Date.parse(e.ts);
    return Number.isFinite(stamp) && stamp > sinceMs;
  }).slice(0, 5);
  return `<section class="today-section today-recent">` +
    `<div class="today-section-head"><h3>${since ? "Since your last visit" : "In the last 24 hours"}</h3>${goBtn("activity", "All activity →")}</div>` +
    (recent.length
      ? `<div class="today-recent-list">${recent.map((e) =>
          `<div class="today-recent-row"><span>${esc(eventLabel(e))}</span><small>${esc(relAge(e.ts))}</small></div>`).join("")}</div>`
      : `<div class="today-clear">No completed tasks or newly visited tickers.</div>`) +
    `</section>`;
}

function comingUpHtml(v: Overview): string {
  const rows: string[] = [];
  if (v.plan?.gates_waiting) rows.push(`${v.plan.gates_waiting} price gate${v.plan.gates_waiting === 1 ? "" : "s"} waiting`);
  if (v.journal.pending_outcomes) rows.push(`${v.journal.pending_outcomes} journal outcome${v.journal.pending_outcomes === 1 ? "" : "s"} unscored`);
  if (v.research.segments.stale_count) rows.push(`${v.research.segments.stale_count} segment refresh${v.research.segments.stale_count === 1 ? "" : "es"} due`);
  const refresh = taskOf(v.automation, "holdings-resync");
  if (v.automation?.enabled && refresh?.enabled && refresh.next_eligible) {
    rows.push(`Automatic holdings check ${onDay(refresh.next_eligible).trim()}`);
  }
  if (!rows.length) return "";
  return `<section class="today-section today-upcoming"><div class="today-section-head"><h3>Coming up</h3></div>` +
    `<div class="today-upcoming-list">${rows.slice(0, 4).map((r) => `<span>${esc(r)}</span>`).join("")}</div></section>`;
}

// ---- render + wiring --------------------------------------------------------
export function overviewHtml(v: Overview, events: ActivityEvent[] = [], since: string | null = null): string {
  const portfolio = [snapshotCard(v.snapshot, v.automation, v.drift), planCard(v.plan), draftCard(v.draft),
    stagedBasketCard(v.staged_basket), journalCard(v.journal), attributionCard(v.attribution)].filter(Boolean).join("");
  const research = [basketTriageCard(v.research), queueCard(v.research),
    segmentsCard(v.research)].filter(Boolean).join("");
  return nextStepHtml(v.next_step) +
    attentionHtml(v) +
    pulseHtml(v) +
    `<div class="today-lower">${recentActivityHtml(events, since)}${comingUpHtml(v)}</div>` +
    `<details class="today-more"><summary>Full system status</summary>` +
      `<div class="today-lanes">` +
      `<section class="today-lane"><div class="subhead">Portfolio</div><div class="today-cards">${portfolio}</div></section>` +
      `<section class="today-lane"><div class="subhead">Research</div><div class="today-cards">${research}</div></section>` +
      `</div></details>`;
}

async function loadOverview(): Promise<void> {
  const body = $("#today-body");
  const status = $("#today-status");
  if (!body) return;
  if (status) { status.textContent = ""; status.classList.remove("err"); }
  try {
    const previousVisit = localStorage.getItem("assay.home.lastVisit");
    const [v, activity] = await Promise.all([
      api<Overview>("/api/overview"),
      api<ActivityResponse>("/api/activity").catch(() => ({ events: [] })),
    ]);
    const stamp = v.generated_at || new Date().toISOString();
    const heading = $("#today-heading");
    if (heading) {
      const when = new Date(stamp);
      heading.textContent = Number.isFinite(when.getTime())
        ? new Intl.DateTimeFormat(undefined, { weekday: "long", day: "numeric", month: "long" }).format(when)
        : "Today";
    }
    body.innerHTML = overviewHtml(v, activity.events || [], previousVisit);
    publishPipelineChanged({
      source: "overview",
      planned: v.execution_plan?.planned || 0,
      queued: v.staged_basket.count || 0,
    });
    localStorage.setItem("assay.home.lastVisit", stamp);
  } catch (e) {
    if (status) { status.textContent = "Could not load the overview: " + (e as Error).message; status.classList.add("err"); }
  }
}

async function resyncHoldings(btn: HTMLButtonElement): Promise<void> {
  const status = $("#today-status");
  await runHoldingsSync({
    btn,
    status,
    onDone: async () => {
      if (status) status.textContent = "Synced.";
      await loadOverview();
    },
  });
}

let _wired = false;
function initOverview(): void {
  if (_wired) return;
  _wired = true;
  $("#today-refresh")?.addEventListener("click", () => loadOverview());
  subscribePipelineChanged((detail) => {
    if (detail.source === "broker" && $("#view-today")?.classList.contains("active")) {
      void loadOverview();
    }
  });
  const host = $("#view-today");
  if (!host) return;
  host.addEventListener("click", (e) => {
    const t = e.target as HTMLElement;
    const go = t.closest<HTMLElement>("[data-goto]");
    if (go) {
      const ticker = go.dataset.ticker;
      if (ticker && go.dataset.goto === "deepdive") { openTicker(ticker); return; }
      pushNav({ view: go.dataset.goto });
      setActiveView(go.dataset.goto || "rebalance");
      return;
    }
    const seg = t.closest<HTMLElement>("[data-segment]");
    if (seg && seg.dataset.segment) {
      void loadCachedSegment(seg.dataset.segment, { push: true });
      return;
    }
    const rs = t.closest<HTMLButtonElement>("[data-action=\"resync\"]");
    if (rs) void resyncHoldings(rs);
  });
}

export { initOverview, loadOverview };
