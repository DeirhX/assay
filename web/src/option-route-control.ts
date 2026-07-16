import { el, esc, fmtCZK } from "./core";
import type {
  RebalanceExecutionRoute, RebalanceRouteResponse,
  RebalanceRouteSelection,
} from "./api-types";
import {
  buildRouteSelection, directRouteFor, fetchRebalanceRoute, optionRouteFor,
  pickStageableRung,
} from "./execution-routes";
import { liquidityChipClass, quoteFreshnessLabel } from "./option-quote";

/** True when the server refused a put for cash collateral, not mark/quote sizing. */
export function isCashCapacityFailure(reasons: string[] | undefined): boolean {
  const text = (reasons ?? []).join(" ").toLowerCase();
  return (
    text.includes("cash-secured put needs")
    || text.includes("uncommitted snapshot cash")
    || text.includes("no uncommitted snapshot cash")
  );
}

// ---- shared route loading with stale-response cancellation -------------------

export class OptionRouteLoader {
  private token = 0;

  cancel(): void {
    this.token += 1;
  }

  isCurrent(token: number): boolean {
    return token === this.token;
  }

  async load(symbol: string, deltaCzk: number): Promise<{
    route: RebalanceRouteResponse;
    token: number;
  } | null> {
    const token = ++this.token;
    try {
      const route = await fetchRebalanceRoute(symbol, deltaCzk);
      if (!this.isCurrent(token)) return null;
      return { route, token };
    } catch (error) {
      if (!this.isCurrent(token)) return null;
      throw error;
    }
  }
}

// ---- rebalance planner route control (cards + compact select) ----------------

export interface OptionRouteControlConfig {
  onSelection?: (selection: RebalanceRouteSelection) => void;
  onExitNavigate?: (symbol: string) => void;
}

export interface OptionRouteControl {
  controls: HTMLElement;
  compact: HTMLSelectElement;
  detail: HTMLElement;
  sync: (deltaCzk: number) => void;
  selectDirect: (limitPrice?: number) => void;
}

export function createOptionRouteControl(
  symbol: string,
  initialDeltaCzk: number,
  selections: Map<string, RebalanceRouteSelection>,
  config: OptionRouteControlConfig = {},
): OptionRouteControl {
  const { onSelection, onExitNavigate } = config;
  const loader = new OptionRouteLoader();
  let deltaCzk = initialDeltaCzk;
  const controls = el("div", "reb-route-controls reb-route-inline");
  const compact = document.createElement("select");
  compact.className = "reb-route-select";
  compact.title = "Execution route";
  compact.setAttribute("aria-label", `${symbol} execution route`);
  const directChoice = document.createElement("option");
  directChoice.value = "direct";
  const optionChoice = document.createElement("option");
  optionChoice.value = "option";
  const exitChoice = document.createElement("option");
  exitChoice.value = "exit";
  exitChoice.textContent = "Open exit plan…";
  compact.append(directChoice, optionChoice, exitChoice);
  const direct = el("button", "ghost active");
  const option = el("button", "ghost");
  direct.type = "button";
  option.type = "button";
  controls.appendChild(direct);
  controls.appendChild(option);
  const exit = el("button", "ghost", "Exit plan");
  exit.type = "button";
  exit.title = "Compare lot timing, scale-out tranches, and covered calls";
  exit.addEventListener("click", () => onExitNavigate?.(symbol));
  controls.appendChild(exit);
  const detail = el("div", "reb-route-detail reb-route-row-detail");
  detail.hidden = true;

  const addDetailClose = () => {
    if (detail.querySelector(".reb-route-detail-close")) return;
    const close = el("button", "ghost reb-route-detail-close", "Close");
    close.type = "button";
    close.addEventListener("click", () => {
      detail.hidden = true;
      detail.innerHTML = "";
      paintCompactSelection();
    });
    detail.prepend(close);
  };

  const resetToStock = () => {
    const route = directRouteFor(deltaCzk);
    selections.set(symbol, { symbol, route });
    direct.classList.add("active");
    option.classList.remove("active");
    compact.value = "direct";
  };
  const paintLabels = () => {
    direct.textContent = "Shares";
    option.textContent = deltaCzk >= 0 ? "Put option" : "Covered call";
    directChoice.textContent = deltaCzk >= 0 ? "Buy shares" : "Sell shares";
    optionChoice.textContent = deltaCzk >= 0 ? "Put option…" : "Covered call…";
  };
  const paintCompactSelection = () => {
    const selected = selections.get(symbol);
    compact.value = selected?.route === optionRouteFor(deltaCzk) ? "option" : "direct";
  };

  const selectDirect = (limitPrice?: number) => {
    resetToStock();
    const selection = selections.get(symbol)!;
    if (limitPrice && limitPrice > 0) selection.limit_price = limitPrice;
    onSelection?.(selection);
    detail.innerHTML = "";
    detail.hidden = true;
  };
  direct.addEventListener("click", () => selectDirect());

  const renderOptionCards = (
    route: RebalanceRouteResponse,
    autoSelect = false,
  ): HTMLButtonElement | null => {
    option.textContent = route.option.label;
    direct.disabled = !route.direct.eligible;
    directChoice.disabled = !route.direct.eligible;
    if (!route.option.eligible) {
      const rawLabel = route.option.label.replace(/^Sell\s+/i, "");
      const unavailableLabel = rawLabel.charAt(0).toUpperCase() + rawLabel.slice(1);
      // Only cash-capacity failures should push users to IBKR margin. Missing
      // marks / quotes are a local sizing problem, not a collateral-mode choice.
      const showMarginNextStep =
        route.direction === "increase"
        && route.option.collateral_mode !== "margin"
        && isCashCapacityFailure(route.option.reasons);
      detail.innerHTML =
        `<div class="reb-route-unavailable">` +
          `<span class="reb-route-eyebrow">Option route unavailable</span>` +
          `<strong>${esc(unavailableLabel)} unavailable</strong>` +
          `<p>${esc(route.option.reasons.join(" · ") || "No suitable contract route.")}</p>` +
        (showMarginNextStep
          ? `<div class="reb-route-unavailable-next"><b>Next:</b> choose Buy shares, ` +
            `or use IBKR directly for a margin-backed short put.</div>`
          : "") +
        `</div>`;
      addDetailClose();
      return null;
    }
    const intro = el("div", "reb-route-option-summary");
    const routeName = route.direction === "increase" ? "Put entry" : "Covered-call reduction";
    intro.innerHTML =
      `<div class="reb-route-option-heading">` +
        `<span class="reb-route-eyebrow">${routeName}</span>` +
        `<strong>Conditional ${route.direction === "increase" ? "entry" : "reduction"}</strong>` +
      `</div>` +
      `<div class="reb-route-option-facts">` +
        `<span><b>${route.option.contracts}</b> contract${route.option.contracts === 1 ? "" : "s"}</span>` +
        `<span><b>${route.option.assignment_shares}</b> shares if assigned</span>` +
      (route.option.share_deviation
        ? `<span class="${route.option.share_deviation > 0 ? "warn" : ""}">` +
          `<b>${route.option.share_deviation > 0 ? "+" : ""}${route.option.share_deviation}</b> shares vs plan</span>`
        : "") +
      (route.option.collateral_mode === "margin"
        ? `<span class="margin"><b>Margin</b> account</span>`
        : "") +
      `</div>`;
    detail.innerHTML = "";
    detail.appendChild(intro);
    const contracts = el("div", "reb-option-contracts");
    let firstStageable: HTMLButtonElement | null = null;
    for (const rung of route.ladder) {
      const card = el("article", "reb-option-contract");
      const effective = route.direction === "increase" ? rung.effective_entry : rung.effective_exit;
      const right = route.direction === "increase" ? "Put" : "Call";
      const quoteState = quoteFreshnessLabel(rung);
      const top = el("div", "reb-option-contract-top");
      top.innerHTML =
        `<div class="reb-option-contract-name">` +
          `<span>${esc(rung.expiry)}</span>` +
          `<strong>${rung.strike} ${right}</strong>` +
        `</div>` +
        `<div class="reb-option-metric"><span>Bid / ask</span><strong>${rung.bid ?? "—"} / ${rung.ask ?? "—"}</strong></div>` +
        `<div class="reb-option-metric"><span>Yield p.a.</span><strong>${rung.premium_yield_annual_pct.toFixed(1)}%</strong></div>` +
        `<div class="reb-option-metric"><span>Effective ${route.direction === "increase" ? "entry" : "exit"}</span>` +
          `<strong>${effective != null ? effective.toFixed(2) : "—"} <small>${esc(route.currency || "")}</small></strong></div>` +
        `<div class="reb-option-metric"><span>Assignment chance</span>` +
          `<strong>${rung.assignment_prob_pct != null ? rung.assignment_prob_pct.toFixed(0) + "%" : "—"}</strong></div>`;
      const footer = el("div", "reb-option-contract-footer");
      const backing = rung.cash_secured_czk
        ? `<div class="reb-option-backing">` +
            `<span>${route.option.collateral_mode === "margin" ? "Assignment notional" : "Cash reserved"}</span>` +
            `<strong>${fmtCZK(rung.cash_secured_czk)} CZK</strong>` +
            `<small>${route.option.collateral_mode === "margin"
              ? "IBKR validates margin at preview"
              : "Fully cash-secured"}</small>` +
          `</div>`
        : `<div class="reb-option-backing">` +
            `<span>Share coverage</span><strong>${route.option.assignment_shares} shares</strong>` +
            `<small>Coverage rechecked before placement</small>` +
          `</div>`;
      footer.innerHTML =
        `<div class="reb-option-market-state">` +
          `<span class="chip ${rung.source === "ibkr" ? "good" : "muted"}">${esc(rung.source.replace(/_/g, " "))}</span>` +
          `<span class="chip ${rung.quote_fresh ? "good" : rung.stageable ? "warn" : "muted"}">${quoteState}</span>` +
          `<span class="chip ${liquidityChipClass(rung.liquidity)}">` +
            `${esc(rung.liquidity)} liquidity</span>` +
        `</div>` +
        backing;
      const use = el("button", "reb-option-use", rung.stageable ? "Use contract" : "Indicative only");
      use.type = "button";
      use.disabled = !rung.stageable || !rung.conid;
      use.title = rung.stageable
        ? "Use this exact contract in the order queue"
        : "Staging requires an exact IBKR contract";
      use.addEventListener("click", () => {
        const selection = buildRouteSelection({
          symbol,
          route: optionRouteFor(deltaCzk),
          rung,
          contracts: route.option.contracts,
          collateralMode: route.option.collateral_mode ?? undefined,
        });
        selections.set(symbol, selection);
        onSelection?.(selection);
        direct.classList.remove("active");
        option.classList.add("active");
        compact.value = "option";
        contracts.querySelectorAll("button").forEach((button) => {
          button.classList.remove("active");
          if (button !== use && !button.textContent?.includes("Indicative")) {
            button.textContent = "Use contract";
          }
        });
        use.classList.add("active");
        use.textContent = "Selected ✓";
      });
      footer.appendChild(use);
      if (!firstStageable && !use.disabled) firstStageable = use;
      card.append(top, footer);
      contracts.appendChild(card);
    }
    detail.appendChild(contracts);
    if (route.option.reasons.length) {
      const hint = el("div", "hint");
      hint.textContent = route.option.reasons.join(" · ");
      detail.appendChild(hint);
    }
    addDetailClose();
    if (autoSelect) firstStageable?.click();
    return firstStageable;
  };

  const loadOption = async (autoSelect = false): Promise<boolean> => {
    option.disabled = true;
    option.textContent = "Loading live option routes…";
    detail.hidden = false;
    detail.innerHTML = `<div class="status"><span class="spinner"></span> loading strikes and quotes…</div>`;
    addDetailClose();
    const requestedDelta = deltaCzk;
    try {
      const result = await loader.load(symbol, requestedDelta);
      if (!result || requestedDelta !== deltaCzk) return false;
      const selectedButton = renderOptionCards(result.route, autoSelect);
      return Boolean(selectedButton);
    } catch (error) {
      if (requestedDelta !== deltaCzk) return false;
      detail.innerHTML =
        `<div class="reb-route-unavailable error">` +
          `<span class="reb-route-eyebrow">Option route error</span>` +
          `<strong>Could not load option routes</strong>` +
          `<p>${esc((error as Error).message)}</p>` +
        `</div>`;
      addDetailClose();
      return false;
    } finally {
      option.disabled = false;
      if (option.textContent === "Loading live option routes…") paintLabels();
    }
  };
  option.addEventListener("click", () => {
    compact.value = "option";
    void loadOption(false);
  });
  compact.addEventListener("change", () => {
    const choice = compact.value;
    if (choice === "direct") {
      direct.click();
      return;
    }
    if (choice === "exit") {
      exit.click();
      paintCompactSelection();
      return;
    }
    compact.value = "option";
    void loadOption(false);
  });

  const sync = (nextDeltaCzk: number) => {
    const next = Math.round(nextDeltaCzk);
    const changed = next !== Math.round(deltaCzk);
    deltaCzk = next;
    controls.hidden = Math.abs(next) < 1;
    compact.hidden = Math.abs(next) < 1;
    exit.hidden = next >= 0;
    exitChoice.hidden = next >= 0;
    if (changed) {
      loader.cancel();
      detail.innerHTML = "";
      detail.hidden = true;
      direct.disabled = false;
      resetToStock();
    }
    const selected = selections.get(symbol);
    if (
      !selected ||
      (selected.route !== directRouteFor(next) && selected.route !== optionRouteFor(next))
    ) {
      resetToStock();
    } else {
      direct.classList.toggle("active", selected.route === directRouteFor(next));
      option.classList.toggle("active", selected.route === optionRouteFor(next));
    }
    paintLabels();
    paintCompactSelection();
    detail.hidden = Math.abs(next) < 1 || !detail.innerHTML;
  };

  sync(initialDeltaCzk);
  return { controls, compact, detail, sync, selectDirect };
}

// ---- compact variant (ticker order composer) ---------------------------------

export interface CompactOptionRouteResult {
  selection: RebalanceRouteSelection | null;
  html: string;
  eligible: boolean;
}

export async function loadCompactOptionRoute(
  loader: OptionRouteLoader,
  symbol: string,
  deltaCzk: number,
  direction: "increase" | "reduce",
): Promise<CompactOptionRouteResult | null> {
  const result = await loader.load(symbol, deltaCzk);
  if (!result) return null;
  const { route } = result;
  const rung = pickStageableRung(route.ladder);
  if (!route.option.eligible || !rung) {
    return {
      selection: null,
      eligible: false,
      html:
        `<span class="chip warn tone-chip">${esc(route.option.label)} unavailable</span> ` +
        `<span class="muted">${esc(route.option.reasons.join(" · ") || "No executable contract.")}</span>`,
    };
  }
  const selection = buildRouteSelection({
    symbol,
    route: optionRouteFor(deltaCzk),
    rung,
    contracts: route.option.contracts,
    collateralMode: route.option.collateral_mode ?? undefined,
  });
  return {
    selection,
    eligible: true,
    html:
      `<span class="chip good tone-chip">${esc(route.option.label)}</span> ` +
      `<strong>${esc(rung.expiry)} · ${rung.strike}${direction === "increase" ? "P" : "C"}</strong> ` +
      `<span class="muted">${route.option.contracts} contract(s) · ${fmtCZK(Math.abs(deltaCzk))} CZK plan</span>`,
  };
}

export function buildDirectRouteSelection(
  symbol: string,
  route: RebalanceExecutionRoute,
  limitPrice?: number,
): RebalanceRouteSelection {
  return buildRouteSelection({ symbol, route, limitPrice });
}
