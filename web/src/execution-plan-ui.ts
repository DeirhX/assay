import { api, el, esc, fmtCZK, sensitive } from "./core";
import type { ExecutionPlanItem, ExecutionPlanState } from "./api-types";

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
  autoSelectOption: () => Promise<boolean>;
}

export interface ExecutionLifecycleCallbacks {
  patchItem: (changes: Partial<ExecutionPlanItem>) => Promise<void>;
  onAmountChange: () => Promise<void>;
  onLimitChange?: (limitPrice: number | null) => void;
  onAutoSelectFailed?: (symbol: string, message: string) => void;
  shouldAutoSelectOnExecute?: (item: ExecutionPlanItem) => boolean;
  shouldAutoSelectOnAmountChange?: (item: ExecutionPlanItem, direction: "increase" | "reduce") => boolean;
  shouldDeferAfterAutoSelect?: (symbol: string, available: boolean) => boolean;
  onRoutePolicyPatch?: (direction: "increase" | "reduce") => Promise<void>;
}

export function createExecutionLifecycleCell(
  symbol: string,
  item: ExecutionPlanItem | null | undefined,
  amountInput: HTMLInputElement,
  route: ExecutionRouteControlRef,
  callbacks: ExecutionLifecycleCallbacks,
): HTMLElement {
  const host = el("div", "reb-execution-cell");
  if (!item) {
    host.appendChild(route.controls);
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
  const label = el("span", "", checkbox.disabled ? item.status : "Execute");
  execute.append(checkbox, label);
  const later = el("button", "ghost", "Later");
  later.type = "button";
  later.disabled = locked;
  const dismiss = el("button", "ghost reb-dismiss", "×");
  dismiss.type = "button";
  dismiss.title = "Dismiss from this execution plan";
  dismiss.disabled = locked;
  lifecycle.append(execute, later, dismiss);

  const syncStatusUi = (status: ExecutionPlanItem["status"]) => {
    checkbox.checked = status === "selected";
    host.dataset.status = status;
    later.classList.toggle("active", status === "deferred");
    dismiss.classList.toggle("active", status === "dismissed");
  };
  const setStatus = async (status: ExecutionPlanItem["status"]) => {
    const previousStatus = item.status;
    item.status = status;
    syncStatusUi(status);
    try {
      await callbacks.patchItem({ status });
      return true;
    } catch {
      item.status = previousStatus;
      syncStatusUi(previousStatus);
      return false;
    }
  };

  checkbox.addEventListener("change", async () => {
    if (!checkbox.checked) {
      await setStatus("deferred");
      return;
    }
    if (!(await setStatus("selected"))) return;
    if (callbacks.shouldAutoSelectOnExecute?.(item)) {
      const available = await route.autoSelectOption();
      if (callbacks.shouldDeferAfterAutoSelect?.(symbol, available) ?? !available) {
        await setStatus("deferred");
        callbacks.onAutoSelectFailed?.(
          symbol,
          `${symbol}: no executable cash-secured put; deferred`,
        );
      }
    }
  });
  later.addEventListener("click", () => { void setStatus("deferred"); });
  dismiss.addEventListener("click", () => { void setStatus("dismissed"); });

  amountInput.addEventListener("change", async () => {
    try {
      await callbacks.onAmountChange();
    } catch {
      syncStatusUi(item.status);
      return;
    }
    checkbox.checked = true;
    host.dataset.status = "selected";
    const deltaPct = Number(amountInput.value) || 0;
    const direction = deltaPct >= 0 ? "increase" : "reduce";
    if (callbacks.shouldAutoSelectOnAmountChange?.(item, direction)) {
      const available = await route.autoSelectOption();
      if (callbacks.shouldDeferAfterAutoSelect?.(symbol, available) ?? !available) {
        await setStatus("deferred");
      }
    } else if (direction === "reduce" && callbacks.onRoutePolicyPatch) {
      await callbacks.onRoutePolicyPatch(direction);
    }
  });

  const limit = document.createElement("input");
  limit.className = "reb-limit-input";
  limit.type = "number";
  limit.min = "0.01";
  limit.step = "0.01";
  limit.placeholder = "Auto limit";
  limit.title = "Editable order limit; option values are minimum credits";
  if (item.limit_price) limit.value = String(item.limit_price);
  limit.addEventListener("change", () => {
    const value = Number(limit.value);
    const nextLimit = value > 0 ? value : null;
    void callbacks.patchItem({ limit_price: nextLimit })
      .then(() => callbacks.onLimitChange?.(nextLimit))
      .catch(() => {
        limit.value = typeof item.limit_price === "number" ? String(item.limit_price) : "";
      });
  });

  const routeLine = el("div", "reb-execution-route-line");
  routeLine.append(route.controls, limit);
  host.dataset.status = item.status;
  host.append(lifecycle, routeLine);

  if (locked) {
    route.controls.querySelectorAll<HTMLButtonElement>("button").forEach((button) => {
      button.disabled = true;
    });
    limit.disabled = true;
  }

  return host;
}
