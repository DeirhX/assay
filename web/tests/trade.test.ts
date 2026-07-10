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
  document.querySelector<HTMLButtonElement>('[data-trade-tab="review"]')!.click();
  await flush();
}

beforeEach(() => {
  apiMock.mockReset();
  state.stagedBasket = [];
  window.history.replaceState({}, "", "/?view=trade");
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

    // Marking one order ready still leaves placement blocked by the other.
    const ready = [...document.querySelectorAll<HTMLButtonElement>('#trade-result .trade-order-ready')];
    expect(ready).toHaveLength(2);
    ready[0].click();
    expect(place.disabled).toBe(true);

    byText((t) => t === "Mark all ready")!.click();
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

    // No bulk-ready shortcut on live; marking the order cannot bypass the lock.
    expect(byText((t) => t === "Mark all ready")).toBeFalsy();
    document.querySelector<HTMLButtonElement>('#trade-result .trade-order-ready')!.click();
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
    byText((t) => t === "Mark all ready")!.click();
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
    // No bulk-ready shortcut on a live account; mark the single order directly.
    expect(byText((t) => t === "Mark all ready")).toBeFalsy();
    document.querySelector<HTMLButtonElement>('#trade-result .trade-order-ready')!.click();

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
  it("shows basket, order review, and working orders as exclusive tabs", async () => {
    await previewWith(PAPER_STATUS, {
      is_paper: true, live_allowed: true, account: "DU1", warnings: [],
      ibkr_preview: null, orders: [order()],
    });
    const reviewTab = document.querySelector<HTMLButtonElement>('[data-trade-tab="review"]')!;
    const basketPanel = document.querySelector<HTMLElement>('[data-trade-panel="basket"]')!;
    const reviewPanel = document.querySelector<HTMLElement>('[data-trade-panel="review"]')!;
    const ordersPanel = document.querySelector<HTMLElement>('[data-trade-panel="orders"]')!;
    expect(reviewTab.disabled).toBe(false);
    expect(reviewTab.getAttribute("aria-selected")).toBe("true");
    expect(reviewPanel.hidden).toBe(false);
    expect(basketPanel.hidden).toBe(true);
    expect(ordersPanel.hidden).toBe(true);

    document.querySelector<HTMLButtonElement>('[data-trade-tab="orders"]')!.click();
    expect(ordersPanel.hidden).toBe(false);
    expect(reviewPanel.hidden).toBe(true);
    expect(new URLSearchParams(window.location.search).get("tab")).toBe("orders");
  });

  it("restores the active workspace tab from the URL on refresh", async () => {
    window.history.replaceState({}, "", "/?view=trade&tab=orders");
    await loadWith(PAPER_STATUS, []);
    expect(document.querySelector<HTMLButtonElement>('[data-trade-tab="orders"]')!
      .getAttribute("aria-selected")).toBe("true");
    expect(document.querySelector<HTMLElement>('[data-trade-panel="orders"]')!.hidden).toBe(false);
  });

  it("opens a deep-linked review tab immediately while its data is loading", async () => {
    window.history.replaceState({}, "", "/?view=trade&tab=review");
    let resolveBasket!: (value: object) => void;
    const basket = new Promise<object>((resolve) => { resolveBasket = resolve; });
    apiMock.mockImplementation((path: string) => {
      if (path === "/api/trade/basket") return basket;
      if (path === "/api/trade/status") return Promise.resolve(PAPER_STATUS);
      if (path === "/api/trade/preview") return Promise.resolve({
        is_paper: true, live_allowed: true, account: "DU1", warnings: [],
        ibkr_preview: null, orders: [order()],
      });
      return Promise.resolve({ orders: [] });
    });

    const loading = loadTrade();
    await flush();
    expect(document.querySelector<HTMLButtonElement>('[data-trade-tab="review"]')!
      .getAttribute("aria-selected")).toBe("true");
    expect(document.querySelector(".trade-review-loading")!.textContent).toContain("Preparing order review");

    resolveBasket({ trades: [{ symbol: "AAPL", delta_czk: 1000 }] });
    await loading;
    await flush();
    expect(document.querySelector(".trade-preview-card")).toBeTruthy();
  });

  it("renders reconciled working-order next steps and confirms only residual orders", async () => {
    const basket = [{ symbol: "NVDA", delta_czk: -1000 }, { symbol: "AAPL", delta_czk: 500 }];
    apiMock.mockImplementation((path: string) => {
      if (path === "/api/trade/status") return Promise.resolve(PAPER_STATUS);
      if (path === "/api/trade/basket") return Promise.resolve({ trades: basket });
      if (path === "/api/trade/orders")
        return Promise.resolve({ orders: [{ orderId: "o-1", ticker: "NVDA", side: "SELL",
          orderType: "LMT", price: 180, tif: "GTC", status: "Submitted" }] });
      if (path === "/api/trade/preview")
        return Promise.resolve({ is_paper: true, live_allowed: true, account: "DU1", warnings: [],
          working_orders_available: true, ibkr_preview: null, orders: [order({ symbol: "AAPL" })],
          order_context: [
            { symbol: "NVDA", side: "SELL", classification: "fully_covered",
              proposed_qty: 3, working_same_qty: 3, residual_qty: 0, placeable: false,
              current_position_qty: 10, projected_position_qty: 7,
              working: [{ order_id: "o-1", side: "SELL", remaining_qty: 3,
                order_type: "LMT", price: 180, status: "Submitted" }],
              next_step: "No new order needed — monitor the existing working order." },
            { symbol: "AAPL", side: "BUY", classification: "none",
              proposed_qty: 3, working_same_qty: 0, residual_qty: 3, placeable: true,
              next_step: "Review and confirm this new order." },
          ] });
      return Promise.resolve({ orders: [] });
    });
    state.stagedBasket = basket;
    await loadTrade();
    await flush();
    document.querySelector<HTMLButtonElement>('[data-trade-tab="review"]')!.click();
    await flush();

    const cards = [...document.querySelectorAll(".trade-order-item")];
    expect(cards).toHaveLength(2);
    const nvda = cards.find((n) => n.textContent?.includes("NVDA"))!;
    expect(nvda.textContent).toContain("Already covered");
    expect(nvda.textContent).toContain("Informational");
    expect(nvda.textContent).toContain("10 shares → 7 shares");
    expect(nvda.querySelector(".trade-order-ready")).toBeFalsy();
    expect(document.querySelectorAll(".trade-order-grid .trade-order-ready")).toHaveLength(1);
    expect(document.querySelector(".trade-preview-summary")!.textContent).toContain("Working adjustments");
  });

  it("blocks placement when live working orders could not be read", async () => {
    await previewWith(PAPER_STATUS, {
      is_paper: true, live_allowed: true, account: "DU1", warnings: [],
      working_orders_available: false, working_orders_error: "no bridge",
      ibkr_preview: null, orders: [order()],
    });
    expect(document.querySelector(".trade-action-item.blocker")!.textContent).toContain("Safety check unavailable");
    expect(byText((t) => t.includes("Working orders unavailable"))!.disabled).toBe(true);
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
    byText((t) => t === "Mark all ready")!.click();
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

// Quotes now arrive from a separate async endpoint. Tests that care about the
// market cell put the quote on the order under `quote` AND pass `quotes` here,
// keyed by conid, so the hydration path fills the same values it would live.
async function loadWith(status: object, orders: object[], quotes: Record<string, object> = {}) {
  apiMock.mockImplementation((path: string) => {
    if (path === "/api/trade/status") return Promise.resolve(status);
    if (path === "/api/trade/orders") return Promise.resolve({ orders });
    if (path.startsWith("/api/trade/quotes")) return Promise.resolve({ quotes });
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
    expect(apiMock).toHaveBeenCalledWith("/api/trade/orders", "GET", null, { timeoutMs: 20_000 });
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
    const cancel = document.querySelector<HTMLButtonElement>('.trade-live-card [aria-label="Cancel order"]');
    expect(cancel).toBeTruthy();
    cancel!.click();
    await flush();
    expect(apiMock).toHaveBeenCalledWith(
      "/api/trade/cancel", "POST",
      expect.objectContaining({ order_id: "o-1", account: "DU1" }),
    );
  });

  it("keeps filled/cancelled orders out of the working list, in a muted summary", async () => {
    await loadWith(PAPER_STATUS, [
      { orderId: "o-1", ticker: "AMD", side: "SELL", orderType: "LMT", price: 100, status: "Submitted" },
      { orderId: "o-2", ticker: "GEV", side: "BUY", status: "Filled" },
      { orderId: "o-3", ticker: "AXON", side: "SELL", status: "Cancelled" },
    ]);
    const card = document.querySelector(".trade-live-card")!;
    expect(card.textContent).toContain("Working orders (1)");            // only the live one counts
    expect(card.querySelectorAll(".trade-live-row").length).toBe(1);
    const done = card.querySelector(".trade-live-done")!;
    expect(done).toBeTruthy();
    expect(done.textContent).toContain("2 recently filled/cancelled");
    expect(done.textContent).toContain("GEV");
    expect(done.textContent).toContain("AXON");
  });

  it("shows order status as a colour dot (state on hover) plus the order's age", async () => {
    const twoDaysAgo = Date.now() - 2 * 86400 * 1000;
    await loadWith(PAPER_STATUS, [
      { orderId: "o-1", ticker: "AMD", side: "SELL", orderType: "LMT", price: 100,
        status: "PreSubmitted", lastExecutionTime_r: twoDaysAgo },
    ]);
    const cell = document.querySelector(".trade-live-status")!;
    const dot = cell.querySelector(".trade-live-dot")!;
    expect(dot).toBeTruthy();
    expect(dot.classList.contains("tone-held")).toBe(true);       // PreSubmitted -> held/blue
    expect(dot.getAttribute("title")).toBe("PreSubmitted");        // exact state on hover
    expect(cell.textContent).not.toContain("PreSubmitted");        // not spelled out as prose
    expect(cell.querySelector(".trade-live-age")!.textContent).toBe("2d");
  });

  it("shows bid × ask, spread, last, and the limit-vs-touch gap as a meter", async () => {
    await loadWith(PAPER_STATUS, [
      { orderId: "o-1", ticker: "AMD", side: "SELL", orderType: "LMT", price: 102, status: "Submitted",
        quote: { bid: 100, ask: 100.5, last: 100.2 } },
    ]);
    const mkt = document.querySelector(".trade-live-row")!;
    expect(mkt.textContent).toContain("100.00");   // bid
    expect(mkt.textContent).toContain("100.50");   // ask
    expect(mkt.querySelector(".trade-live-spread")!.textContent).toContain("0.50");
    expect(mkt.querySelector(".trade-live-last")!.textContent).toContain("100.20");
    // sell limit 102 sits 2% above the 100 bid — direction in the tooltip, figure beside the bar
    expect(mkt.querySelector(".edge-num")!.textContent).toBe("2.0%");
    expect(mkt.querySelector(".trade-live-edge")!.getAttribute("title")).toContain("above bid");
  });

  it("draws the limit-vs-touch gap as a log-scaled, colour-graded meter", async () => {
    await loadWith(PAPER_STATUS, [
      { orderId: "o", ticker: "AMD", side: "SELL", orderType: "LMT", price: 103, status: "Submitted",
        quote: { bid: 100, ask: 100.5, last: 100.2 } },   // 3% above bid
    ]);
    const fill = document.querySelector<HTMLElement>(".edge-fill")!;
    // log-scaled: round(log1p(3)/log1p(120) * 100) = 29%, not the old linear 60%
    expect(fill.style.width).toBe("29%");
    // colour is set inline (green->red by distance), not a discrete tone class
    expect(fill.getAttribute("style")).toContain("hsl(");
    expect(document.querySelector(".edge-num")!.textContent).toBe("3.0%");
  });

  it("gives a far order a wider, redder edge bar than a near one (not monotonous)", async () => {
    await loadWith(PAPER_STATUS, [
      { orderId: "near", conid: 1, ticker: "AMD", side: "SELL", orderType: "LMT", price: 102, status: "Submitted",
        quote: { bid: 100, ask: 100.5, last: 100.2 } },    // 2% above bid
      { orderId: "far", conid: 2, ticker: "MSFT", side: "SELL", orderType: "LMT", price: 140, status: "Submitted",
        quote: { bid: 100, ask: 100.5, last: 100.2 } },    // 40% above bid
    ]);
    const rows = [...document.querySelectorAll(".trade-live-row")];
    const near = rows.find((r) => (r.textContent || "").includes("AMD"))!.querySelector<HTMLElement>(".edge-fill")!;
    const far = rows.find((r) => (r.textContent || "").includes("MSFT"))!.querySelector<HTMLElement>(".edge-fill")!;
    const hue = (e: HTMLElement) => Number(/hsl\((\d+)/.exec(e.getAttribute("style") || "")![1]);
    expect(parseFloat(far.style.width)).toBeGreaterThan(parseFloat(near.style.width));
    expect(hue(far)).toBeLessThan(hue(near));   // lower hue = redder = further
  });

  it("paints the list first, then hydrates the market cell from the async quotes endpoint", async () => {
    await loadWith(
      PAPER_STATUS,
      [{ orderId: "o-1", conid: 265598, ticker: "AMD", side: "SELL", orderType: "LMT",
        price: 102, status: "Submitted" }],  // no inline quote -> must come from /api/trade/quotes
      { "265598": { bid: 100, ask: 100.5, last: 100.2 } },
    );
    // The quotes call is keyed by the working order's conid.
    expect(apiMock).toHaveBeenCalledWith(
      expect.stringContaining("/api/trade/quotes?conids=265598"), "GET", null, { timeoutMs: 15_000 });
    const mkt = document.querySelector(".trade-live-row")!;
    expect(mkt.textContent).not.toContain("quote\u2026");  // placeholder resolved
    expect(mkt.textContent).toContain("100.00");            // bid, hydrated in
    expect(mkt.querySelector(".trade-live-last")!.textContent).toContain("100.20");
    expect(mkt.querySelector(".edge-num")!.textContent).toBe("2.0%");
  });

  it("shows 'no quote' (not a stuck spinner) once hydration returns nothing", async () => {
    await loadWith(
      PAPER_STATUS,
      [{ orderId: "o-1", conid: 999, ticker: "AMD", side: "SELL", orderType: "LMT",
        price: 102, status: "Submitted" }],
      {},  // cold feed: the endpoint returns no quote for this conid
    );
    const mkt = document.querySelector(".trade-live-row")!;
    expect(mkt.textContent).not.toContain("quote\u2026");
    expect(mkt.textContent).toContain("no quote");
  });

  it("shows a sell's average purchase price and the limit's gain vs cost", async () => {
    await loadWith(
      PAPER_STATUS,
      [{ orderId: "o-1", conid: 5, ticker: "AMD", side: "SELL", orderType: "LMT",
        price: 102, status: "Submitted", avg_cost: 95 }],
      { "5": { bid: 100, ask: 100.5, last: 100.2 } },
    );
    const cost = document.querySelector(".trade-live-cost")!;
    expect(cost).toBeTruthy();
    expect(cost.textContent).toContain("95.00");
    // sell limit 102 vs cost 95 => (102-95)/95 = +7.4%, in the gain colour
    expect(cost.querySelector(".gain")!.textContent).toContain("7.4%");
    expect(cost.querySelector(".loss")).toBeFalsy();
  });

  it("colours a below-cost sell as a loss, and shows nothing for a buy", async () => {
    await loadWith(
      PAPER_STATUS,
      [{ orderId: "s", conid: 5, ticker: "AMD", side: "SELL", orderType: "LMT",
         price: 90, status: "Submitted", avg_cost: 95 },
       { orderId: "b", conid: 6, ticker: "MSFT", side: "BUY", orderType: "LMT",
         price: 90, status: "Submitted", avg_cost: 95 }],
      {},
    );
    const rows = [...document.querySelectorAll(".trade-live-row")];
    const sell = rows.find((r) => (r.textContent || "").includes("AMD"))!;
    const buy = rows.find((r) => (r.textContent || "").includes("MSFT"))!;
    // sell 90 vs cost 95 => -5.3%, loss colour
    expect(sell.querySelector(".trade-live-cost .loss")!.textContent).toContain("5.3%");
    // a buy never gets a cost read even when the name is held
    expect(buy.querySelector(".trade-live-cost")).toBeFalsy();
  });

  it("renders working-order tickers (and the done summary) as deep-dive links", async () => {
    await loadWith(PAPER_STATUS, [
      { orderId: "o-1", conid: 5, ticker: "AMD", side: "SELL", orderType: "LMT", price: 100, status: "Submitted" },
      { orderId: "o-2", ticker: "GEV", side: "BUY", status: "Filled" },   // done -> summary
    ]);
    const sym = document.querySelector<HTMLAnchorElement>(".trade-live-sym a.tlink")!;
    expect(sym).toBeTruthy();
    expect(sym.dataset.ticker).toBe("AMD");
    expect(sym.getAttribute("href")).toContain("ticker=AMD");
    const done = document.querySelector<HTMLAnchorElement>(".trade-live-done a.tlink")!;
    expect(done.dataset.ticker).toBe("GEV");
  });

  it("sorts the working list by age and by distance-from-last, clearing on the third click", async () => {
    const now = Date.now();
    const q = { bid: 99, ask: 101, last: 100 };
    await loadWith(PAPER_STATUS, [
      { orderId: "a", ticker: "AAA", side: "SELL", orderType: "LMT", price: 100, status: "Submitted",
        lastExecutionTime_r: now - 1 * 86400000, quote: q },   // age 1d, 0% from last
      { orderId: "b", ticker: "BBB", side: "SELL", orderType: "LMT", price: 110, status: "Submitted",
        lastExecutionTime_r: now - 5 * 86400000, quote: q },   // age 5d, 10% from last
      { orderId: "c", ticker: "CCC", side: "SELL", orderType: "LMT", price: 105, status: "Submitted",
        lastExecutionTime_r: now - 3 * 86400000, quote: q },   // age 3d, 5% from last
    ]);
    const syms = () => [...document.querySelectorAll(".trade-live-row .trade-live-sym")]
      .map((e) => (e.textContent || "").trim().slice(0, 3));
    const click = (k: string) => document.querySelector<HTMLButtonElement>(`[data-osort="${k}"]`)!.click();

    expect(syms()).toEqual(["AAA", "BBB", "CCC"]);   // IBKR order
    click("age");                                     // desc: oldest first
    expect(syms()).toEqual(["BBB", "CCC", "AAA"]);
    click("age");                                     // asc: newest first
    expect(syms()).toEqual(["AAA", "CCC", "BBB"]);
    click("age");                                     // off: back to IBKR order
    expect(syms()).toEqual(["AAA", "BBB", "CCC"]);
    click("lastdist");                                // desc: farthest from last first
    expect(syms()).toEqual(["BBB", "CCC", "AAA"]);
  });
});

describe("trade desk order pegging", () => {
  const LIMIT = {
    orderId: "o-9", ticker: "NVDA", side: "SELL", remainingQuantity: 10,
    orderType: "LMT", price: 180, tif: "GTC", status: "Submitted",
  };

  it("offers Keep at top on a limit order and arms a peg against the account", async () => {
    await loadWith(PAPER_STATUS, [LIMIT]);
    const keep = document.querySelector<HTMLButtonElement>('.trade-live-card [aria-label="Keep at top"]');
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
    const keep = document.querySelector<HTMLButtonElement>('.trade-live-card [aria-label="Keep at top"]');
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
    const stop = card.querySelector<HTMLButtonElement>('[aria-label="Stop keeping at top"]');
    expect(stop).toBeTruthy();
    stop!.click();
    await flush();
    expect(apiMock).toHaveBeenCalledWith(
      "/api/trade/peg/stop", "POST", expect.objectContaining({ order_id: "o-9" }),
    );
  });
});

describe("trade desk staged basket", () => {
  it("renders side, coloured amounts, a diverging size bar, trend slots and totals", async () => {
    const basket = [
      { symbol: "NVDA", delta_czk: 1000 },   // largest -> full half-bar
      { symbol: "ARM", delta_czk: -500 },
    ];
    apiMock.mockImplementation((path: string) => {
      if (path === "/api/trade/status") return Promise.resolve(PAPER_STATUS);
      if (path === "/api/trade/basket") return Promise.resolve({ trades: basket });
      if (path.startsWith("/api/spark")) return Promise.resolve({ spark: {} });
      return Promise.resolve({ orders: [] });
    });
    await loadTrade();
    await flush();

    const table = document.querySelector(".trade-basket-table")!;
    const rows = [...table.querySelectorAll("tbody tr")];
    expect(rows).toHaveLength(2);
    // side tags, colour-coded
    expect(rows[0].querySelector(".trade-side.buy")).toBeTruthy();
    expect(rows[1].querySelector(".trade-side.sell")).toBeTruthy();
    // coloured, signed amount cells
    expect(rows[0].querySelector("td.tb-buy")!.textContent).toContain("+");
    expect(rows[1].querySelector("td.tb-sell")!.textContent).toContain("\u2212");
    // diverging magnitude bar: buy grows right, sell left, scaled to the largest
    const buy = rows[0].querySelector<HTMLElement>(".basket-bar-fill.buy")!;
    const sell = rows[1].querySelector<HTMLElement>(".basket-bar-fill.sell")!;
    expect(buy.style.left).toBe("50%");
    expect(buy.style.width).toBe("50.0%");
    expect(sell.style.right).toBe("50%");
    expect(sell.style.width).toBe("25.0%");   // 500/1000 * 50
    // trend sparkline slots for the async batch hydrate
    expect(table.querySelectorAll(".tb-trend .spark-slot").length).toBe(2);
    // totals footer
    const foot = table.querySelector("tfoot")!;
    expect(foot.textContent).toContain("1 buy");
    expect(foot.textContent).toContain("1 sell");
    expect(foot.textContent).toContain("net");
    expect(foot.textContent).toContain("gross");
  });
});

describe("trade desk connection banner", () => {
  it("blocks preview and explains when trading is disabled", async () => {
    apiMock.mockResolvedValue({ trading_enabled: false, authenticated: false, accounts: [] });
    state.stagedBasket = [{ symbol: "AAPL", delta_czk: 1000 }];
    await loadTrade();
    await flush();

    expect(document.querySelector("#trade-banner")!.textContent).toContain("Trading is disabled");
    const preview = document.querySelector<HTMLButtonElement>('[data-trade-tab="review"]');
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
