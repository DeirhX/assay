// Tests for the rebalance flow bar's pure builders: stage counts and tones from
// the overview payload, the working-orders chip only when the gateway answered,
// the view->stage mapping, and the active-stage highlight.
import { beforeEach, describe, expect, it, vi } from "vitest";

// Keep updateFlowBar's best-effort data fetch off the network so it doesn't leave
// a pending request happy-dom has to abort on teardown; $ and esc stay real.
vi.mock("../src/core", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/core")>()),
  api: vi.fn(() => Promise.resolve({})),
}));

import { api } from "../src/core";
import { resetGatewayState } from "../src/gateway";
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
  gateway: { authenticated: true, connected: true },
  ...over,
});

beforeEach(() => {
  apiMock.mockReset();
  apiMock.mockResolvedValue({});
  resetGatewayState();
  invalidateFlowData();
});

describe("stageForView", () => {
  it("maps the three execution stages without treating model planning as execution", () => {
    expect(stageForView("orders")).toBe(0);
    expect(stageForView("rebalance")).toBe(1);
    expect(stageForView("exit")).toBe(1);
    expect(stageForView("target-state")).toBe(2);
    expect(stageForView("trade")).toBe(3);
  });
});

describe("updateFlowBar visibility", () => {
  it("is execution navigation, so it hides after leaving the Orders group", () => {
    document.body.innerHTML = '<nav id="flowbar" hidden></nav>';
    const host = document.getElementById("flowbar") as HTMLElement;
    updateFlowBar("rebalance", "rebalance");
    expect(host.hidden).toBe(false);
    updateFlowBar("holdings", "portfolio");
    expect(host.hidden).toBe(true);
  });
});

describe("working-order polling gate", () => {
  it("does not request working orders while the gateway is disconnected", async () => {
    apiMock.mockImplementation((path: string) => {
      if (path === "/api/overview") return Promise.resolve({});
      if (path === "/api/trade/status") {
        return Promise.resolve({
          trading_enabled: false, authenticated: false, connected: false,
        });
      }
      return Promise.reject(new Error("orders should not be requested"));
    });
    document.body.innerHTML = '<nav id="flowbar" hidden></nav>';
    invalidateFlowData();
    updateFlowBar("rebalance", "rebalance");
    await vi.waitFor(() => expect(apiMock).toHaveBeenCalledWith(
      "/api/trade/status", "GET", null,
      { timeoutMs: 15_000, reportError: false },
    ));
    expect(apiMock).not.toHaveBeenCalledWith("/api/trade/orders");
  });

  it("loads working orders for an authenticated data session even when trading is disabled", async () => {
    apiMock.mockImplementation((path: string) => {
      if (path === "/api/overview") return Promise.resolve({});
      if (path === "/api/trade/status") {
        return Promise.resolve({
          trading_enabled: false, authenticated: true, connected: true,
        });
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
  it("labels Preview & place when IBKR data is unavailable", () => {
    const orders = flowStages(data({
      working: null,
      gateway: { authenticated: false, connected: false },
    }))[2];
    expect(orders.sub).toContain("IBKR offline");
  });

  it("summarises suggestions and the persistent order queue in Build orders", () => {
    const build = flowStages(data())[0];
    expect(build.label).toBe("Build orders");
    expect(build.sub).toContain("4 queued");
    expect(build.sub).toContain("3 suggested");
    expect(build.sub).toContain("1 gate triggered");
    expect(build.sub).not.toContain("drafted");
    expect(build.tone).toBe("warn");
  });

  it("Review impact reads bands-in and flags off-target cash; all-in reads ok", () => {
    const review = flowStages(data())[1];
    expect(review.sub).toBe("13/16 bands in");
    const done = flowStages(data({
      ov: { snapshot: { exists: true, positions: 1, age_days: 0 },
            plan: { rows: 5, out_of_band: 0, actionable: 0, cash: { status: "IN" } },
            draft: { pending: 0 }, staged_basket: { count: 1 } },
      working: 0,
    }))[1];
    expect(done.tone).toBe("ok");
    const cashOff = flowStages(data({
      ov: { ...data().ov, plan: { rows: 5, out_of_band: 0, actionable: 0, cash: { status: "BELOW" } } },
    }))[1];
    expect(cashOff.sub).toContain("cash off target");
  });

  it("routes Review impact to the projected-portfolio view", () => {
    expect(flowStages(data())[1].view).toBe("target-state");
  });

  it("shows Current book as an input and routes only missing holdings to setup", () => {
    const html = flowBarHtml({ ov: { snapshot: { exists: false } }, working: null }, 1);
    expect(html).toContain('data-flow-view="setup"');
    expect(html).toContain("Current book");
    expect(html).toContain("connect holdings");
  });

  it("opens fresh positions in-place instead of navigating away", () => {
    const html = flowBarHtml(data(), 1);
    expect(html).toContain("data-flow-book");
    expect(html).toContain("View positions ↗");
    expect(html).not.toContain('data-flow-view="holdings"');
    expect(html).toContain("42 positions");
    expect(html).toContain("synced 2d ago");
  });

  it("offers an in-place refresh when the current book is stale", () => {
    const stale = data({
      ov: { ...data().ov, snapshot: { exists: true, positions: 42, age_days: 8, stale: true } },
    });
    const html = flowBarHtml(stale, 1);
    expect(html).toContain("data-flow-refresh");
    expect(html).toContain("Refresh holdings");
    expect(html).not.toContain("data-flow-book");
  });
});

describe("flowBarHtml", () => {
  it("shows the execution path without highlighting a stage on the Orders index", () => {
    const html = flowBarHtml(data(), 0);
    expect((html.match(/ active"/g) || []).length).toBe(0);
  });

  it("highlights the active stage and wires click targets", () => {
    const html = flowBarHtml(data(), 2);
    expect(html).toContain('data-flow-view="trade"');
    expect((html.match(/flow-stage/g) || []).length).toBeGreaterThanOrEqual(3);
    expect(html).toContain("active");
    // Exactly one active stage.
    expect((html.match(/ active"/g) || []).length).toBe(1);
  });
});
