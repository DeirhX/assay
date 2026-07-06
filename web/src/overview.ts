// The "Today" cockpit: the portfolio group's front door. It reads the one
// aggregate /api/overview payload and renders two lanes — where the portfolio
// process stands (snapshot → plan → draft → basket → journal) and where the
// research funnel stands (basket triage, unresearched queue, segment caches) —
// plus a single next-step banner so the loop always shows its next door.
// Pure HTML builders below are exported for tests; DOM wiring stays in
// initOverview()/loadOverview() (same import-cycle discipline as the peers).
import { starHtml } from "./basket";
import { $, api, esc, fmtCZK, sensitive } from "./core";
import { pollDeepJob } from "./errors";
import { pushNav, setActiveView } from "./shell";
import { openTicker } from "./rebalance";
import { loadCachedSegment } from "./segment";

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
interface DraftSum { has_draft: boolean; pending: number }
interface StagedBasketSum { count: number; buys: number; sells: number; total_abs_czk: number }
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
export interface Overview {
  snapshot: SnapshotSum;
  plan?: PlanSum | null;
  draft: DraftSum;
  staged_basket: StagedBasketSum;
  journal: JournalSum;
  research: ResearchSum;
  automation?: AutomationSum;
  next_step: NextStep;
}

// ---- tiny shared bits -------------------------------------------------------
const tlink = (sym: string) =>
  `<a class="tlink" data-ticker="${esc(sym)}" href="?view=deepdive&ticker=${encodeURIComponent(sym)}"><strong>${esc(sym)}</strong></a>`;
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
  const urgent = ["setup", "resync", "commit-draft", "place-basket", "gates-open"].includes(step.id);
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

export function snapshotCard(s: SnapshotSum, auto?: AutomationSum): string {
  if (!s.exists) {
    return card("bad", "Holdings snapshot", `<span class="chip bad">missing</span>`,
      `No broker snapshot yet — every portfolio view below needs one.`,
      goBtn("setup", "Open Setup"));
  }
  const resync = taskOf(auto, "holdings-resync");
  const armed = !!(auto?.enabled && resync?.enabled);
  const tone = s.stale ? "warn" : "ok";
  const chip = `<span class="chip ${tone === "ok" ? "good" : "warn"}">synced ${esc(agoText(s.age_days))}</span>`;
  let body = `${s.positions} position${s.positions === 1 ? "" : "s"} on file.`;
  if (s.stale && armed) {
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
      `No target model yet. Draft one with the guided Planner or size the whole book in the Optimizer.`,
      goBtn("strategy", "Planner →") + goBtn("optimizer", "Optimizer →"));
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
  return card(tone, "Standing plan", chip, bits.join(" "), goBtn("rebalance", "Rebalance →"));
}

export function draftCard(d: DraftSum): string {
  if (!d.pending) return "";
  return card("warn", "Working draft",
    `<span class="chip warn">${d.pending} pending</span>`,
    `Uncommitted plan changes — the Rebalance planner is previewing the draft, not your live model.`,
    goBtn("working-draft", "Review & commit →", "primary"));
}

export function stagedBasketCard(b: StagedBasketSum): string {
  if (!b.count) return "";
  return card("warn", "Staged basket",
    `<span class="chip warn">${b.count} trade${b.count === 1 ? "" : "s"}</span>`,
    `${b.buys} buy${b.buys === 1 ? "" : "s"}, ${b.sells} sell${b.sells === 1 ? "" : "s"} · ` +
    `${sensitive(`${fmtCZK(b.total_abs_czk)} CZK`, "staged basket size")} total — simulated in the planner, not yet placed.`,
    goBtn("trade", "Trade desk →", "primary"));
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

// ---- research lane ----------------------------------------------------------
export function basketTriageCard(r: ResearchSum): string {
  const b = r.basket;
  if (!b.count) {
    return card("muted", "Shortlist triage", "",
      `Your basket is empty. Star (☆) names from a segment table, a report, or a deep-dive to build the funnel.`,
      goBtn("analyses", "Research →"));
  }
  if (!b.unresearched_count) {
    return card("ok", "Shortlist triage", `<span class="chip good">all researched</span>`,
      `All ${b.count} basket pick${b.count === 1 ? "" : "s"} have a saved analysis.`,
      goBtn("basket", "Basket →"));
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
    goBtn("basket", "Basket →"));
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

// ---- render + wiring --------------------------------------------------------
export function overviewHtml(v: Overview): string {
  const portfolio = [snapshotCard(v.snapshot, v.automation), planCard(v.plan), draftCard(v.draft),
    stagedBasketCard(v.staged_basket), journalCard(v.journal)].filter(Boolean).join("");
  const research = [basketTriageCard(v.research), queueCard(v.research),
    segmentsCard(v.research)].filter(Boolean).join("");
  return nextStepHtml(v.next_step) +
    `<div class="today-lanes">` +
    `<section class="today-lane"><div class="subhead">Portfolio</div><div class="today-cards">${portfolio}</div></section>` +
    `<section class="today-lane"><div class="subhead">Research</div><div class="today-cards">${research}</div></section>` +
    `</div>`;
}

async function loadOverview(): Promise<void> {
  const body = $("#today-body");
  const status = $("#today-status");
  if (!body) return;
  if (status) { status.textContent = ""; status.classList.remove("err"); }
  try {
    const v = await api<Overview>("/api/overview");
    body.innerHTML = overviewHtml(v);
  } catch (e) {
    if (status) { status.textContent = "Could not load the overview: " + (e as Error).message; status.classList.add("err"); }
  }
}

async function resyncHoldings(btn: HTMLButtonElement): Promise<void> {
  const status = $("#today-status");
  const prev = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Syncing…";
  if (status) {
    status.classList.remove("err");
    status.innerHTML = `<span class="spinner"></span> Re-pulling portfolio from IBKR (read-only, can take a minute)…`;
  }
  try {
    const job = await api<{ id: string }>("/api/holdings/sync", "POST", {});
    await pollDeepJob(job.id, status, async () => {
      if (status) status.textContent = "Synced.";
      await loadOverview();
    }, "IBKR sync");
  } catch (e) {
    if (status) { status.textContent = "Sync failed: " + (e as Error).message; status.classList.add("err"); }
  } finally {
    btn.disabled = false;
    btn.textContent = prev;
  }
}

let _wired = false;
function initOverview(): void {
  if (_wired) return;
  _wired = true;
  $("#today-refresh")?.addEventListener("click", () => loadOverview());
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
