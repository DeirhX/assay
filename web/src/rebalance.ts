// @ts-nocheck
import { $, api, el, esc, fmtCZK, fmtSignedWeight, fmtStamp, sensitive } from "./core";
import { hydrateHistory, pullTicker, renderDeepDive } from "./deepdive";
import { cleanSymbol, pushNav, setActiveView } from "./shell";

// ---- rebalance planner -----------------------------------------------------
const REB_RULE_LABEL = {
  trim_only: "trim only", do_not_add: "don't add", reduce: "reduce",
  avoid: "avoid", accumulate: "accumulate", hold: "hold", wait: "wait",
};
const rebStatusClass = (s) => (s === "ABOVE" ? "bad" : s === "BELOW" ? "warn" : "good");
const rebActionClass = (a) => (a === "trim" ? "bad" : a === "buy" ? "good" : a === "review" ? "warn" : "muted");
// Weights are percent of the invested book, so size money off invested value
// (not NAV) — that keeps a row's CZK equal to its actual market value.
const pctToCzk = (pct, base) => (typeof base === "number" && pct != null ? Math.round((pct / 100) * base) : null);
// Default planned amount: prefill the minimal band-closing trade only for clear
// trim/buy actions. "review" (accumulate over ceiling) and untargeted names are
// judgement calls, so they start at zero — the human decides.
const rebDefaultDelta = (r) => (r.action === "trim" || r.action === "buy" ? r.suggest_delta_pct : 0);

async function loadRebalance() {
  const status = $("#reb-status");
  const summary = $("#reb-summary");
  const out = $("#reb-result");
  status.classList.remove("err");
  status.textContent = "Loading rebalance plan…";
  summary.innerHTML = "";
  out.innerHTML = "";
  try {
    const plan = await api("/api/rebalance");
    status.textContent = "";
    renderRebalance(plan);
  } catch (e) {
    summary.innerHTML = "";
    out.innerHTML = "";
    status.textContent = "Could not load rebalance plan: " + e.message;
    status.classList.add("err");
  }
}

function renderRebalance(plan) {
  const summary = $("#reb-summary");
  const out = $("#reb-result");
  const nav = plan.nav;
  // Weights are % of invested book, so money is sized off invested value.
  const base = typeof plan.invested === "number" ? plan.invested : nav;
  out.innerHTML = "";

  summary.innerHTML =
    `<div class="reb-meta">` +
    `<span>NAV ${sensitive(`${fmtCZK(nav)} ${esc(plan.currency)}`, "total NAV")}</span>` +
    `<span>invested ${sensitive(`${fmtCZK(plan.invested)} ${esc(plan.currency)}`, "invested book")}</span>` +
    `<span>snapshot ${esc(fmtStamp(plan.snapshot))}</span>` +
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

  const cells = []; // live-updated derived references, one per interactive row

  const headRow = (title) => {
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

  const buildRow = (r) => {
    const row = el("div", "reb-row reb-data-row");
    const sym = el("span", "reb-sym", esc(r.name));
    if (r.kind === "target" && r.held) {
      sym.classList.add("reb-link");
      sym.title = "Open dossier";
      sym.addEventListener("click", () => analyzeFromAnywhere(r.name));
    }
    const nameCell = el("div", "reb-c reb-name");
    nameCell.appendChild(sym);
    nameCell.appendChild(el("span", "reb-rule", esc(REB_RULE_LABEL[r.rule] || r.rule)));
    if (r.note) nameCell.title = r.note;

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
      input.title = r.action
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
  targetRows.forEach((r) => grid.appendChild(buildRow(r)));
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
  recompute();
}

function analyzeFromAnywhere(sym) {
  const ticker = cleanSymbol(sym);
  if (!ticker) return;
  pushNav({ view: "deepdive", ticker });
  setActiveView("deepdive");
  $("#ticker-input").value = ticker;
  pullTicker(ticker, { push: false });
}

// Cache-first open for in-report ticker links: show what we already have
// instantly, and only hit the network (live pull) when there's no cached
// dossier. Browsing a report shouldn't trigger a slow pull per click.
async function openTicker(sym) {
  const ticker = cleanSymbol(sym);
  if (!ticker) return;
  pushNav({ view: "deepdive", ticker });
  setActiveView("deepdive");
  $("#ticker-input").value = ticker;
  const status = $("#dd-status");
  status.classList.remove("err");
  status.textContent = `Loading ${ticker}…`;
  try {
    const rec = await api("/api/research/" + encodeURIComponent(ticker));
    status.textContent = `Cached ${rec.symbol} from ${new Date(rec.as_of).toLocaleString()} — press Analyze to refresh`;
    // Paint everything that's already on file now; the recent-pulls change log is
    // a separate fetch that streams in under its own progress bar (see below).
    renderDeepDive(rec);
    hydrateHistory(rec);
  } catch (_e) {
    await pullTicker(ticker, { push: false });  // nothing cached -> pull live
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
