// The Target state view: flow-bar stage 3's pre-trade review gate. One screen that
// answers "if I execute what's on the table, where does the book land?" —
// current vs projected weight for every targeted name and sleeve, drawn on the
// same band tracks the planner uses, with the headline deltas (bands in, cash
// vs its band, net cash, realized Czech tax) up top.
//
// The projection source is the staged basket when one exists (that's what will
// actually be placed), else the plan's own suggested amounts (what the book
// would look like if you simply took every suggestion). With nothing on the
// table it degrades to "current book vs its bands" — every tick single.
// Approving records only the exact queue revision shown; this view never trades.
import { $, api, el, esc, fmtCZK, sensitive, statTile } from "./core";
import type { PlanRow, RebalancePlan, TradeQueueState, Whatif, WhatifTrade } from "./api-types";
import { axisMax, onAxis, r1 } from "./weight-axis";
import { pushNav, setActiveView } from "./shell";

// ---- pure builders (exported for tests) -------------------------------------
// The plan's suggested amounts as a tradeable basket: interactive buy/trim rows
// plus per-member sleeve suggestions — exactly the amounts the planner prefills.
export function deriveSuggestionTrades(plan: RebalancePlan): WhatifTrade[] {
  const out: WhatifTrade[] = [];
  (plan.rows || []).forEach((r) => {
    if (r.kind === "target" && r.interactive && (r.action === "buy" || r.action === "trim")) {
      if (typeof r.suggest_delta_czk === "number" && Math.round(r.suggest_delta_czk) !== 0) {
        out.push({ symbol: r.name, delta_czk: Math.round(r.suggest_delta_czk) });
      }
    }
    if (r.kind === "sleeve") {
      (r.members || []).forEach((m) => {
        if (m.member_action && typeof m.suggest_delta_czk === "number" && Math.round(m.suggest_delta_czk) !== 0) {
          out.push({ symbol: m.symbol, delta_czk: Math.round(m.suggest_delta_czk) });
        }
      });
    }
  });
  return out;
}

export interface CompareRow {
  name: string;
  kind: string;
  rule: string;
  low: number;
  high: number;
  cur: number;
  proj: number;
  statusBefore: string;
  statusAfter: string;
  changed: boolean;
}

// Pair every before-plan row with its after-plan counterpart (matched by kind +
// name; a missing after book means "no trades" and the projection equals now).
export function compareRows(before: PlanRow[], after: PlanRow[] | null): CompareRow[] {
  const afterBy: Record<string, PlanRow> = {};
  (after || []).forEach((r) => { afterBy[`${r.kind}:${r.name}`] = r; });
  return (before || []).map((r) => {
    const a = after ? afterBy[`${r.kind}:${r.name}`] : null;
    const proj = a ? a.current_pct : r.current_pct;
    return {
      name: r.name, kind: r.kind, rule: r.rule,
      low: r.low, high: r.high,
      cur: r.current_pct, proj,
      statusBefore: r.status,
      statusAfter: a ? a.status : r.status,
      changed: Math.abs(proj - r.current_pct) > 0.005,
    };
  }).sort((x, y) => (Math.abs(y.proj - y.cur) - Math.abs(x.proj - x.cur)) || x.name.localeCompare(y.name));
}

const statusCls = (s: string) => (s === "ABOVE" ? "bad" : s === "BELOW" ? "warn" : "good");

export function scaleMaxOf(rows: CompareRow[]): number {
  const vals: number[] = [];
  rows.forEach((r) => { vals.push(r.high, r.cur, r.proj); });
  return axisMax(vals);
}

// One comparison row: name + rule, the shared band track with a ghost "now"
// tick and a solid "after" tick, and the numeric now → after with status chips.
export function compareRowHtml(r: CompareRow, scaleMax: number): string {
  const toP = (v: number) => onAxis(v, scaleMax);
  const zL = toP(r.low);
  const zW = Math.max(1.5, toP(r.high) - zL);
  const curP = toP(r.cur);
  const projP = toP(r.proj);
  const inAfter = r.statusAfter === "IN";
  const conn = r.changed
    ? `<span class="reb-conn ${r.proj > r.cur ? "buy" : "sell"}" style="left:${r1(Math.min(curP, projP))}%;width:${r1(Math.abs(projP - curP))}%"></span>`
    : "";
  const afterChip = `<span class="chip ${statusCls(r.statusAfter)}">${esc(r.statusAfter)}</span>`;
  const arrow = r.changed
    ? `<span class="chip ${statusCls(r.statusBefore)} tstate-before">${esc(r.statusBefore)}</span><span class="tstate-arrow">→</span>${afterChip}`
    : afterChip;
  return `<div class="tstate-row${r.changed ? " tstate-changed" : ""}">` +
    `<div class="tstate-name"><strong>${esc(r.name)}</strong><span class="reb-rule">${esc(r.rule)}</span></div>` +
    `<div class="reb-track" role="img" aria-label="${esc(r.name)}: now ${r.cur.toFixed(1)}%, after ${r.proj.toFixed(1)}%, band ${r.low}–${r.high}%">` +
      `<span class="reb-zone" style="left:${r1(zL)}%;width:${r1(zW)}%"></span>` +
      conn +
      `<span class="reb-cur-mark" style="left:${r1(curP)}%" title="now ${r.cur.toFixed(2)}%"></span>` +
      (r.changed ? `<span class="reb-proj-mark ${inAfter ? "in" : "out"}" style="left:${r1(projP)}%" title="after ${r.proj.toFixed(2)}%"></span>` : "") +
    `</div>` +
    `<div class="tstate-nums">${r.cur.toFixed(2)}%` +
    (r.changed ? ` <span class="tstate-arrow">→</span> <strong>${r.proj.toFixed(2)}%</strong>` : "") +
    `</div>` +
    `<div class="tstate-status">${arrow}</div>` +
    `</div>`;
}

// ---- rendering ---------------------------------------------------------------
// Thin string adapter over core's element-based statTile: this view assembles
// its tiles into an HTML string, so serialize the shared tile rather than
// re-implementing its markup (same `reb-stat` family, so the output is identical).
const tile = (label: string, valueHtml: string, cls = ""): string =>
  statTile(label, valueHtml, { html: true, cls }).outerHTML;

function summaryTiles(plan: RebalancePlan, wf: Whatif | null, rows: CompareRow[]): string {
  const total = rows.length;
  const inBefore = rows.filter((r) => r.statusBefore === "IN").length;
  const inAfter = rows.filter((r) => r.statusAfter === "IN").length;
  const bands = tile("Bands in band",
    wf ? `${inBefore} <span class="tstate-arrow">→</span> ${inAfter} <small>/ ${total}</small>` : `${inAfter} <small>/ ${total}</small>`,
    inAfter === total ? "good" : inAfter >= inBefore ? "" : "bad");

  let cash = "";
  const ct = wf && wf.cash && wf.cash.target;
  if (ct) {
    const cls = ct.status_after === "IN" ? "good" : ct.status_after === "BELOW" ? "bad" : "warn";
    cash = tile("Cash after", `${ct.before_pct.toFixed(1)}% <span class="tstate-arrow">→</span> ${ct.after_pct.toFixed(1)}% <small>of NAV · target ${ct.target_pct}%</small>`, cls);
  } else if (plan.cash) {
    const c = plan.cash;
    cash = tile("Cash", `${c.pct_of_nav.toFixed(1)}% <small>of NAV · target ${c.target_pct}%</small>`,
      c.status === "IN" ? "good" : c.status === "BELOW" ? "bad" : "warn");
  }

  let net = "", tax = "";
  if (wf && wf.summary) {
    const s = wf.summary;
    const n = s.net_cash_czk || 0;
    net = tile("Net cash from trades",
      sensitive(`${n >= 0 ? "+" : "−"}${fmtCZK(Math.abs(n))} CZK`, "net cash"), n >= 0 ? "good" : "bad");
    const g = s.realized_taxable_gain_czk || 0;
    tax = tile("Realized taxable gain",
      sensitive(`${fmtCZK(g)} CZK`, "taxable gain"), g > 0 ? "warn" : "good");
  }
  return `<div class="reb-stats tstate-tiles">${bands}${cash}${net}${tax}</div>`;
}

export function sourceBanner(
  source: "basket" | "suggestions" | "none",
  n: number,
  queue: TradeQueueState | null,
): string {
  if (source === "basket") {
    const reviewed = !!queue?.reviewed;
    const optionCount = (queue?.trades || []).filter(
      (trade) => trade.type === "covered_call",
    ).length;
    return `<div class="tstate-src"><span class="chip warn">order queue</span>` +
      ` Projected from the <strong>${n} staged order${n === 1 ? "" : "s"}</strong> waiting in the Trade desk — review this outcome before placing them.` +
      (optionCount
        ? ` ${optionCount} covered call${optionCount === 1 ? "" : "s"} ${optionCount === 1 ? "is" : "are"} conditional and ${optionCount === 1 ? "does" : "do"} not change share weights unless assigned.`
        : "") +
      (reviewed
        ? ` <span class="chip good">projection approved</span>` +
          ` <button class="primary" type="button" data-ts-goto="trade">Open Trade desk →</button>`
        : ` <button class="primary" type="button" data-ts-review="${esc(queue?.revision || "")}">Approve this projection →</button>`) +
      `</div>`;
  }
  if (source === "suggestions") {
    return `<div class="tstate-src"><span class="chip muted">plan suggestions</span>` +
      ` Nothing is staged yet, so this projects the <strong>${n} suggested amount${n === 1 ? "" : "s"}</strong> from the Rebalance planner — the book if you simply took every suggestion.` +
      ` <button class="ghost" type="button" data-ts-goto="rebalance">Adjust the plan →</button></div>`;
  }
  return `<div class="tstate-src"><span class="chip good">at rest</span>` +
    ` No staged orders and no suggested trades — the projection equals the current book.</div>`;
}

function render(
  plan: RebalancePlan,
  wf: Whatif | null,
  source: "basket" | "suggestions" | "none",
  nTrades: number,
  queue: TradeQueueState | null,
): void {
  const body = $("#tstate-body");
  if (!body) return;
  const rows = compareRows(plan.rows || [], wf && wf.after ? wf.after.rows || null : null);
  const scaleMax = scaleMaxOf(rows);
  const changed = rows.filter((r) => r.changed);
  const same = rows.filter((r) => !r.changed);

  const head = `<div class="tstate-head-row"><span class="tstate-col-name">Name</span>` +
    `<span>0–${scaleMax}% of book · <span class="tstate-ghost-key">◦ now</span> · <span class="tstate-proj-key">● after</span></span>` +
    `<span class="tstate-col-nums">now → after</span><span>status</span></div>`;

  const changedBlock = changed.length
    ? `<div class="tstate-grid">${head}${changed.map((r) => compareRowHtml(r, scaleMax)).join("")}</div>`
    : "";
  // Unchanged names collapse — the point of this screen is what moves.
  const sameBlock = same.length
    ? `<details class="tstate-same"><summary>${same.length} name${same.length === 1 ? "" : "s"} unchanged</summary>` +
      `<div class="tstate-grid">${changed.length ? "" : head}${same.map((r) => compareRowHtml(r, scaleMax)).join("")}</div></details>`
    : "";

  const trades = (wf && wf.trades) || [];
  const tradesBlock = trades.length
    ? `<details class="tstate-trades"><summary>The ${trades.length} trade${trades.length === 1 ? "" : "s"} producing this state</summary>` +
      `<div class="tstate-trades-list">` +
      trades.map((t) => `<span class="tstate-trade">${esc(t.symbol)} ` +
        `<span class="${t.delta_czk >= 0 ? "good" : "bad"}">${sensitive(`${t.delta_czk >= 0 ? "+" : "−"}${fmtCZK(Math.abs(t.delta_czk))}`, "trade size")}</span></span>`).join("") +
      `</div></details>`
    : "";

  const caveats = (wf && wf.caveats || []).map((c) => `<div class="hint">${esc(c)}</div>`).join("");

  body.innerHTML =
    sourceBanner(source, nTrades, queue) +
    summaryTiles(plan, wf, rows) +
    changedBlock + sameBlock + tradesBlock + caveats;
}

// ---- load --------------------------------------------------------------------
async function loadTargetState(): Promise<void> {
  const status = $("#tstate-status");
  const body = $("#tstate-body");
  if (!body) return;
  if (status) { status.classList.remove("err"); status.innerHTML = `<span class="spinner"></span> projecting the book…`; }
  body.innerHTML = "";
  let plan: RebalancePlan;
  try {
    plan = await api<RebalancePlan>("/api/rebalance");
  } catch (e) {
    if (status) { status.textContent = "Could not load the plan: " + (e as Error).message; status.classList.add("err"); }
    return;
  }

  // Projection source: the staged basket wins (it's what will be placed);
  // else the plan's suggested amounts; else nothing (projection = now).
  let trades: WhatifTrade[] = [];
  let stagedCount = 0;
  let source: "basket" | "suggestions" | "none" = "none";
  let queue: TradeQueueState | null = null;
  try {
    queue = await api<TradeQueueState>("/api/trade/basket");
    if (Array.isArray(queue.trades) && queue.trades.length) {
      stagedCount = queue.trades.length;
      trades = queue.trades
        .filter((trade) => trade.type !== "covered_call")
        .map((trade) => ({ symbol: trade.symbol, delta_czk: trade.delta_czk }));
      source = "basket";
    }
  } catch { /* fall through to suggestions */ }
  if (source !== "basket") {
    trades = deriveSuggestionTrades(plan);
    source = trades.length ? "suggestions" : "none";
  }

  let wf: Whatif | null = null;
  if (trades.length) {
    try {
      wf = await api<Whatif>("/api/whatif", "POST", { trades });
    } catch (e) {
      if (status) { status.textContent = "Projection failed: " + (e as Error).message; status.classList.add("err"); }
      return;
    }
  }
  if (status) status.textContent = "";
  render(plan, wf, source, source === "basket" ? stagedCount : trades.length, queue);
}

let _wired = false;
function initTargetState(): void {
  if (_wired) return;
  _wired = true;
  const host = $("#view-target-state");
  if (!host) return;
  host.addEventListener("click", (e) => {
    const review = (e.target as HTMLElement).closest<HTMLButtonElement>("[data-ts-review]");
    if (review) {
      const revision = review.dataset.tsReview || "";
      const status = $("#tstate-status");
      if (status) status.classList.remove("err");
      review.disabled = true;
      review.textContent = "Approving…";
      void api<TradeQueueState>("/api/trade/basket/review", "POST", { revision })
        .then(() => {
          review.className = "ghost";
          review.textContent = "Projection approved ✓";
          const open = el("button", "primary", "Open Trade desk →");
          open.type = "button";
          open.dataset.tsGoto = "trade";
          review.insertAdjacentElement("afterend", open);
          if (status) status.textContent = "Approved for this exact order-queue revision.";
        })
        .catch((err) => {
          review.disabled = false;
          review.textContent = "Approve this projection →";
          if (status) {
            status.classList.add("err");
            status.textContent = (err as Error).message;
          }
        });
      return;
    }
    const b = (e.target as HTMLElement).closest<HTMLElement>("[data-ts-goto]");
    if (!b || !b.dataset.tsGoto) return;
    pushNav({ view: b.dataset.tsGoto });
    setActiveView(b.dataset.tsGoto);
  });
  $("#tstate-refresh")?.addEventListener("click", () => loadTargetState());
}

export { initTargetState, loadTargetState };
