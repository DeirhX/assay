// Tests for the trade desk -- the ONLY surface that can place real orders, so the
// safety gating is the thing worth pinning down: the Place button stays disabled
// until every order is individually confirmed, a live account with live
// placement locked can never place, and nothing reaches /api/trade/place unless
// the human accepts the confirm() dialog.
//
// `api` from core is the single seam we stub; everything else (DOM helpers,
// shared state) runs for real against the index.html shell loaded in setup.ts.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { apiMock } = vi.hoisted(() => ({ apiMock: vi.fn() }));
vi.mock("../src/core", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/core")>();
  return { ...actual, api: apiMock };
});

import { state } from "../src/core";
import { loadTrade } from "../src/trade";

const flush = async () => {
  for (let i = 0; i < 6; i++) await Promise.resolve();
  await new Promise((r) => setTimeout(r, 0));
};

const buttons = () => [...document.querySelectorAll<HTMLButtonElement>("#trade-result button")];
const byText = (pred: (t: string) => boolean) =>
  buttons().find((b) => pred(b.textContent || ""));

const PAPER_STATUS = {
  trading_enabled: true, authenticated: true, default_account: "DU1",
  accounts: [{ id: "DU1", kind: "paper" }], live_allowed: false,
};

function order(over = {}) {
  return { symbol: "AAPL", side: "BUY", quantity: 3, orderType: "MKT", tif: "DAY", conid: 1, ...over };
}

async function previewWith(status: object, preview: object, place: object = {}) {
  apiMock.mockImplementation((path: string) => {
    if (path === "/api/trade/status") return Promise.resolve(status);
    if (path === "/api/trade/preview") return Promise.resolve(preview);
    if (path === "/api/trade/place") return Promise.resolve(place);
    return Promise.resolve({ orders: [] });
  });
  state.stagedBasket = [{ symbol: "AAPL", delta_czk: 1000 }];
  await loadTrade();
  await flush();
  byText((t) => t.includes("Preview"))!.click();
  await flush();
}

beforeEach(() => {
  apiMock.mockReset();
  state.stagedBasket = [];
  const wrap = document.querySelector("#trade-result");
  if (wrap) wrap.innerHTML = "";
});

afterEach(() => vi.restoreAllMocks());

describe("trade desk placement gating", () => {
  it("keeps Place disabled until every order is confirmed, then unlocks on paper", async () => {
    await previewWith(PAPER_STATUS, {
      is_paper: true, live_allowed: true, account: "DU1", warnings: [], ibkr_preview: null,
      orders: [order({ conid: 1 }), order({ symbol: "MSFT", side: "SELL", conid: 2 })],
    });

    const place = byText((t) => t.startsWith("Place"))!;
    expect(place).toBeTruthy();
    expect(place.disabled).toBe(true); // nothing ticked yet

    // Tick a single box: still blocked while the other order is unconfirmed.
    const boxes = [...document.querySelectorAll<HTMLInputElement>('#trade-result input[type="checkbox"]')];
    expect(boxes).toHaveLength(2);
    boxes[0].checked = true;
    boxes[0].dispatchEvent(new Event("change"));
    expect(place.disabled).toBe(true);

    byText((t) => t === "Confirm all")!.click();
    expect(place.disabled).toBe(false); // all confirmed -> placement unlocked
  });

  it("never unlocks placement on a live account when live is not allowed", async () => {
    await previewWith(
      { ...PAPER_STATUS, default_account: "U1", accounts: [{ id: "U1", kind: "live" }] },
      { is_paper: false, live_allowed: false, account: "U1", warnings: [], ibkr_preview: null,
        orders: [order()] },
    );

    const place = byText((t) => t === "Live placement locked")!;
    expect(place).toBeTruthy();
    expect(place.disabled).toBe(true);

    byText((t) => t === "Confirm all")!.click();
    expect(place.disabled).toBe(true); // confirming every order cannot override the live lock
  });

  it("does not hit /api/trade/place unless the confirm() dialog is accepted", async () => {
    await previewWith(
      PAPER_STATUS,
      { is_paper: true, live_allowed: true, account: "DU1", token: "tok-1", trades: [order()],
        warnings: [], ibkr_preview: null, orders: [order()] },
      { placed: [{ order_id: "o-1" }], kind: "paper", account: "DU1" },
    );
    byText((t) => t === "Confirm all")!.click();
    const place = byText((t) => t.startsWith("Place"))!;

    // happy-dom has no native confirm(); install a controllable stub.
    const confirmFn = vi.fn().mockReturnValue(false);
    (window as unknown as { confirm: () => boolean }).confirm = confirmFn;
    place.click();
    await flush();
    expect(apiMock).not.toHaveBeenCalledWith("/api/trade/place", expect.anything(), expect.anything());

    confirmFn.mockReturnValue(true);
    place.click();
    await flush();
    expect(apiMock).toHaveBeenCalledWith(
      "/api/trade/place", "POST",
      expect.objectContaining({ confirm: true, token: "tok-1", account: "DU1" }),
    );
  });
});

describe("trade desk connection banner", () => {
  it("blocks preview and explains when trading is disabled", async () => {
    apiMock.mockResolvedValue({ trading_enabled: false, authenticated: false, accounts: [] });
    state.stagedBasket = [{ symbol: "AAPL", delta_czk: 1000 }];
    await loadTrade();
    await flush();

    expect(document.querySelector("#trade-banner")!.textContent).toContain("Trading is disabled");
    const preview = byText((t) => t.includes("Preview"));
    expect(preview!.disabled).toBe(true); // can't preview without an enabled, connected gateway
  });
});
