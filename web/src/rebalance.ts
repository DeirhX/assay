import { starHtml } from "./basket";
import { $, api, apiLoad, el, esc, fmtCZK, fmtSignedWeight, fmtStamp, freshnessNote, isStaleToken, nextToken, sensitive, simpleTable, state, statTile } from "./core";
import type { Provenance, RebalancePlan as RebPlan, PlanRow as RebRow, Whatif, WhatifTrade } from "./api-types";
import { hydrateHistory, pullTicker, renderDeepDive } from "./deepdive";
import { openJournalWith } from "./journal";
import { cleanSymbol, pushNav, setActiveView } from "./shell";

// ---- rebalance planner -----------------------------------------------------
// Plan/row/what-if shapes are the API contract, so they live in ./api-types as
// the single source of truth (no local shadows). The aliases keep the call
// sites below reading in planner vocabulary.

const REB_RULE_LABEL: Record<string, string> = {
  trim_only: "trim only", do_not_add: "don't add", reduce: "reduce",
  avoid: "avoid", accumulate: "accumulate", hold: "hold", wait: "wait",
};
const rebStatusClass = (s: string | null | undefined) => (s === "ABOVE" ? "bad" : s === "BELOW" ? "warn" : "good");
const rebActionClass = (a: string | null | undefined) => (a === "trim" ? "bad" : a === "buy" ? "good" : a === "review" ? "warn" : "muted");
// Weights are percent of the invested book, so size money off invested value
// (not NAV) — that keeps a row's CZK equal to its actual market value.
const pctToCzk = (pct: number | null | undefined, base: number | null | undefined) =>
  (typeof base === "number" && pct != null ? Math.round((pct / 100) * base) : null);
// Default planned amount: prefill the minimal band-closing trade only for clear
// trim/buy actions. "review" (accumulate over ceiling) and untargeted names are
// judgement calls, so they start at zero — the human decides.
const rebDefaultDelta = (r: Pick<RebRow, "action" | "suggest_delta_pct">) => (r.action === "trim" || r.action === "buy" ? r.suggest_delta_pct : 0);

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
  if (total > 1) bits.push(`tranche ${live} of ${total}`);
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
  const input = $<HTMLInputElement>("#strat-direction");
  if (input) { input.value = hint; input.focus(); }
}

async function loadRebalance() {
  const token = nextToken("rebalance");
  await apiLoad({
    path: "/api/rebalance",
    status: $("#reb-status"),
    clear: [$("#reb-summary"), $("#reb-result")],
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
  const summary = $("#reb-summary");
  const out = $("#reb-result");
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
    (plan.funding_order && plan.funding_order.length
      ? `<span>funding order ${plan.funding_order.map(esc).join(" \u2192 ")}</span>` : "") +
    `</div>` +
    `<div class="reb-stats">` +
    `<div class="reb-stat"><span class="reb-stat-k">Cash freed by trims</span><span class="reb-stat-v" id="reb-stat-raised">—</span></div>` +
    `<div class="reb-stat"><span class="reb-stat-k">Cash needed for buys</span><span class="reb-stat-v" id="reb-stat-spent">—</span></div>` +
    `<div class="reb-stat"><span class="reb-stat-k">Net cash</span><span class="reb-stat-v" id="reb-stat-net">—</span></div>` +
    `<div class="reb-stat"><span class="reb-stat-k">Target bands closed</span><span class="reb-stat-v" id="reb-stat-closed">—</span></div>` +
    `</div>`;

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
  }
  const cells: RowCell[] = [];

  const headRow = (title: string) => {
    const h = el("div", "reb-row reb-head-row");
    h.innerHTML =
      `<div class="reb-c reb-name">${esc(title)}</div>` +
      `<div class="reb-c reb-cur">Current</div>` +
      `<div class="reb-c reb-band">Band</div>` +
      `<div class="reb-c reb-status">Status</div>` +
      `<div class="reb-c reb-plan">Plan (% of book)</div>` +
      `<div class="reb-c reb-proj">Projected</div>`;
    return h;
  };

  const buildRow = (r: RebRow) => {
    const row = el("div", "reb-row reb-data-row");
    const sym = el("span", "reb-sym", esc(r.name));
    if (r.kind === "target" && r.held) {
      sym.classList.add("reb-link");
      sym.title = "Open dossier";
      sym.addEventListener("click", () => analyzeFromAnywhere(r.name));
    }
    const nameCell = el("div", "reb-c reb-name");
    // Star only single tickers — a sleeve row is itself a basket, not one name.
    if (r.kind === "target") nameCell.insertAdjacentHTML("beforeend", starHtml(r.name, "rebalance"));
    nameCell.appendChild(sym);
    nameCell.appendChild(el("span", "reb-rule", esc(REB_RULE_LABEL[r.rule] || r.rule)));
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

    const curCell = el("div", "reb-c reb-cur",
      `<span>${r.current_pct.toFixed(2)}%</span>` +
      `<small>${sensitive(`${fmtCZK(r.current_czk)} CZK`, "position value")}</small>`);
    const bandCell = el("div", "reb-c reb-band", `${r.low.toFixed(1)}–${r.high.toFixed(1)}%`);
    const statusCell = el("div", "reb-c reb-status",
      `<span class="chip ${rebStatusClass(r.status)}">${r.status}</span>`);

    row.appendChild(nameCell);
    row.appendChild(curCell);
    row.appendChild(bandCell);
    row.appendChild(statusCell);

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

      cells.push({ r, input, czk, projPct, projBand, row });
      input.addEventListener("input", recompute);
    } else {
      // Sleeve: combined band, no single trade — the human spreads it across members.
      const planCell = el("div", "reb-c reb-plan reb-plan-ro",
        (r.action
          ? `<span class="chip ${rebActionClass(r.action)}">${fmtSignedWeight(r.suggest_delta_pct)}</span>`
          : `<span class="muted">in band</span>`) +
        `<small>spread across members</small>`);
      row.appendChild(planCell);
      row.appendChild(el("div", "reb-c reb-proj", "<span class=\"muted\">—</span>"));
    }
    return row;
  };

  const targetRows = (plan.rows || []).filter((r) => r.kind === "target");
  const sleeveRows = (plan.rows || []).filter((r) => r.kind === "sleeve");

  const grid = el("div", "reb-tbl");
  grid.appendChild(headRow("Targets"));
  targetRows.forEach((r) => {
    grid.appendChild(buildRow(r));
    const tax = taxDetails(r);
    if (tax) grid.appendChild(tax);
  });
  out.appendChild(grid);

  if (sleeveRows.length) {
    const sgrid = el("div", "reb-tbl reb-tbl-sleeves");
    sgrid.appendChild(headRow("Sleeves"));
    sleeveRows.forEach((r) => {
      sgrid.appendChild(buildRow(r));
      if (r.members && r.members.length) {
        const det = el("details", "reb-members");
        const held = r.members.filter((m) => m.current_pct > 0).length;
        det.appendChild(el("summary", null,
          `${r.members.length} members · ${held} held`));
        const ml = el("div", "reb-members-list");
        r.members.forEach((m) => {
          ml.appendChild(el("div", "reb-member",
            `<span class="reb-member-sym">${esc(m.symbol)}</span>` +
            `<span>${m.current_pct.toFixed(2)}%</span>` +
            `<small>${sensitive(`${fmtCZK(m.current_czk)} CZK`, "position value")}</small>`));
        });
        det.appendChild(ml);
        sgrid.appendChild(det);
      }
    });
    out.appendChild(sgrid);
  }

  if (plan.untargeted && plan.untargeted.length) {
    const det = el("details", "reb-untargeted");
    det.appendChild(el("summary", null,
      `Untargeted holdings — ${plan.untargeted.length} names, ` +
      `${plan.untargeted_pct.toFixed(1)}% of NAV (no band; not in the plan)`));
    const list = el("div", "reb-untargeted-list");
    plan.untargeted.forEach((u) => {
      const r = el("div", "reb-untargeted-row");
      r.innerHTML =
        `<span class="reb-link reb-member-sym">${esc(u.symbol)}</span>` +
        `<span>${u.current_pct.toFixed(2)}%</span>` +
        `<small>${sensitive(`${fmtCZK(u.current_czk)} CZK`, "position value")}</small>`;
      r.querySelector(".reb-link").addEventListener("click", () => analyzeFromAnywhere(u.symbol));
      list.appendChild(r);
    });
    det.appendChild(list);
    out.appendChild(det);
  }

  out.appendChild(el("div", "hint",
    "Suggested amounts move each name to the nearest band edge (the minimal action). " +
    "Edit any Plan amount to simulate; \u201cReset to suggested\u201d restores them. " +
    "Cash totals include the sleeves' suggested buys/sells (fixed — you allocate those across members). " +
    "Net cash > 0 means trims fund the buys; < 0 means you'd need fresh cash (e.g. from the untargeted bucket)."));

  function recompute() {
    let raised = 0, spent = 0, closed = 0, total = 0;
    cells.forEach(({ r, input, czk, projPct, projBand, row }) => {
      total += 1;
      let d = parseFloat(input.value);
      if (!Number.isFinite(d)) d = 0;
      const proj = r.current_pct + d;
      const inBand = proj >= r.low - 0.01 && proj <= r.high + 0.01;
      if (inBand) closed += 1;
      if (d < 0) raised += -d; else spent += d;

      czk.innerHTML = d
        ? sensitive(`${d > 0 ? "+" : "\u2212"}${fmtCZK(Math.abs(pctToCzk(d, base)))} CZK`, "planned trade size")
        : "<span class=\"muted\">no change</span>";
      projPct.textContent = `${proj.toFixed(2)}%`;
      projBand.textContent = inBand ? "in band" : "out";
      projBand.className = "chip reb-proj-band " + (inBand ? "good" : "warn");
      row.classList.toggle("planned-sell", d < -0.001);
      row.classList.toggle("planned-buy", d > 0.001);
    });

    // Sleeves aren't per-name editable (the human spreads them across members),
    // but their suggested buys/sells are real capital the plan needs. Folding
    // them in at the suggested amount keeps "cash needed" honest — otherwise the
    // headline silently ignores ~15% of NAV in sleeve buys.
    (plan.rows || []).filter((r) => r.kind === "sleeve").forEach((r) => {
      const d = r.suggest_delta_pct || 0;
      if (r.action === "trim") raised += -d;
      else if (r.action === "buy") spent += d;
    });

    const raisedCzk = pctToCzk(raised, base);
    const spentCzk = pctToCzk(spent, base);
    const net = raised - spent;
    const netCzk = pctToCzk(net, base);
    $("#reb-stat-raised").innerHTML =
      `${sensitive(`${fmtCZK(raisedCzk)} CZK`, "cash freed")} <small>${raised.toFixed(2)}%</small>`;
    $("#reb-stat-spent").innerHTML =
      `${sensitive(`${fmtCZK(spentCzk)} CZK`, "cash needed")} <small>${spent.toFixed(2)}%</small>`;
    const netEl = $("#reb-stat-net");
    netEl.innerHTML =
      `${sensitive(`${net >= 0 ? "+" : "\u2212"}${fmtCZK(Math.abs(netCzk))} CZK`, "net cash")} ` +
      `<small>${fmtSignedWeight(net)}</small>`;
    netEl.classList.toggle("good", net >= -0.01);
    netEl.classList.toggle("bad", net < -0.01);
    const closedEl = $("#reb-stat-closed");
    closedEl.textContent = `${closed}/${total}`;
    closedEl.classList.toggle("good", total > 0 && closed === total);
  }

  const reset = $("#reb-reset");
  if (reset) {
    reset.onclick = () => {
      cells.forEach(({ r, input }) => { input.value = String(rebDefaultDelta(r)); });
      recompute();
    };
  }

  const simBtn = $<HTMLButtonElement>("#reb-simulate");
  if (simBtn) {
    simBtn.onclick = async () => {
      const trades: WhatifTrade[] = [];
      cells.forEach(({ r, input }) => {
        const d = parseFloat(input.value);
        if (!Number.isFinite(d) || Math.abs(d) < 0.001) return;
        const czk = pctToCzk(d, base);
        if (czk == null || czk === 0) return;
        trades.push({ symbol: r.name, delta_czk: czk });
      });
      // Share the staged basket with the Trade desk so it can preview/place the
      // exact same trades you just simulated here. Persisted server-side too, so
      // it survives a reload / navigation instead of living only in this tab.
      state.stagedBasket = trades.slice();
      void api("/api/trade/basket", "POST", { trades }).catch(() => { /* best-effort */ });
      const box = $("#reb-whatif");
      if (!trades.length) {
        box.innerHTML = `<div class="hint">Nothing staged — edit a Plan amount on a targeted name, then simulate. (Sleeves are spread across members by hand, so they are not staged.)</div>`;
        return;
      }
      box.innerHTML = `<div class="status">Simulating…</div>`;
      simBtn.disabled = true;
      try {
        const wf = await api("/api/whatif", "POST", { trades });
        renderWhatif(wf);
      } catch (e) {
        box.innerHTML = `<div class="status err">Simulation failed: ${esc(e.message)}</div>`;
      } finally {
        simBtn.disabled = false;
      }
    };
  }

  recompute();
}

// ---- what-if "after" panel -------------------------------------------------
const whatifStat = (label: string, valueHtml: string, cls?: string) => statTile(label, valueHtml, { cls, html: true });

function renderWhatif(wf: Whatif) {
  const box = $("#reb-whatif");
  box.innerHTML = "";
  const s = wf.summary || {};
  const ccy = wf.currency;
  const card = el("div", "whatif-card");
  card.appendChild(el("div", "whatif-title", `Projected portfolio after ${wf.trades.length} trade(s)`));

  const stats = el("div", "reb-stats");
  stats.appendChild(whatifStat("Bands in-band",
    `${s.bands_in_before} \u2192 ${s.bands_in_after} / ${s.bands_total}`,
    s.bands_in_after >= s.bands_in_before ? "good" : "bad"));
  const cashAfter = wf.cash ? wf.cash.after : null;
  stats.appendChild(whatifStat("Cash after",
    cashAfter == null ? "n/a" : sensitive(`${fmtCZK(cashAfter)} ${esc(ccy)}`, "cash after"),
    cashAfter == null ? "muted" : cashAfter < 0 ? "bad" : "good"));
  stats.appendChild(whatifStat("Net cash",
    sensitive(`${s.net_cash_czk >= 0 ? "+" : "\u2212"}${fmtCZK(Math.abs(s.net_cash_czk))} ${esc(ccy)}`, "net cash"),
    s.net_cash_czk >= 0 ? "good" : "bad"));
  stats.appendChild(whatifStat("Realized taxable gain",
    sensitive(`${fmtCZK(s.realized_taxable_gain_czk)} ${esc(ccy)}`, "taxable gain"),
    s.realized_taxable_gain_czk > 0 ? "warn" : "good"));
  card.appendChild(stats);

  const afterRows: Record<string, RebRow> = {};
  ((wf.after && wf.after.rows) || []).forEach((r) => { if (r.kind === "target") afterRows[r.name] = r; });
  card.appendChild(simpleTable({
    className: "whatif-table",
    head: `<tr><th>Name</th><th class="num">Trade</th><th>Before</th><th>After</th><th class="num">After weight</th></tr>`,
    rows: wf.trades,
    cells: (t: { symbol: string; delta_czk: number }) => {
      const ar = afterRows[t.symbol];
      const before = (wf.before_status && wf.before_status[t.symbol]) || "\u2014";
      return `<td>${esc(t.symbol)}</td>` +
        `<td class="num">${sensitive(`${t.delta_czk >= 0 ? "+" : "\u2212"}${fmtCZK(Math.abs(t.delta_czk))}`, "trade size")}</td>` +
        `<td><span class="chip ${rebStatusClass(before)}">${esc(before)}</span></td>` +
        `<td>${ar ? `<span class="chip ${rebStatusClass(ar.status)}">${esc(ar.status)}</span>` : "\u2014"}</td>` +
        `<td class="num">${ar ? ar.current_pct.toFixed(2) + "%" : "\u2014"}</td>`;
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
      action: trade.delta_czk < 0 ? "trim" : "buy",
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
  det.appendChild(el("summary", null, `Tax lots to sell for the ${esc(r.name)} trim — ${bits.join(" · ")}`));

  const list = el("div", "reb-tax-list");
  t.lots.forEach((l) => {
    const b = TAX_BUCKET[l.bucket] || { label: l.bucket, cls: "muted" };
    const when = l.open_datetime ? String(l.open_datetime).slice(0, 10) : "?";
    const dte = (l.days_to_exempt != null && l.days_to_exempt > 0)
      ? `<small class="muted">${l.days_to_exempt}d to exempt</small>` : "";
    list.appendChild(el("div", "reb-tax-row",
      `<span class="chip ${b.cls}">${esc(b.label)}</span>` +
      `<span class="reb-tax-date">opened ${esc(when)} ${dte}</span>` +
      `<span>${sensitive(`${fmtCZK(l.proceeds)} ${esc(t.currency)}`, "lot proceeds")}</span>` +
      `<span class="${l.gain >= 0 ? "good" : "bad"}">gain ${sensitive(`${l.gain >= 0 ? "+" : "\u2212"}${fmtCZK(Math.abs(l.gain))}`, "lot gain")}</span>`));
  });
  det.appendChild(list);

  if (t.shortfall > 0) {
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

function analyzeFromAnywhere(sym: string | null | undefined) {
  const ticker = cleanSymbol(sym);
  if (!ticker) return;
  pushNav({ view: "deepdive", ticker });
  setActiveView("deepdive");
  $<HTMLInputElement>("#ticker-input").value = ticker;
  pullTicker(ticker, { push: false });
}

// Cache-first open for in-report ticker links: show what we already have
// instantly, and only hit the network (live pull) when there's no cached
// dossier. Browsing a report shouldn't trigger a slow pull per click.
async function openTicker(sym: string | null | undefined) {
  const ticker = cleanSymbol(sym);
  if (!ticker) return;
  pushNav({ view: "deepdive", ticker });
  setActiveView("deepdive");
  $<HTMLInputElement>("#ticker-input").value = ticker;
  const status = $("#dd-status");
  status.classList.remove("err");
  status.textContent = `Loading ${ticker}…`;
  try {
    const rec = await api("/api/research/" + encodeURIComponent(ticker));
    status.textContent = `Cached ${rec.symbol} from ${new Date(rec.as_of).toLocaleString()} — press Analyze to refresh`;
    // Paint everything that's already on file now; the recent-pulls change log is
    // a separate fetch that streams in under its own progress bar (see below).
    // Opening a ticker anchors on its price history (nav already pushed above).
    renderDeepDive(rec, { anchorChart: true });
    hydrateHistory(rec);
  } catch (_e) {
    await pullTicker(ticker, { push: false, anchor: true });  // nothing cached -> pull live
  }
}

export {
  REB_RULE_LABEL,
  rebStatusClass,
  rebActionClass,
  pctToCzk,
  rebDefaultDelta,
  loadRebalance,
  renderRebalance,
  analyzeFromAnywhere,
  openTicker,
};
