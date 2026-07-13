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
import type { ExecutionPlanItem } from "../src/api-types";

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

  it("wires Execute / Later / Dismiss and patches status changes", async () => {
    const item = baseItem();
    apiMock.mockResolvedValue({ schema_version: 1, version: 1, items: [item] });
    const input = document.createElement("input");
    input.type = "number";
    input.value = "1.5";
    input.dataset.currentPct = "3.5";
    const routeControls = document.createElement("div");
    const patches: Partial<ExecutionPlanItem>[] = [];
    const host = createExecutionLifecycleCell("NVDA", item, input, {
      controls: routeControls,
      autoSelectOption: async () => true,
    }, {
      patchItem: async (changes) => { patches.push(changes); },
      onAmountChange: async () => {},
    });
    document.body.appendChild(host);

    expect(host.querySelector(".reb-execute-toggle input")).toBeTruthy();
    expect(host.querySelector("button")?.textContent).toBe("Later");

    host.querySelector<HTMLButtonElement>(".reb-dismiss")!.click();
    await vi.waitFor(() => expect(patches.some((p) => p.status === "dismissed")).toBe(true));
    expect(host.dataset.status).toBe("dismissed");
  });

  it("disables lifecycle controls when queued or submitted", () => {
    const item = { ...baseItem(), status: "queued" as const };
    const host = createExecutionLifecycleCell("NVDA", item, document.createElement("input"), {
      controls: document.createElement("div"),
      autoSelectOption: async () => true,
    }, {
      patchItem: async () => {},
      onAmountChange: async () => {},
    });
    expect(executionPlanItemLocked(item)).toBe(true);
    expect(host.querySelector<HTMLInputElement>(".reb-execute-toggle input")!.disabled).toBe(true);
    expect(host.querySelector<HTMLInputElement>(".reb-limit-input")!.disabled).toBe(true);
  });

  it("rolls lifecycle UI back when persistence fails", async () => {
    const item = baseItem();
    const host = createExecutionLifecycleCell("NVDA", item, document.createElement("input"), {
      controls: document.createElement("div"),
      autoSelectOption: async () => true,
    }, {
      patchItem: async () => { throw new Error("write failed"); },
      onAmountChange: async () => {},
    });

    host.querySelector<HTMLButtonElement>(".reb-dismiss")!.click();
    await vi.waitFor(() => expect(host.dataset.status).toBe("deferred"));
    expect(item.status).toBe("deferred");
  });

  it("returns route controls only when no execution item exists", () => {
    const routeControls = document.createElement("div");
    routeControls.className = "reb-route-controls";
    const host = createExecutionLifecycleCell("NVDA", null, document.createElement("input"), {
      controls: routeControls,
      autoSelectOption: async () => false,
    }, {
      patchItem: async () => {},
      onAmountChange: async () => {},
    });
    expect(host.querySelector(".reb-execution-life")).toBeNull();
    expect(host.querySelector(".reb-route-controls")).toBe(routeControls);
  });
});
