// The Target state view: flow-bar stage 3's pre-trade review gate. One screen that
// answers "if I execute what's on the table, where does the book land?" —
// current vs projected weight for every targeted name and sleeve, drawn on the
// same band tracks the planner uses, with the headline deltas (bands in, cash
// vs its band, net cash, realized Czech tax) up top.
//
// The projection source is only the staged basket (that's what can actually be
// placed). The durable execution plan is shown separately as desired intent;
// unqueued suggestions never masquerade as an executable projection.
// Approving records only the exact queue revision shown; this view never trades.
import { $, api, el, esc, fmtCZK, sensitive, statTile } from "./core";
import type {
  ExecutionPlanItem, ExecutionPlanState, PlanRow, RebalancePlan,
  StockSellViolation, TradeQueueState, Whatif, WhatifTrade,
} from "./api-types";
import { coverageConflictsHtml } from "./coverage-conflicts";
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
    const proj = Math.max(0, a ? a.current_pct : r.current_pct);
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
const ruleCls = (rule: string) => {
  const normalized = rule.toLowerCase();
  if (normalized.includes("reduce") || normalized.includes("trim")) return "bad";
  if (
    normalized.includes("wait")
    || normalized.includes("don\u2019t add")
    || normalized.includes("don't add")
  ) return "warn";
  if (normalized.includes("accumulate") || normalized.includes("add")) return "good";
  return "muted";
};

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
  const outcome = (
    r.statusBefore !== "IN" && r.statusAfter === "IN"
      ? " tstate-resolved"
      : r.statusBefore === "IN" && r.statusAfter !== "IN"
        ? " tstate-regressed"
        : r.statusAfter !== "IN"
          ? " tstate-unresolved"
          : ""
  );
  const kind = r.kind === "sleeve" ? "sleeve total" : "target";
  return `<div class="tstate-row${r.changed ? " tstate-changed" : ""}${outcome}">` +
    `<div class="tstate-name">` +
      `<strong title="${esc(r.name)}">${esc(r.name)}</strong>` +
      `<span class="tstate-name-meta"><span class="tstate-kind">${kind}</span>` +
      `<span class="tstate-rule ${ruleCls(r.rule)}">${esc(r.rule)}</span></span>` +
    `</div>` +
    `<div class="reb-track" role="img" aria-label="${esc(r.name)}: now ${r.cur.toFixed(1)}%, after ${r.proj.toFixed(1)}%, band ${r.low}–${r.high}%">` +
      `<span class="reb-zone" style="left:${r1(zL)}%;width:${r1(zW)}%"></span>` +
      conn +
      `<span class="reb-cur-mark" style="left:${r1(curP)}%" title="now ${r.cur.toFixed(2)}%"></span>` +
      (r.changed ? `<span class="reb-proj-mark ${inAfter ? "in" : "out"}" style="left:${r1(projP)}%" title="after ${r.proj.toFixed(2)}%"></span>` : "") +
    `</div>` +
    `<div class="tstate-nums"><span><small>now</small>${r.cur.toFixed(2)}%</span>` +
    (r.changed
      ? ` <span class="tstate-arrow">→</span> <strong><small>after</small>${r.proj.toFixed(2)}%</strong>`
      : "") +
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

export function executionPlanHtml(state: ExecutionPlanState | null | undefined): string {
  const allItems = state?.items || [];
  const submitted = allItems.filter((item) => item.status === "submitted").length;
  const items = allItems.filter(
    (item) => !["dismissed", "superseded", "submitted"].includes(item.status),
  );
  if (!items.length) {
    return `<section class="exec-review"><div class="exec-review-head"><div>` +
      `<h3>Execution plan</h3><p>No active actions${submitted ? ` · ${submitted} submitted` : ""}.</p></div>` +
      `<button class="ghost" data-ts-goto="rebalance" type="button">Build actions →</button></div></section>`;
  }
  const grouped = new Map<string, ExecutionPlanItem[]>();
  items.forEach((item) => {
    const rows = grouped.get(item.symbol) || [];
    rows.push(item);
    grouped.set(item.symbol, rows);
  });
  const selected = items.filter((item) => item.status === "selected").length;
  const queued = items.filter((item) => item.status === "queued").length;
  const deferred = items.filter((item) => item.status === "deferred").length;
  const rows = [...grouped.entries()].map(([symbol, symbolItems]) => {
    const delta = symbolItems.reduce((sum, item) => sum + Number(item.delta_czk || 0), 0);
    const latest = symbolItems[symbolItems.length - 1];
    const statuses = [...new Set(symbolItems.map((item) => item.status))];
    const sources = [...new Set(symbolItems.map((item) => item.source))];
    const routes = [...new Set(symbolItems.map((item) =>
      item.route_selection?.route || item.route_policy).filter(Boolean))];
    return `<tr><td><strong>${esc(symbol)}</strong><small>${esc(sources.join(" + "))}</small></td>` +
      `<td>${latest.desired_weight_pct != null ? `${latest.desired_weight_pct.toFixed(2)}% target` : "custom action"}</td>` +
      `<td class="num ${delta >= 0 ? "good" : "bad"}">${sensitive(`${delta >= 0 ? "+" : "−"}${fmtCZK(Math.abs(delta))} CZK`, "planned execution")}</td>` +
      `<td>${esc(routes.map((route) => String(route).replace(/_/g, " ")).join(" + "))}</td>` +
      `<td>${statuses.map((status) => `<span class="chip ${status === "queued" ? "good" : status === "deferred" ? "warn" : "muted"}">${esc(status)}</span>`).join(" ")}</td></tr>`;
  }).join("");
  return `<section class="exec-review"><div class="exec-review-head"><div>` +
    `<h3>Execution plan</h3><p>Desired position changes consolidated across Rebalance, ticker dossiers, and Exit.</p></div>` +
    `<div class="exec-review-actions">` +
      `<span>${selected} selected · ${deferred} later · ${queued} queued${submitted ? ` · ${submitted} submitted` : ""}</span>` +
      (selected
        ? `<button class="primary" data-ts-queue-selected type="button">Add ${selected} selected to queue</button>`
        : `<button class="ghost" data-ts-goto="rebalance" type="button">Select actions →</button>`) +
    `</div></div><div class="table-wrap"><table class="whatif-table exec-review-table">` +
    `<thead><tr><th>Position</th><th>Desired target</th><th class="num">Net action</th><th>Route</th><th>Lifecycle</th></tr></thead>` +
    `<tbody>${rows}</tbody></table></div></section>`;
}

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
  projectionValid = true,
): string {
  if (source === "basket") {
    const reviewed = !!queue?.reviewed;
    const optionCount = (queue?.trades || []).filter(
      (trade) => trade.type === "covered_call" || trade.type === "cash_secured_put",
    ).length;
    const assignment = (queue?.trades || [])
      .filter((trade) => trade.type === "covered_call" || trade.type === "cash_secured_put")
      .map((trade) =>
        `${trade.symbol} ${trade.type === "cash_secured_put" ? "+" : "−"}` +
        `${trade.contracts * (trade.multiplier || 100)} shares`,
      )
      .join(" · ");
    return `<div class="tstate-src"><span class="chip warn">order queue</span>` +
      ` Projected from the <strong>${n} queued order${n === 1 ? "" : "s"}</strong> — review this exact outcome before IBKR preview.` +
      (optionCount
        ? ` ${optionCount} written option${optionCount === 1 ? " is" : "s are"} conditional and ` +
          `${optionCount === 1 ? "does" : "do"} not change share weights unless assigned.` +
          ` <span class="muted">If assigned: ${esc(assignment)}.</span>`
        : "") +
      (!projectionValid
        ? ` <button class="primary" type="button" disabled>Resolve queue conflicts first</button>`
        : reviewed
        ? ` <span class="chip good">projection approved</span>` +
          ` <button class="primary" type="button" data-ts-goto="trade">Preview &amp; place →</button>`
        : ` <button class="primary" type="button" data-ts-review="${esc(queue?.revision || "")}">Approve order queue →</button>`) +
      `</div>`;
  }
  if (source === "suggestions") {
    return `<div class="tstate-src"><span class="chip muted">plan suggestions</span>` +
      ` Nothing is staged yet, so this projects the <strong>${n} suggested amount${n === 1 ? "" : "s"}</strong> from the Rebalance planner — the book if you simply took every suggestion.` +
      ` <button class="ghost" type="button" data-ts-goto="rebalance">Adjust the plan →</button></div>`;
  }
  return `<div class="tstate-src"><span class="chip muted">order queue empty</span>` +
    ` Build and queue orders first. Plan suggestions are not treated as executable orders on this safety screen.` +
    ` <button class="primary" type="button" data-ts-goto="rebalance">Build orders →</button></div>`;
}

export function violationsHtml(violations: StockSellViolation[]): string {
  if (!violations.length) return "";
  return `<div class="tstate-invalid"><strong>Projection blocked — staged sells exceed holdings</strong>` +
    violations.map((violation) =>
      `<div class="tstate-invalid-row"><span><b>${esc(violation.symbol)}</b>: selling ` +
      `${sensitive(`${fmtCZK(violation.requested_sell_czk)} CZK`, "requested sell")} against ` +
      `${sensitive(`${fmtCZK(violation.held_czk)} CZK`, "held value")} held; reduce by at least ` +
      `${sensitive(`${fmtCZK(violation.excess_czk)} CZK`, "oversell excess")}.</span>` +
      `<button class="ghost" type="button" data-ts-remove-leg="stock:${esc(violation.symbol)}">` +
      `Remove ${esc(violation.symbol)} sell</button></div>`,
    ).join("") +
    `</div>`;
}

export function queueOrdersHtml(queue: TradeQueueState | null): string {
  const active = queue?.trades || [];
  const rows = (queue?.queue_trades || active.map((trade) => ({
    ...trade,
    included: true,
  }))).slice().sort((a, b) => {
    const aOption = a.type === "covered_call" || a.type === "cash_secured_put";
    const bOption = b.type === "covered_call" || b.type === "cash_secured_put";
    return Number(bOption) - Number(aOption);
  });
  if (!rows.length) return "";
  const included = rows.filter((trade) => trade.included !== false);
  const allIds = rows.map((trade) => String(trade.leg_id || "")).filter(Boolean);
  const callIds = rows
    .filter((trade) => trade.type === "covered_call")
    .map((trade) => String(trade.leg_id || "")).filter(Boolean);
  const onlyCallsAlready = included.length === callIds.length
    && included.every((trade) => trade.type === "covered_call");
  const controls =
    (callIds.length && !onlyCallsAlready
      ? `<button class="primary" type="button" data-ts-only-leg-ids="${esc(callIds.join(","))}">` +
        `Trade only covered calls</button>`
      : "") +
    (included.length < rows.length
      ? `<button class="ghost" type="button" data-ts-only-leg-ids="${esc(allIds.join(","))}">` +
        `Include all orders</button>`
      : "");
  return `<section class="tstate-queue">` +
    `<header><div><span class="reb-route-eyebrow">Execution queue</span>` +
      `<strong>Queued orders</strong><small>${included.length} included · ` +
      `${rows.length - included.length} excluded</small></div>` +
      `<div class="tstate-queue-actions">${controls}</div></header>` +
    `<div class="tstate-queue-rows">` +
      rows.map((trade) => {
        const option = trade.type === "covered_call" || trade.type === "cash_secured_put";
        const includedNow = trade.included !== false;
        const legId = String(trade.leg_id || "");
        const kind = trade.type === "covered_call"
          ? "Covered call"
          : trade.type === "cash_secured_put"
            ? "Short put"
            : Number(trade.delta_czk || 0) >= 0 ? "Buy shares" : "Sell shares";
        const detail = option
          ? `${trade.contracts} contract${trade.contracts === 1 ? "" : "s"} · ` +
            `${trade.contracts * (trade.multiplier || 100)} shares ` +
            `${trade.type === "covered_call" ? "covered" : "if assigned"} · ` +
            `${esc(trade.expiry)} · ${esc(trade.strike)}${trade.type === "covered_call" ? "C" : "P"}` +
            `${trade.limit_price != null ? ` · limit ${esc(trade.limit_price)}` : ""}`
          : `${trade.estimated_shares
              ? `<b>≈ ${trade.estimated_shares.toLocaleString()} shares</b> ` +
                `<small title="Snapshot estimate; IBKR preview locks the final whole-share quantity">estimated</small> · `
              : `<b>Shares priced at preview</b> · `}` +
            sensitive(
              `${Number(trade.delta_czk || 0) >= 0 ? "+" : "−"}` +
              `${fmtCZK(Math.abs(Number(trade.delta_czk || 0)))} CZK`,
              "queued trade size",
            );
        return `<article class="tstate-queue-row${includedNow ? "" : " excluded"}">` +
          `<div class="tstate-queue-symbol"><strong>${esc(trade.symbol)}</strong>` +
            `<span>${kind}</span></div>` +
          `<div class="tstate-queue-detail">${detail}</div>` +
          `<span class="chip ${includedNow ? "good" : "muted"}">` +
            `${includedNow ? "included" : "excluded"}</span>` +
          `<div class="tstate-queue-row-actions">` +
            `<button class="ghost" type="button" data-ts-toggle-leg="${esc(legId)}" ` +
              `data-ts-include="${includedNow ? "false" : "true"}">` +
              `${includedNow ? "Exclude" : "Include"}</button>` +
            `<button class="ghost danger" type="button" data-ts-delete-leg="${esc(legId)}" ` +
              `data-ts-delete-label="${esc(`${trade.symbol} ${kind}`)}">Delete</button>` +
          `</div>` +
        `</article>`;
      }).join("") +
    `</div></section>`;
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

  const head = `<div class="tstate-head-row"><span class="tstate-col-name">Position</span>` +
    `<span class="tstate-col-track">Allocation path <small>0–${scaleMax}% of book · ` +
    `<span class="tstate-band-key">target band</span> · ` +
    `<span class="tstate-ghost-key">◆ now</span> · <span class="tstate-proj-key">● after</span></small></span>` +
    `<span class="tstate-col-nums">Weight</span><span>Status</span></div>`;

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

  const violations = (wf && wf.stock_sell_violations) || queue?.stock_sell_violations || [];
  const coverageViolations = queue?.coverage_violations || [];
  const projectionValid =
    (wf ? wf.valid !== false : true) && queue?.valid !== false;
  const violationsBlock = violationsHtml(violations);
  const coverageBlock = coverageConflictsHtml(coverageViolations);
  const workingVerificationBlock = queue?.working_orders_verified === false
    ? `<div class="tstate-invalid"><strong>Approval blocked — working orders not verified</strong>` +
      `<span>IBKR working orders could not be checked. Reconnect and refresh before approval.` +
      `${queue.working_orders_error ? ` ${esc(queue.working_orders_error)}` : ""}</span></div>`
    : "";
  const queueBlock = queueOrdersHtml(queue);
  const caveats = (wf && wf.caveats || []).map((c) => {
    const severe = /negative|blocked|exceed|cannot/i.test(c);
    return `<div class="tstate-caveat${severe ? " bad" : ""}">` +
      `<span aria-hidden="true">${severe ? "!" : "i"}</span><p>${esc(c)}</p></div>`;
  }).join("");

  body.innerHTML =
    sourceBanner(source, nTrades, queue, projectionValid) +
    queueBlock +
    workingVerificationBlock +
    violationsBlock +
    coverageBlock +
    executionPlanHtml(plan.execution_plan) +
    summaryTiles(plan, wf, rows) +
    changedBlock + sameBlock + tradesBlock +
    (caveats ? `<div class="tstate-caveats">${caveats}</div>` : "");
}

// ---- load --------------------------------------------------------------------
interface TargetStateLoadOptions {
  queue?: TradeQueueState;
  quiet?: boolean;
  reusePlan?: boolean;
}

let _loadedPlan: RebalancePlan | null = null;

async function loadTargetState(options: TargetStateLoadOptions = {}): Promise<void> {
  const status = $("#tstate-status");
  const body = $("#tstate-body");
  if (!body) return;
  if (status) {
    status.classList.remove("err");
    status.innerHTML = options.quiet
      ? ""
      : `<span class="spinner"></span> projecting the book…`;
  }
  if (!options.quiet) body.innerHTML = "";
  let plan: RebalancePlan;
  try {
    plan = options.reusePlan && _loadedPlan
      ? _loadedPlan
      : await api<RebalancePlan>("/api/rebalance");
    _loadedPlan = plan;
  } catch (e) {
    if (status) { status.textContent = "Could not load the plan: " + (e as Error).message; status.classList.add("err"); }
    return;
  }

  // This is an execution gate, so only the exact order queue is projected.
  // Suggestions remain visible in Build orders but never masquerade as orders.
  let trades: WhatifTrade[] = [];
  let stagedCount = 0;
  let source: "basket" | "suggestions" | "none" = "none";
  let queue: TradeQueueState;
  try {
    queue = options.queue || await api<TradeQueueState>("/api/trade/basket");
    if (Array.isArray(queue.trades) && queue.trades.length) {
      stagedCount = queue.trades.length;
      trades = queue.trades
        .filter(
          (trade) => trade.type !== "covered_call" && trade.type !== "cash_secured_put",
        )
        .map((trade) => ({ symbol: trade.symbol, delta_czk: trade.delta_czk }));
      source = "basket";
    }
  } catch (e) {
    if (status) {
      status.textContent = "Could not load the order queue: " + (e as Error).message;
      status.classList.add("err");
    }
    return;
  }
  if ((queue.trades || []).some((trade) => trade.type === "covered_call")) {
    try {
      const working = await api<Pick<
        TradeQueueState,
        "coverage_violations" | "working_orders_verified" | "working_orders_error"
      >>("/api/trade/queue-conflicts");
      const workingCoverage = working.coverage_violations || [];
      queue = {
        ...queue,
        ...working,
        coverage_violations: workingCoverage,
        valid: queue.valid !== false
          && working.working_orders_verified !== false
          && workingCoverage.length === 0,
      };
    } catch (error) {
      queue = {
        ...queue,
        valid: false,
        working_orders_verified: false,
        working_orders_error: (error as Error).message,
      };
    }
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
    const queueSelected = (e.target as HTMLElement).closest<HTMLButtonElement>("[data-ts-queue-selected]");
    if (queueSelected) {
      const status = $("#tstate-status");
      queueSelected.disabled = true;
      queueSelected.textContent = "Resolving routes…";
      void api("/api/execution-plan", "POST", { action: "queue_selected" }, { timeoutMs: 120_000 })
        .then(() => {
          window.dispatchEvent(new Event("assay:queue-changed"));
          return loadTargetState({ quiet: true });
        })
        .catch((err) => {
          queueSelected.disabled = false;
          queueSelected.textContent = "Add selected to queue";
          if (status) {
            status.classList.add("err");
            status.textContent = (err as Error).message;
          }
        });
      return;
    }
    const onlyLegs = (e.target as HTMLElement).closest<HTMLButtonElement>(
      "[data-ts-only-leg-ids]",
    );
    if (onlyLegs) {
      const legIds = (onlyLegs.dataset.tsOnlyLegIds || "").split(",").filter(Boolean);
      const status = $("#tstate-status");
      onlyLegs.disabled = true;
      onlyLegs.textContent = "Updating queue…";
      void api<TradeQueueState>("/api/trade/basket", "POST", {
        only_leg_ids: legIds,
      }).then((saved) => {
        window.dispatchEvent(new Event("assay:queue-changed"));
        return loadTargetState({ queue: saved, quiet: true, reusePlan: true });
      }).catch((err) => {
        onlyLegs.disabled = false;
        onlyLegs.textContent = "Try again";
        if (status) {
          status.classList.add("err");
          status.textContent = (err as Error).message;
        }
      });
      return;
    }
    const deleteLeg = (e.target as HTMLElement).closest<HTMLButtonElement>(
      "[data-ts-delete-leg]",
    );
    if (deleteLeg) {
      const label = deleteLeg.dataset.tsDeleteLabel || "this order";
      if (!window.confirm(
        `Permanently delete ${label} from the queue? Exclude is the reversible alternative.`,
      )) return;
      const status = $("#tstate-status");
      deleteLeg.disabled = true;
      deleteLeg.textContent = "Deleting…";
      void api<TradeQueueState>("/api/trade/basket", "POST", {
        remove_leg_id: deleteLeg.dataset.tsDeleteLeg || "",
      }).then((saved) => {
        window.dispatchEvent(new Event("assay:queue-changed"));
        return loadTargetState({ queue: saved, quiet: true, reusePlan: true });
      }).catch((err) => {
        deleteLeg.disabled = false;
        deleteLeg.textContent = "Delete";
        if (status) {
          status.classList.add("err");
          status.textContent = (err as Error).message;
        }
      });
      return;
    }
    const toggleLeg = (e.target as HTMLElement).closest<HTMLButtonElement>(
      "[data-ts-toggle-leg]",
    );
    if (toggleLeg) {
      const status = $("#tstate-status");
      toggleLeg.disabled = true;
      toggleLeg.textContent = "Updating…";
      void api<TradeQueueState>("/api/trade/basket", "POST", {
        toggle_leg_id: toggleLeg.dataset.tsToggleLeg || "",
        included: toggleLeg.dataset.tsInclude === "true",
      }).then((saved) => {
        window.dispatchEvent(new Event("assay:queue-changed"));
        return loadTargetState({ queue: saved, quiet: true, reusePlan: true });
      }).catch((err) => {
        toggleLeg.disabled = false;
        toggleLeg.textContent = "Try again";
        if (status) {
          status.classList.add("err");
          status.textContent = (err as Error).message;
        }
      });
      return;
    }
    const cancelWorking = (e.target as HTMLElement).closest<HTMLButtonElement>(
      "[data-coverage-cancel-order-ids]",
    );
    if (cancelWorking) {
      const orderIds = (cancelWorking.dataset.coverageCancelOrderIds || "")
        .split(",").filter(Boolean);
      const symbol = cancelWorking.dataset.coverageSymbol || "this symbol";
      const kind = cancelWorking.dataset.coverageCancelKind || "sell";
      if (!window.confirm(
        `Cancel ${orderIds.length} working IBKR ${kind} order(s) for ${symbol} and keep the queued calls?`,
      )) return;
      const conflict = cancelWorking.closest<HTMLElement>(".coverage-conflict");
      const buttons = conflict?.querySelectorAll<HTMLButtonElement>("button") || [];
      const conflictStatus = conflict?.querySelector<HTMLElement>(
        ".coverage-conflict-status",
      );
      buttons.forEach((button) => { button.disabled = true; });
      cancelWorking.textContent = "Cancelling at IBKR…";
      void (async () => {
        try {
          for (const orderId of orderIds) {
            await api("/api/trade/cancel", "POST", { order_id: orderId });
          }
          await loadTargetState({ quiet: true, reusePlan: true });
        } catch (error) {
          buttons.forEach((button) => { button.disabled = false; });
          if (conflictStatus) conflictStatus.textContent = (error as Error).message;
        }
      })();
      return;
    }
    const coverageAction = (e.target as HTMLElement).closest<HTMLButtonElement>(
      "[data-coverage-action]",
    );
    if (coverageAction) {
      const legIds = (coverageAction.dataset.coverageLegIds || "")
        .split(",").filter(Boolean);
      const conflict = coverageAction.closest<HTMLElement>(".coverage-conflict");
      const conflictStatus = conflict?.querySelector<HTMLElement>(
        ".coverage-conflict-status",
      );
      const buttons = conflict?.querySelectorAll<HTMLButtonElement>("button") || [];
      buttons.forEach((button) => { button.disabled = true; });
      coverageAction.textContent = "Reconciling…";
      void (async () => {
        try {
          let saved: TradeQueueState | null = null;
          for (const legId of legIds) {
            saved = await api<TradeQueueState>("/api/trade/basket", "POST", {
              toggle_leg_id: legId,
              included: false,
            });
          }
          window.dispatchEvent(new Event("assay:queue-changed"));
          await loadTargetState({
            ...(saved ? { queue: saved } : {}),
            quiet: true,
            reusePlan: true,
          });
        } catch (err) {
          buttons.forEach((button) => { button.disabled = false; });
          if (conflictStatus) conflictStatus.textContent = (err as Error).message;
        }
      })();
      return;
    }
    const remove = (e.target as HTMLElement).closest<HTMLButtonElement>("[data-ts-remove-leg]");
    if (remove) {
      const legId = remove.dataset.tsRemoveLeg || "";
      const status = $("#tstate-status");
      remove.disabled = true;
      remove.textContent = "Removing…";
      void api<TradeQueueState>("/api/trade/basket", "POST", { remove_leg_id: legId })
        .then((saved) => loadTargetState({
          queue: saved,
          quiet: true,
          reusePlan: true,
        }))
        .catch((err) => {
          remove.disabled = false;
          remove.textContent = "Remove blocked sell";
          if (status) {
            status.classList.add("err");
            status.textContent = (err as Error).message;
          }
        });
      return;
    }
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
          const open = el("button", "primary", "Preview & place →");
          open.type = "button";
          open.dataset.tsGoto = "trade";
          review.insertAdjacentElement("afterend", open);
          if (status) status.textContent = "Approved for this exact order-queue revision.";
        })
        .catch((err) => {
          review.disabled = false;
          review.textContent = "Approve order queue →";
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
