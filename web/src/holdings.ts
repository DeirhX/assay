import type { HoldingPosition, HoldingsLiveResponse, HoldingsPayload, RebalancePlan } from "./api-types";
import { $$, api, copyToClipboard, el, esc, fmtStamp, freshnessNote, loadError, sensitive, state } from "./core";
import { gatewayUnavailableReason, getGatewayStatus } from "./gateway";
import { analyzeFromAnywhere } from "./ticker-nav";
import { buildPortfolioPrompt } from "./prompt-export";

// Fetch the plan (for stance/band/sleeve), build the prompt, copy it, and toast
// on the button. Holdings payload is passed in (already loaded); the plan is
// best-effort so the summary still copies (weights + options) if it fails.
// `focus` adds a per-ticker block + question scaffold. Exported so the deep-dive
// toolbar can reuse the exact same behaviour for the ticker in view.
export async function copyPortfolioPrompt(btn: HTMLButtonElement, holdings: HoldingsPayload, focus: string | null, label = "Copy for prompt"): Promise<void> {
  btn.disabled = true;
  btn.textContent = "Building\u2026";
  let plan: RebalancePlan | null = null;
  try {
    plan = await api<RebalancePlan>("/api/rebalance");
  } catch { /* no plan: fall back to weights + options only */ }
  const text = buildPortfolioPrompt(holdings, plan, focus);
  const ok = await copyToClipboard(text);
  btn.textContent = ok ? "Copied\u2713" : "Copy failed";
  window.setTimeout(() => { btn.textContent = label; btn.disabled = false; }, 1600);
}

// ---- holdings -------------------------------------------------------------
// Bumped on every (re)load so a slow live overlay can't repaint a view the user
// already navigated away from (or a newer snapshot load).
let _holdToken = 0;

interface RenderOpts { live: boolean; asOf?: string | null; coverage?: { live: number; eligible: number; total: number }; }

export interface HoldingGroup {
  symbol: string;
  stocks: HoldingPosition[];
  options: HoldingPosition[];
  stockWeight: number;
  optionExercisePct: number;
  baseMarketValue: number | null;
  stockPnlPct: number | null;
  optionPnlPct: number | null;
  unrealizedPnlPct: number | null;
}

function optionUnderlying(position: HoldingPosition): string {
  if (position.option?.underlying) return position.option.underlying;
  const compact = String(position.symbol || "").replace(/\s/g, "");
  return compact.length > 15 ? compact.slice(0, -15) : String(position.symbol || "");
}

const groupKey = (symbol: string) => symbol.split(".")[0].trim().toUpperCase();

function aggregatePnlPct(positions: HoldingPosition[]): number | null {
  let pnl = 0;
  let cost = 0;
  let usable = 0;
  for (const position of positions) {
    const basePnl = position.base_unrealized_pnl;
    const marketValue = position.base_market_value;
    if (
      typeof basePnl !== "number" || !Number.isFinite(basePnl)
      || typeof marketValue !== "number" || !Number.isFinite(marketValue)
    ) continue;
    pnl += basePnl;
    cost += Math.abs(marketValue - basePnl);
    usable += 1;
  }
  return usable === positions.length && usable > 0 && cost > 1e-9
    ? pnl / cost * 100
    : null;
}

/** One display row per underlying, with its stock and every option leg together. */
export function groupHoldingPositions(positions: HoldingPosition[]): HoldingGroup[] {
  const groups = new Map<string, HoldingGroup>();
  const stocksByRoot = new Map<string, HoldingGroup>();
  for (const position of positions || []) {
    if (position.asset_class === "OPT") continue;
    const key = groupKey(position.symbol);
    let group = groups.get(key);
    if (!group) {
      group = {
        symbol: position.symbol,
        stocks: [],
        options: [],
        stockWeight: 0,
        optionExercisePct: 0,
        baseMarketValue: 0,
        stockPnlPct: null,
        optionPnlPct: null,
        unrealizedPnlPct: null,
      };
      groups.set(key, group);
      stocksByRoot.set(key, group);
    }
    group.stocks.push(position);
    group.stockWeight += Number(position.percent_of_nav) || 0;
    group.baseMarketValue = (group.baseMarketValue || 0)
      + (Number(position.base_market_value) || 0);
  }
  for (const position of positions || []) {
    if (position.asset_class !== "OPT") continue;
    const underlying = optionUnderlying(position);
    const key = groupKey(underlying);
    let group = stocksByRoot.get(key) || groups.get(key);
    if (!group) {
      group = {
        symbol: underlying || position.symbol,
        stocks: [],
        options: [],
        stockWeight: 0,
        optionExercisePct: 0,
        baseMarketValue: 0,
        stockPnlPct: null,
        optionPnlPct: null,
        unrealizedPnlPct: null,
      };
      groups.set(key, group);
    }
    group.options.push(position);
    group.optionExercisePct += Number(position.option?.exercise_pct) || 0;
    group.baseMarketValue = (group.baseMarketValue || 0)
      + (Number(position.base_market_value) || 0);
  }
  for (const group of groups.values()) {
    group.stockPnlPct = aggregatePnlPct(group.stocks);
    group.optionPnlPct = aggregatePnlPct(group.options);
    group.unrealizedPnlPct = aggregatePnlPct([...group.stocks, ...group.options]);
  }
  return [...groups.values()].sort((a, b) =>
    (b.stockWeight - a.stockWeight)
    || (Math.abs(b.optionExercisePct) - Math.abs(a.optionExercisePct))
    || a.symbol.localeCompare(b.symbol));
}

function optionLegLabel(position: HoldingPosition): string {
  const option = position.option;
  if (!option) return position.description || position.symbol;
  const contracts = Number(option.contracts) || 0;
  const side = contracts < 0 ? "short" : "long";
  const count = Math.abs(contracts).toLocaleString(undefined, { maximumFractionDigits: 4 });
  const expiry = option.expiry
    ? new Date(`${option.expiry}T00:00:00Z`).toLocaleDateString(undefined, {
      month: "short", day: "numeric",
    })
    : "";
  const strike = Number(option.strike).toLocaleString(undefined, {
    maximumFractionDigits: 4,
  });
  return `${side} ${count}× ${strike}${option.right}${expiry ? ` · ${expiry}` : ""}`;
}

function marketLineHtml(position: HoldingPosition): string {
  const option = position.option;
  const label = option ? `${option.strike}${option.right}` : "Shares";
  const price = position.mark_price;
  const priceText = typeof price === "number" && Number.isFinite(price)
    ? price.toLocaleString(undefined, { maximumFractionDigits: 4 })
    : "\u2014";
  const pnl = position.unrealized_pnl_pct;
  const hasPnl = typeof pnl === "number" && Number.isFinite(pnl);
  const pnlText = hasPnl
    ? `${pnl >= 0 ? "+" : ""}${pnl.toFixed(1)}%`
    : "\u2014";
  const pnlClass = hasPnl ? (pnl >= 0 ? "good" : "bad") : "";
  const average = position.average_cost_price;
  const averageText = typeof average === "number" && Number.isFinite(average)
    ? `${average.toLocaleString(undefined, { maximumFractionDigits: 4 })} ${position.currency || ""}`
    : "unavailable";
  return `<span class="pos-val-detail" title="${esc(`Average purchase price: ${averageText}`)}">` +
    `<b>${esc(label)}</b>` +
    `<span>${esc(priceText)} ${esc(position.currency || "")}</span>` +
    `<em class="${pnlClass}">P/L ${esc(pnlText)}</em></span>`;
}

async function loadHoldings() {
  const status = $$("#hold-status");
  const token = ++_holdToken;
  renderLiveNotice(null);
  status.textContent = "Loading portfolio snapshot...";
  try {
    const h = await api<HoldingsPayload>("/api/holdings");
    if (token !== _holdToken) return;
    status.textContent = "";
    // Paint the delayed Flex snapshot immediately, then overlay live marks when
    // the gateway answers (the user chose fast-first-paint over one blocking call).
    renderHoldings(h, { live: false });
    overlayLive(token);
  } catch (e) {
    loadError(status, "Could not load holdings", e);
  }
}

function renderLiveNotice(reason: string | null): void {
  const host = document.getElementById("hold-gateway-notice");
  if (!host) return;
  host.innerHTML = reason
    ? `<div class="ibkr-data-notice"><strong>Showing the Flex snapshot.</strong> ` +
      `Live IBKR marks were not received: ${esc(reason)}</div>`
    : "";
}

// Best-effort live-mark overlay. The delayed Flex snapshot paints first; a
// failed live overlay remains usable but is now explicit instead of silent.
async function overlayLive(token: number) {
  try {
    const live = await api<HoldingsLiveResponse>("/api/holdings/live");
    if (token !== _holdToken) return;                 // navigated away / reloaded
    if (!live.available || !live.payload) {
      renderLiveNotice(
        live.reason || gatewayUnavailableReason(getGatewayStatus()) ||
        "the gateway returned no live position data",
      );
      return;
    }
    renderLiveNotice(null);
    renderHoldings(live.payload, { live: true, asOf: live.as_of, coverage: live.coverage });
  } catch {
    renderLiveNotice(
      gatewayUnavailableReason(getGatewayStatus()) || "the live-mark request failed",
    );
  }
}

function renderHoldings(h: HoldingsPayload, opts: RenderOpts) {
  const out = $$("#hold-result");
  {
    state.nav = h.net_asset_value;
    state.holdings = {};
    (h.positions || []).forEach((p) => {
      state.holdings[p.symbol] = p.percent_of_nav;
      if (p.provider_symbol && p.provider_symbol !== p.symbol) {
        state.holdings[p.provider_symbol] = p.percent_of_nav;
      }
    });
    // NAV/concentration is the portfolio's headline, so give it a visual hero
    // rather than a muted text line -- and keep it in #hold-result, not the
    // transient #hold-status, so a "Synced…" message can't overwrite it.
    const synced = $$("#hold-synced");
    if (synced) synced.innerHTML = freshnessLabel(h, opts);
    out.innerHTML = "";

    const metric = localStorage.getItem("holdings.barMetric") === "pnl" ? "pnl" : "size";
    const pnlOrder = localStorage.getItem("holdings.pnlOrder") === "gains" ? "gains" : "losses";
    const rows = groupHoldingPositions(h.positions || []);
    if (metric === "pnl") {
      const direction = pnlOrder === "losses" ? 1 : -1;
      rows.sort((a, b) => {
        if (a.unrealizedPnlPct == null) return 1;
        if (b.unrealizedPnlPct == null) return -1;
        return direction * (a.unrealizedPnlPct - b.unrealizedPnlPct);
      });
    }
    const weights = rows.map((group) => group.stockWeight).sort((a, b) => b - a);
    const barMagnitudes = rows.flatMap((group) => [
      Math.abs(metric === "pnl" ? group.stockPnlPct || 0 : group.stockWeight),
      Math.abs(metric === "pnl" ? group.optionPnlPct || 0 : group.optionExercisePct),
    ]).filter((value) => value > 0).sort((a, b) => a - b);
    const robustIndex = Math.max(0, Math.ceil(barMagnitudes.length * 0.9) - 1);
    const maxW = Math.max(
      1e-6,
      metric === "pnl"
        ? barMagnitudes[robustIndex] || 0
        : barMagnitudes[barMagnitudes.length - 1] || 0,
    );
    const cum = (n: number) => weights.slice(0, n).reduce((s, w) => s + w, 0);

    out.appendChild(portfolioHero(h, rows.length, cum));
    // Legend goes above the list: with 40+ positions it was below the fold, so
    // the colour coding (its whole point) was invisible until you scrolled past
    // everything it explains.
    out.appendChild(el("div", "hint pos-legend", metric === "pnl"
      ? "Bars show unrealized return on cost: green is a gain, red is a loss. Shares use the solid upper bar; held options use the striped lower bar. Rows are ordered from " +
        (pnlOrder === "losses" ? "largest loss to largest gain." : "largest gain to largest loss.") +
        " A brighter inner band marks returns beyond the shared bar scale. Click a row to deep-dive."
      : "Each row is one underlying: shares use the solid upper bar; held options use the striped lower bar. " +
        "Colour = share concentration: red >10%, amber 5\u201310%, blue 1\u20135%, grey <1%. " +
        "Option arrows show assignment/exercise exposure, not capital at risk. Click a row to deep-dive."));

    // Opt-in column of each position's current market value (base currency).
    // Off by default because the bars are about concentration, not money, and the
    // values are sensitive; the preference sticks across reloads. Toggling just
    // flips a class so we never re-fetch or re-render the rows.
    const showValues = localStorage.getItem("holdings.showValues") === "1";
    const controls = el("div", "pos-controls");
    controls.innerHTML =
      `<div class="pos-metric-switch" role="group" aria-label="Order and draw bars by">` +
        `<button type="button" data-hold-metric="size" class="${metric === "size" ? "active" : ""}" aria-pressed="${metric === "size"}">Position size</button>` +
        `<button type="button" data-hold-metric="pnl" class="${metric === "pnl" ? "active" : ""}" aria-pressed="${metric === "pnl"}">P/L return</button>` +
      `</div>` +
      (metric === "pnl"
        ? `<button type="button" class="ghost pos-pnl-order" id="hold-pnl-order" title="Reverse P/L order">${pnlOrder === "losses" ? "Losses first" : "Gains first"}</button>`
        : "") +
      `<label class="setup-toggle pos-val-toggle"><input type="checkbox" id="hold-show-values"` +
      `${showValues ? " checked" : ""}><span class="setup-toggle-track"></span> ` +
      `Show asset values</label>`;
    // Copy a compact, privacy-safe portfolio snapshot for pasting into an LLM.
    // Needs the plan (stance/band/sleeve), fetched lazily on click so the holdings
    // view itself stays a single request.
    const copyBtn = el("button", "ghost pos-copy-prompt") as HTMLButtonElement;
    copyBtn.type = "button";
    copyBtn.textContent = "Copy for prompt";
    copyBtn.title = "Copy a compact, weight-based portfolio summary (no absolute values) to paste into an LLM";
    copyBtn.addEventListener("click", () => copyPortfolioPrompt(copyBtn, h, null));
    controls.appendChild(copyBtn);
    out.appendChild(controls);

    const list = el("div", "pos-list metric-" + metric + (showValues ? " show-values" : ""));
    const listHead = el("div", "pos-list-head");
    listHead.innerHTML =
      `<span>Underlying</span><span>${metric === "pnl" ? "Return on cost" : "Exposure"}</span><span>${metric === "pnl" ? "Unrealized P/L" : "Weight"}</span>` +
      `<span class="pos-head-values">Value · current price · unrealized P/L</span>`;
    list.appendChild(listHead);
    rows.forEach((group) => {
      const stock = group.stocks[0] || null;
      const hasOptions = group.options.length > 0;
      const researchable = stock
        ? isResearchableHolding(stock)
        : !!group.symbol && !group.symbol.toUpperCase().endsWith(".DRRT");
      const providerSymbol = stock?.provider_symbol || group.symbol;
      const w = group.stockWeight;
      // Tier by absolute concentration (flags the AMD/ARM problem on sight);
      // bar length is relative to the largest holding for visual ranking.
      const tier = stock ? (w >= 10 ? "core" : w >= 5 ? "large" : w >= 1 ? "mid" : "small") : "opt";
      const stockQty = group.stocks.reduce(
        (sum, position) => sum + (Number(position.quantity) || 0), 0,
      );
      const qtyText = stock
        ? `${stockQty.toLocaleString(undefined, { maximumFractionDigits: 4 })} shares`
        : "no shares";
      const stockMetric = metric === "pnl" ? group.stockPnlPct || 0 : w;
      const optionMetric = metric === "pnl" ? group.optionPnlPct || 0 : group.optionExercisePct;
      const barScale = metric === "pnl" ? 50 : 100;
      const stockBarW = Math.min(barScale, (Math.abs(stockMetric) / maxW) * barScale);
      const optionBarW = Math.min(
        barScale, (Math.abs(optionMetric) / maxW) * barScale,
      );
      const barSide = (value: number) => metric !== "pnl"
        ? ""
        : value < 0 ? "right:50%;left:auto;" : "left:50%;right:auto;";
      const displaySymbol = stock && providerSymbol !== stock.symbol
        ? `${stock.symbol} \u2192 ${providerSymbol}`
        : group.symbol;
      const optionSummary = group.options.map(optionLegLabel).join(" · ");
      const optionDirection = group.optionExercisePct < 0 ? "\u2193" : "\u2191";
      const optionExposure = hasOptions
        ? `${optionDirection}${Math.abs(group.optionExercisePct).toFixed(1)}% if exercised`
        : "";
      const pnlText = group.unrealizedPnlPct == null
        ? "P/L \u2014"
        : `${group.unrealizedPnlPct >= 0 ? "+" : ""}${group.unrealizedPnlPct.toFixed(2)}%`;
      const metricClass = (value: number | null) => metric !== "pnl"
        ? ""
        : value == null ? " pnl-bar-missing"
        : value > 0.005
          ? ` pnl-bar-good pnl-positive${Math.abs(value) > maxW ? " pnl-bar-overflow" : ""}`
          : value < -0.005
            ? ` pnl-bar-bad pnl-negative${Math.abs(value) > maxW ? " pnl-bar-overflow" : ""}`
            : " pnl-bar-flat";
      const overflowStyle = (value: number) => metric === "pnl" && Math.abs(value) > maxW
        ? `--pnl-overflow:${Math.min(100, (Math.abs(value) / maxW - 1) * 100).toFixed(2)}%;`
        : "";
      const tag = hasOptions
        ? ` <span class="opt-tag">${group.options.length} OPT</span>`
        : "";
      // When live marks are on, flag any equity still riding the delayed Flex
      // mark (no live match) so coverage is honest at the row level.
      const delayed = opts.live && group.stocks.some((position) => position.live_mark === false);
      const delayTag = delayed
        ? ` <span class="pos-delayed" title="Delayed \u2014 still on the Flex snapshot mark (no live match)">\u23f1</span>` : "";
      const totalValue = group.baseMarketValue == null
        ? "\u2014"
        : sensitive(`${Math.round(group.baseMarketValue).toLocaleString()} CZK`, "position value");
      const valText = `<span class="pos-val-total"><b>Value</b><span>${totalValue}</span></span>` +
        [...group.stocks, ...group.options].map(marketLineHtml).join("");
      const row = el("div", "pos-row tier-" + tier);
      row.innerHTML =
        `<span class="pos-sym"><span class="pos-sym-main">${esc(displaySymbol)}${tag}${delayTag}</span>` +
          (hasOptions ? `<span class="pos-option-summary">${esc(optionSummary)}</span>` : "") +
        `</span>` +
        `<span class="pos-bar-track${hasOptions ? " has-options" : ""}">` +
          (stock
            ? `<span class="pos-bar pos-group-stock${w < 0 ? " pos-bar-short" : ""}${metricClass(group.stockPnlPct)}" style="${barSide(stockMetric)}${overflowStyle(stockMetric)}width:${stockBarW.toFixed(2)}%"></span>`
            : "") +
          (hasOptions
            ? `<span class="pos-bar opt-bar pos-group-opt${metricClass(group.optionPnlPct)}" style="${barSide(optionMetric)}${overflowStyle(optionMetric)}width:${optionBarW.toFixed(2)}%"></span>`
            : "") +
        `</span>` +
        `<span class="pos-w" title="${esc(`Position: ${qtyText}`)}">` +
          `<span class="pos-w-pct${metric === "pnl" && group.unrealizedPnlPct != null ? (group.unrealizedPnlPct >= 0 ? " good" : " bad") : ""}">${metric === "pnl" ? pnlText : stock ? `${w.toFixed(2)}%` : "options only"}</span>` +
          `<span class="pos-w-qty">${esc(qtyText)}</span>` +
          (hasOptions ? `<span class="pos-w-opt">${esc(metric === "pnl" && group.optionPnlPct != null ? `Options ${group.optionPnlPct >= 0 ? "+" : ""}${group.optionPnlPct.toFixed(2)}%` : optionExposure)}</span>` : "") +
        `</span>` +
        `<span class="pos-val">${valText}</span>`;
      row.title = `${stock?.description || group.symbol} \u00b7 ${w.toFixed(2)}% of invested \u00b7 ${qtyText}` +
        (hasOptions ? ` \u00b7 ${optionSummary} \u00b7 ${optionExposure} (notional, not capital)` : "") +
        (stock && providerSymbol !== stock.symbol ? ` \u00b7 opens ${providerSymbol}` : "");
      if (researchable) {
        row.addEventListener("click", () => analyzeFromAnywhere(providerSymbol));
      } else {
        row.classList.add("disabled");
        row.title += " \u00b7 not a researchable ticker";
      }
      list.appendChild(row);
    });
    out.appendChild(list);

    const valToggle = controls.querySelector<HTMLInputElement>("#hold-show-values");
    valToggle?.addEventListener("change", () => {
      list.classList.toggle("show-values", valToggle.checked);
      localStorage.setItem("holdings.showValues", valToggle.checked ? "1" : "0");
    });
    controls.querySelectorAll<HTMLButtonElement>("[data-hold-metric]").forEach((button) => {
      button.addEventListener("click", () => {
        localStorage.setItem("holdings.barMetric", button.dataset.holdMetric || "size");
        renderHoldings(h, opts);
      });
    });
    controls.querySelector<HTMLButtonElement>("#hold-pnl-order")?.addEventListener("click", () => {
      localStorage.setItem("holdings.pnlOrder", pnlOrder === "losses" ? "gains" : "losses");
      renderHoldings(h, opts);
    });
  }
}

// The freshness line: delayed Flex snapshot, or live marks with coverage and the
// Flex base age (so it's clear what's live vs. what's still on the snapshot).
function freshnessLabel(h: HoldingsPayload, opts: RenderOpts): string {
  if (!opts.live) {
    return h.generated_at ? `Last synced ${freshnessNote(h.generated_at) || esc(fmtStamp(h.generated_at))}` : "No snapshot yet";
  }
  const when = opts.asOf ? esc(fmtStamp(opts.asOf)) : "now";
  const cov = opts.coverage;
  const covTxt = cov ? ` \u00b7 ${cov.live}/${cov.eligible} marks live` : "";
  const flex = h.generated_at ? ` \u00b7 Flex base ${freshnessNote(h.generated_at) || esc(fmtStamp(h.generated_at))}` : "";
  return `<span class="hold-live-dot" title="Marks refreshed from the live IBKR gateway"></span> Live ${when}${covTxt}${flex}`;
}

const fmtCzkNum = (v: number | null | undefined) => `${Math.round(Number(v) || 0).toLocaleString()}`;

// Concentration severity from a cumulative weight: the book gets risky fast, so
// the chips escalate grey -> blue -> amber -> red as the cumulative share climbs.
function concSeverity(pct: number) {
  return pct >= 60 ? "bad" : pct >= 40 ? "warn" : pct >= 20 ? "info" : "small";
}

// The portfolio headline: NAV / invested / uninvested as stat tiles plus the
// top-N concentration as escalating, severity-coloured bars. Money is masked by
// the privacy toggle; the bar widths tell the concentration story at a glance.
function portfolioHero(h: HoldingsPayload, count: number, cum: (n: number) => number) {
  const nav = h.net_asset_value || 0;
  const invested = h.invested_value || 0;
  const hero = el("div", "port-hero");

  const stats = el("div", "port-stats");
  const stat = (label: string, valueHtml: string, title?: string) =>
    `<div class="port-stat"${title ? ` title="${esc(title)}"` : ""}>` +
    `<span class="ps-label">${esc(label)}</span>` +
    `<span class="ps-value">${valueHtml} <span class="ps-unit">CZK</span></span></div>`;
  stats.innerHTML =
    stat("Net asset value", sensitive(fmtCzkNum(nav), "total NAV")) +
    stat("Invested", sensitive(fmtCzkNum(invested), "invested value")) +
    stat("Uninvested", sensitive(fmtCzkNum(nav - invested), "uninvested"),
      "NAV minus invested book value (cash and anything not in the invested book)");
  hero.appendChild(stats);

  const conc = el("div", "conc-card");
  const chips = ([["Top 2", cum(2)], ["Top 5", cum(5)], ["Top 10", cum(10)]] as [string, number][])
    .map(([label, pct]) =>
      `<div class="conc-chip tier-${concSeverity(pct)}">` +
      `<div class="cc-top"><span class="cc-label">${label}</span>` +
      `<span class="cc-pct">${pct.toFixed(1)}%</span></div>` +
      `<div class="cc-track"><span class="cc-fill" style="width:${Math.min(100, pct).toFixed(1)}%"></span></div>` +
      `</div>`).join("");
  conc.innerHTML =
    `<div class="conc-head"><span class="conc-title">Concentration</span>` +
    `<span class="muted">${count} underlyings \u00b7 weights = % of invested</span></div>` +
    `<div class="conc-bars">${chips}</div>`;
  hero.appendChild(conc);

  return hero;
}

function isResearchableHolding(position: HoldingPosition) {
  if (position.researchable === false) return false;
  if (position.asset_class === "OPT") return false;
  const symbol = String(position.symbol || "").toUpperCase();
  return !symbol.endsWith(".DRRT");
}

export {
  loadHoldings,
};
