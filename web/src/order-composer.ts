import { api, el, esc, fmtCZK } from "./core";
import type {
  ExecutionPlanItem, ExecutionPlanState, RebalanceOptionRung,
  RebalanceRouteResponse, RebalanceRouteSelection, TradeQueueState,
} from "./api-types";
import { cleanSymbol } from "./shell";

interface ComposerOptions {
  symbol: string;
  currentPrice?: number | null;
  currency?: string | null;
  held?: boolean;
}

export function renderOrderComposer(options: ComposerOptions): HTMLElement {
  const symbol = cleanSymbol(options.symbol);
  const root = document.createElement("details");
  root.className = "ticker-order-composer";
  root.innerHTML =
    `<summary><span>Plan an order</span><small>Add ${esc(symbol)} shares or a covered option without leaving this dossier</small></summary>`;
  const body = el("div", "ticker-order-body");
  const fields = el("div", "ticker-order-fields");
  fields.innerHTML =
    `<label>Direction<select data-order-direction>` +
      `<option value="increase">Increase</option>` +
      `<option value="reduce"${options.held ? "" : " disabled"}>Reduce</option>` +
    `</select></label>` +
    `<label>Amount (CZK)<input data-order-amount type="number" min="1" step="1000" value="100000"></label>` +
    `<label>Execution<select data-order-route>` +
      `<option value="cash_secured_put">Cash-secured put</option>` +
      `<option value="buy_shares">Shares</option>` +
    `</select></label>` +
    `<label>Limit<input data-order-limit type="number" min="0.01" step="0.01" placeholder="Recommended"></label>`;
  const direction = fields.querySelector<HTMLSelectElement>("[data-order-direction]")!;
  const amount = fields.querySelector<HTMLInputElement>("[data-order-amount]")!;
  const routeSelect = fields.querySelector<HTMLSelectElement>("[data-order-route]")!;
  const limit = fields.querySelector<HTMLInputElement>("[data-order-limit]")!;
  const routeBox = el("div", "ticker-order-route");
  const status = el("div", "status");
  const actions = el("div", "ticker-order-actions");
  const save = el("button", "ghost", "Save to execution plan");
  const queue = el("button", "primary", "Add to order queue");
  save.type = "button";
  queue.type = "button";
  actions.append(save, queue);
  body.append(fields, routeBox, actions, status);
  root.appendChild(body);

  let optionSelection: RebalanceRouteSelection | null = null;
  let routeLoad = 0;

  const deltaCzk = () => {
    const value = Math.abs(Number(amount.value) || 0);
    return direction.value === "increase" ? value : -value;
  };
  const directRoute = () => direction.value === "increase" ? "buy_shares" : "sell_shares";
  const optionRoute = () => direction.value === "increase" ? "cash_secured_put" : "covered_call";
  const selectedRoute = () => routeSelect.value;
  const selectedLimit = () => {
    const value = Number(limit.value);
    return value > 0 ? value : undefined;
  };

  const paintDirect = () => {
    optionSelection = null;
    routeBox.innerHTML =
      `<span class="chip good">Shares</span> ` +
      `<span class="muted">Immediate ${direction.value === "increase" ? "increase" : "reduction"}.</span>`;
    if (!limit.value && options.currentPrice) limit.value = String(options.currentPrice);
    queue.disabled = false;
  };

  const loadOption = async () => {
    const token = ++routeLoad;
    optionSelection = null;
    queue.disabled = true;
    routeBox.innerHTML = `<span class="spinner"></span> Loading executable contracts…`;
    try {
      const query = new URLSearchParams({ symbol, delta_czk: String(deltaCzk()) });
      const route = await api<RebalanceRouteResponse>(
        `/api/rebalance/route?${query.toString()}`, "GET", null, { timeoutMs: 60_000 },
      );
      if (token !== routeLoad) return;
      const rung = route.ladder.find((candidate: RebalanceOptionRung) =>
        candidate.stageable && candidate.conid);
      if (!route.option.eligible || !rung) {
        routeBox.innerHTML =
          `<span class="chip warn">${esc(route.option.label)} unavailable</span> ` +
          `<span class="muted">${esc(route.option.reasons.join(" · ") || "No executable contract.")}</span>`;
        return;
      }
      optionSelection = {
        symbol,
        route: optionRoute(),
        conid: Number(rung.conid),
        expiry: rung.expiry,
        strike: rung.strike,
        contracts: route.option.contracts,
        ...(typeof rung.limit_price === "number" ? { limit_price: rung.limit_price } : {}),
      };
      if (typeof rung.limit_price === "number") limit.value = String(rung.limit_price);
      routeBox.innerHTML =
        `<span class="chip good">${esc(route.option.label)}</span> ` +
        `<strong>${esc(rung.expiry)} · ${rung.strike}${direction.value === "increase" ? "P" : "C"}</strong> ` +
        `<span class="muted">${route.option.contracts} contract(s) · ${fmtCZK(Math.abs(deltaCzk()))} CZK plan</span>`;
      queue.disabled = false;
    } catch (error) {
      if (token !== routeLoad) return;
      routeBox.innerHTML = `<span class="status err">${esc((error as Error).message)}</span>`;
    }
  };

  const syncDirection = () => {
    const option = optionRoute();
    routeSelect.innerHTML =
      `<option value="${option}">${direction.value === "increase" ? "Cash-secured put" : "Covered call"}</option>` +
      `<option value="${directRoute()}">Shares</option>`;
    limit.value = "";
    if (selectedRoute() === option) void loadOption();
  };
  direction.addEventListener("change", syncDirection);
  routeSelect.addEventListener("change", () => {
    limit.value = "";
    if (selectedRoute() === optionRoute()) void loadOption();
    else paintDirect();
  });
  amount.addEventListener("change", () => {
    if (selectedRoute() === optionRoute()) void loadOption();
  });
  root.addEventListener("toggle", () => {
    if (root.open && selectedRoute() === optionRoute() && !optionSelection) void loadOption();
  });

  const createItem = async (forQueue: boolean): Promise<ExecutionPlanItem> => {
    if (!deltaCzk()) throw new Error("Enter a non-zero CZK amount");
    const isOption = selectedRoute() === optionRoute();
    const selection = isOption ? optionSelection : {
      symbol,
      route: directRoute(),
      ...(selectedLimit() ? { limit_price: selectedLimit() } : {}),
    } as RebalanceRouteSelection;
    if (forQueue && isOption && !selection) throw new Error("No executable option contract selected");
    const response = await api<{ state: ExecutionPlanState; item: ExecutionPlanItem }>(
      "/api/execution-plan", "POST", {
        action: "manual",
        item: {
          symbol,
          delta_czk: deltaCzk(),
          source: "ticker",
          route_policy: selectedRoute(),
          route_selection: selection,
          limit_price: selectedLimit(),
          status: forQueue ? "selected" : (selection ? "selected" : "deferred"),
        },
      },
    );
    return response.item;
  };

  save.addEventListener("click", async () => {
    save.disabled = true;
    status.className = "status";
    status.textContent = "Saving…";
    try {
      await createItem(false);
      status.textContent = "Saved to the execution plan ✓";
    } catch (error) {
      status.className = "status err";
      status.textContent = (error as Error).message;
    } finally {
      save.disabled = false;
    }
  });

  queue.addEventListener("click", async () => {
    queue.disabled = true;
    status.className = "status";
    status.textContent = "Adding to queue…";
    try {
      const item = await createItem(true);
      const selection = item.route_selection || {
        symbol,
        route: directRoute(),
        ...(selectedLimit() ? { limit_price: selectedLimit() } : {}),
      };
      selection.execution_item_id = item.id;
      if (selectedLimit()) selection.limit_price = selectedLimit();
      await api<TradeQueueState>("/api/rebalance/stage", "POST", {
        trades: [{ symbol, delta_czk: deltaCzk() }],
        selections: [selection],
        mode: "append",
        source: "ticker",
      });
      window.dispatchEvent(new Event("assay:queue-changed"));
      status.textContent = "Added to the order queue ✓";
    } catch (error) {
      status.className = "status err";
      status.textContent = (error as Error).message;
      queue.disabled = false;
    }
  });

  return root;
}
