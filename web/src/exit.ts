// Exit view: the advisory graceful-exit planner (tools/exit_plan.py).
//
// For every name the rebalance planner wants to shrink, this renders a
// tax-timed, liquidity-aware scale-out: which lots to sell now vs defer past the
// Czech 3-year exemption, a suggested GTC limit ladder split into tranches, and
// an options overlay (covered call / protective-put collar). Nothing here trades
// — "Stage tranche" just drops one slice into the shared trade-desk basket, which
// you still preview and place by hand on the Trade view.
import {
  $, api, el, esc, fmtCZK, loadError, relAge, sensitive, setLoading, state, statTile, nextToken, isStaleToken,
} from "./core";
import type {
  ExitPlanResponse, ExitPosition, ExitStageResponse, ExitStageCallResponse,
  ExitProtectivePut, ExitCoveredCallRung,
} from "./api-types";
import { openTicker } from "./ticker-nav";
import { pushNav, setActiveView } from "./shell";

// Config knobs (mirror exit_plan.py defaults); tunable from the header and sent
// back on every (re)build and stage so the server rebuilds an identical plan.
const cfg = { horizon_days: 10, adv_slice_pct: 0.12, near_exempt_days: 120, tax_rate: 0.15 };

const czk = (v: number | null | undefined) => (v == null ? "n/a" : sensitive(fmtCZK(v)));
const pct = (v: number | null | undefined, digits = 1) => (v == null ? "n/a" : `${Number(v).toFixed(digits)}%`);

const END_STATE_LABEL: Record<string, string> = {
  zero: "Full exit → 0%",
  ceiling: "Trim to ceiling",
  stub: "Trim to stub",
};

export async function loadExit(): Promise<void> {
  const token = nextToken("exit");
  const status = $("#exit-status");
  setLoading(status, "Building exit plans…", true);
  const summary = $("#exit-summary");
  const body = $("#exit-body");
  if (summary) summary.innerHTML = "";
  if (body) body.innerHTML = "";
  try {
    const data = await api<ExitPlanResponse>(`/api/exit-plan?${cfgQuery()}`);
    if (isStaleToken("exit", token)) return;
    renderExit(data);
    if (status) status.textContent = "";
  } catch (e) {
    if (isStaleToken("exit", token)) return;
    if (summary) summary.innerHTML = "";
    if (body) body.innerHTML = "";
    loadError(status, "Could not build exit plan", e);
  }
}

function cfgQuery(): string {
  const p = new URLSearchParams();
  p.set("horizon_days", String(cfg.horizon_days));
  p.set("adv_slice_pct", String(cfg.adv_slice_pct));
  p.set("near_exempt_days", String(cfg.near_exempt_days));
  p.set("tax_rate", String(cfg.tax_rate));
  return p.toString();
}

function renderExit(data: ExitPlanResponse): void {
  renderControls();
  renderSummary(data);
  const body = $("#exit-body");
  if (!body) return;
  body.innerHTML = "";
  if (!data.positions.length) {
    body.innerHTML =
      `<div class="empty-state">Nothing to exit — every targeted name is within its band. ` +
      `Names appear here when the rebalance planner marks them <strong>reduce</strong>, ` +
      `<strong>trim only</strong>, <strong>hold-don't-add</strong>, or <strong>avoid</strong> above the band.</div>`;
    return;
  }
  data.positions.forEach((p) => body.appendChild(positionCard(p, data.currency)));
}

// ---- header config controls ------------------------------------------------
function renderControls(): void {
  const host = $("#exit-controls");
  if (!host || host.dataset.wired === "1") return;
  host.dataset.wired = "1";
  host.innerHTML =
    `<label>Horizon <input id="exit-horizon" type="number" min="1" max="60" step="1" value="${cfg.horizon_days}"> d</label>` +
    `<label>ADV slice <input id="exit-adv" type="number" min="1" max="100" step="1" value="${Math.round(cfg.adv_slice_pct * 100)}"> %</label>` +
    `<label>Near-exempt <input id="exit-near" type="number" min="0" max="400" step="5" value="${cfg.near_exempt_days}"> d</label>` +
    `<label>Tax rate <input id="exit-tax" type="number" min="0" max="60" step="1" value="${Math.round(cfg.tax_rate * 100)}"> %</label>`;
  const rebuild = () => {
    cfg.horizon_days = clampNum($<HTMLInputElement>("#exit-horizon")?.value, 1, 60, cfg.horizon_days);
    cfg.adv_slice_pct = clampNum($<HTMLInputElement>("#exit-adv")?.value, 1, 100, cfg.adv_slice_pct * 100) / 100;
    cfg.near_exempt_days = clampNum($<HTMLInputElement>("#exit-near")?.value, 0, 400, cfg.near_exempt_days);
    cfg.tax_rate = clampNum($<HTMLInputElement>("#exit-tax")?.value, 0, 60, cfg.tax_rate * 100) / 100;
    loadExit();
  };
  host.querySelectorAll("input").forEach((i) => i.addEventListener("change", rebuild));
}

function clampNum(raw: string | undefined, lo: number, hi: number, fallback: number): number {
  const n = parseFloat(raw ?? "");
  if (!Number.isFinite(n)) return fallback;
  return Math.min(hi, Math.max(lo, n));
}

// ---- summary ---------------------------------------------------------------
function renderSummary(data: ExitPlanResponse): void {
  const host = $("#exit-summary");
  if (!host) return;
  host.innerHTML = "";
  const strip = el("div", "reb-stats");
  strip.appendChild(statTile("Names to exit", String(data.positions.length)));
  strip.appendChild(statTile("Total to sell", czk(data.totals.exit_czk), { html: true }));
  strip.appendChild(statTile("Sell now", czk(data.totals.sell_now_czk), { html: true, cls: "good", title: "Tax-free / loss-harvest lots that can go immediately" }));
  strip.appendChild(statTile("Deferred", czk(data.totals.defer_czk), { html: true, cls: data.totals.defer_czk > 0 ? "warn" : "muted", title: "Held back on near-exempt taxable-gain lots" }));
  strip.appendChild(statTile("Tax cost now", czk(data.totals.tax_cost_now), { html: true, cls: data.totals.tax_cost_now > 0 ? "bad" : "muted" }));
  strip.appendChild(statTile("Tax saved by waiting", czk(data.totals.tax_saved_by_waiting), { html: true, cls: data.totals.tax_saved_by_waiting > 0 ? "good" : "muted" }));
  host.appendChild(strip);
}

// ---- per-position card -----------------------------------------------------
function positionCard(p: ExitPosition, baseCcy: string): HTMLElement {
  const card = el("div", "card exit-card");

  const head = el("div", "exit-head");
  const title = el("div", "exit-title");
  const sym = el("button", "exit-sym tlink-btn");
  sym.type = "button";
  sym.textContent = p.symbol;
  sym.title = `Open ${p.symbol} analysis`;
  sym.addEventListener("click", () => openTicker(p.symbol));
  title.appendChild(sym);
  const state_ = el("span", "exit-state " + (p.end_state === "zero" ? "bad" : "warn"));
  state_.textContent = END_STATE_LABEL[p.end_state] || p.end_state;
  title.appendChild(state_);
  if (p.rule) {
    const rule = el("span", "exit-rule muted");
    rule.textContent = p.rule.replace(/_/g, " ");
    title.appendChild(rule);
  }
  head.appendChild(title);

  // Just the drift here — the "how much to sell" lives (once) in the
  // recommendation block below, so we don't print the same numbers twice.
  const meta = el("div", "exit-meta");
  meta.innerHTML =
    `<span class="exit-drift" title="Current weight → target weight (% of invested book)">` +
    `${pct(p.current_pct, 2)} <span class="exit-arrow">→</span> <strong>${pct(p.target_pct, 2)}</strong></span>`;
  head.appendChild(meta);
  card.appendChild(head);

  // The headline: one plain-language recommendation + the primary action. The
  // tax/liquidity/options machinery that justifies it lives in the expander so
  // the card reads as "do this" first, "here's the math" on demand.
  card.appendChild(recommendationBlock(p));

  const hasOpts = !!(p.options && (p.options.covered_call || p.options.protective_put));
  const details = el("details", "exit-details");
  const summary = el("summary", "exit-details-summary");
  summary.textContent = `Show details — tax layering, scale-out schedule${hasOpts ? ", options overlay" : ""}`;
  details.appendChild(summary);
  details.appendChild(taxBlock(p));
  details.appendChild(scheduleBlock(p, baseCcy));
  const opt = optionsBlock(p);
  if (opt) details.appendChild(opt);
  card.appendChild(details);
  return card;
}

// One-sentence "what to do", built from the same numbers the details expand on.
function recommendationBlock(p: ExitPosition): HTMLElement {
  const box = el("div", "exit-reco");
  const hasCallRoute = !!p.options?.covered_call_ladder?.length;
  if (hasCallRoute) {
    const tabs = el("div", "exit-route-tabs");
    tabs.setAttribute("role", "tablist");
    const shares = el("button", "exit-route-tab active", "Sell shares") as HTMLButtonElement;
    const calls = el("button", "exit-route-tab", "Covered-call exit") as HTMLButtonElement;
    shares.type = calls.type = "button";
    shares.setAttribute("aria-selected", "true");
    calls.setAttribute("aria-selected", "false");
    tabs.append(shares, calls);
    box.appendChild(tabs);
  }

  const sharePanel = el("div", "exit-route-panel active");
  sharePanel.dataset.exitRoute = "shares";
  const t = p.tax;
  const s = p.schedule;
  const keepPct = p.current_czk > 0 ? (100 * (p.current_czk - p.exit_czk)) / p.current_czk : 0;
  const verb = p.end_state === "zero" ? "Exit fully" : `Reduce to ${pct(p.target_pct, 2)}`;
  const keepStr = p.end_state === "zero" ? "" : ` <span class="muted">Keeps ${pct(keepPct, 0)} of the position.</span>`;

  let timing: string;
  if (t.sell_now_czk <= 0 && t.defer_czk > 0) {
    timing = `Hold all ${czk(t.defer_czk)} for now — every lot is a near-exempt taxable gain, so selling today just donates tax`;
  } else if (t.defer_czk > 0) {
    timing = `Sell ${czk(t.sell_now_czk)} now, hold back ${czk(t.defer_czk)} until the near-exempt lots turn tax-free`;
  } else {
    timing = `The whole ${czk(p.exit_czk)} can be sold now`;
  }
  const taxBits: string[] = [];
  if (t.harvested_loss_now > 0) taxBits.push(`harvests ${czk(t.harvested_loss_now)} of loss`);
  if (t.exempt_gain_now > 0) taxBits.push(`banks ${czk(t.exempt_gain_now)} tax-free`);
  if (t.taxable_gain_now > 0) taxBits.push(`realizes ~${czk(t.tax_cost_now)} tax`);
  const taxStr = taxBits.length ? ` — ${taxBits.join(", ")}` : (t.sell_now_czk > 0 ? " — no tax cost" : "");
  const liq = s.tranches.length > 1
    ? ` Work it out over ${s.tranches.length} slices${s.adv ? ` (~${Math.round(cfg.adv_slice_pct * 100)}% of ADV/day)` : ""}.`
    : "";
  const thin = s.tranches.some((tr) => tr.over_adv_cap)
    ? ` <span class="warn">Thin name — slices may move the price.</span>` : "";

  const lead = el("div", "exit-reco-lead");
  lead.innerHTML = `<span class="exit-reco-verb">${verb}:</span> sell <strong>${fmtNum(p.exit_shares)} sh</strong> (${czk(p.exit_czk)}).${keepStr}`;
  sharePanel.appendChild(lead);
  const sub = el("div", "exit-reco-sub");
  sub.innerHTML = `${timing}${taxStr}.${liq}${thin}`;
  sharePanel.appendChild(sub);

  if (s.tranches.length) {
    const first = s.tranches[0];
    const cta = el("button", "primary exit-reco-cta");
    cta.type = "button";
    cta.textContent = s.tranches.length > 1
      ? `Stage first slice (${fmtNum(first.shares)} sh) →`
      : `Stage the sell (${fmtNum(first.shares)} sh) →`;
    cta.title = "Drop this slice into the Trade desk basket (you still preview & place it)";
    cta.addEventListener("click", () => stageTranche(p.symbol, first.index, cta));
    sharePanel.appendChild(cta);
  }
  box.appendChild(sharePanel);

  if (hasCallRoute) {
    const callPanel = coveredCallRoute(p);
    box.appendChild(callPanel);
    const [shares, calls] = Array.from(box.querySelectorAll<HTMLButtonElement>(".exit-route-tab"));
    const select = (route: "shares" | "covered_call") => {
      shares.classList.toggle("active", route === "shares");
      calls.classList.toggle("active", route === "covered_call");
      shares.setAttribute("aria-selected", String(route === "shares"));
      calls.setAttribute("aria-selected", String(route === "covered_call"));
      sharePanel.classList.toggle("active", route === "shares");
      callPanel.classList.toggle("active", route === "covered_call");
    };
    shares.addEventListener("click", () => select("shares"));
    calls.addEventListener("click", () => select("covered_call"));
  }
  return box;
}

function fmtNum(v: number | null | undefined): string {
  if (v == null) return "n/a";
  return Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 });
}

// ---- tax layering ----------------------------------------------------------
function taxBlock(p: ExitPosition): HTMLElement {
  const box = el("div", "exit-section");
  const keepPct = p.current_czk > 0 ? (100 * (p.current_czk - p.exit_czk)) / p.current_czk : 0;
  const sub = p.end_state === "zero"
    ? `<span class="exit-h3-sub">full exit — nothing kept</span>`
    : `<span class="exit-h3-sub">of the current ${czk(p.current_czk)} position (keeping ${pct(keepPct, 0)})</span>`;
  box.appendChild(el("h3", "exit-h3", `Tax layering ${sub}`));
  const t = p.tax;

  // One bar over the WHOLE position so a partial reduce reads as partial: the
  // sell-now (green) + defer (amber) slices are what leaves, the muted remainder
  // is what stays. (The old two bars normalized to the exit amount, so a trim
  // with no deferral showed a misleading full "Sell now" bar.)
  const sellNow = Math.max(0, t.sell_now_czk);
  const defer = Math.max(0, t.defer_czk);
  const keep = Math.max(0, p.current_czk - p.exit_czk);
  const total = Math.max(1, sellNow + defer + keep);
  const w = (v: number) => ((v / total) * 100).toFixed(1);
  const seg = (v: number, cls: string, label: string) =>
    v > 0 ? `<span class="exit-posbar-seg ${cls}" style="width:${w(v)}%" title="${esc(label)} — ${((v / total) * 100).toFixed(0)}% of the position"></span>` : "";
  const legend = (cls: string, label: string, v: number) =>
    `<span class="exit-legend"><i class="exit-dot ${cls}"></i>${esc(label)} <b>${czk(v)}</b></span>`;
  const bars = el("div", "exit-posbar-wrap");
  bars.innerHTML =
    `<div class="exit-posbar" role="img" aria-label="share of the current position sold now, deferred, and kept">` +
      seg(sellNow, "good", "Sell now") + seg(defer, "warn", "Defer") + seg(keep, "keep", "Keep") +
    `</div>` +
    `<div class="exit-posbar-legend">` +
      legend("good", "Sell now", sellNow) +
      (defer > 0 ? legend("warn", "Defer", defer) : "") +
      (keep > 0 ? legend("keep", "Keep", keep) : "") +
    `</div>`;
  box.appendChild(bars);

  const notes = el("div", "exit-taxnotes hint");
  const parts: string[] = [];
  if (t.exempt_gain_now > 0) parts.push(`banks ${czk(t.exempt_gain_now)} of tax-free (3y+) gain`);
  if (t.harvested_loss_now > 0) parts.push(`harvests ${czk(t.harvested_loss_now)} of loss`);
  if (t.taxable_gain_now > 0) parts.push(`realizes ${czk(t.taxable_gain_now)} taxable gain (~${czk(t.tax_cost_now)} tax)`);
  notes.innerHTML = parts.length ? "Selling now " + parts.join("; ") + "." : "Sell-now leg carries no tax cost.";
  box.appendChild(notes);

  if (t.defer_lots.length) {
    const defer = el("div", "exit-defer");
    defer.innerHTML =
      `<div class="exit-defer-head warn">Hold back ${czk(t.defer_czk)} on near-exempt lots ` +
      `— waiting saves ~${czk(t.tax_saved_by_waiting)} in tax:</div>`;
    const ul = el("ul", "exit-defer-list");
    t.defer_lots.forEach((l) => {
      const li = el("li");
      const days = l.days_to_exempt == null ? "" : ` (${l.days_to_exempt}d)`;
      li.innerHTML =
        `${fmtNum(l.shares)} sh · gain ${czk(l.gain)} · tax if sold now ${czk(l.tax_if_sold_now)} · ` +
        `<strong>${esc(l.note)}${days}</strong>`;
      ul.appendChild(li);
    });
    defer.appendChild(ul);
    box.appendChild(defer);
  }
  return box;
}

// ---- schedule --------------------------------------------------------------
function scheduleBlock(p: ExitPosition, baseCcy: string): HTMLElement {
  const box = el("div", "exit-section");
  const s = p.schedule;
  const advNote = s.adv
    ? `sized to ~${Math.round(cfg.adv_slice_pct * 100)}% of ${fmtNum(s.adv)} ADV (≤${fmtNum(s.max_shares_per_day)} sh/day)`
    : "even time-slices (no volume data)";
  box.innerHTML = `<h3 class="exit-h3">Scale-out schedule <span class="muted exit-h3-sub">${esc(advNote)}</span></h3>`;

  if (!s.tranches.length) {
    box.appendChild(el("div", "hint", "Nothing to schedule now — the whole exit is on deferred near-exempt lots."));
    return box;
  }

  const tbl = el("table", "exit-sched");
  tbl.innerHTML =
    `<thead><tr><th>#</th><th>Date</th><th>Shares</th><th>${esc(baseCcy)}</th><th>GTC limit</th><th></th></tr></thead>`;
  const tbody = el("tbody");
  s.tranches.forEach((tr) => {
    const row = el("tr");
    const limit = tr.limit_price == null
      ? `<span class="muted">market</span>`
      : `${fmtNum(tr.limit_price)} ${esc(tr.limit_currency || "")}`;
    row.innerHTML =
      `<td>${tr.index}</td><td>${esc(tr.date)}</td>` +
      `<td>${fmtNum(tr.shares)}</td><td>${czk(tr.czk)}</td>` +
      `<td>${limit}${tr.over_adv_cap ? ` <span class="warn" title="Above the liquidity cap — thin name, slice may move the market">⚠</span>` : ""}</td>` +
      `<td></td>`;
    const btnCell = row.lastElementChild as HTMLElement;
    const btn = el("button", "ghost exit-stage-btn");
    btn.type = "button";
    btn.textContent = "Stage →";
    btn.title = "Drop this tranche into the Trade desk basket (you still preview & place it)";
    btn.addEventListener("click", () => stageTranche(p.symbol, tr.index, btn));
    btnCell.appendChild(btn);
    tbody.appendChild(row);
  });
  tbl.appendChild(tbody);
  box.appendChild(tbl);
  return box;
}

async function stageTranche(symbol: string, index: number, btn: HTMLButtonElement): Promise<void> {
  const prev = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Staging…";
  try {
    const resp = await api<ExitStageResponse>("/api/exit-plan/stage", "POST", { symbol, index, cfg });
    state.stagedBasket = resp.basket.slice();
    btn.textContent = "Staged ✓";
    // Jump to the trade desk so the human can preview/place the accumulated basket.
    pushNav({ view: "trade" });
    setActiveView("trade");
  } catch (e) {
    btn.textContent = "Failed";
    btn.title = (e as Error)?.message || "stage failed";
    setTimeout(() => { btn.disabled = false; btn.textContent = prev; }, 1500);
  }
}

async function stageCoveredCall(symbol: string, rungIndex: number, btn: HTMLButtonElement): Promise<void> {
  const prev = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Validating…";
  try {
    const resp = await api<ExitStageCallResponse>("/api/exit-plan/stage-call", "POST", {
      symbol, rung_index: rungIndex, cfg,
    }, { timeoutMs: 60_000 });
    state.stagedBasket = resp.basket.slice();
    btn.textContent = "Staged ✓";
    pushNav({ view: "trade", tab: "review" });
    setActiveView("trade");
  } catch (e) {
    btn.textContent = "Unavailable";
    btn.title = (e as Error)?.message || "covered-call staging failed";
    setTimeout(() => { btn.disabled = false; btn.textContent = prev; }, 1800);
  }
}

// ---- options overlay -------------------------------------------------------
function optionsBlock(p: ExitPosition): HTMLElement | null {
  const o = p.options;
  if (!o || !o.protective_put) return null;
  const box = el("div", "exit-section exit-options");
  box.appendChild(el("h3", "exit-h3", "Other options <span class=\"muted exit-h3-sub\">analysis only in this release</span>"));

  if (o.protective_put) box.appendChild(protectivePutCard(o.protective_put, o.currency));

  if (o.notes.length) {
    const notes = el("ul", "exit-opt-notes hint");
    o.notes.forEach((n) => { const li = el("li"); li.textContent = n; notes.appendChild(li); });
    box.appendChild(notes);
  }
  return box;
}

function quotePrice(v: number | null | undefined): string {
  return v == null || !Number.isFinite(Number(v)) ? "—" : fmtNum(v);
}

function coveredCallRoute(p: ExitPosition): HTMLElement {
  const panel = el("div", "exit-route-panel exit-call-route");
  panel.dataset.exitRoute = "covered_call";
  const o = p.options!;
  const rungs = o.covered_call_ladder || [];
  const lead = rungs.find((r) => r.recommended) || rungs[0];
  const uq = lead?.underlying_quote || o.covered_call?.underlying_quote;
  const hasIbkrUnderlying = uq?.last != null;
  const last = hasIbkrUnderlying ? uq!.last : o.underlying;
  const age = uq?.quote_at ? (relAge(uq.quote_at) || "age unavailable") : "age unavailable";
  const source = hasIbkrUnderlying ? `IBKR · ${age}` : "plan mark · execution quote unavailable";
  panel.innerHTML =
    `<div class="exit-call-route-head">` +
      `<div><span class="muted">Underlying last</span> <strong>${quotePrice(last)} ${esc(o.currency || "")}</strong></div>` +
      `<span class="${hasIbkrUnderlying ? "exit-live" : "exit-est"}">${esc(source)}</span>` +
    `</div>` +
    `<div class="exit-reco-sub"><strong>Conditional exit:</strong> sell calls against held shares. ` +
      `Premium is collected now, but shares leave only if assigned. Assignment is not guaranteed, so the position may not be reduced.</div>` +
    `<div class="exit-call-capacity">` +
      `<span><strong>${fmtNum(o.route_contracts)}</strong> contracts planned for this reduction</span>` +
      `<span><strong>${fmtNum(o.route_assigned_shares)}</strong> maximum assigned shares</span>` +
      `<span><strong>${fmtNum(o.available_contracts)}</strong> contracts available before working orders</span>` +
      `<span><strong>${fmtNum(o.available_covered_shares)}</strong> shares available to cover calls</span>` +
      `<span class="muted">held calls included; working orders are rechecked before staging</span>` +
    `</div>`;
  const executable = rungs.filter((r) => r.executable).length;
  if (!executable) {
    panel.appendChild(el("div", "trade-action-item blocker",
      "No executable covered call. Connect IBKR and require an exact contract with a live, uncrossed bid and ask; modeled, Yahoo, and Alpaca rungs stay analysis-only."));
  }
  panel.appendChild(coveredCallLadder(rungs, o.currency, p.symbol, true, o.route_contracts));
  return panel;
}

// Name the provenance of the premium: a live IBKR/Yahoo chain quote, or a
// Black-Scholes estimate when no chain quote was available (`estimate` is set
// server-side exactly when the premium was modeled, so it wins over `source`).
function sourceBadge(source: string, estimate: boolean): string {
  if (!estimate && source === "ibkr")
    return `<span class="exit-live" title="Live from your IBKR option chain">IBKR</span>`;
  if (!estimate && source === "alpaca")
    return `<span class="exit-live" title="From Alpaca's option feed (indicative/delayed unless OPRA-entitled)">Alpaca</span>`;
  if (!estimate && source === "yahoo")
    return `<span class="exit-live" title="From a live-ish Yahoo option chain (delayed/mid)">Yahoo</span>`;
  return `<span class="exit-est" title="Black-Scholes estimate — no live chain quote for this name">estimate</span>`;
}

function liqBadge(liq: ExitCoveredCallRung["liquidity"]): string {
  if (liq === "ok")
    return `<span class="exit-liq exit-liq-ok" title="Tight spread and some open interest/volume">liquid</span>`;
  if (liq === "thin")
    return `<span class="exit-liq exit-liq-thin" title="Wide spread or little open interest/volume — mind the fill">thin</span>`;
  return `<span class="exit-liq exit-liq-unknown" title="No live quote — premium is modeled">n/a</span>`;
}

// StrikePeek-style ladder: annualized yield vs. assignment odds across OTM
// strikes for the recommended expiry, so the user can pick their own cushion.
function coveredCallLadder(
  rungs: ExitCoveredCallRung[],
  ccy: string | null,
  symbol = "",
  stageable = false,
  routeContracts?: number,
): HTMLElement {
  const cur = ccy || "";
  const wrap = el("div", "exit-opt-card exit-ladder-wrap");
  const rows = rungs.map((r, index) => {
    const oi = r.open_interest == null ? "–" : String(r.open_interest);
    const vol = r.volume == null ? "–" : String(r.volume);
    const star = r.recommended ? ` <span class="exit-ladder-star" title="Matches the recommended strike above">★</span>` : "";
    const mny = `${r.moneyness_pct >= 0 ? "+" : ""}${r.moneyness_pct.toFixed(1)}%`;
    const canStage = stageable && !!r.executable && Number(routeContracts) > 0;
    const action = stageable
      ? `<button class="ghost exit-stage-call" type="button" data-rung-index="${index}" ${canStage ? "" : "disabled"} ` +
        `title="${canStage ? "Stage this exact IBKR contract in the Trade desk" : "Requires an exact IBKR contract with live bid and ask"}">` +
        `${canStage ? `Stage ${fmtNum(routeContracts)}× →` : "Unavailable"}</button>`
      : "";
    return `<tr class="${r.recommended ? "exit-ladder-rec" : ""}">` +
      `<td>${fmtNum(r.strike)} ${esc(cur)} <span class="muted">(${mny})</span>${star}</td>` +
      `<td class="num">${quotePrice(r.bid)}</td>` +
      `<td class="num">${quotePrice(r.ask)}</td>` +
      `<td class="num">${quotePrice(r.last)}</td>` +
      `<td>${fmtNum(r.premium)} · ${czk(r.premium_czk)}</td>` +
      `<td>${r.assignment_prob_pct == null ? "n/a" : "~" + pct(r.assignment_prob_pct, 0)}</td>` +
      `<td class="muted">${oi} / ${vol}</td>` +
      `<td>${liqBadge(r.liquidity)} ${sourceBadge(r.source, r.estimate)}</td>` +
      (stageable ? `<td>${action}</td>` : "") +
    `</tr>`;
  }).join("");
  wrap.innerHTML =
    `<div class="exit-opt-title">Covered-call strike ladder <span class="muted exit-h3-sub">live execution prices across OTM strikes` +
      (rungs[0]?.expiry ? ` · ${esc(rungs[0].expiry)} expiry` : "") + `</span></div>` +
    `<table class="exit-ladder"><thead><tr>` +
      `<th>Strike</th><th class="num">Bid (sell)</th><th class="num">Ask (buy)</th><th class="num">Last</th>` +
      `<th>Limit credit</th><th>Assign</th><th>OI / Vol</th><th>Quality</th>${stageable ? "<th></th>" : ""}` +
    `</tr></thead><tbody>${rows}</tbody></table>`;
  if (stageable && symbol) {
    wrap.querySelectorAll<HTMLButtonElement>("[data-rung-index]").forEach((btn) => {
      btn.addEventListener("click", () =>
        void stageCoveredCall(symbol, Number(btn.dataset.rungIndex), btn));
    });
  }
  return wrap;
}

function protectivePutCard(pp: ExitProtectivePut, ccy: string | null): HTMLElement {
  const card = el("div", "exit-opt-card");
  const cur = ccy || "";
  const net = pp.net_collar_premium;
  const collar = net == null ? "n/a"
    : (net >= 0 ? `${fmtNum(net)} ${esc(cur)} debit` : `${fmtNum(-net)} ${esc(cur)} credit`) + ` · ${czk(pp.net_collar_czk)}`;
  const lead =
    `Buy <strong>${pp.contracts}× ${fmtNum(pp.put_strike)} ${esc(cur)}</strong> puts to floor the exit at ` +
    `<strong>${fmtNum(pp.protected_floor)} ${esc(cur)}</strong> through the ${pp.days_to_exempt}-day wait to ${esc(pp.exempt_on)}.`;
  card.innerHTML =
    `<div class="exit-opt-title">Protective put / collar ${sourceBadge(pp.source, pp.estimate)}</div>` +
    `<div class="exit-opt-lead">${lead}</div>` +
    `<div class="exit-opt-grid">` +
      kv("Buy put", `${pp.contracts}× ${fmtNum(pp.put_strike)} ${esc(cur)}`) +
      kv("Expiry", `${esc(pp.expiry)} (${pp.dte}d · after ${esc(pp.exempt_on)})`) +
      kv("Put cost", `${fmtNum(pp.put_premium)} ${esc(cur)} · ${czk(pp.put_cost_czk)}`) +
      kv("Protected floor", `${fmtNum(pp.protected_floor)} ${esc(cur)}`) +
      kv("Collar (sell call)", `${fmtNum(pp.collar_call_strike)} ${esc(cur)} → ${collar}`) +
      kv("Tax saved by waiting", czk(pp.tax_saved_by_waiting_czk)) +
    `</div>`;
  return card;
}

function kv(k: string, v: string): string {
  return `<div class="exit-kv"><span class="exit-kv-k">${esc(k)}</span><span class="exit-kv-v">${v}</span></div>`;
}
