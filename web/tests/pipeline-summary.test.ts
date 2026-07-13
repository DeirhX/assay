import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  countWorkingOrders, PIPELINE_CHANGED_EVENT, publishPipelineChanged,
  queueWorkflowView, subscribePipelineChanged, updatePipelineChrome,
} from "../src/pipeline-summary";

describe("pipeline order truth", () => {
  it("counts only non-terminal broker orders as working", () => {
    expect(countWorkingOrders([
      { status: "Submitted" },
      { order_status: "PreSubmitted" },
      { status: "Filled" },
      { status: "Cancelled" },
    ])).toBe(2);
  });

  it("routes only a reviewed, valid queue to placement", () => {
    expect(queueWorkflowView({ count: 2, reviewed: false, valid: true })).toBe("target-state");
    expect(queueWorkflowView({ count: 2, reviewed: true, valid: false })).toBe("target-state");
    expect(queueWorkflowView({ count: 2, reviewed: true, valid: true })).toBe("trade");
    expect(queueWorkflowView({ count: 0, reviewed: true, valid: true })).toBeNull();
  });
});

describe("pipeline state event and chrome", () => {
  beforeEach(() => {
    document.body.innerHTML =
      `<span id="orders-count" hidden></span>` +
      `<button id="today-orders-inflight"><strong></strong><small></small></button>`;
    updatePipelineChrome({ planned: 0, queued: 0, working: null });
  });

  it("publishes one shared invalidation event", () => {
    const handler = vi.fn();
    const unsubscribe = subscribePipelineChanged(handler);
    publishPipelineChanged({ source: "plan", planned: 2 });
    expect(handler).toHaveBeenCalledWith({ source: "plan", planned: 2 });
    unsubscribe();
    publishPipelineChanged({ source: "queue" });
    expect(handler).toHaveBeenCalledTimes(1);
    expect(PIPELINE_CHANGED_EVENT).toBe("assay:pipeline-changed");
  });

  it("paints the header and Today from the same counts", () => {
    updatePipelineChrome({ planned: 2, queued: 1, working: 3 });
    const badge = document.querySelector<HTMLElement>("#orders-count")!;
    const pulse = document.querySelector<HTMLElement>("#today-orders-inflight")!;
    expect(badge.textContent).toBe("6");
    expect(badge.hidden).toBe(false);
    expect(badge.title).toContain("3 working");
    expect(pulse.querySelector("strong")?.textContent).toBe("6");
    expect(pulse.querySelector("small")?.textContent).toContain("2 planned");
  });
});
