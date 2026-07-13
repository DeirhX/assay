import { api, el, esc, fmtCZK, sensitive } from "./core";
import type { ExecutionPlanItem, ExecutionPlanState, RebalanceRouteSelection } from "./api-types";

const normalizeSymbol = (raw: string) => raw.trim().toUpperCase();

// ---- status / tone helpers ---------------------------------------------------

export function executionLifecycleChipClass(status: string): string {
  if (status === "queued") return "good";
  if (status === "deferred") return "warn";
  return "muted";
}

export function executionPlanItemLocked(item: ExecutionPlanItem): boolean {
  return item.status === "queued" || item.status === "submitted";
}

// ---- API patch helper --------------------------------------------------------

export async function patchExecutionPlanItem(
  item: ExecutionPlanItem,
  changes: Partial<ExecutionPlanItem>,
  statusEl?: HTMLElement | null,
): Promise<void> {
  const record = item as unknown as Record<string, unknown>;
  const previous = new Map<string, { present: boolean; value: unknown }>();
  Object.keys(changes).forEach((key) => {
    previous.set(key, {
      present: Object.prototype.hasOwnProperty.call(record, key),
      value: record[key],
    });
  });
  Object.assign(item, changes);
  try {
    const updated = await api<ExecutionPlanState>("/api/execution-plan", "POST", {
      action: "patch",
      item_id: item.id,
      changes,
    });
    const fresh = updated.items.find((candidate) => candidate.id === item.id);
    if (fresh) Object.assign(item, fresh);
    if (statusEl) {
      statusEl.textContent = "plan saved ✓";
      statusEl.className = "";
    }
  } catch (error) {
    previous.forEach(({ present, value }, key) => {
      if (present) record[key] = value;
      else delete record[key];
    });
    if (statusEl) {
      statusEl.textContent = `plan save failed: ${(error as Error).message}`;
      statusEl.className = "bad";
    }
    throw error;
  }
}

// ---- execution plan review table (Target state) ------------------------------

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
      `<td>${statuses.map((status) => `<span class="chip tone-chip ${executionLifecycleChipClass(status)}">${esc(status)}</span>`).join(" ")}</td></tr>`;
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

// ---- rebalance execution lifecycle cell --------------------------------------

export interface ExecutionRouteControlRef {
  controls: HTMLElement;
  compact: HTMLSelectElement;
  detail: HTMLElement;
  selectDirect: (limitPrice?: number) => void;
}

export interface ExecutionLifecycleConfig {
  patchItem: (changes: Partial<ExecutionPlanItem>) => Promise<void>;
  routeSelections: Map<string, RebalanceRouteSelection>;
  suggestedLimit?: number | null;
  marketReference?: number | null;
  limitCurrency?: string;
  pctToCzk: (deltaPct: number, base: number) => number | null;
  base: number;
  parseDelta: (value: string) => number;
  deltaEpsilon?: number;
}

export function createExecutionLifecycleCell(
  symbol: string,
  item: ExecutionPlanItem | null | undefined,
  amountInput: HTMLInputElement,
  route: ExecutionRouteControlRef,
  config: ExecutionLifecycleConfig,
): HTMLElement {
  const {
    patchItem,
    routeSelections,
    suggestedLimit = null,
    marketReference = null,
    limitCurrency = "",
    pctToCzk,
    base,
    parseDelta,
    deltaEpsilon = 0.0001,
  } = config;
  const host = el("div", "reb-execution-cell");
  if (!item) {
    host.classList.add("reb-execution-manual");
    const note = el("span", "reb-execution-na");
    const paintManual = () => {
      const hasAmount = Math.abs(parseDelta(amountInput.value)) > deltaEpsilon;
      note.textContent = hasAmount ? "Manual trade" : "No new trade";
      route.compact.hidden = !hasAmount;
    };
    amountInput.addEventListener("input", paintManual);
    host.append(note, route.compact);
    paintManual();
    return host;
  }

  const locked = executionPlanItemLocked(item);
  const lifecycle = el("div", "reb-execution-life");
  const execute = document.createElement("label");
  execute.className = "reb-execute-toggle";
  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.checked = item.status === "selected";
  checkbox.disabled = locked;
  const label = el("span", "", checkbox.disabled ? item.status : "Include trade");
  execute.title = "Include this amount in Preview impact and the order queue";
  execute.append(checkbox, label);
  const stateNote = el("span", "reb-execution-state");
  const exclude = el("button", "ghost reb-execution-exclude", "Exclude");
  exclude.type = "button";
  exclude.title = "Keep this recommendation, but remove it from Preview impact and the order queue";
  exclude.disabled = checkbox.disabled;
  const more = document.createElement("details");
  more.className = "reb-execution-more";
  const moreSummary = el("summary", "", "…");
  moreSummary.title = "More scheduling options";
  moreSummary.setAttribute("aria-label", "More scheduling options");
  const menu = el("div", "reb-execution-menu");
  const later = el("button", "", "Skip for now");
  later.type = "button";
  later.title = "Keep the recommendation, but exclude it from Preview impact and the order queue";
  later.disabled = checkbox.disabled;
  const dismiss = el("button", "reb-dismiss", "Dismiss recommendation");
  dismiss.type = "button";
  dismiss.title = "Dismiss from this execution plan";
  dismiss.disabled = checkbox.disabled;
  menu.append(later, dismiss);
  more.append(moreSummary, menu);
  lifecycle.append(execute, stateNote);
  if (!checkbox.disabled) lifecycle.appendChild(more);

  const paintStatus = () => {
    checkbox.checked = item.status === "selected";
    host.dataset.status = item.status;
    label.textContent = checkbox.disabled
      ? item.status
      : item.status === "selected"
        ? "Included"
        : "Include trade";
    stateNote.textContent = item.status === "deferred"
      ? "skipped"
      : item.status === "dismissed"
        ? "dismissed"
        : "";
    exclude.hidden = item.status !== "selected" || checkbox.disabled;
    more.classList.toggle(
      "has-state",
      item.status === "deferred" || item.status === "dismissed",
    );
  };

  const setStatus = async (status: ExecutionPlanItem["status"]): Promise<boolean> => {
    const previousStatus = item.status;
    item.status = status;
    paintStatus();
    try {
      await patchItem({ status });
      return true;
    } catch {
      item.status = previousStatus;
      paintStatus();
      return false;
    }
  };
  checkbox.addEventListener("change", async () => {
    if (!checkbox.checked) {
      await setStatus("deferred");
      return;
    }
    if (await setStatus("selected")) {
      route.selectDirect(Number(limit.value) || undefined);
    }
  });
  later.addEventListener("click", () => {
    more.open = false;
    void setStatus("deferred");
  });
  exclude.addEventListener("click", async () => {
    if (!(await setStatus("deferred"))) return;
    route.detail.hidden = true;
    route.detail.innerHTML = "";
  });
  dismiss.addEventListener("click", () => {
    more.open = false;
    void setStatus("dismissed");
  });
  amountInput.addEventListener("change", async () => {
    const deltaPct = parseDelta(amountInput.value);
    const deltaCzk = pctToCzk(deltaPct, base) || 0;
    const direction = deltaCzk >= 0 ? "increase" : "reduce";
    try {
      await patchItem({
        delta_pct: deltaPct,
        delta_czk: deltaCzk,
        desired_weight_pct: Math.max(0, Number(amountInput.dataset.currentPct || 0) + deltaPct),
        direction,
        status: "selected",
      });
      paintStatus();
      route.selectDirect(Number(limit.value) || undefined);
    } catch {
      paintStatus();
    }
  });

  const limit = document.createElement("input");
  limit.className = "reb-limit-input";
  limit.type = "number";
  limit.min = "0.01";
  limit.step = "0.01";
  limit.placeholder = "Market";
  limit.title = marketReference
    ? `Editable order limit; an empty market value ticks from the last ${marketReference}`
    : "Editable order limit; option values are minimum credits";
  const initialLimit = item.limit_price || suggestedLimit;
  if (initialLimit) limit.value = String(initialLimit);
  const limitLabel = el("span", "", "");
  const paintLimitLabel = () => {
    const value = Number(limit.value);
    const recommended = Boolean(
      suggestedLimit && value > 0 && Math.abs(value - suggestedLimit) < 0.000001,
    );
    limitLabel.textContent = `Limit${limitCurrency ? ` (${limitCurrency})` : ""} · ` +
      (recommended ? "recommended" : value > 0 ? "custom" : "market");
  };
  limit.addEventListener("change", async () => {
    const value = Number(limit.value);
    const nextLimit = value > 0 ? value : null;
    const selection = routeSelections.get(normalizeSymbol(symbol));
    const persistedSelectionLimit = selection?.limit_price;
    try {
      await patchItem({ limit_price: nextLimit });
      if (selection) {
        if (value > 0) selection.limit_price = value;
        else delete selection.limit_price;
      }
      paintLimitLabel();
    } catch {
      limit.value = typeof item.limit_price === "number" ? String(item.limit_price) : "";
      if (selection) {
        if (typeof persistedSelectionLimit === "number") selection.limit_price = persistedSelectionLimit;
        else delete selection.limit_price;
      }
      paintLimitLabel();
    }
  });
  limit.addEventListener("keydown", (event) => {
    if (
      limit.value.trim()
      || marketReference == null
      || (event.key !== "ArrowUp" && event.key !== "ArrowDown")
    ) return;
    event.preventDefault();
    const direction = event.key === "ArrowUp" ? 1 : -1;
    limit.value = String(Math.max(0.01, Math.round(
      (marketReference + direction * Number(limit.step || 0.01)) * 100,
    ) / 100));
    limit.dispatchEvent(new Event("change", { bubbles: true }));
  });
  limit.addEventListener("pointerdown", (event) => {
    if (limit.value.trim() || marketReference == null) return;
    const rect = limit.getBoundingClientRect();
    if (event.clientX < rect.right - 20) return;
    limit.value = String(marketReference);
    paintLimitLabel();
  });
  const limitField = document.createElement("label");
  limitField.className = "reb-limit-field";
  limitField.append(limitLabel, limit);
  paintLimitLabel();
  const routeLine = el("div", "reb-execution-route-line");
  routeLine.append(route.compact, exclude, limitField);
  paintStatus();
  host.append(lifecycle, routeLine);
  if (locked) {
    route.controls.querySelectorAll<HTMLButtonElement>("button").forEach((button) => {
      button.disabled = true;
    });
    route.compact.disabled = true;
    limit.disabled = true;
  }
  return host;
}
