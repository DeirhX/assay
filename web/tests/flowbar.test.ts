// Tests for the rebalance flow bar's pure builders: stage counts and tones from
// the overview payload, the working-orders chip only when the gateway answered,
// the view->stage mapping, and the active-stage highlight.
import { describe, expect, it, vi } from "vitest";

// Keep updateFlowBar's best-effort data fetch off the network so it doesn't leave
// a pending request happy-dom has to abort on teardown; $ and esc stay real.
vi.mock("../src/core", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/core")>()),
  api: vi.fn(() => Promise.resolve({})),
}));

import { api } from "../src/core";
import {
  flowBarHtml, flowStages, invalidateFlowData, stageForView, updateFlowBar, type FlowData,
} from "../src/flowbar";

const apiMock = vi.mocked(api);

const data = (over: Partial<FlowData> = {}): FlowData => ({
  ov: {
    snapshot: { exists: true, positions: 42, age_days: 2, stale: false },
    plan: { rows: 16, out_of_band: 3, actionable: 3, gates_open: 1, cash: { status: "IN" } },
    draft: { pending: 2 },
    staged_basket: { count: 4 },
  },
  working: 1,
  ...over,
});

describe("stageForView", () => {
  it("maps target-state before the Trade desk and the rest to Plan changes", () => {
    expect(stageForView("target-state")).toBe(3);
    expect(stageForView("trade")).toBe(4);
    for (const v of ["rebalance", "optimizer", "working-draft", "exit"]) {
      expect(stageForView(v)).toBe(2);
    }
  });

  it("maps the current-book views (holdings/setup) to stage 1", () => {
    expect(stageForView("holdings")).toBe(1);
    expect(stageForView("setup")).toBe(1);
  });
});

describe("updateFlowBar visibility", () => {
  it("stays visible on holdings (stage 1) but hides on other portfolio views", () => {
    document.body.innerHTML = '<nav id="flowbar" hidden></nav>';
    const host = document.getElementById("flowbar") as HTMLElement;
    updateFlowBar("rebalance", "rebalance");
    expect(host.hidden).toBe(false);           // its own group
    updateFlowBar("holdings", "portfolio");
    expect(host.hidden).toBe(false);           // Current book, though a portfolio view
    updateFlowBar("history", "portfolio");
    expect(host.hidden).toBe(true);            // not a pipeline step
  });
});

describe("working-order polling gate", () => {
  it("does not request protected orders while trading is disabled", async () => {
    apiMock.mockImplementation((path: string) => {
      if (path === "/api/overview") return Promise.resolve({});
      if (path === "/api/trade/status") {
        return Promise.resolve({ trading_enabled: false, authenticated: false });
      }
      return Promise.reject(new Error("orders should not be requested"));
    });
    document.body.innerHTML = '<nav id="flowbar" hidden></nav>';
    invalidateFlowData();
    updateFlowBar("rebalance", "rebalance");
    await vi.waitFor(() => expect(apiMock).toHaveBeenCalledWith("/api/trade/status"));
    expect(apiMock).not.toHaveBeenCalledWith("/api/trade/orders");
  });

  it("loads working orders only for an authenticated enabled session", async () => {
    apiMock.mockImplementation((path: string) => {
      if (path === "/api/overview") return Promise.resolve({});
      if (path === "/api/trade/status") {
        return Promise.resolve({ trading_enabled: true, authenticated: true });
      }
      if (path === "/api/trade/orders") return Promise.resolve({ orders: [{ id: 1 }] });
      return Promise.resolve({});
    });
    document.body.innerHTML = '<nav id="flowbar" hidden></nav>';
    invalidateFlowData();
    updateFlowBar("trade", "rebalance");
    await vi.waitFor(() => expect(apiMock).toHaveBeenCalledWith("/api/trade/orders"));
    expect(document.getElementById("flowbar")?.textContent).toContain("1 working");
  });
});

describe("flowStages", () => {
  it("summarises the current book with positions, freshness, and working orders", () => {
    const s1 = flowStages(data())[0];
    expect(s1.sub).toContain("42 positions");
    expect(s1.sub).toContain("synced 2d ago");
    expect(s1.sub).toContain("1 working order");
    expect(s1.view).toBe("holdings");
  });

  it("omits the working-order bit when the gateway state is unknown", () => {
    const s1 = flowStages(data({ working: null }))[0];
    expect(s1.sub).not.toContain("working");
  });

  it("counts suggestions, drafts, and triggered gates in stage 2", () => {
    const s2 = flowStages(data())[1];
    expect(s2.sub).toContain("3 suggested");
    expect(s2.sub).toContain("2 drafted");
    expect(s2.sub).toContain("1 gate triggered");
    expect(s2.tone).toBe("warn");
  });

  it("stage 3 reads bands-in and flags off-target cash; all-in reads ok", () => {
    const s3 = flowStages(data())[2];
    expect(s3.sub).toBe("13/16 bands in");
    const done = flowStages(data({
      ov: { snapshot: { exists: true, positions: 1, age_days: 0 },
            plan: { rows: 5, out_of_band: 0, actionable: 0, cash: { status: "IN" } },
            draft: { pending: 0 }, staged_basket: { count: 0 } },
      working: 0,
    }))[2];
    expect(done.tone).toBe("ok");
    const cashOff = flowStages(data({
      ov: { ...data().ov, plan: { rows: 5, out_of_band: 0, actionable: 0, cash: { status: "BELOW" } } },
    }))[2];
    expect(cashOff.sub).toContain("cash off target");
  });

  it("routes stage 3 to the Target state comparison view", () => {
    expect(flowStages(data())[2].view).toBe("target-state");
  });

  it("degrades to setup guidance without a snapshot", () => {
    const s1 = flowStages({ ov: { snapshot: { exists: false } }, working: null })[0];
    expect(s1.view).toBe("setup");
    expect(s1.sub).toContain("no holdings");
  });
});

describe("flowBarHtml", () => {
  it("highlights the active stage and wires click targets", () => {
    const html = flowBarHtml(data(), 3);
    expect(html).toContain('data-flow-view="trade"');
    expect((html.match(/flow-stage/g) || []).length).toBeGreaterThanOrEqual(4);
    expect(html).toContain("active");
    // Exactly one active stage.
    expect((html.match(/ active"/g) || []).length).toBe(1);
  });
});
