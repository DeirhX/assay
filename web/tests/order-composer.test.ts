import { describe, expect, it } from "vitest";
import { renderOrderComposer } from "../src/order-composer";

describe("ticker order composer", () => {
  it("defaults increases to a cash-secured put and keeps direct shares available", () => {
    const host = renderOrderComposer({
      symbol: "nvda",
      currentPrice: 123.45,
      currency: "USD",
      held: true,
    });
    const route = host.querySelector<HTMLSelectElement>("[data-order-route]")!;
    expect(route.value).toBe("cash_secured_put");
    expect(route.textContent).toContain("Shares");
    expect(host.textContent).toContain("Add NVDA shares or a covered option");
  });

  it("does not offer a reduction for an unheld ticker", () => {
    const host = renderOrderComposer({ symbol: "AMD", held: false });
    const reduce = host.querySelector<HTMLOptionElement>(
      '[data-order-direction] option[value="reduce"]',
    )!;
    expect(reduce.disabled).toBe(true);
  });
});
