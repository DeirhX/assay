import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../src/core", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/core")>()),
  api: vi.fn(),
}));

import { api } from "../src/core";
import {
  createOptionRouteControl,
  loadCompactOptionRoute,
  OptionRouteLoader,
} from "../src/option-route-control";
import type { RebalanceRouteResponse, RebalanceRouteSelection } from "../src/api-types";
import { rebalanceRoute } from "./fixtures/rebalance-route";

const apiMock = vi.mocked(api);

describe("createOptionRouteControl", () => {
  beforeEach(() => apiMock.mockReset());

  it("defaults to direct shares and loads card ladder on option click", async () => {
    apiMock.mockResolvedValue(rebalanceRoute("increase"));
    const selected = new Map<string, RebalanceRouteSelection>();
    const control = createOptionRouteControl("NVDA", 230_000, selected);
    document.body.innerHTML = "";
    document.body.append(control.controls, control.detail);

    expect(selected.get("NVDA")?.route).toBe("buy_shares");
    expect(control.compact.value).toBe("direct");
    const optionBtn = [...control.controls.querySelectorAll("button")]
      .find((node) => node.textContent === "Put option")!;
    optionBtn.click();
    await vi.waitFor(() => expect(control.detail.querySelector(".reb-option-contract")).toBeTruthy());

    const use = [...control.detail.querySelectorAll("button")]
      .find((node) => node.textContent === "Use contract")!;
    use.click();
    expect(selected.get("NVDA")).toMatchObject({
      route: "cash_secured_put",
      conid: 556,
      strike: 93,
      collateral_mode: "cash",
    });
    expect(control.compact.value).toBe("option");
  });

  it("cancels in-flight ladder loads when sync changes the amount", async () => {
    let resolveRoute!: (value: RebalanceRouteResponse) => void;
    const pending = new Promise<RebalanceRouteResponse>((resolve) => {
      resolveRoute = resolve;
    });
    apiMock.mockReturnValue(pending);
    const selected = new Map<string, RebalanceRouteSelection>();
    const control = createOptionRouteControl("NVDA", 230_000, selected);
    control.controls.querySelectorAll("button")[1].dispatchEvent(new MouseEvent("click"));
    control.sync(0);
    resolveRoute(rebalanceRoute("increase"));
    await pending;
    expect(control.detail.querySelector(".reb-option-contract")).toBeNull();
  });

  it("renders unavailable state with margin guidance for cash-secured puts", async () => {
    const blocked = rebalanceRoute("increase");
    blocked.option.eligible = false;
    blocked.option.reasons = [
      "One cash-secured put needs about 230,000 CZK; 100,000 CZK remains after held, working, and queued obligations.",
    ];
    apiMock.mockResolvedValue(blocked);
    const control = createOptionRouteControl("NVDA", 230_000, new Map());
    control.controls.querySelectorAll("button")[1].dispatchEvent(new MouseEvent("click"));
    await vi.waitFor(() => expect(control.detail.querySelector(".reb-route-unavailable")).toBeTruthy());
    expect(control.detail.textContent).toContain("margin-backed short put");
  });

  it("does not suggest IBKR margin when the failure is a missing mark", async () => {
    const blocked = rebalanceRoute("increase");
    blocked.option.eligible = false;
    blocked.option.collateral_mode = "cash";
    blocked.option.reasons = [
      "No usable underlying quote or holdings mark is available to size this option.",
    ];
    apiMock.mockResolvedValue(blocked);
    const control = createOptionRouteControl("ADI", 1_122_062, new Map());
    control.controls.querySelectorAll("button")[1].dispatchEvent(new MouseEvent("click"));
    await vi.waitFor(() => expect(control.detail.querySelector(".reb-route-unavailable")).toBeTruthy());
    expect(control.detail.textContent).toContain("underlying quote");
    expect(control.detail.textContent).not.toContain("margin-backed short put");
  });

  it("calls onExitNavigate from compact select and exit button", () => {
    const onExitNavigate = vi.fn();
    const control = createOptionRouteControl("NVDA", -100_000, new Map(), { onExitNavigate });
    control.compact.value = "exit";
    control.compact.dispatchEvent(new Event("change"));
    expect(onExitNavigate).toHaveBeenCalledWith("NVDA");
  });

  it("renders option reason hints as literal text, not HTML", async () => {
    const route = rebalanceRoute("increase");
    route.option.reasons = ['<img src=x onerror="alert(1)">', "thin book"];
    apiMock.mockResolvedValue(route);
    const control = createOptionRouteControl("NVDA", 230_000, new Map());
    control.controls.querySelectorAll("button")[1].dispatchEvent(new MouseEvent("click"));
    await vi.waitFor(() => expect(control.detail.querySelector(".hint")).toBeTruthy());
    const hint = control.detail.querySelector(".hint")!;
    expect(hint.textContent).toBe('<img src=x onerror="alert(1)"> · thin book');
    expect(hint.querySelector("img")).toBeNull();
    expect(hint.innerHTML).not.toContain("<img");
  });
});

describe("loadCompactOptionRoute", () => {
  beforeEach(() => apiMock.mockReset());

  it("picks the first stageable rung for the composer summary", async () => {
    apiMock.mockResolvedValue(rebalanceRoute("increase"));
    const loader = new OptionRouteLoader();
    const result = await loadCompactOptionRoute(loader, "NVDA", 100_000, "increase");
    expect(result?.eligible).toBe(true);
    expect(result?.selection).toMatchObject({ route: "cash_secured_put", conid: 556 });
    expect(result?.html).toContain("2026-08-07 · 93P");
  });

  it("returns null when a newer load supersedes the request", async () => {
    apiMock.mockResolvedValue(rebalanceRoute("increase"));
    const loader = new OptionRouteLoader();
    const first = loadCompactOptionRoute(loader, "NVDA", 100_000, "increase");
    loader.cancel();
    const second = await loadCompactOptionRoute(loader, "NVDA", 200_000, "increase");
    expect(await first).toBeNull();
    expect(second?.eligible).toBe(true);
  });
});
