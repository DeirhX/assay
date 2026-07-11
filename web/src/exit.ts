// Exit view: the advisory graceful-exit planner (tools/exit_plan.py).
//
// For every name the rebalance planner wants to shrink, this renders a
// tax-timed, liquidity-aware scale-out: which lots to sell now vs defer past the
// Czech 3-year exemption, a suggested GTC limit ladder split into tranches, and
// an options overlay (covered call / protective-put collar). Nothing here trades
// — "Stage tranche" just adds one slice to the shared order queue. The projected
// book is reviewed next; placement remains a separate explicit Trade-desk step.
import {
  $, api, el, esc, loadError, sensitive, setLoading, state, statTile, nextToken, isStaleToken,
  relAge,
} from "./core";
import {
  gatewayUnavailableReason, getGatewayStatus, refreshGatewayStatus,
} from "./gateway";
import type {
  ExitPlanResponse, ExitPosition, ExitStageResponse, ExitCoveredCall, ExitProtectivePut,
  ExitCoveredCallRoute, ExitCoveredCallRung, ExitRouteKind, ExitRoutes,
} from "./api-types";
import { openTicker } from "./ticker-nav";
import { pushNav, setActiveView } from "./shell";

// Config knobs (mirror exit_plan.py defaults); tunable from the header and sent
// back on every (re)build and stage so the server rebuilds an identical plan.
const cfg = { horizon_days: 10, adv_slice_pct: 0.12, near_exempt_days: 120, tax_rate: 0.15 };

// Per-symbol route tab state survives re-renders until the user switches away.
const routePick = new Map<string, ExitRouteKind>();
let _lastExitPlan: ExitPlanResponse | null = null;

const fmtSignificant = (v: number, digits = 2) =>
  Number(v).toLocaleString(undefined, { maximumSignificantDigits: digits });
function fmtCompactMoney(v: number): string {
  const units = ["", "K", "M", "B", "T"];
  const sign = v < 0 ? -1 : 1;
  let scaled = Math.abs(Number(v));
  let unit = 0;
  while (scaled >= 1_000 && unit < units.length - 1) {
    scaled /= 1_000;
    unit += 1;
  }
  scaled = Number(scaled.toPrecision(2));
  if (scaled >= 1_000 && unit < units.length - 1) {
    scaled /= 1_000;
    unit += 1;
  }
  return `${fmtSignificant(sign * scaled)}${units[unit]}`;
}
const fmtShares = (v: number | null | undefined) =>
  v == null ? "n/a" : Math.round(Number(v)).toLocaleString();
const czk = (v: number | null | undefined) =>
  v == null ? "n/a" : sensitive(`${fmtCompactMoney(v)} CZK`);
const pct = (v: number | null | undefined, digits = 1) => (v == null ? "n/a" : `${Number(v).toFixed(digits)}%`);
const dash = "–";
const EXECUTION_QUOTE_MAX_AGE_MS = 120_000;

function selectedRoute(p: ExitPosition): ExitRouteKind {
  const routes = routesFor(p);
  const picked = routePick.get(p.symbol);
  if (picked && routes[picked].eligible) return picked;
  const recommended = p.routes?.recommended ?? "sell_shares";
  if (routes[recommended].eligible) return recommended;
  if (routes.sell_shares.eligible) return "sell_shares";
  if (routes.covered_call.eligible) return "covered_call";
  return "sell_shares";
}

function routesFor(p: ExitPosition): ExitRoutes {
  const cc = p.routes?.covered_call;
  const ss = p.routes?.sell_shares;
  return {
    sell_shares: ss ?? { eligible: true, reasons: [] },
    covered_call: cc ?? { eligible: false, reasons: ["Covered-call route not available for this name"] },
    recommended: p.routes?.recommended ?? "sell_shares",
  };
}

function quotePrice(v: number | null | undefined, ccy?: string | null): string {
  if (v == null || !Number.isFinite(v)) return dash;
  const cur = ccy ? ` ${esc(ccy)}` : "";
  return `${fmtNum(v)}${cur}`;
}

const END_STATE_LABEL: Record<string, string> = {
  zero: "Full exit → 0%",
  ceiling: "Trim to ceiling",
  stub: "Trim to stub",
};

export async function loadExit(): Promise<void> {
  const token = nextToken("exit");
  _lastExitPlan = null;
  renderExitGatewayNotice(null);
  void refreshGatewayStatus()
    .then(() => renderExitGatewayNotice(_lastExitPlan))
    .catch(() => renderExitGatewayNotice(_lastExitPlan));
  const status = $("#exit-status");
  setLoading(status, "Building exit plans…", true);
  const summary = $("#exit-summary");
  const body = $("#exit-body");
  if (summary) summary.innerHTML = "";
  if (body) body.innerHTML = "";
  try {
    const query = cfgQuery();
    const base = await api<ExitPlanResponse>(
      `/api/exit-plan?${query}&with_options=0`,
    );
    if (isStaleToken("exit", token)) return;
    _lastExitPlan = base;
    renderExit(base);
    renderExitGatewayNotice(base);
    if (!base.positions.length) {
      if (status) status.textContent = "";
      return;
    }
    setLoading(status, "Exit plans ready — loading live option routes…", true);
    try {
      const enriched = await api<ExitPlanResponse>(
        `/api/exit-plan?${query}&with_options=1`,
        "GET",
        null,
        { timeoutMs: 60_000 },
      );
      if (isStaleToken("exit", token)) return;
      _lastExitPlan = enriched;
      renderExit(enriched);
      renderExitGatewayNotice(enriched);
      if (status) status.textContent = "";
    } catch (e) {
      if (isStaleToken("exit", token)) return;
      if (status) {
        status.textContent =
          `Exit plans are ready, but live option routes could not load: ${(e as Error).message}`;
        status.classList.add("err");
      }
    }
  } catch (e) {
    if (isStaleToken("exit", token)) return;
    if (summary) summary.innerHTML = "";
    if (body) body.innerHTML = "";
    loadError(status, "Could not build exit plan", e);
  }
}

function renderExitGatewayNotice(data: ExitPlanResponse | null): void {
  const host = $("#exit-gateway-notice");
  if (!host) return;
  if (data && !data.positions.length) {
    host.innerHTML = "";
    return;
  }
  const gateway = getGatewayStatus();
  if (!gateway) {
    host.innerHTML =
      `<div class="ibkr-data-notice info"><strong>Checking live IBKR option data…</strong> ` +
      `The tax and direct-sale plan is already available.</div>`;
    return;
  }
  const reason = gatewayUnavailableReason(gateway);
  if (reason) {
    host.innerHTML =
      `<div class="ibkr-data-notice"><strong>Live IBKR option data unavailable.</strong> ` +
      `${esc(reason)} Exit plans remain usable with Yahoo/Alpaca or modeled levels, ` +
      `but exact contracts and executable bid/ask staging require IBKR.</div>`;
    return;
  }
  const sources = new Set(
    (data?.positions || [])
      .map((position) => position.options?.source)
      .filter((source): source is string => !!source && source !== "ibkr"),
  );
  if (sources.size) {
    const labels = [...sources].map((source) => source.replace(/_/g, " ")).join(", ");
    host.innerHTML =
      `<div class="ibkr-data-notice info"><strong>IBKR is connected, but did not return ` +
      `usable option data for every exit.</strong> Fallback levels shown from ${esc(labels)}; ` +
      `those rows remain indicative until an exact live IBKR quote is available.</div>`;
    return;
  }
  host.innerHTML = "";
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

  const hasOpts = !!(p.options && (p.options.protective_put || (!p.routes && p.options.covered_call)));
  const details = el("details", "exit-details");
  const summary = el("summary", "exit-details-summary");
  summary.textContent = `Show details — tax layering, scale-out schedule${hasOpts ? ", options analysis" : ""}`;
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
  const routes = routesFor(p);
  const route = selectedRoute(p);
  const o = p.options;
  const ccy = p.currency || o?.currency || "";

  if (o) box.appendChild(underlyingQuoteBar(o, ccy));
  if (p.routes || o?.covered_call_ladder?.length) {
    box.appendChild(routeControls(p, routes, () => {
      box.replaceWith(recommendationBlock(p));
    }));
  }

  if (route === "sell_shares") {
    box.appendChild(sellSharesReco(p, routes.sell_shares));
  } else {
    box.appendChild(coveredCallReco(p, routes.covered_call, ccy));
  }
  return box;
}

function underlyingQuoteBar(o: NonNullable<ExitPosition["options"]>, ccy: string): HTMLElement {
  const bar = el("div", "exit-underlying-quote");
  const q = o.underlying_quote;
  const last = q?.last != null ? `<strong class="exit-underlying-last">${fmtNum(q.last)} ${esc(ccy)}</strong>` : dash;
  const src = q?.source ? esc(q.source) : "source unknown";
  const age = q?.quote_timestamp
    ? `<span class="muted">${esc(relAge(q.quote_timestamp))}</span>`
    : "";
  bar.innerHTML =
    `<span class="exit-underlying-label">Underlying last</span> ${last}` +
    `<span class="muted"> · ${src}${age ? ` · ${age}` : ""}</span>`;
  return bar;
}

function routeControls(
  p: ExitPosition,
  routes: ExitRoutes,
  onChange: () => void,
): HTMLElement {
  const wrap = el("div", "exit-route-controls");
  const route = selectedRoute(p);
  const mk = (kind: ExitRouteKind, label: string, elig: { eligible: boolean; reasons: string[] }) => {
    const btn = el("button", `exit-route-btn${route === kind ? " active" : ""}${elig.eligible ? "" : " disabled"}`);
    btn.type = "button";
    btn.textContent = label;
    if (!elig.eligible && elig.reasons.length) btn.title = elig.reasons.join("; ");
    if (elig.eligible) {
      btn.addEventListener("click", () => {
        routePick.set(p.symbol, kind);
        onChange();
      });
    } else {
      btn.disabled = true;
    }
    return btn;
  };
  wrap.appendChild(mk("sell_shares", "Sell shares", routes.sell_shares));
  wrap.appendChild(mk("covered_call", "Covered-call exit", routes.covered_call));
  if (!routes.covered_call.eligible && routes.covered_call.reasons.length) {
    const why = el("span", "exit-route-why muted");
    why.textContent = `Covered call unavailable: ${routes.covered_call.reasons.join(" · ")}`;
    wrap.appendChild(why);
  }
  if (routes.recommended === route) {
    const tag = el("span", "exit-route-rec muted", "recommended");
    wrap.appendChild(tag);
  }
  return wrap;
}

function routeBlocked(box: HTMLElement, reasons: string[]): void {
  if (!reasons.length) return;
  const note = el("div", "exit-route-blocked warn");
  note.textContent = reasons.join(" · ");
  box.appendChild(note);
}

function sellSharesReco(p: ExitPosition, elig: { eligible: boolean; reasons: string[] }): HTMLElement {
  const inner = el("div", "exit-reco-route");
  if (!elig.eligible) {
    routeBlocked(inner, elig.reasons);
    return inner;
  }

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
  lead.innerHTML = `<span class="exit-reco-verb">${verb}:</span> sell <strong>${fmtShares(p.exit_shares)} sh</strong> (${czk(p.exit_czk)}).${keepStr}`;
  inner.appendChild(lead);
  const sub = el("div", "exit-reco-sub");
  sub.innerHTML = `${timing}${taxStr}.${liq}${thin}`;
  inner.appendChild(sub);

  if (s.tranches.length) {
    const first = s.tranches[0];
    const cta = el("button", "primary exit-reco-cta");
    cta.type = "button";
    cta.textContent = s.tranches.length > 1
      ? `Stage first slice (${fmtShares(first.shares)} sh) →`
      : `Stage the sell (${fmtShares(first.shares)} sh) →`;
    cta.title = "Add this slice to the order queue, then review the projected portfolio";
    cta.addEventListener("click", () => stageTranche(p.symbol, first.index, cta));
    inner.appendChild(cta);
  }
  return inner;
}

function coveredCallReco(
  p: ExitPosition,
  elig: ExitCoveredCallRoute,
  ccy: string,
): HTMLElement {
  const inner = el("div", "exit-reco-route");
  const o = p.options;
  const ladder = o?.covered_call_ladder ?? [];
  const contracts = elig.capacity_contracts ?? o?.covered_call?.contracts ?? 0;

  const disclaimer = el("div", "exit-reco-sub hint");
  disclaimer.innerHTML =
    `Covered-call exit is <strong>conditional</strong>: you collect premium now, but the position shrinks only if the calls are assigned. ` +
    `Assignment is not guaranteed — you may need to buy back the calls or roll.`;
  inner.appendChild(disclaimer);

  if (!elig.eligible) {
    routeBlocked(inner, elig.reasons);
    return inner;
  }
  if (elig.stageable === false && elig.reasons.length) {
    routeBlocked(inner, elig.reasons);
  }

  const cap = el("div", "exit-reco-sub");
  cap.textContent = contracts > 0
    ? `Up to ${contracts} contract${contracts === 1 ? "" : "s"} (${fmtShares(contracts * 100)} sh if assigned).`
    : "No whole contracts available to write.";
  inner.appendChild(cap);
  inner.appendChild(el(
    "div", "hint",
    "Live positions, working orders, and quote freshness are rechecked when you stage and place.",
  ));

  if (!ladder.length) {
    inner.appendChild(el("div", "hint", "No covered-call rungs available for this name."));
    return inner;
  }

  inner.appendChild(coveredCallLadderTable(p.symbol, ladder, ccy, contracts));
  return inner;
}

function assignmentCell(r: ExitCoveredCallRung): string {
  if (r.assignment_prob_pct == null) return dash;
  return `~${pct(r.assignment_prob_pct, 0)} assign prob`;
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
        `${fmtShares(l.shares)} sh · gain ${czk(l.gain)} · tax if sold now ${czk(l.tax_if_sold_now)} · ` +
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
    ? `sized to ~${Math.round(cfg.adv_slice_pct * 100)}% of ${fmtSignificant(s.adv)} ADV (≤${fmtShares(s.max_shares_per_day)} sh/day)`
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
      `<td>${fmtShares(tr.shares)}</td><td>${czk(tr.czk)}</td>` +
      `<td>${limit}${tr.over_adv_cap ? ` <span class="warn" title="Above the liquidity cap — thin name, slice may move the market">⚠</span>` : ""}</td>` +
      `<td></td>`;
    const btnCell = row.lastElementChild as HTMLElement;
    const btn = el("button", "ghost exit-stage-btn");
    btn.type = "button";
    btn.textContent = "Stage →";
    btn.title = "Add this tranche to the order queue, then review the projected portfolio";
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
    const resp = await api<ExitStageResponse>("/api/exit-plan/stage", "POST", {
      symbol, route: "sell_shares", index, cfg,
    });
    state.stagedBasket = resp.basket.slice() as typeof state.stagedBasket;
    btn.textContent = "Staged ✓";
    // Projection is a pre-trade safety gate: show where the accumulated order
    // queue lands before offering IBKR preview / placement.
    pushNav({ view: "target-state" });
    setActiveView("target-state");
  } catch (e) {
    btn.textContent = "Failed";
    btn.title = (e as Error)?.message || "stage failed";
    setTimeout(() => { btn.disabled = false; btn.textContent = prev; }, 1500);
  }
}

async function stageCoveredCall(
  symbol: string,
  rung: ExitCoveredCallRung,
  contracts: number,
  btn: HTMLButtonElement,
): Promise<void> {
  const prev = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Staging…";
  try {
    const resp = await api<ExitStageResponse>("/api/exit-plan/stage", "POST", {
      symbol,
      route: "covered_call",
      conid: rung.conid,
      expiry: rung.expiry,
      strike: rung.strike,
      contracts,
      cfg,
    });
    state.stagedBasket = resp.basket.slice() as typeof state.stagedBasket;
    btn.textContent = "Staged ✓";
    pushNav({ view: "target-state" });
    setActiveView("target-state");
  } catch (e) {
    btn.textContent = "Failed";
    btn.title = (e as Error)?.message || "stage failed";
    setTimeout(() => { btn.disabled = false; btn.textContent = prev; }, 1500);
  }
}

function rungBlockedReasons(r: ExitCoveredCallRung): string[] {
  const quoteTime = r.quote_timestamp ? new Date(r.quote_timestamp).getTime() : NaN;
  const localAge = Date.now() - quoteTime;
  const locallyFresh = Number.isFinite(quoteTime)
    && localAge >= -10_000 && localAge <= EXECUTION_QUOTE_MAX_AGE_MS;
  const quoteOk = typeof r.bid === "number" && typeof r.ask === "number"
    && r.bid > 0 && r.ask > 0 && r.bid <= r.ask;
  if (r.executable === true && r.conid && r.quote_fresh === true && locallyFresh && quoteOk) return [];
  const reasons: string[] = [];
  if (r.executable === false) reasons.push("Not executable");
  if (r.estimate) reasons.push("Modeled premium — no live IBKR quote");
  if (r.bid == null || r.ask == null) reasons.push("Missing bid/ask");
  if (typeof r.bid === "number" && typeof r.ask === "number" && r.bid > r.ask)
    reasons.push("Crossed bid/ask");
  if (!r.conid) reasons.push("No contract id");
  if (r.quote_fresh === false || !locallyFresh) reasons.push("Quote is stale");
  if (!reasons.length) reasons.push("Quote not executable");
  return reasons;
}

// ---- options overlay (analysis-only: puts/collars; covered calls live in route UI) ----
function optionsBlock(p: ExitPosition): HTMLElement | null {
  const o = p.options;
  if (!o) return null;
  const showCcAnalysis = !p.routes && (o.covered_call || o.covered_call_ladder?.length);
  if (!o.protective_put && !showCcAnalysis && !o.notes.length) return null;
  const box = el("div", "exit-section exit-options");
  box.appendChild(el("h3", "exit-h3",
    "Options analysis <span class=\"muted exit-h3-sub\">advisory only — protective puts and collars are not placeable from Exit</span>"));

  if (showCcAnalysis && o.covered_call) box.appendChild(coveredCallCard(o.covered_call, o.currency));
  if (showCcAnalysis && o.covered_call_ladder && o.covered_call_ladder.length > 1)
    box.appendChild(coveredCallLadder(o.covered_call_ladder, o.currency));
  if (o.protective_put) box.appendChild(protectivePutCard(o.protective_put, o.currency));

  if (o.notes.length) {
    const notes = el("ul", "exit-opt-notes hint");
    o.notes.forEach((n) => { const li = el("li"); li.textContent = n; notes.appendChild(li); });
    box.appendChild(notes);
  }
  return box;
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

// Executable covered-call ladder for the route UI. Bid/ask drive the order;
// annualized premium yield is more decision-useful than the historical last.
type LadderSortKey = "strike" | "expiry" | "bid" | "ask" | "yield" | "credit" | "assignment";
type SortDirection = "asc" | "desc";

function sortedRungs(
  rungs: ExitCoveredCallRung[],
  key: LadderSortKey,
  direction: SortDirection,
): ExitCoveredCallRung[] {
  const value = (rung: ExitCoveredCallRung): number | string | null => {
    if (key === "yield") return rung.premium_yield_annual_pct;
    if (key === "credit") return rung.limit_price ?? null;
    if (key === "assignment") return rung.assignment_prob_pct;
    return rung[key] ?? null;
  };
  return rungs
    .map((rung, index) => ({ rung, index }))
    .sort((a, b) => {
      const av = value(a.rung), bv = value(b.rung);
      if (av == null && bv == null) return a.index - b.index;
      if (av == null) return 1;
      if (bv == null) return -1;
      const compared = typeof av === "string"
        ? av.localeCompare(String(bv))
        : Number(av) - Number(bv);
      return (direction === "asc" ? compared : -compared) || a.index - b.index;
    })
    .map(({ rung }) => rung);
}

function coveredCallLadderTable(
  symbol: string,
  rungs: ExitCoveredCallRung[],
  ccy: string,
  contracts: number,
): HTMLElement {
  const wrap = el("div", "exit-opt-card exit-ladder-wrap");
  const tbl = el("table", "exit-ladder exit-ladder-exec");
  const thead = el("thead");
  const tbody = el("tbody");
  let sortKey: LadderSortKey | null = null;
  let direction: SortDirection = "asc";
  const columns: Array<{ key: LadderSortKey; label: string; title?: string }> = [
    { key: "strike", label: "Strike" },
    { key: "expiry", label: "Expiry" },
    { key: "bid", label: "Bid (sell)" },
    { key: "ask", label: "Ask (buy)" },
    {
      key: "yield",
      label: "Yield p.a.",
      title: "Option premium annualized over days to expiry, as a percentage of strike",
    },
    {
      key: "credit",
      label: "Min credit",
      title: "Minimum premium per share accepted by the sell-to-open limit order; multiply by 100 per contract",
    },
    { key: "assignment", label: "Assignment" },
  ];

  const renderHead = () => {
    const tr = el("tr");
    columns.forEach((column) => {
      const active = sortKey === column.key;
      const th = el("th", active ? "active" : "");
      if (column.title) th.title = column.title;
      th.setAttribute("aria-sort", active ? (direction === "asc" ? "ascending" : "descending") : "none");
      const btn = el("button", "exit-ladder-sort");
      btn.type = "button";
      btn.dataset.sort = column.key;
      btn.innerHTML = `${esc(column.label)} <span aria-hidden="true">${active ? (direction === "asc" ? "▲" : "▼") : "↕"}</span>`;
      btn.addEventListener("click", () => {
        if (sortKey === column.key) direction = direction === "asc" ? "desc" : "asc";
        else { sortKey = column.key; direction = "asc"; }
        renderHead();
        renderRows();
      });
      th.appendChild(btn);
      tr.appendChild(th);
    });
    tr.appendChild(el("th", undefined, "Action"));
    thead.replaceChildren(tr);
  };

  const renderRows = () => {
    tbody.replaceChildren();
    const visible = sortKey ? sortedRungs(rungs, sortKey, direction) : rungs;
    visible.forEach((r) => {
      const row = el("tr", r.recommended ? "exit-ladder-rec" : "");
      const blocked = rungBlockedReasons(r);
      const canStage = blocked.length === 0 && contracts > 0;

      const actionCell = el("td");
      if (canStage) {
        const btn = el("button", "ghost exit-stage-cc-btn");
        btn.type = "button";
        btn.textContent = "Stage →";
        btn.title = `Stage ${contracts}× ${fmtNum(r.strike)} call — assignment is conditional`;
        btn.addEventListener("click", () => stageCoveredCall(symbol, r, contracts, btn));
        actionCell.appendChild(btn);
      } else {
        const span = el("span", "muted exit-rung-blocked");
        span.textContent = blocked.length ? blocked.join("; ") : dash;
        if (blocked.length) span.title = blocked.join("; ");
        actionCell.appendChild(span);
      }

      row.innerHTML =
        `<td>${quotePrice(r.strike, ccy)}${r.recommended ? ` <span class="exit-ladder-star" title="Recommended rung">★</span>` : ""}</td>` +
        `<td>${esc(r.expiry)}<div class="muted">${sourceBadge(r.source, r.estimate)} ` +
          `${r.quote_timestamp ? esc(relAge(r.quote_timestamp)) : "age unavailable"}</div></td>` +
        `<td>${quotePrice(r.bid, ccy)}</td>` +
        `<td>${quotePrice(r.ask, ccy)}</td>` +
        `<td>${pct(r.premium_yield_annual_pct, 1)}</td>` +
        `<td>${r.limit_price == null ? dash : quotePrice(r.limit_price, ccy)}</td>` +
        `<td>${assignmentCell(r)}</td>`;
      row.appendChild(actionCell);
      tbody.appendChild(row);
    });
  };
  renderHead();
  renderRows();
  tbl.appendChild(thead);
  tbl.appendChild(tbody);
  wrap.appendChild(tbl);
  return wrap;
}

// Analysis ladder (yield vs assignment) for names without route UI.
function coveredCallLadder(rungs: ExitCoveredCallRung[], ccy: string | null): HTMLElement {
  const cur = ccy || "";
  const wrap = el("div", "exit-opt-card exit-ladder-wrap");
  const rows = rungs.map((r) => {
    const oi = r.open_interest == null ? "–" : String(r.open_interest);
    const vol = r.volume == null ? "–" : String(r.volume);
    const star = r.recommended ? ` <span class="exit-ladder-star" title="Matches the recommended strike above">★</span>` : "";
    const mny = `${r.moneyness_pct >= 0 ? "+" : ""}${r.moneyness_pct.toFixed(1)}%`;
    return `<tr class="${r.recommended ? "exit-ladder-rec" : ""}">` +
      `<td>${fmtNum(r.strike)} ${esc(cur)} <span class="muted">(${mny})</span>${star}</td>` +
      `<td>${pct(r.premium_yield_annual_pct, 1)}</td>` +
      `<td>${fmtNum(r.premium)} · ${czk(r.premium_czk)}</td>` +
      `<td>${r.assignment_prob_pct == null ? "n/a" : "~" + pct(r.assignment_prob_pct, 0)}</td>` +
      `<td class="muted">${oi} / ${vol}</td>` +
      `<td>${liqBadge(r.liquidity)} ${sourceBadge(r.source, r.estimate)}</td>` +
    `</tr>`;
  }).join("");
  wrap.innerHTML =
    `<div class="exit-opt-title">Strike ladder <span class="muted exit-h3-sub">yield vs. assignment across OTM strikes` +
      (rungs[0]?.expiry ? ` · ${esc(rungs[0].expiry)} expiry` : "") + `</span></div>` +
    `<table class="exit-ladder"><thead><tr>` +
      `<th>Strike</th><th>Yield p.a.</th><th>Premium</th><th>Assign</th><th>OI / Vol</th><th>Quality</th>` +
    `</tr></thead><tbody>${rows}</tbody></table>`;
  return wrap;
}

function coveredCallCard(c: ExitCoveredCall, ccy: string | null): HTMLElement {
  const card = el("div", "exit-opt-card");
  const cur = ccy || "";
  const assign = c.assignment_prob_pct == null ? "" : ` if assigned (~${pct(c.assignment_prob_pct, 0)})`;
  const lead =
    `Sell <strong>${c.contracts}× ${fmtNum(c.strike)} ${esc(cur)}</strong> calls (${c.dte}d) to ` +
    `collect ~<strong>${fmtNum(c.premium)} ${esc(cur)}</strong>/sh;${assign} you exit at ` +
    `<strong>${fmtNum(c.effective_exit)} ${esc(cur)}</strong>.`;
  card.innerHTML =
    `<div class="exit-opt-title">Covered call ${sourceBadge(c.source, c.estimate)}` +
    (c.assignment_guard ? ` <span class="warn" title="Pushed far-OTM / post-exemption to protect a deferred lot">tax-guarded</span>` : "") +
    `</div>` +
    `<div class="exit-opt-lead">${lead}</div>` +
    `<div class="exit-opt-grid">` +
      kv("Premium", `${fmtNum(c.premium)} ${esc(cur)} · ${czk(c.premium_czk)}`) +
      kv("Effective exit", `${fmtNum(c.effective_exit)} ${esc(cur)}`) +
      kv("Ann. yield", c.premium_yield_annual_pct == null ? "n/a" : pct(c.premium_yield_annual_pct, 1)) +
      kv("Assignment", c.assignment_prob_pct == null ? "n/a" : `~${pct(c.assignment_prob_pct, 0)}`) +
    `</div>`;
  return card;
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
