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
import { loadTrade, placeResultHtml } from "../src/trade";

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

describe("placeResultHtml (post-placement loop close)", () => {
  const res = {
    kind: "paper",
    account: "DU12345",
    staged_basket_cleared: true,
    placed: [{ order_id: "1" }, { orderId: "2" }, { note: "no id" }],
  };

  it("counts acknowledged orders and names the account", () => {
    const html = placeResultHtml(res);
    expect(html).toContain("2 order(s) acknowledged");
    expect(html).toContain("DU12345");
    expect(html).toContain("trade-bnr paper");
  });

  it("offers the loop-closing next steps and the cleared-basket notice", () => {
    const html = placeResultHtml(res);
    expect(html).toContain('data-trade-next="resync"');
    expect(html).toContain('data-trade-next="journal"');
    expect(html).toContain("cleared so it can't be placed twice");
  });

  it("collapses the raw response instead of dumping JSON", () => {
    const html = placeResultHtml(res);
    expect(html).toContain("<details");
    expect(html).toContain("Raw IBKR response");
  });

  it("warns when nothing was acknowledged; no cleared note when the basket was kept", () => {
    const html = placeResultHtml({ kind: "paper", account: "DU1", placed: [{}] });
    expect(html).toContain("0 order(s) acknowledged");
    expect(html).toContain("trade-bnr warn");
    expect(html).not.toContain("placed twice");
  });
});

async function loadWith(status: object, orders: object[]) {
  apiMock.mockImplementation((path: string) => {
    if (path === "/api/trade/status") return Promise.resolve(status);
    if (path === "/api/trade/orders") return Promise.resolve({ orders });
    if (path === "/api/trade/cancel") return Promise.resolve({ ok: true });
    return Promise.resolve({ trades: [] });  // /api/trade/basket
  });
  await loadTrade();
  await flush();
}

describe("trade desk working orders", () => {
  it("fetches and lists working orders on load when the gateway is connected", async () => {
    await loadWith(PAPER_STATUS, [
      { orderId: "o-9", ticker: "NVDA", side: "SELL", remainingQuantity: 10,
        orderType: "LMT", price: 180, tif: "GTC", status: "Submitted" },
    ]);
    const card = document.querySelector(".trade-live-card");
    expect(card).toBeTruthy();
    expect(card!.textContent).toContain("Working orders (1)");
    expect(card!.textContent).toContain("NVDA");
    expect(card!.textContent).toContain("GTC");
    expect(apiMock).toHaveBeenCalledWith("/api/trade/orders");
  });

  it("shows an empty state when connected with no working orders", async () => {
    await loadWith(PAPER_STATUS, []);
    const card = document.querySelector(".trade-live-card");
    expect(card!.textContent).toContain("Working orders (0)");
    expect(card!.textContent).toContain("No working orders");
  });

  it("does not query orders and prompts to connect when the gateway is offline", async () => {
    await loadWith({ trading_enabled: true, authenticated: false, accounts: [] }, []);
    const card = document.querySelector(".trade-live-card");
    expect(card).toBeTruthy();
    expect(card!.textContent).toContain("Connect the IBKR Client Portal Gateway");
    expect(apiMock).not.toHaveBeenCalledWith("/api/trade/orders");
  });

  it("cancels a working order against the connected account", async () => {
    await loadWith(PAPER_STATUS, [{ orderId: "o-1", ticker: "AMD", side: "SELL", status: "Submitted" }]);
    const cancel = [...document.querySelectorAll<HTMLButtonElement>(".trade-live-card button")]
      .find((b) => (b.textContent || "") === "Cancel");
    expect(cancel).toBeTruthy();
    cancel!.click();
    await flush();
    expect(apiMock).toHaveBeenCalledWith(
      "/api/trade/cancel", "POST",
      expect.objectContaining({ order_id: "o-1", account: "DU1" }),
    );
  });
});

describe("trade desk order pegging", () => {
  const LIMIT = {
    orderId: "o-9", ticker: "NVDA", side: "SELL", remainingQuantity: 10,
    orderType: "LMT", price: 180, tif: "GTC", status: "Submitted",
  };

  it("offers Keep at top on a limit order and arms a peg against the account", async () => {
    await loadWith(PAPER_STATUS, [LIMIT]);
    const keep = [...document.querySelectorAll<HTMLButtonElement>(".trade-live-card button")]
      .find((b) => (b.textContent || "") === "Keep at top");
    expect(keep).toBeTruthy();
    keep!.click();
    await flush();
    expect(apiMock).toHaveBeenCalledWith(
      "/api/trade/peg", "POST",
      expect.objectContaining({ order_id: "o-9", account: "DU1" }),
    );
  });

  it("does not offer a peg on a non-limit order", async () => {
    await loadWith(PAPER_STATUS, [{ orderId: "o-3", ticker: "AMD", side: "SELL",
      orderType: "MKT", status: "Submitted" }]);
    const keep = [...document.querySelectorAll<HTMLButtonElement>(".trade-live-card button")]
      .find((b) => (b.textContent || "") === "Keep at top");
    expect(keep).toBeFalsy();
  });

  it("badges an active peg and offers Stop, which calls /api/trade/peg/stop", async () => {
    let active = true;
    apiMock.mockImplementation((path: string) => {
      if (path === "/api/trade/status") return Promise.resolve(PAPER_STATUS);
      if (path === "/api/trade/orders")
        return Promise.resolve({ orders: [LIMIT], pegs: active
          ? [{ order_id: "o-9", state: "running", reprices: 3, message: "3 reprice(s), resting @ 179.9" }] : [] });
      if (path === "/api/trade/peg/stop") { active = false; return Promise.resolve({ stopped: true }); }
      return Promise.resolve({ trades: [] });
    });
    await loadTrade();
    await flush();

    const card = document.querySelector(".trade-live-card")!;
    expect(card.querySelector(".trade-peg-badge")).toBeTruthy();
    const stop = [...card.querySelectorAll<HTMLButtonElement>("button")]
      .find((b) => (b.textContent || "") === "Stop peg");
    expect(stop).toBeTruthy();
    stop!.click();
    await flush();
    expect(apiMock).toHaveBeenCalledWith(
      "/api/trade/peg/stop", "POST", expect.objectContaining({ order_id: "o-9" }),
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
