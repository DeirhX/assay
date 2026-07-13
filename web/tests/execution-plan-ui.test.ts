import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../src/core", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/core")>()),
  api: vi.fn(),
}));

import { api } from "../src/core";
import {
  createExecutionLifecycleCell,
  executionLifecycleChipClass,
  executionPlanHtml,
  executionPlanItemLocked,
  patchExecutionPlanItem,
} from "../src/execution-plan-ui";
import type { ExecutionPlanItem, RebalanceRouteSelection } from "../src/api-types";

const apiMock = vi.mocked(api);

const baseItem = (): ExecutionPlanItem => ({
  id: "item-1",
  symbol: "NVDA",
  source: "rebalance",
  direction: "increase",
  delta_czk: 100_000,
  delta_pct: 1.5,
  desired_weight_pct: 5,
  route_policy: "auto_put",
  status: "deferred",
});

function routeRef() {
  const controls = document.createElement("div");
  const compact = document.createElement("select");
  const detail = document.createElement("div");
  return {
    controls,
    compact,
    detail,
    selectDirect: vi.fn(),
  };
}

function lifecycleConfig(
  item: ExecutionPlanItem | null | undefined,
  routeSelections = new Map<string, RebalanceRouteSelection>(),
  patchItemImpl?: (changes: Partial<ExecutionPlanItem>) => Promise<void>,
) {
  const patches: Partial<ExecutionPlanItem>[] = [];
  return {
    config: {
      patchItem: patchItemImpl ?? (async (changes: Partial<ExecutionPlanItem>) => {
        patches.push(changes);
        if (item) Object.assign(item, changes);
      }),
      routeSelections,
      suggestedLimit: 95.5,
      marketReference: 96,
      limitCurrency: "USD",
      pctToCzk: (pct: number) => pct * 10_000,
      base: 1_000_000,
      parseDelta: (value: string) => Number(value) || 0,
      deltaEpsilon: 0.0001,
    },
    patches,
  };
}

describe("executionPlanHtml", () => {
  it("maps lifecycle statuses to chip tone classes", () => {
    expect(executionLifecycleChipClass("queued")).toBe("good");
    expect(executionLifecycleChipClass("deferred")).toBe("warn");
    expect(executionLifecycleChipClass("selected")).toBe("muted");
  });

  it("renders an empty-state CTA when no active items remain", () => {
    const html = executionPlanHtml({ schema_version: 1, version: 1, items: [] });
    expect(html).toContain("No active actions");
    expect(html).toContain('data-ts-goto="rebalance"');
  });
});

describe("patchExecutionPlanItem", () => {
  beforeEach(() => apiMock.mockReset());

  it("merges server state back onto the local item", async () => {
    const item = baseItem();
    apiMock.mockResolvedValue({
      schema_version: 1,
      version: 2,
      items: [{ ...item, status: "selected" }],
    });
    const statusEl = document.createElement("span");
    await patchExecutionPlanItem(item, { status: "selected" }, statusEl);
    expect(item.status).toBe("selected");
    expect(statusEl.textContent).toBe("plan saved ✓");
  });
});

describe("createExecutionLifecycleCell", () => {
  beforeEach(() => apiMock.mockReset());

  it("wires Include / Exclude / overflow menu and patches status changes", async () => {
    const item = baseItem();
    const input = document.createElement("input");
    input.type = "number";
    input.value = "1.5";
    input.dataset.currentPct = "3.5";
    const route = routeRef();
    const { config } = lifecycleConfig(item);
    const host = createExecutionLifecycleCell("NVDA", item, input, route, config);
    document.body.appendChild(host);

    expect(host.querySelector(".reb-execute-toggle input")).toBeTruthy();
    expect(host.querySelector(".reb-execution-exclude")).toBeTruthy();
    expect(host.querySelector(".reb-execution-more")).toBeTruthy();

    host.querySelector<HTMLButtonElement>(".reb-dismiss")!.click();
    await vi.waitFor(() => expect(item.status).toBe("dismissed"));
    expect(host.dataset.status).toBe("dismissed");
  });

  it("disables lifecycle controls when queued or submitted", () => {
    const item = { ...baseItem(), status: "queued" as const };
    const { config } = lifecycleConfig(item);
    const host = createExecutionLifecycleCell("NVDA", item, document.createElement("input"), routeRef(), config);
    expect(executionPlanItemLocked(item)).toBe(true);
    expect(host.querySelector<HTMLInputElement>(".reb-execute-toggle input")!.disabled).toBe(true);
    expect(host.querySelector<HTMLInputElement>(".reb-limit-input")!.disabled).toBe(true);
    expect(host.querySelector(".reb-execution-more")).toBeNull();
  });

  it("labels limit as recommended when it matches the suggested value", () => {
    const item = { ...baseItem(), limit_price: 95.5 };
    const { config } = lifecycleConfig(item);
    const host = createExecutionLifecycleCell("NVDA", item, document.createElement("input"), routeRef(), config);
    expect(host.querySelector(".reb-limit-field span")?.textContent).toContain("recommended");
  });

  it("shows manual-trade branch when no execution item exists", () => {
    const route = routeRef();
    const { config } = lifecycleConfig(null);
    const input = document.createElement("input");
    input.value = "0";
    const host = createExecutionLifecycleCell("NVDA", null, input, route, config);
    expect(host.classList.contains("reb-execution-manual")).toBe(true);
    expect(host.querySelector(".reb-execution-na")?.textContent).toBe("No new trade");
    input.value = "1.5";
    input.dispatchEvent(new Event("input"));
    expect(host.querySelector(".reb-execution-na")?.textContent).toBe("Manual trade");
  });

  it("patches amount changes and selects direct shares", async () => {
    const item = baseItem();
    const input = document.createElement("input");
    input.type = "number";
    input.value = "2";
    input.dataset.currentPct = "3";
    const route = routeRef();
    const { config, patches } = lifecycleConfig(item);
    const host = createExecutionLifecycleCell("NVDA", item, input, route, config);
    document.body.appendChild(host);
    input.dispatchEvent(new Event("change"));
    await vi.waitFor(() => expect(patches.some((p) => p.delta_pct === 2)).toBe(true));
    expect(route.selectDirect).toHaveBeenCalled();
    expect(host.dataset.status).toBe("selected");
  });

  it("rolls status UI back when persistence rejects", async () => {
    const item = baseItem();
    const { config } = lifecycleConfig(item, new Map(), async () => {
      throw new Error("write failed");
    });
    const host = createExecutionLifecycleCell("NVDA", item, document.createElement("input"), routeRef(), config);
    document.body.appendChild(host);

    host.querySelector<HTMLButtonElement>(".reb-dismiss")!.click();
    await vi.waitFor(() => expect(host.dataset.status).toBe("deferred"));
    expect(item.status).toBe("deferred");
    expect(host.querySelector<HTMLInputElement>(".reb-execute-toggle input")!.checked).toBe(false);
  });

  it("does not select direct shares when amount persistence fails", async () => {
    const item = baseItem();
    const input = document.createElement("input");
    input.type = "number";
    input.value = "2";
    input.dataset.currentPct = "3";
    const route = routeRef();
    const { config } = lifecycleConfig(item, new Map(), async () => {
      throw new Error("write failed");
    });
    const host = createExecutionLifecycleCell("NVDA", item, input, route, config);
    document.body.appendChild(host);
    input.dispatchEvent(new Event("change"));
    await vi.waitFor(() => expect(route.selectDirect).not.toHaveBeenCalled());
    expect(host.dataset.status).toBe("deferred");
    expect(item.status).toBe("deferred");
  });

  it("restores limit input and route selection when limit persistence fails", async () => {
    const item = { ...baseItem(), limit_price: 95.5 };
    const routeSelections = new Map<string, RebalanceRouteSelection>([[
      "NVDA",
      { symbol: "NVDA", route: "buy_shares", limit_price: 95.5 },
    ]]);
    const { config } = lifecycleConfig(item, routeSelections, async () => {
      throw new Error("write failed");
    });
    const host = createExecutionLifecycleCell("NVDA", item, document.createElement("input"), routeRef(), config);
    document.body.appendChild(host);
    const limit = host.querySelector<HTMLInputElement>(".reb-limit-input")!;
    limit.value = "101";
    limit.dispatchEvent(new Event("change"));
    await vi.waitFor(() => expect(limit.value).toBe("95.5"));
    expect(routeSelections.get("NVDA")?.limit_price).toBe(95.5);
    expect(host.querySelector(".reb-limit-field span")?.textContent).toContain("recommended");
  });

  it("does not clear route detail when exclude persistence fails", async () => {
    const item = { ...baseItem(), status: "selected" as const };
    const route = routeRef();
    route.detail.innerHTML = "<div class=\"reb-option-contract\">open</div>";
    const { config } = lifecycleConfig(item, new Map(), async () => {
      throw new Error("write failed");
    });
    const host = createExecutionLifecycleCell("NVDA", item, document.createElement("input"), route, config);
    document.body.appendChild(host);
    host.querySelector<HTMLButtonElement>(".reb-execution-exclude")!.click();
    await vi.waitFor(() => expect(item.status).toBe("selected"));
    expect(route.detail.innerHTML).toContain("reb-option-contract");
    expect(host.dataset.status).toBe("selected");
  });
});
