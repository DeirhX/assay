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

// The confirmation modal mounts on document.body, not inside #trade-result.
const modalBtn = (text: string) =>
  [...document.querySelectorAll<HTMLButtonElement>(".trade-confirm-modal button")]
    .find((b) => (b.textContent || "") === text);

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
  // The confirmation modal mounts on body; drop any left by a prior test.
  document.querySelectorAll(".modal-overlay").forEach((n) => n.remove());
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

    // No "Confirm all" on live; tick the box directly — the live lock still wins.
    expect(byText((t) => t === "Confirm all")).toBeFalsy();
    const box = document.querySelector<HTMLInputElement>('#trade-result input[type="checkbox"]')!;
    box.checked = true;
    box.dispatchEvent(new Event("change"));
    expect(place.disabled).toBe(true); // confirming the order cannot override the live lock
  });

  it("does not hit /api/trade/place unless the confirmation modal is accepted", async () => {
    await previewWith(
      PAPER_STATUS,
      { is_paper: true, live_allowed: true, account: "DU1", token: "tok-1",
        trades: [{ symbol: "AAPL", delta_czk: 1000 }],
        warnings: [], ibkr_preview: null, orders: [order()] },
      { placed: [{ order_id: "o-1" }], kind: "paper", account: "DU1" },
    );
    byText((t) => t === "Confirm all")!.click();
    const place = byText((t) => t.startsWith("Place"))!;

    // Opening the desk Place button raises the modal; cancelling it must not place.
    place.click();
    await flush();
    let modal = document.querySelector(".trade-confirm-modal")!;
    expect(modal).toBeTruthy();
    modalBtn("Cancel")!.click();
    await flush();
    expect(apiMock).not.toHaveBeenCalledWith("/api/trade/place", expect.anything(), expect.anything());
    expect(document.querySelector(".trade-confirm-modal")).toBeFalsy();

    // Re-open and accept: now it places, echoing the token + account.
    place.click();
    await flush();
    modalBtn("Place orders")!.click();
    await flush();
    expect(apiMock).toHaveBeenCalledWith(
      "/api/trade/place", "POST",
      expect.objectContaining({ confirm: true, token: "tok-1", account: "DU1" }),
    );
  });
});

describe("trade desk live confirmation modal", () => {
  const LIVE_OK = {
    trading_enabled: true, authenticated: true, default_account: "U777",
    accounts: [{ id: "U777", kind: "live" }], live_allowed: true,
  };

  it("requires typing the account id (or PLACE) to arm a live placement", async () => {
    await previewWith(LIVE_OK, {
      is_paper: false, live_allowed: true, account: "U777", token: "tok-live",
      trades: [{ symbol: "AAPL", delta_czk: 1000 }],
      warnings: [], ibkr_preview: null, orders: [order()],
    });
    // No "Confirm all" on a live account; tick the single order directly.
    expect(byText((t) => t === "Confirm all")).toBeFalsy();
    const box = document.querySelector<HTMLInputElement>('#trade-result input[type="checkbox"]')!;
    box.checked = true;
    box.dispatchEvent(new Event("change"));

    const place = byText((t) => t.startsWith("Place"))!;
    expect(place.disabled).toBe(false);
    place.click();
    await flush();

    const confirm = modalBtn("Place LIVE orders")!;
    expect(confirm).toBeTruthy();
    expect(confirm.disabled).toBe(true); // armed only by typing the id/PLACE

    const input = document.querySelector<HTMLInputElement>(".trade-cf-input")!;
    input.value = "nope";
    input.dispatchEvent(new Event("input"));
    expect(confirm.disabled).toBe(true);

    input.value = "U777";
    input.dispatchEvent(new Event("input"));
    expect(confirm.disabled).toBe(false);

    confirm.click();
    await flush();
    expect(apiMock).toHaveBeenCalledWith(
      "/api/trade/place", "POST",
      expect.objectContaining({ confirm: true, token: "tok-live", account: "U777" }),
    );
  });
});

describe("trade desk safety gates", () => {
  it("flags an order that collides with an existing working order", async () => {
    const basket = [{ symbol: "NVDA", delta_czk: -1000 }, { symbol: "AAPL", delta_czk: 500 }];
    apiMock.mockImplementation((path: string) => {
      if (path === "/api/trade/status") return Promise.resolve(PAPER_STATUS);
      if (path === "/api/trade/basket") return Promise.resolve({ trades: basket });
      if (path === "/api/trade/orders")
        return Promise.resolve({ orders: [{ orderId: "o-1", ticker: "NVDA", side: "SELL",
          orderType: "LMT", price: 180, tif: "GTC", status: "Submitted" }] });
      if (path === "/api/trade/preview")
        return Promise.resolve({ is_paper: true, live_allowed: true, account: "DU1", warnings: [],
          ibkr_preview: null, orders: [order({ symbol: "NVDA", side: "SELL" }), order({ symbol: "AAPL" })] });
      return Promise.resolve({ orders: [] });
    });
    state.stagedBasket = basket;
    await loadTrade();
    await flush();
    byText((t) => t.includes("Preview"))!.click();
    await flush();

    const note = document.querySelector(".trade-collide-note");
    expect(note).toBeTruthy();
    expect(note!.textContent).toContain("NVDA");
    expect(note!.textContent).toContain("double-trade");
    // Only the colliding symbol is flagged, not every order.
    expect(document.querySelectorAll(".trade-collide-note")).toHaveLength(1);
  });

  it("renders an effect-on-band track per order and flags an out-of-band land", async () => {
    await previewWith(PAPER_STATUS, {
      is_paper: true, live_allowed: true, account: "DU1", warnings: [], ibkr_preview: null,
      orders: [order({ symbol: "AMD", side: "SELL" }), order({ symbol: "MSFT" })],
      order_bands: {
        AMD: { low: 5, high: 7, before_pct: 8.2, after_pct: 6.5, status_after: "IN" },
        MSFT: { low: 3, high: 4, before_pct: 2.0, after_pct: 2.4, status_after: "BELOW" },
      },
    });
    const rows = [...document.querySelectorAll("#trade-result .trade-band-row")];
    expect(rows).toHaveLength(2);
    // AMD lands back inside its band; MSFT is still below -> flagged out.
    const amd = rows.find((r) => (r.textContent || "").includes("inside 5–7%"));
    expect(amd).toBeTruthy();
    expect(amd!.textContent).toContain("8.2% → 6.5%");
    const out = document.querySelector("#trade-result .trade-band-row.out");
    expect(out).toBeTruthy();
    expect(out!.textContent).toContain("out of band");
  });

  it("renders the pre-trade risk delta and promotes threshold breaches to warnings", async () => {
    await previewWith(PAPER_STATUS, {
      is_paper: true, live_allowed: true, account: "DU1", warnings: [], ibkr_preview: null,
      orders: [order({ symbol: "NVDA" })],
      local_whatif: {
        risk: {
          top5_pct: { before: 58.0, after: 64.0, delta: 6.0 },
          top1_pct: { before: 30.0, after: 34.0, delta: 4.0 },
          effective_names: { before: 6.2, after: 5.4, delta: -0.8 },
          has_correlation: false,
          warnings: ["This basket raises top-5 concentration by 6.0pp (58% -> 64%)."],
        },
      },
    });
    const panel = document.querySelector("#trade-result .trade-risk");
    expect(panel).toBeTruthy();
    expect(panel!.textContent).toContain("58.0% → 64.0%");
    // A concentration rise is bad; a diversification (effective-names) fall is bad too.
    const bad = [...panel!.querySelectorAll(".trade-risk-delta.bad")];
    expect(bad.length).toBeGreaterThanOrEqual(2);
    // The server's threshold breach is echoed as a loud pre-flight warning.
    expect(panel!.querySelector(".trade-warn")!.textContent).toContain("top-5 concentration by 6.0pp");
  });

  it("locks Place behind the stale-snapshot gate until stale marks are accepted", async () => {
    await previewWith(PAPER_STATUS, {
      is_paper: true, live_allowed: true, account: "DU1", warnings: ["snapshot old"],
      snapshot_stale: true, snapshot_age_days: 30, ibkr_preview: null, orders: [order()],
    });
    byText((t) => t === "Confirm all")!.click();
    const place = byText((t) => t.startsWith("Place"))!;
    expect(place.disabled).toBe(true); // confirmed, but the stale gate still holds

    const gate = document.querySelector(".trade-stale-gate")!;
    expect(gate).toBeTruthy();
    expect(gate.textContent).toContain("30 day(s)");
    const ack = gate.querySelector<HTMLInputElement>('input[type="checkbox"]')!;
    ack.checked = true;
    ack.dispatchEvent(new Event("change"));
    expect(place.disabled).toBe(false); // stale marks explicitly accepted -> armed
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

  it("wraps the account id in the connected banner so privacy mode blurs it", async () => {
    apiMock.mockImplementation((path: string) =>
      Promise.resolve(path === "/api/trade/status" ? PAPER_STATUS : { orders: [] }));
    await loadTrade();
    await flush();

    const banner = document.querySelector("#trade-banner")!;
    expect(banner.textContent).toContain("Paper account DU1");
    expect(banner.querySelector("[data-sensitive]")!.textContent).toBe("DU1");
  });
});
