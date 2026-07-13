import { el, esc, fmtCZK } from "./core";
import type {
  RebalanceExecutionRoute, RebalanceRouteResponse,
  RebalanceRouteSelection,
} from "./api-types";
import {
  buildRouteSelection, directRouteFor, fetchRebalanceRoute, optionRouteFor,
  pickStageableRung,
} from "./execution-routes";
import {
  formatQuoteSourceLabel, liquidityChipClass, quoteFreshnessCaption, quoteSourceChipClass,
} from "./option-quote";

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

// ---- full ladder variant (rebalance planner / what-if) -----------------------

export interface OptionRouteControl {
  controls: HTMLElement;
  detail: HTMLElement;
  sync: (deltaCzk: number) => void;
  autoSelectOption: () => Promise<boolean>;
  disable: () => void;
}

export function createOptionRouteControl(
  symbol: string,
  initialDeltaCzk: number,
  selections: Map<string, RebalanceRouteSelection>,
  onSelection?: (selection: RebalanceRouteSelection) => void,
): OptionRouteControl {
  const loader = new OptionRouteLoader();
  let deltaCzk = initialDeltaCzk;
  const controls = el("div", "route-controls reb-route-controls reb-route-inline");
  const direct = el("button", "ghost active");
  const option = el("button", "ghost");
  direct.type = "button";
  option.type = "button";
  controls.appendChild(direct);
  controls.appendChild(option);
  const detail = el("div", "reb-route-detail reb-route-row-detail");
  detail.hidden = true;

  const resetToStock = () => {
    const route = directRouteFor(deltaCzk);
    selections.set(symbol, { symbol, route });
    direct.classList.add("active");
    option.classList.remove("active");
  };
  const paintLabels = () => {
    direct.textContent = "Shares";
    option.textContent = deltaCzk >= 0 ? "Cash-secured put" : "Covered call";
  };

  direct.addEventListener("click", () => {
    resetToStock();
    onSelection?.(selections.get(symbol)!);
    detail.innerHTML = "";
    detail.hidden = true;
  });

  const renderLadder = (
    route: RebalanceRouteResponse,
    autoSelect = false,
  ): HTMLButtonElement | null => {
    option.textContent = route.option.label;
    direct.disabled = !route.direct.eligible;
    if (!route.option.eligible) {
      detail.innerHTML =
        `<div class="ibkr-data-notice"><strong>${esc(route.option.label)} unavailable.</strong> ` +
        `${esc(route.option.reasons.join(" · ") || "No suitable contract route.")}</div>`;
      return null;
    }
    const intro = el("div", "reb-route-option-summary");
    intro.innerHTML =
      `<strong>Conditional ${route.direction === "increase" ? "entry" : "reduction"}</strong> · ` +
      `${route.option.contracts} contract${route.option.contracts === 1 ? "" : "s"} / ` +
      `${route.option.assignment_shares} shares if assigned` +
      (route.option.share_deviation
        ? ` · ${route.option.share_deviation > 0 ? "+" : ""}${route.option.share_deviation} shares vs plan`
        : "");
    detail.innerHTML = "";
    detail.appendChild(intro);
    const table = el("div", "table-wrap");
    table.innerHTML =
      `<table class="whatif-table reb-route-ladder"><thead><tr>` +
      `<th>Expiry / strike</th><th class="num">Bid / ask</th>` +
      `<th class="num">Yield p.a.</th><th class="num">Effective</th>` +
      `<th class="num">Assign.</th><th>Source / quote</th><th>Liquidity</th><th></th>` +
      `</tr></thead><tbody></tbody></table>`;
    const tbody = table.querySelector("tbody")!;
    let firstStageable: HTMLButtonElement | null = null;
    for (const rung of route.ladder) {
      const tr = document.createElement("tr");
      const effective = route.direction === "increase" ? rung.effective_entry : rung.effective_exit;
      tr.innerHTML =
        `<td>${esc(rung.expiry)} · ${rung.strike}${route.direction === "increase" ? "P" : "C"}</td>` +
        `<td class="num">${rung.bid ?? "—"} / ${rung.ask ?? "—"}</td>` +
        `<td class="num">${rung.premium_yield_annual_pct.toFixed(1)}%</td>` +
        `<td class="num">${effective != null ? effective.toFixed(2) : "—"} ${esc(route.currency || "")}` +
        (rung.cash_secured_czk
          ? `<small class="muted">${fmtCZK(rung.cash_secured_czk)} CZK secured</small>`
          : `<small class="muted">${route.option.assignment_shares} shares covered</small>`) +
        `</td>` +
        `<td class="num">${rung.assignment_prob_pct != null ? rung.assignment_prob_pct.toFixed(0) + "%" : "—"}</td>` +
        `<td><span class="chip ${quoteSourceChipClass(rung.source)} tone-chip">${esc(formatQuoteSourceLabel(rung.source))}</span>` +
        `<small class="muted">${quoteFreshnessCaption(rung)}</small></td>` +
        `<td><span class="chip ${liquidityChipClass(rung.liquidity)} tone-chip">${esc(rung.liquidity)}</span></td>`;
      const action = document.createElement("td");
      const use = el("button", "ghost", rung.stageable ? "Use" : "Indicative");
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
        });
        selections.set(symbol, selection);
        onSelection?.(selection);
        direct.classList.remove("active");
        option.classList.add("active");
        table.querySelectorAll("button").forEach((button) => {
          button.classList.remove("active");
          if (button !== use && !button.textContent?.includes("Indicative")) button.textContent = "Use";
        });
        use.classList.add("active");
        use.textContent = "Selected ✓";
      });
      action.appendChild(use);
      if (!firstStageable && !use.disabled) firstStageable = use;
      tr.appendChild(action);
      tbody.appendChild(tr);
    }
    detail.appendChild(table);
    if (route.option.reasons.length) {
      const hint = el("div", "hint");
      hint.textContent = route.option.reasons.join(" · ");
      detail.appendChild(hint);
    }
    if (autoSelect) firstStageable?.click();
    return firstStageable;
  };

  const loadOption = async (autoSelect = false): Promise<boolean> => {
    option.disabled = true;
    option.textContent = "Loading live option routes…";
    detail.hidden = false;
    detail.innerHTML = `<div class="status"><span class="spinner"></span> loading strikes and quotes…</div>`;
    const requestedDelta = deltaCzk;
    try {
      const result = await loader.load(symbol, requestedDelta);
      if (!result || requestedDelta !== deltaCzk) return false;
      const selectedButton = renderLadder(result.route, autoSelect);
      return Boolean(selectedButton);
    } catch (error) {
      if (requestedDelta !== deltaCzk) return false;
      detail.innerHTML =
        `<div class="status err">Could not load option routes: ${esc((error as Error).message)}</div>`;
      return false;
    } finally {
      option.disabled = false;
      if (option.textContent === "Loading live option routes…") paintLabels();
    }
  };
  option.addEventListener("click", () => { void loadOption(false); });

  const sync = (nextDeltaCzk: number) => {
    const next = Math.round(nextDeltaCzk);
    const changed = next !== Math.round(deltaCzk);
    deltaCzk = next;
    controls.hidden = Math.abs(next) < 1;
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
    detail.hidden = Math.abs(next) < 1 || !detail.innerHTML;
  };

  const disable = () => {
    controls.querySelectorAll<HTMLButtonElement>("button").forEach((button) => {
      button.disabled = true;
    });
  };

  sync(initialDeltaCzk);
  return { controls, detail, sync, autoSelectOption: () => loadOption(true), disable };
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
