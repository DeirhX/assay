import { starHtml } from "./basket";
import { $, $$, api, apiLoad, el, esc, fmtCZK, fmtSignedWeight, fmtStamp, freshnessNote, isStaleToken, nextToken, sensitive, simpleTable, state, statTile } from "./core";
import type { FundingCandidate, FundingResponse, Provenance, RebalancePlan as RebPlan, PlanRow as RebRow, PlanMember, Whatif, WhatifTrade } from "./api-types";
import { ruleWord } from "./band-viz";
import { openJournalWith } from "./journal";
import { sparkPlaceholder, hydrateSparks } from "./spark";
import { analyzeFromAnywhere } from "./ticker-nav";
import { cleanSymbol, pushNav, setActiveView } from "./shell";
import {
  clampPct, computePlan, connectorGeom, DELTA_EPS, fundingNeededCzk, inBandAfter,
  parseDelta, projectedCash, r1, rebDefaultDelta, rebScaleMax, scalePct, tradesFrom,
} from "./rebalance-model";
import type { MemberInput, RowInput, SleeveInput } from "./rebalance-model";

// ---- rebalance planner -----------------------------------------------------
// Plan/row/what-if shapes are the API contract, so they live in ./api-types as
// the single source of truth (no local shadows). All plan arithmetic lives in
// ./rebalance-model (pure, unit-tested); this module owns the DOM only.

const rebStatusClass = (s: string | null | undefined) => (s === "ABOVE" ? "bad" : s === "BELOW" ? "warn" : "good");
const rebActionClass = (a: string | null | undefined) => (a === "trim" ? "bad" : a === "buy" ? "good" : a === "review" ? "warn" : "muted");

// The applied-funding summary card (pure; exported for tests): which trims got
// filled in, from which bucket, and what each would realize tax-wise.
export function fundingCardHtml(res: FundingResponse, applied: FundingCandidate[]): string {
  const rows = applied.map((c) => {
    const isOrder = c.source === "funding_order";
    const t = c.tax;
    const tax = t && t.has_lots
      ? ((t.taxable_gain ? `taxable gain ${fmtCZK(t.taxable_gain)}` : "no taxable gain") +
         (t.exempt_proceeds ? ` · ${fmtCZK(t.exempt_proceeds)} already 3y-exempt` : ""))
      : "no lot data";
    return `<div class="reb-fund-row"><strong>${esc(c.symbol)}</strong>` +
      `<span class="chip ${isOrder ? "good" : "muted"}">${isOrder ? "funding order" : "untargeted"}</span>` +
      `<span>${sensitive(`−${fmtCZK(c.suggest_czk)} CZK`, "funding trim")}</span>` +
      `<small class="muted">${tax}</small></div>`;
  }).join("");
  const short = res.shortfall_czk || 0;
  return `<div class="whatif-card reb-fund-card">` +
    `<div class="whatif-title">Funding plan — ${applied.length} trim${applied.length === 1 ? "" : "s"} filled in</div>` +
    (rows || `<div class="hint">No candidates had headroom.</div>`) +
    (short > 0
      ? `<div class="hint bad">Still ${sensitive(`${fmtCZK(short)} CZK`, "funding shortfall")} short — funding_order and the untargeted bucket are out of headroom.</div>`
      : "") +
    `<div class="hint">Amounts were filled into the plan inputs above (band floors respected — trims stop at each name's floor). Edit to taste, then Simulate basket.</div></div>`;
}

// ---- position track --------------------------------------------------------
// A horizontal weight axis shared by every row so a 2% band and an 8% band are
// visually comparable. The track shows the target band as a shaded zone, the
// current weight as a ghost tick, and the projected weight (current + planned
// delta) as a live tick that slides as you edit the plan. Far more skimmable
// than reading three numeric columns.

// Refs into a row's track that recompute() nudges live as the plan changes.
interface PosRefs { proj: HTMLElement; conn: HTMLElement; curP: number; }

// Build the Position cell. Returns the cell plus the live-updatable bits (for
// interactive rows); sleeve rows render a static projected tick at the
// suggested amount.
function posCell(r: RebRow, scaleMax: number): { cell: HTMLElement; refs: PosRefs } {
  const cell = el("div", "reb-c reb-pos");
  const toP = (v: number) => scalePct(v, scaleMax);
  const low = typeof r.low === "number" ? r.low : 0;
  const high = typeof r.high === "number" ? r.high : low;
  const zL = toP(low);
  const zW = Math.max(1.5, toP(high) - zL);
  const curP = toP(r.current_pct);
  const defDelta = r.interactive ? rebDefaultDelta(r) : (r.suggest_delta_pct || 0);
  const projInit = (r.current_pct || 0) + defDelta;
  const projP = toP(projInit);
  const inBand0 = inBandAfter(projInit, low, high);

  const meta =
    `<span class="reb-pos-cur">${r.current_pct.toFixed(2)}%</span>` +
    `<small>${sensitive(`${fmtCZK(r.current_czk)} CZK`, "position value")}</small>` +
    `<span class="reb-pos-band">band ${low.toFixed(1)}–${high.toFixed(1)}%</span>` +
    `<span class="chip ${rebStatusClass(r.status)} reb-pos-status">${esc(r.status)}</span>`;
  const aria = `${esc(r.name)}: current ${r.current_pct.toFixed(1)}%, target band ${low.toFixed(1)} to ${high.toFixed(1)}%`;
  cell.innerHTML =
    `<div class="reb-pos-meta">${meta}</div>` +
    `<div class="reb-track" role="img" aria-label="${aria}">` +
      `<span class="reb-zone" style="left:${r1(zL)}%;width:${r1(zW)}%"></span>` +
      `<span class="reb-conn" style="left:${r1(Math.min(curP, projP))}%;width:${r1(Math.abs(projP - curP))}%"></span>` +
      `<span class="reb-cur-mark" style="left:${r1(curP)}%" title="current ${r.current_pct.toFixed(2)}%"></span>` +
      `<span class="reb-proj-mark ${inBand0 ? "in" : "out"}" style="left:${r1(projP)}%" title="projected ${projInit.toFixed(2)}%"></span>` +
    `</div>` +
    `<div class="reb-axis"><span>0%</span><span>${scaleMax}%</span></div>`;

  const proj = cell.querySelector(".reb-proj-mark") as HTMLElement;
  const conn = cell.querySelector(".reb-conn") as HTMLElement;
  return { cell, refs: { proj, conn, curP } };
}

// Server-classified thesis lean -> chip color. The add/trim vocabulary lives in
// exactly one place (tools/rebalance_overlay.py); here we only map its verdict to
// a color so the chip and the backend's conflict flag can't drift apart.
const LEAN_CLASS: Record<string, string> = { add: "good", trim: "bad", neutral: "muted" };
const thesisLean = (lean: string | null | undefined) => LEAN_CLASS[lean || "neutral"] || "muted";

// One compact line of independent research context under a target's name: a
// data-trust dot, the thesis verdict, 3-month momentum, and report freshness.
// Pure decision support — it never changes the trade math. Returns null when the
// row carries no dossier so the name reads as "no signal".
function researchLine(r: RebRow) {
  const res = r.research;
  if (!res) return null;
  const bits = [];
  const dq = res.data_quality || "INFO";
  const dqLabels: Record<string, string> = { ERROR: "data conflicts", WARN: "minor data disagreement", INFO: "data looks clean" };
  const dqTitle = dqLabels[dq] || dq;
  bits.push(`<span class="dot ${esc(dq)}" title="Data trust: ${esc(dqTitle)}"></span>`);
  if (res.thesis_action) {
    bits.push(`<span class="chip ${thesisLean(res.thesis_lean)} reb-thesis-chip" title="Your saved thesis verdict">${esc(res.thesis_action)}</span>`);
  }
  if (typeof res.momentum_3m_pct === "number") {
    const m = res.momentum_3m_pct;
    bits.push(`<span class="reb-mom ${m >= 0 ? "good" : "bad"}" title="3-month price change">${m >= 0 ? "+" : "\u2212"}${Math.abs(m).toFixed(1)}%</span>`);
  }
  const fresh = freshnessNote(res.as_of);
  if (fresh) bits.push(fresh);
  const line = el("div", "reb-research", bits.join(" "));
  // Thesis summary as the hover tooltip; the line itself stays terse.
  if (res.thesis_summary) line.title = res.thesis_summary;
  // A conflict isn't something the planner can resolve (bands are human-set), so
  // the flag doubles as a one-click escalation into the guided strategy flow,
  // which owns the human-gated path to actually change the target model.
  if (r.research_conflict) {
    const chip = el("button", "chip bad reb-conflict-chip",
      "conflict \u2192 planner");
    chip.type = "button";
    chip.title = "The suggested trade and your saved thesis disagree — open the Planner to reassess this name";
    chip.addEventListener("click", (e) => { e.stopPropagation(); escalateToStrategy(r); });
    line.appendChild(chip);
  }
  return line;
}

// A locked valuation ladder on a target row (from the deep-dive). The backend
// has already graded the move: fully blocked rows are downgraded to "wait";
// partially-triggered rows keep their action but with a scaled-down delta. This
// line reads out the gate state — what's armed, how many tranches are live, the
// active fraction, and the next trigger price. Pure annotation; the band sets
// the target delta and the ladder decides how much of it to act on now.
function priceGateLine(r: RebRow) {
  const g = r.price_gate;
  if (!g) return null;
  const ccy = g.currency ? g.currency + " " : "";
  const r2 = (v: number | null | undefined) => (typeof v === "number" ? Math.round(v * 100) / 100 : v);
  const fmt = (v: number | null | undefined) => (v == null ? "?" : ccy + r2(v));
  const pct = (f: number | null | undefined) => (f == null ? "" : Math.round(f * 100) + "%");

  // Which side does this row act on? Prefer the action, then what's blocked,
  // then whichever ladder exists.
  const focus = (r.action === "trim" || g.blocked_action === "trim") ? "trim"
    : (r.action === "buy" || g.blocked_action === "buy") ? "buy"
    : (g.buy_total ? "buy" : (g.trim_total ? "trim" : null));

  const conds = [];
  if (g.buy_below != null) {
    const waiting = g.blocked_action === "buy";
    conds.push(`<span class="reb-gate-cond ${waiting ? "warn" : "good"}">buy \u2264 ${esc(fmt(g.buy_below))}</span>`);
  }
  if (g.trim_above != null) {
    const waiting = g.blocked_action === "trim";
    conds.push(`<span class="reb-gate-cond ${waiting ? "warn" : "good"}">trim \u2265 ${esc(fmt(g.trim_above))}</span>`);
  }
  if (!conds.length) return null;

  const blocked = !!g.blocked_action;
  const partial = !!g.partial;
  const live = focus === "trim" ? g.trim_live : g.buy_live;
  const total = focus === "trim" ? g.trim_total : g.buy_total;
  const next = focus === "trim" ? g.next_trim : g.next_buy;
  const frac = g.applied_fraction;

  let label = "Armed", cls = " reb-gate-armed";
  if (blocked) { label = "\u23f3 Waiting"; cls = " reb-gate-blocked"; }
  else if (partial) { label = "\u25d4 Partial"; cls = " reb-gate-partial"; }

  const bits = [];
  if ((total ?? 0) > 1) bits.push(`tranche ${live} of ${total}`);
  if (frac != null && frac > 0 && frac < 1) bits.push(`${pct(frac)} sized`);
  if (next && next.price != null) {
    const sign = focus === "trim" ? "+" : "\u2212";
    const dp = next.distance_pct != null ? ` (${sign}${Math.round(next.distance_pct * 100)}%)` : "";
    bits.push(`next ${esc(fmt(next.price))}${dp}`);
  }
  const now = g.price_known ? `now ${esc(fmt(g.current))}` : "price unknown";
  const detail = bits.length ? ` <small class="muted reb-gate-detail">${bits.join(" \u00b7 ")}</small>` : "";
  const line = el("div", "reb-gate" + cls,
    `<span class="reb-gate-label" title="Locked valuation ladder from the deep dive — grades this trade by how much of the ladder the price unlocks">${label}:</span> ` +
    conds.join(" \u00b7 ") + detail + ` <small class="muted">(${now})</small>`);
  return line;
}

// Take an unresolved planner conflict into the guided "Direction -> Rebalance"
// flow, pre-filling a direction that names the disagreement. Phase 1 stays
// read-only: this only navigates + seeds the input; the human still drives every
// gate of the strategy run that follows.
function escalateToStrategy(r: RebRow) {
  const res = r.research || {};
  const sym = cleanSymbol(r.name) || r.name;
  const verb = r.action === "trim" ? "trimming" : r.action === "buy" ? "adding to" : "rebalancing";
  const hint = `Reassess ${sym}: the rebalance plan suggests ${verb} it, ` +
    `but my thesis says "${res.thesis_action || "the opposite"}".` +
    (res.thesis_summary ? ` ${res.thesis_summary}` : "");
  pushNav({ view: "strategy" });
  setActiveView("strategy");
  const input = $$<HTMLInputElement>("#strat-direction");
  if (input) { input.value = hint; input.focus(); }
}

async function loadRebalance() {
  const token = nextToken("rebalance");
  await apiLoad({
    path: "/api/rebalance",
    status: $$("#reb-status"),
    clear: [$$("#reb-summary"), $$("#reb-result")],
    loading: "Loading rebalance plan…",
    errorLabel: "Could not load rebalance plan",
    render: renderRebalance,
    stale: () => isStaleToken("rebalance", token),
  });
}

// Compact lineage badge for a band's name cell: pinned (your standing intent),
// legacy/stale hand-set band, or research-derived (which run/segment).
function provBadge(prov: Provenance | null | undefined) {
  if (!prov || typeof prov !== "object") return null;
  let cls: string, label: string, title: string;
  if (prov.source === "user-pin") {
    cls = "warn"; label = "pinned"; title = `pinned ${prov.stance || ""}${prov.rationale ? " — " + prov.rationale : ""}`;
  } else if (prov.source === "legacy-plan") {
    cls = "muted"; label = "legacy"; title = `hand-set band${prov.set_at ? " (" + prov.set_at + ")" : ""} — no research lineage`;
  } else if (prov.source === "strategy" || prov.source === "pipeline") {
    cls = "good"; label = prov.conviction || "research"; title = `from ${prov.source} ${prov.run_id || ""} ${prov.segment || ""}`.trim();
  } else if (prov.source === "manual") {
    cls = "muted"; label = "manual"; title = "manual edit";
  } else {
    return null;
  }
  const badge = el("span", `chip reb-prov reb-prov-${cls}`, esc(label));
  badge.title = title;
  return badge;
}

// "N uncommitted changes are staged" banner linking to the working draft. The
// planner now previews the draft itself, so the copy says so — the drift and
// suggested trades below reflect the staged (not yet committed) targets.
function stagedBannerHtml(plan: RebPlan) {
  const s = plan.staged;
  if (!s || !s.has_draft) return "";
  const n = s.pending || 0;
  return `<div class="reb-staged-banner" id="reb-staged-banner">` +
    `<span><strong>${n}</strong> pending change(s) — this planner is previewing your <em>working draft</em>, not the committed model. Commit the draft to make it live.</span>` +
    `<button class="ghost" id="reb-open-draft" type="button">Review working draft →</button>` +
    `</div>`;
}

function renderRebalance(plan: RebPlan) {
  const summary = $$("#reb-summary");
  const out = $$("#reb-result");
  const nav = plan.nav;
  const provenance: Record<string, Provenance | null | undefined> = plan.provenance || {};
  // Weights are % of invested book, so money is sized off invested value.
  const base = typeof plan.invested === "number" ? plan.invested : nav;
  out.innerHTML = "";

  summary.innerHTML =
    stagedBannerHtml(plan) +
    `<div class="reb-meta">` +
    `<span>NAV ${sensitive(`${fmtCZK(nav)} ${esc(plan.currency)}`, "total NAV")}</span>` +
    `<span>invested ${sensitive(`${fmtCZK(plan.invested)} ${esc(plan.currency)}`, "invested book")}</span>` +
    `<span>snapshot ${freshnessNote(plan.snapshot) || esc(fmtStamp(plan.snapshot))}</span>` +
    `<span>target as of ${esc(plan.as_of || "n/a")}</span>` +
    `<span>cash target ${plan.cash_target_pct}%</span>` +
    // The full trim priority is a hover detail, not a headline \u2014 the "Fund this
    // plan" button applies it for you.
    (plan.funding_order && plan.funding_order.length
      ? `<span class="reb-meta-hint" title="Funding order (trim priority): ${esc(plan.funding_order.join(" \u2192 "))}, then cash, then the untargeted bucket">funding order \u24d8</span>` : "") +
    `</div>` +
    `<div class="reb-stats">` +
    `<div class="reb-stat"><span class="reb-stat-k">Cash freed by trims</span><span class="reb-stat-v" id="reb-stat-raised">—</span>` +
      `<div class="reb-stat-bar"><span class="freed" id="reb-bar-raised"></span></div></div>` +
    `<div class="reb-stat"><span class="reb-stat-k">Cash needed for buys</span><span class="reb-stat-v" id="reb-stat-spent">—</span>` +
      `<div class="reb-stat-bar"><span class="need" id="reb-bar-spent"></span></div></div>` +
    `<div class="reb-stat"><span class="reb-stat-k">Net cash</span><span class="reb-stat-v" id="reb-stat-net">—</span>` +
      `<button class="ghost reb-fund-btn" id="reb-fund" type="button" hidden ` +
      `title="Fill in suggested trims — funding order first, then untargeted names — until the buys are covered">Fund this plan</button></div>` +
    (plan.cash
      ? `<div class="reb-stat" title="Current cash plus the plan's net CZK, as % of NAV, vs your cash target band">` +
        `<span class="reb-stat-k">Cash after plan</span><span class="reb-stat-v" id="reb-stat-cash">—</span>` +
        `<small class="reb-stat-sub" id="reb-stat-cash-sub"></small></div>`
      : "") +
    `<div class="reb-stat"><span class="reb-stat-k">Target bands closed</span><span class="reb-stat-v" id="reb-stat-closed">—</span>` +
      `<div class="reb-stat-bar"><span class="closed" id="reb-bar-closed"></span></div></div>` +
    `</div>`;

  // Nullable: the banner (and this button) only render when a working draft
  // exists, so its absence is a legitimate state — not a missing-shell error.
  const openDraft = $("#reb-open-draft");
  if (openDraft) openDraft.addEventListener("click", () => { pushNav({ view: "working-draft" }); setActiveView("working-draft"); });

  // Live-updated derived references, one per interactive row.
  interface RowCell {
    r: RebRow;
    input: HTMLInputElement;
    czk: HTMLElement;
    projPct: HTMLElement;
    projBand: HTMLElement;
    row: HTMLElement;
    pos: PosRefs;
  }
  const cells: RowCell[] = [];
  // Target-row name cells by symbol, so the async working-orders pass below can
  // badge names that already have an unfilled order at IBKR.
  const nameCells: Record<string, HTMLElement> = {};
  // Sleeve members are now editable too: each carries an input that stages a real
  // ticker trade into the basket, and the parent sleeve's projected marker tracks
  // the sum of its members' moves.
  interface MemberRef {
    symbol: string;
    input: HTMLInputElement;
    czk: HTMLElement;
    proj: HTMLElement;
    cur: number;
    target: number;
    cap: number | null;
    def: number;
  }
  interface SleeveUnit { r: RebRow; pos: PosRefs; members: MemberRef[]; }
  const sleeveUnits: SleeveUnit[] = [];
  const scaleMax = rebScaleMax(plan.rows || []);

  // One per-member recommendation row inside a sleeve's drawer: order, ticker +
  // conviction, current→target share, an editable buy/trim amount, and a live
  // projected weight. The amount defaults to the server's suggested allocation of
  // the sleeve's buy/trim so simulating "just works" — edit down to taste.
  const buildMemberRow = (m: PlanMember): { rowEl: HTMLElement; ref: MemberRef } => {
    const cur = m.current_pct || 0;
    const target = typeof m.target_pct === "number" ? m.target_pct : 0;
    const def = m.suggest_delta_pct || 0;
    const cap = typeof m.cap === "number" ? m.cap : null;
    const rowEl = el("div", "reb-mem-row" + (m.member_action ? " reb-mem-act" : ""));

    const order = el("span", "reb-mem-order", m.order ? `${m.order}` : "\u00b7");
    order.title = m.member_action ? "buy/trim order within the sleeve" : "no move suggested";

    const symWrap = el("span", "reb-mem-sym");
    const sym = el("span", "reb-link reb-member-sym", esc(m.symbol));
    sym.title = "Open dossier";
    sym.addEventListener("click", () => analyzeFromAnywhere(m.symbol));
    symWrap.appendChild(sym);
    if (m.conviction) {
      const cc = String(m.conviction).toLowerCase();
      const cls = cc === "high" ? "good" : cc === "low" ? "warn" : "muted";
      symWrap.appendChild(el("span", `chip reb-mem-conv ${cls}`, esc(cc)));
    }

    const curCell = el("span", "reb-mem-cur",
      `<span>${cur.toFixed(2)}%</span>` +
      `<span class="reb-mem-arrow">\u2192</span>` +
      `<span class="reb-mem-tgt">${target.toFixed(2)}%` +
      (cap != null ? ` <small title="member cap">\u2264${cap.toFixed(1)}</small>` : "") +
      `</span>`);

    const planCell = el("div", "reb-mem-plan");
    const wrap = el("div", "reb-plan-input-wrap");
    const input = el("input", "reb-plan-input") as HTMLInputElement;
    input.type = "number";
    input.step = "0.1";
    input.value = String(r1(def));
    input.title = m.member_action
      ? `suggested ${fmtSignedWeight(def)} toward its ${target.toFixed(1)}% share`
      : "at or above its share — no buy suggested; type an amount to stage one anyway";
    wrap.appendChild(input);
    wrap.appendChild(el("span", "reb-unit", "%"));
    planCell.appendChild(wrap);
    const czk = el("small", "reb-plan-czk");
    planCell.appendChild(czk);

    const proj = el("span", "reb-mem-proj");

    rowEl.appendChild(order);
    rowEl.appendChild(symWrap);
    rowEl.appendChild(curCell);
    rowEl.appendChild(planCell);
    rowEl.appendChild(proj);

    const ref: MemberRef = { symbol: m.symbol, input, czk, proj, cur, target, cap, def };
    input.addEventListener("input", recompute);
    return { rowEl, ref };
  };

  const headRow = (title: string) => {
    const h = el("div", "reb-row reb-head-row");
    h.innerHTML =
      `<div class="reb-c reb-name">${esc(title)}</div>` +
      `<div class="reb-c reb-pos">Current · band · status</div>` +
      `<div class="reb-c reb-plan">Plan (% of book)</div>` +
      `<div class="reb-c reb-proj">Projected</div>`;
    return h;
  };

  const buildRow = (r: RebRow) => {
    const row = el("div", "reb-row reb-data-row");
    const sym = el("span", "reb-sym", esc(r.name));
    // Every single name opens its dossier — held or not (a not-yet-owned buy
    // candidate is exactly what you want to research). Sleeves are baskets, not
    // one ticker, so their row header stays non-clickable (members link below).
    if (r.kind === "target") {
      sym.classList.add("reb-link");
      sym.title = r.held ? "Open dossier" : "Open dossier (not held yet)";
      sym.addEventListener("click", () => analyzeFromAnywhere(r.name));
    }
    const nameCell = el("div", "reb-c reb-name");
    // Star only single tickers — a sleeve row is itself a basket, not one name.
    if (r.kind === "target") nameCell.insertAdjacentHTML("beforeend", starHtml(r.name, "rebalance"));
    nameCell.appendChild(sym);
    nameCell.appendChild(el("span", "reb-rule", esc(ruleWord(r.rule) || r.rule)));
    // A single-name row gets a cached-only trend cue; sleeves are baskets, no
    // one price to spark. Filled by the batch hydrateSparks() call after render.
    if (r.kind === "target") nameCell.insertAdjacentHTML("beforeend", sparkPlaceholder(r.name));
    if (r.kind === "target") nameCells[cleanSymbol(r.name)] = nameCell;
    const prov = provBadge(provenance[r.kind === "sleeve" ? `[${r.name}]` : r.name]);
    if (prov) nameCell.appendChild(prov);
    if (r.note) nameCell.title = r.note;
    const research = researchLine(r);
    if (research) {
      nameCell.appendChild(research);
      if (r.research_conflict) row.classList.add("reb-conflict");
    }
    const gate = priceGateLine(r);
    if (gate) {
      nameCell.appendChild(gate);
      if (r.price_gate && r.price_gate.blocked_action) row.classList.add("reb-gated");
    }

    const { cell: posC, refs: posRefs } = posCell(r, scaleMax);

    row.appendChild(nameCell);
    row.appendChild(posC);

    if (r.interactive) {
      const planCell = el("div", "reb-c reb-plan");
      const wrap = el("div", "reb-plan-input-wrap");
      const input = el("input", "reb-plan-input");
      input.type = "number";
      input.step = "0.1";
      input.value = String(rebDefaultDelta(r));
      input.title = r.action === "wait"
        ? `gated by a price trigger — would ${r.suggest_delta_pct >= 0 ? "buy" : "trim"} ${fmtSignedWeight(r.suggest_delta_pct)} on the band; type an amount to override the trigger`
        : r.action
          ? `suggested ${fmtSignedWeight(r.suggest_delta_pct)} to reach the band edge`
          : "in band — no action suggested";
      wrap.appendChild(input);
      wrap.appendChild(el("span", "reb-unit", "%"));
      planCell.appendChild(wrap);
      const czk = el("small", "reb-plan-czk");
      planCell.appendChild(czk);
      row.appendChild(planCell);

      const projCell = el("div", "reb-c reb-proj");
      const projPct = el("span", "reb-proj-pct");
      const projBand = el("span", "chip reb-proj-band");
      projCell.appendChild(projPct);
      projCell.appendChild(projBand);
      row.appendChild(projCell);

      cells.push({ r, input, czk, projPct, projBand, row, pos: posRefs });
      input.addEventListener("input", recompute);
    } else {
      // Sleeve: combined band sized across members. The per-member breakdown +
      // editable amounts live in the expandable drawer below this row.
      const planCell = el("div", "reb-c reb-plan reb-plan-ro",
        (r.action
          ? `<span class="chip ${rebActionClass(r.action)}">${fmtSignedWeight(r.suggest_delta_pct)}</span>`
          : `<span class="muted">in band</span>`) +
        `<small>across members ↓</small>`);
      row.appendChild(planCell);
      row.appendChild(el("div", "reb-c reb-proj", "<span class=\"muted\">—</span>"));
    }
    return { row, pos: posRefs };
  };

  const targetRows = (plan.rows || []).filter((r) => r.kind === "target");
  const sleeveRows = (plan.rows || []).filter((r) => r.kind === "sleeve");

  // Every filterable row-group, tagged with the facets the omnifilter slices on:
  // status, suggested action, conviction (confidence) and the ticker text.
  interface FilterItem { group: HTMLElement; name: string; status: string; action: string; conv: string; }
  const filterItems: FilterItem[] = [];
  const convOf = (r: RebRow): string => {
    const key = r.kind === "sleeve" ? `[${r.name}]` : r.name;
    const c = String((provenance[key] || {}).conviction || "").toLowerCase();
    return c === "high" || c === "medium" || c === "low" ? c : "none";
  };

  const grid = el("div", "reb-tbl");
  grid.appendChild(headRow("Targets"));
  targetRows.forEach((r) => {
    // Row + its tax-lot drawer live in one group so the drawer reads as an
    // expandable part of the ticker, not a separator floating between names.
    const group = el("div", "reb-row-group");
    group.appendChild(buildRow(r).row);
    const tax = taxDetails(r);
    if (tax) { group.classList.add("has-tax"); group.appendChild(tax); }
    grid.appendChild(group);
    filterItems.push({ group, name: r.name.toLowerCase(), status: r.status, action: r.action || "none", conv: convOf(r) });
  });
  out.appendChild(grid);

  let sgrid: HTMLElement | null = null;
  if (sleeveRows.length) {
    const sg = el("div", "reb-tbl reb-tbl-sleeves");
    sgrid = sg;
    sg.appendChild(headRow("Sleeves"));
    sleeveRows.forEach((r) => {
      const group = el("div", "reb-row-group reb-sleeve-group");
      const built = buildRow(r);
      group.appendChild(built.row);
      filterItems.push({ group, name: r.name.toLowerCase(), status: r.status, action: r.action || "none", conv: convOf(r) });
      const members = r.members || [];
      if (members.length) {
        const det = el("details", "reb-members");
        // Open by default when there's something to do, so the per-name plan is
        // visible rather than hidden behind a click.
        det.open = !!r.action;
        const held = members.filter((m) => m.current_pct > 0).length;
        const actN = members.filter((m) => m.member_action).length;
        const verb = r.action === "trim" ? "trim" : r.action === "buy" ? "buy" : null;
        const hint = verb
          ? `<span class="reb-mem-sum-act ${rebActionClass(r.action)}">${verb} ${fmtSignedWeight(r.suggest_delta_pct)} \u00b7 ${actN} name${actN === 1 ? "" : "s"}</span>`
          : `<span class="muted">in band \u2014 hold</span>`;
        det.appendChild(el("summary", "reb-mem-summary",
          `<span><strong>${members.length}</strong> members \u00b7 ${held} held</span>${hint}`));

        const ml = el("div", "reb-members-list");
        ml.appendChild(el("div", "reb-mem-row reb-mem-head",
          `<span class="reb-mem-order" title="suggested order">#</span>` +
          `<span class="reb-mem-sym">Ticker</span>` +
          `<span class="reb-mem-cur">Current \u2192 target</span>` +
          `<span class="reb-mem-plan">Buy / trim</span>` +
          `<span class="reb-mem-proj">Projected</span>`));

        const unit: SleeveUnit = { r, pos: built.pos, members: [] };
        // Render in the server's suggested order (biggest move first).
        members.slice().sort((a, b) => (a.order || 99) - (b.order || 99)).forEach((m) => {
          const built2 = buildMemberRow(m);
          ml.appendChild(built2.rowEl);
          unit.members.push(built2.ref);
        });
        sleeveUnits.push(unit);
        det.appendChild(ml);
        group.appendChild(det);
      }
      sg.appendChild(group);
    });
    out.appendChild(sg);
  }

  // Untargeted names carry no band/action/conviction, so the omnifilter only
  // text-matches them; an active categorical facet hides the whole section.
  // Each row still carries an editable amount: the model's own docs say this
  // bucket funds the plan, so its trims must be stageable like any other.
  const untargetedItems: { row: HTMLElement; name: string }[] = [];
  interface UntargetedCell { symbol: string; input: HTMLInputElement; czk: HTMLElement; }
  const untargetedCells: UntargetedCell[] = [];
  let untargetedDet: HTMLDetailsElement | null = null;
  if (plan.untargeted && plan.untargeted.length) {
    const det = el("details", "reb-untargeted") as HTMLDetailsElement;
    untargetedDet = det;
    det.appendChild(el("summary", undefined,
      `Untargeted holdings — ${plan.untargeted.length} names, ` +
      `${plan.untargeted_pct.toFixed(1)}% of NAV (no band; candidate funding)`));
    const list = el("div", "reb-untargeted-list");
    // Shared scale so the fill behind each name reads as its relative size — the
    // 6% names stand out from the 0.x% dust at a glance.
    const uMax = Math.max(0.01, ...plan.untargeted.map((u) => Math.max(0, u.current_pct || 0)));
    plan.untargeted.forEach((u) => {
      const r = el("div", "reb-untargeted-row");
      const w = clampPct((Math.max(0, u.current_pct || 0) / uMax) * 100);
      r.style.setProperty("--w", `${r1(w)}%`);
      r.innerHTML =
        `<span class="reb-link reb-member-sym">${esc(u.symbol)}</span>` +
        `<span>${u.current_pct.toFixed(2)}%</span>` +
        `<small>${sensitive(`${fmtCZK(u.current_czk)} CZK`, "position value")}</small>`;
      r.querySelector(".reb-link")?.addEventListener("click", () => analyzeFromAnywhere(u.symbol));
      const wrap = el("div", "reb-plan-input-wrap");
      const input = el("input", "reb-plan-input") as HTMLInputElement;
      input.type = "number";
      input.step = "0.1";
      input.value = "0";
      input.title = "No band governs this name — type a trim (negative) to fund the plan, or use “Fund this plan”";
      wrap.appendChild(input);
      wrap.appendChild(el("span", "reb-unit", "%"));
      r.appendChild(wrap);
      const czk = el("small", "reb-plan-czk");
      r.appendChild(czk);
      input.addEventListener("input", recompute);
      untargetedCells.push({ symbol: u.symbol, input, czk });
      list.appendChild(r);
      untargetedItems.push({ row: r, name: String(u.symbol || "").toLowerCase() });
    });
    det.appendChild(list);
    out.appendChild(det);
  }

  // The mechanics explainer collapses like the view-header help: it matters the
  // first few visits, then it's noise under the plan.
  const mech = el("details", "view-help");
  mech.innerHTML = `<summary>How amounts, cash, and funding work</summary>` +
    `<div class="hint">Suggested amounts move each name to the nearest band edge (the minimal action). ` +
    `Edit any Plan amount to simulate; “Reset to suggested” restores them. ` +
    `Cash totals include the sleeves' suggested buys/sells (fixed — you allocate those across members). ` +
    `Net cash &gt; 0 means trims fund the buys; &lt; 0 means you'd need fresh cash — ` +
    `“Fund this plan” fills suggested trims (funding order first, then untargeted names) to cover it.</div>`;
  out.appendChild(mech);

  // Unfilled orders already working at IBKR are part of the current state a
  // suggestion must be judged against - badge the rows that have one. Silent
  // no-op when trading is disabled or the gateway is down.
  void api<{ orders?: Array<{ ticker?: string; symbol?: string; side?: string; orderDesc?: string; remainingQuantity?: number | string; status?: string }> }>("/api/trade/orders")
    .then((res) => {
      (res.orders || []).forEach((o) => {
        const osym = cleanSymbol(o.ticker || o.symbol);
        const cell = nameCells[osym];
        if (!osym || !cell || cell.querySelector(".reb-working")) return;
        const chip = el("span", "chip warn reb-working", "⏳ order working");
        chip.title = (o.orderDesc || `${o.side || ""} ${o.remainingQuantity ?? ""} ${osym} ${o.status || "working"}`.trim()) +
          " — an unfilled order at IBKR already moves this name; check the Trade tab before staging more";
        cell.appendChild(chip);
      });
    })
    .catch(() => { /* trading disabled / gateway offline - no badges */ });

  // ---- omnifilter: ticker search + status / action / confidence facets -----
  // Pills within a facet OR together; facets AND together; the search box ANDs
  // a substring match on the ticker. Untargeted names have no facets, so they
  // only react to the search and hide entirely when a facet is active.
  const FILTER_FACETS: { key: "status" | "action" | "conv"; label: string; opts: [string, string][] }[] = [
    { key: "status", label: "Status", opts: [["BELOW", "below"], ["IN", "in band"], ["ABOVE", "above"]] },
    { key: "action", label: "Action", opts: [["buy", "buy"], ["trim", "trim"], ["review", "review"], ["wait", "wait"], ["none", "no action"]] },
    { key: "conv", label: "Confidence", opts: [["high", "high"], ["medium", "medium"], ["low", "low"], ["none", "none"]] },
  ];
  const filterBar = el("div", "reb-filter");
  // The search box + live count stay one line; the three facet-pill rows tuck
  // behind a "Filters" expander so the default view is a single quiet strip.
  // The expander auto-opens whenever a facet is active so the state that's
  // hiding rows is never itself hidden.
  filterBar.innerHTML =
    `<div class="reb-filter-top">` +
      `<div class="reb-filter-search">` +
        `<input type="search" id="reb-filter-q" placeholder="Filter by ticker…" autocomplete="off" spellcheck="false">` +
      `</div>` +
      `<span class="reb-filter-count" id="reb-filter-count"></span>` +
      `<button type="button" class="ghost reb-filter-clear" id="reb-filter-clear">Clear</button>` +
    `</div>` +
    `<details class="reb-filter-facets" id="reb-filter-facets">` +
    `<summary>Filters <small class="muted">status · action · confidence</small></summary>` +
    FILTER_FACETS.map((f) =>
      `<div class="reb-filter-row"><span class="reb-filter-label">${f.label}</span>` +
      `<div class="reb-filter-pills" data-facet="${f.key}">` +
      f.opts.map(([v, l]) => `<button type="button" class="reb-fpill" data-val="${esc(v)}">${esc(l)}</button>`).join("") +
      `</div></div>`).join("") +
    `</details>`;
  out.prepend(filterBar);

  const qInput = filterBar.querySelector("#reb-filter-q") as HTMLInputElement;
  const countEl = filterBar.querySelector("#reb-filter-count") as HTMLElement;
  const selectedVals = (facet: string): string[] =>
    [...filterBar.querySelectorAll(`[data-facet="${facet}"] .reb-fpill.on`)].map((b) => (b as HTMLElement).dataset.val || "");

  const syncSection = (g: HTMLElement | null) => {
    if (!g) return;
    const groups = [...g.querySelectorAll(".reb-row-group")];
    const vis = groups.some((x) => (x as HTMLElement).style.display !== "none");
    const head = g.querySelector(".reb-head-row") as HTMLElement | null;
    if (head) head.style.display = vis ? "" : "none";
    g.style.display = vis ? "" : "none";
  };

  function applyFilter() {
    const q = qInput.value.trim().toLowerCase();
    const st = selectedVals("status"), ac = selectedVals("action"), cv = selectedVals("conv");
    const catActive = !!(st.length || ac.length || cv.length);
    let shown = 0;
    filterItems.forEach((it) => {
      let ok = true;
      if (q && !it.name.includes(q)) ok = false;
      if (ok && st.length && !st.includes(it.status)) ok = false;
      if (ok && ac.length && !ac.includes(it.action)) ok = false;
      if (ok && cv.length && !cv.includes(it.conv)) ok = false;
      it.group.style.display = ok ? "" : "none";
      if (ok) shown += 1;
    });
    syncSection(grid);
    syncSection(sgrid);

    let uShown = 0;
    if (untargetedDet) {
      if (catActive) {
        untargetedDet.style.display = "none";
      } else {
        untargetedItems.forEach((u) => {
          const ok = !q || u.name.includes(q);
          u.row.style.display = ok ? "" : "none";
          if (ok) uShown += 1;
        });
        untargetedDet.style.display = uShown ? "" : "none";
      }
    }

    const active = !!q || catActive;
    const tail = (!catActive && untargetedDet && untargetedItems.length) ? ` · ${uShown} untargeted` : "";
    countEl.textContent = active ? `${shown} of ${filterItems.length} plan names${tail}` : `${filterItems.length} plan names`;
    countEl.classList.toggle("active", active);
    filterBar.classList.toggle("filtering", active);
    // Never let an active facet hide inside a collapsed expander.
    if (catActive) {
      const det = filterBar.querySelector<HTMLDetailsElement>("#reb-filter-facets");
      if (det) det.open = true;
    }
  }

  filterBar.querySelectorAll(".reb-fpill").forEach((b) => {
    b.addEventListener("click", () => { b.classList.toggle("on"); applyFilter(); });
  });
  qInput.addEventListener("input", applyFilter);
  filterBar.querySelector("#reb-filter-clear")!.addEventListener("click", () => {
    qInput.value = "";
    filterBar.querySelectorAll(".reb-fpill.on").forEach((b) => b.classList.remove("on"));
    applyFilter();
  });
  applyFilter();

  // Slide a track's projected tick + redraw the current→projected connector.
  const paintTrack = (pos: PosRefs, proj: number, inBand: boolean, delta: number) => {
    const projP = scalePct(proj, scaleMax);
    const geom = connectorGeom(pos.curP, projP);
    pos.proj.style.left = `${r1(projP)}%`;
    pos.proj.title = `projected ${proj.toFixed(2)}%`;
    pos.proj.classList.toggle("in", inBand);
    pos.proj.classList.toggle("out", !inBand);
    pos.conn.style.left = `${r1(geom.left)}%`;
    pos.conn.style.width = `${r1(geom.width)}%`;
    pos.conn.classList.toggle("buy", delta > DELTA_EPS);
    pos.conn.classList.toggle("sell", delta < -DELTA_EPS);
  };

  const plannedCzkHtml = (delta: number, czkAmount: number | null, empty: string) =>
    (delta
      ? sensitive(`${delta > 0 ? "+" : "−"}${fmtCZK(Math.abs(czkAmount || 0))} CZK`, "planned trade size")
      : empty);

  function recompute() {
    // All plan arithmetic lives in the pure model (rebalance-model.ts); this
    // function only reads the edited inputs and paints the computed results.
    const rowInputs: RowInput[] = cells.map(({ r, input }) =>
      ({ current: r.current_pct, low: r.low, high: r.high, delta: parseDelta(input.value) }));
    const sleeveInputs: SleeveInput[] = sleeveUnits.map(({ r, members }) => ({
      current: r.current_pct, low: r.low, high: r.high,
      members: members.map((mc): MemberInput =>
        ({ cur: mc.cur, target: mc.target, cap: mc.cap, delta: parseDelta(mc.input.value) })),
    }));
    const comp = computePlan(rowInputs, sleeveInputs,
      untargetedCells.map((uc) => parseDelta(uc.input.value)), base);

    comp.rows.forEach((res, i) => {
      const { czk, projPct, projBand, row, pos } = cells[i];
      czk.innerHTML = plannedCzkHtml(res.delta, res.czk, "<span class=\"muted\">no change</span>");
      projPct.textContent = `${res.proj.toFixed(2)}%`;
      projBand.textContent = res.inBand ? "in band" : "out";
      projBand.className = "chip reb-proj-band " + (res.inBand ? "good" : "warn");
      row.classList.toggle("planned-sell", res.delta < -DELTA_EPS);
      row.classList.toggle("planned-buy", res.delta > DELTA_EPS);
      paintTrack(pos, res.proj, res.inBand, res.delta);
    });

    // Sleeve members are editable too, so the headline sums the per-member
    // amounts (which default to the server's split of the sleeve buy/trim). Each
    // sleeve's projected marker slides to current + the sum of its members' moves
    // so the aggregate band still reads true.
    comp.sleeves.forEach((sres, i) => {
      const unit = sleeveUnits[i];
      sres.members.forEach((mres, j) => {
        const mc = unit.members[j];
        mc.czk.innerHTML = plannedCzkHtml(mres.delta, mres.czk, "<span class=\"muted\">no change</span>");
        mc.proj.innerHTML = `${mres.proj.toFixed(2)}%` +
          (mres.overCap ? ` <span class="chip warn" title="over its member cap">cap</span>` : "");
        mc.proj.classList.toggle("good", mres.atTarget);
      });
      paintTrack(unit.pos, sres.proj, sres.inBand, sres.sum);
    });

    // Untargeted names have no band to project against; their edited amounts
    // still move the cash math (that's their whole role: funding).
    comp.untargeted.forEach((ures, i) => {
      untargetedCells[i].czk.innerHTML = plannedCzkHtml(ures.delta, ures.czk, "");
    });

    const { raised, spent, net, closed, total, raisedCzk, spentCzk, netCzk, fundMax } = comp.totals;
    $$("#reb-stat-raised").innerHTML =
      `${sensitive(`${fmtCZK(raisedCzk)} CZK`, "cash freed")} <small>${raised.toFixed(2)}%</small>`;
    $$("#reb-stat-spent").innerHTML =
      `${sensitive(`${fmtCZK(spentCzk)} CZK`, "cash needed")} <small>${spent.toFixed(2)}%</small>`;
    const netEl = $$("#reb-stat-net");
    netEl.innerHTML =
      `${sensitive(`${net >= 0 ? "+" : "\u2212"}${fmtCZK(Math.abs(netCzk ?? 0))} CZK`, "net cash")} ` +
      `<small>${fmtSignedWeight(net)}</small>`;
    netEl.classList.toggle("good", net >= -0.01);
    netEl.classList.toggle("bad", net < -0.01);
    // Offer funding only when the plan genuinely needs fresh cash (beyond the
    // headroom above the cash floor).
    const fundBtn = $$<HTMLButtonElement>("#reb-fund");
    if (fundBtn) {
      const needed = fundingNeededCzk(netCzk, plan.cash);
      fundBtn.hidden = needed <= 0;
      fundBtn.dataset.needed = String(needed);
    }
    const closedEl = $$("#reb-stat-closed");
    closedEl.textContent = `${closed}/${total}`;
    closedEl.classList.toggle("good", total > 0 && closed === total);

    // Cash after plan: slide with every edit so a basket that breaches the
    // cash floor announces itself before it's ever simulated or staged.
    const cashEl = $$("#reb-stat-cash");
    const proj = projectedCash(plan.cash, netCzk);
    if (cashEl && proj && plan.cash) {
      cashEl.innerHTML =
        `${sensitive(`${fmtCZK(Math.round(proj.czk))} ${esc(plan.currency)}`, "projected cash")} ` +
        `<small>${proj.pct.toFixed(1)}% of NAV</small>`;
      cashEl.classList.remove("good", "warn", "bad");
      cashEl.classList.add(proj.cls);
      const sub = $$("#reb-stat-cash-sub");
      if (sub) {
        sub.textContent = proj.cls === "good"
          ? `in the ${plan.cash.low}–${plan.cash.high}% target band`
          : proj.cls === "bad"
            ? `under your ${plan.cash.low}% cash floor (target ${plan.cash.target_pct}%)`
            : `above the ${plan.cash.high}% band ceiling (target ${plan.cash.target_pct}%)`;
      }
    }

    // Funding bars: freed vs needed on a shared scale, so a glance shows whether
    // trims cover the buys (freed ≥ needed) or you'd need fresh cash.
    const barRaised = $$("#reb-bar-raised");
    const barSpent = $$("#reb-bar-spent");
    const barClosed = $$("#reb-bar-closed");
    if (barRaised) barRaised.style.width = `${r1((raised / fundMax) * 100)}%`;
    if (barSpent) barSpent.style.width = `${r1((spent / fundMax) * 100)}%`;
    if (barClosed) barClosed.style.width = `${r1(total > 0 ? (closed / total) * 100 : 0)}%`;
  }

  const reset = $$("#reb-reset");
  if (reset) {
    reset.onclick = () => {
      cells.forEach(({ r, input }) => { input.value = String(rebDefaultDelta(r)); });
      sleeveUnits.forEach(({ members }) => members.forEach((mc) => { mc.input.value = String(r1(mc.def)); }));
      untargetedCells.forEach((uc) => { uc.input.value = "0"; });
      recompute();
    };
  }

  // "Fund this plan": ask the server which names to trim (funding_order first,
  // then untargeted, floors respected, tax-annotated), fill the amounts into
  // the same editable inputs as any hand edit, and summarise what was applied.
  const nonZero = (input: HTMLInputElement) => Math.abs(parseDelta(input.value)) > DELTA_EPS;
  const fundBtnEl = $$<HTMLButtonElement>("#reb-fund");
  if (fundBtnEl) {
    fundBtnEl.onclick = async () => {
      const needed = parseInt(fundBtnEl.dataset.needed || "0", 10);
      if (!needed) return;
      // Names the user is already trading keep their edits — never overwrite.
      const exclude: string[] = [];
      cells.forEach(({ r, input }) => { if (nonZero(input)) exclude.push(r.name); });
      sleeveUnits.forEach(({ members }) => members.forEach((mc) => { if (nonZero(mc.input)) exclude.push(mc.symbol); }));
      untargetedCells.forEach((uc) => { if (nonZero(uc.input)) exclude.push(uc.symbol); });
      const box = $$("#reb-whatif");
      fundBtnEl.disabled = true;
      box.innerHTML = `<div class="status">Finding funding…</div>`;
      try {
        const res = await api<FundingResponse>("/api/rebalance/funding", "POST",
          { needed_czk: needed, exclude });
        const byName: Record<string, HTMLInputElement> = {};
        cells.forEach((c) => { byName[c.r.name] = c.input; });
        untargetedCells.forEach((uc) => { byName[uc.symbol] = uc.input; });
        const applied: FundingCandidate[] = [];
        (res.candidates || []).forEach((c) => {
          const input = byName[c.symbol];
          if (!input || !c.suggest_pct) return;
          input.value = String(r1(c.suggest_pct));
          applied.push(c);
        });
        if (untargetedDet && applied.some((c) => c.source === "untargeted")) untargetedDet.open = true;
        recompute();
        box.innerHTML = fundingCardHtml(res, applied);
      } catch (e) {
        box.innerHTML = `<div class="status err">Funding lookup failed: ${esc((e as Error).message)}</div>`;
      } finally {
        fundBtnEl.disabled = false;
      }
    };
  }

  const simBtn = $$<HTMLButtonElement>("#reb-simulate");
  if (simBtn) {
    simBtn.onclick = async () => {
      // Every edited amount, from all three sections, in planner order. Sleeve
      // members stage as real ticker trades — the simulator recomputes the
      // sleeve aggregate from them — and untargeted funding trims are trades
      // like any other; the noise-floor / CZK filtering lives in tradesFrom.
      const entries: { symbol: string; delta: number }[] = [];
      cells.forEach(({ r, input }) => entries.push({ symbol: r.name, delta: parseDelta(input.value) }));
      sleeveUnits.forEach(({ members }) => members.forEach((mc) =>
        entries.push({ symbol: mc.symbol, delta: parseDelta(mc.input.value) })));
      untargetedCells.forEach((uc) => entries.push({ symbol: uc.symbol, delta: parseDelta(uc.input.value) }));
      const trades: WhatifTrade[] = tradesFrom(entries, base);
      // Share the staged basket with the Trade desk so it can preview/place the
      // exact same trades you just simulated here. Persisted server-side too, so
      // it survives a reload / navigation instead of living only in this tab.
      state.stagedBasket = trades.slice();
      void api("/api/trade/basket", "POST", { trades }).catch(() => { /* best-effort */ });
      const box = $$("#reb-whatif");
      if (!trades.length) {
        box.innerHTML = `<div class="hint">Nothing staged — edit a Plan amount on a targeted name or a sleeve member, then simulate.</div>`;
        return;
      }
      box.innerHTML = `<div class="status">Simulating…</div>`;
      simBtn.disabled = true;
      try {
        const wf = await api("/api/whatif", "POST", { trades });
        renderWhatif(wf);
      } catch (e) {
        box.innerHTML = `<div class="status err">Simulation failed: ${esc((e as Error).message)}</div>`;
      } finally {
        simBtn.disabled = false;
      }
    };
  }

  recompute();
  // One batch /api/spark call fills every target row's trend cue; cached-only,
  // so names without a dossier just leave an empty slot.
  void hydrateSparks(out);
}

// ---- what-if "after" panel -------------------------------------------------
const whatifStat = (label: string, valueHtml: string, cls?: string) => statTile(label, valueHtml, { cls, html: true });

function renderWhatif(wf: Whatif) {
  const box = $$("#reb-whatif");
  box.innerHTML = "";
  const s = wf.summary || {};
  const ccy = wf.currency;
  const card = el("div", "whatif-card");
  card.appendChild(el("div", "whatif-title", `Projected portfolio after ${(wf.trades || []).length} trade(s)`));

  const stats = el("div", "reb-stats");
  stats.appendChild(whatifStat("Bands in-band",
    `${s.bands_in_before} \u2192 ${s.bands_in_after} / ${s.bands_total}`,
    (s.bands_in_after ?? 0) >= (s.bands_in_before ?? 0) ? "good" : "bad"));
  const cashAfter = wf.cash ? wf.cash.after : null;
  // Grade against the cash target band when the server computed one; a basket
  // that dips under the cash floor reads amber/red even while cash stays > 0.
  const ct = wf.cash && wf.cash.target;
  const cashCls = cashAfter == null ? "muted"
    : cashAfter < 0 ? "bad"
    : ct ? (ct.status_after === "BELOW" ? "bad" : ct.status_after === "ABOVE" ? "warn" : "good")
    : "good";
  stats.appendChild(whatifStat("Cash after",
    cashAfter == null ? "n/a"
      : sensitive(`${fmtCZK(cashAfter)} ${esc(ccy)}`, "cash after") +
        (ct ? ` <small>${ct.after_pct.toFixed(1)}% of NAV · target ${ct.target_pct}%</small>` : ""),
    cashCls));
  stats.appendChild(whatifStat("Net cash",
    sensitive(`${(s.net_cash_czk ?? 0) >= 0 ? "+" : "\u2212"}${fmtCZK(Math.abs(s.net_cash_czk ?? 0))} ${esc(ccy)}`, "net cash"),
    (s.net_cash_czk ?? 0) >= 0 ? "good" : "bad"));
  stats.appendChild(whatifStat("Realized taxable gain",
    sensitive(`${fmtCZK(s.realized_taxable_gain_czk)} ${esc(ccy)}`, "taxable gain"),
    (s.realized_taxable_gain_czk ?? 0) > 0 ? "warn" : "good"));
  // Concentration/diversification delta: risk.py's lens on the *decision*, not
  // just the destination view. A rise in top-5 or a fall in effective names reads
  // amber; the deeper correlation-aware numbers stay on the Risk page.
  const risk = wf.risk;
  const t5 = risk && risk.top5_pct;
  if (t5 && typeof t5.before === "number" && typeof t5.after === "number") {
    stats.appendChild(whatifStat("Top-5 concentration",
      `${t5.before.toFixed(1)}% \u2192 ${t5.after.toFixed(1)}%`,
      t5.after > t5.before + 0.05 ? "warn" : "good"));
  }
  const en = risk && risk.effective_names;
  if (en && typeof en.before === "number" && typeof en.after === "number") {
    stats.appendChild(whatifStat("Effective names",
      `${en.before.toFixed(1)} \u2192 ${en.after.toFixed(1)}`,
      en.after < en.before - 0.05 ? "warn" : "good"));
  }
  card.appendChild(stats);
  (risk && risk.warnings || []).forEach((w) =>
    card.appendChild(el("div", "whatif-risk-warn", `\u26a0 ${esc(w)}`)));

  const afterRows: Record<string, RebRow> = {};
  // A member trade (e.g. XSD) doesn't have its own row — it rolls up into its
  // sleeve, so map members to the sleeve row to show the sleeve's after band.
  const sleeveByMember: Record<string, RebRow> = {};
  ((wf.after && wf.after.rows) || []).forEach((r) => {
    if (r.kind === "target") afterRows[r.name] = r;
    else if (r.kind === "sleeve" && r.members) r.members.forEach((m) => { sleeveByMember[m.symbol] = r; });
  });
  card.appendChild(simpleTable({
    className: "whatif-table",
    head: `<tr><th>Name</th><th class="num">Trade</th><th>Before</th><th>After</th><th class="num">After weight</th></tr>`,
    rows: wf.trades || [],
    cells: (t: { symbol: string; delta_czk: number }) => {
      const ar = afterRows[t.symbol];
      const sleeve = ar ? null : sleeveByMember[t.symbol];
      const status = ar || sleeve;
      const before = (wf.before_status && wf.before_status[t.symbol]) || "\u2014";
      const nameCell = sleeve
        ? `${esc(t.symbol)} <small class="muted">\u2192 ${esc(sleeve.name)}</small>`
        : esc(t.symbol);
      return `<td>${nameCell}</td>` +
        `<td class="num">${sensitive(`${t.delta_czk >= 0 ? "+" : "\u2212"}${fmtCZK(Math.abs(t.delta_czk))}`, "trade size")}</td>` +
        `<td><span class="chip ${rebStatusClass(before)}">${esc(before)}</span></td>` +
        `<td>${status ? `<span class="chip ${rebStatusClass(status.status)}">${esc(status.status)}</span>` : "\u2014"}</td>` +
        `<td class="num">${status ? status.current_pct.toFixed(2) + "%" : "\u2014"}</td>`;
    },
  }));

  const tt = wf.tax && wf.tax.totals;
  if (tt && (tt.proceeds || tt.taxable_gain || tt.exempt_proceeds || tt.harvestable_loss)) {
    card.appendChild(el("div", "hint",
      `Realized: ${sensitive(`${fmtCZK(tt.proceeds)}`, "proceeds")} ${esc(ccy)} proceeds · ` +
      `${sensitive(`${fmtCZK(tt.exempt_proceeds)}`, "exempt")} already 3y-exempt · ` +
      `taxable gain ${sensitive(`${fmtCZK(tt.taxable_gain)}`, "taxable gain")} · ` +
      `harvestable loss ${sensitive(`${fmtCZK(tt.harvestable_loss)}`, "harvest")}`));
  }
  (wf.caveats || []).forEach((c) => card.appendChild(el("div", "hint", esc(c))));

  const actions = el("div", "thesis-actions");
  const logBtn = el("button", "ghost", "Log to journal");
  logBtn.type = "button";
  logBtn.addEventListener("click", () => {
    const trade: Partial<WhatifTrade> = (wf.trades && wf.trades[0]) || {};
    const summary = (wf.trades || [])
      .map((t) => `${t.symbol} ${t.delta_czk >= 0 ? "+" : "\u2212"}${fmtCZK(Math.abs(t.delta_czk))}`)
      .join(", ");
    openJournalWith({
      symbol: trade.symbol || "",
      action: (trade.delta_czk ?? 0) < 0 ? "trim" : "buy",
      size_czk: trade.delta_czk != null ? Math.abs(trade.delta_czk) : "",
      thesis: `Rebalance basket: ${summary}. Realized taxable gain ` +
        `${fmtCZK(s.realized_taxable_gain_czk)} ${ccy}; net cash ${fmtCZK(s.net_cash_czk)} ${ccy}.`,
    });
  });
  actions.appendChild(logBtn);
  card.appendChild(actions);

  box.appendChild(card);
}

// ---- tax-lot breakdown (Czech 3-year aware) --------------------------------
const TAX_BUCKET: Record<string, { label: string; cls: string }> = {
  exempt_gain: { label: "exempt gain", cls: "good" },
  taxable_loss: { label: "harvest loss", cls: "warn" },
  exempt_loss: { label: "exempt loss", cls: "muted" },
  taxable_gain: { label: "taxable gain", cls: "bad" },
};

function taxDetails(r: RebRow) {
  const t = r.tax;
  if (!t || !t.has_lots || !t.lots || !t.lots.length) return null;
  const tot = t.totals || {};
  const det = el("details", "reb-tax");
  const bits = [
    `${sensitive(`${fmtCZK(t.raised)} ${esc(t.currency)}`, "trim proceeds")} from ${t.n_lots_used} lot(s)`,
    `taxable gain ${sensitive(`${fmtCZK(tot.taxable_gain)} ${esc(t.currency)}`, "taxable gain")}`,
  ];
  if (tot.exempt_proceeds > 0) bits.push(`${sensitive(`${fmtCZK(tot.exempt_proceeds)}`, "exempt proceeds")} already 3y-exempt`);
  if (tot.harvestable_loss > 0) bits.push(`${sensitive(`${fmtCZK(tot.harvestable_loss)}`, "harvestable loss")} harvestable loss`);
  det.appendChild(el("summary", undefined,
    `<span class="reb-tax-caret" aria-hidden="true"></span>` +
    `<span class="reb-tax-tag">Tax lots</span>` +
    `<span class="reb-tax-count">${t.n_lots_used} of ${t.n_lots_total} lot(s) to sell</span>` +
    `<span class="reb-tax-bits">${bits.join(" · ")}</span>`));

  const list = el("div", "reb-tax-list");
  t.lots.forEach((l) => {
    const b = TAX_BUCKET[l.bucket ?? ""] || { label: l.bucket ?? "", cls: "muted" };
    const when = l.open_datetime ? String(l.open_datetime).slice(0, 10) : "?";
    const dte = (l.days_to_exempt != null && l.days_to_exempt > 0)
      ? `<small class="muted">${l.days_to_exempt}d to exempt</small>` : "";
    list.appendChild(el("div", "reb-tax-row",
      `<span class="chip ${b.cls}">${esc(b.label)}</span>` +
      `<span class="reb-tax-date">opened ${esc(when)} ${dte}</span>` +
      `<span>${sensitive(`${fmtCZK(l.proceeds)} ${esc(t.currency)}`, "lot proceeds")}</span>` +
      `<span class="${(l.gain ?? 0) >= 0 ? "good" : "bad"}">gain ${sensitive(`${(l.gain ?? 0) >= 0 ? "+" : "\u2212"}${fmtCZK(Math.abs(l.gain ?? 0))}`, "lot gain")}</span>`));
  });
  det.appendChild(list);

  if ((t.shortfall ?? 0) > 0) {
    det.appendChild(el("div", "hint bad",
      `Lots cover only ${sensitive(`${fmtCZK(t.raised)}`, "raised")} of the ` +
      `${sensitive(`${fmtCZK(t.requested)}`, "requested")} ${esc(t.currency)} trim — ` +
      `${sensitive(`${fmtCZK(t.shortfall)}`, "shortfall")} short of available lots.`));
  }
  det.appendChild(el("div", "hint",
    "Czech 3-year rule (lot open date, not IBKR ST/LT). Order: tax-free gains, then " +
    "harvestable losses, then taxable gains. Analysis, not tax advice — verify before trading."));
  return det;
}

export {
  rebStatusClass,
  rebActionClass,
  loadRebalance,
  renderRebalance,
};
// The pure plan math moved to ./rebalance-model; re-exported so existing
// imports (tests, other views) keep one stable entry point.
export { fundingNeededCzk, pctToCzk, projectedCash, rebDefaultDelta } from "./rebalance-model";
