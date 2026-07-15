// Tests for the risk lens helper and that the new tabs are routable views.
import { beforeEach, describe, expect, it } from "vitest";
import { corrColor, renderRisk, sortRiskPositions } from "../src/risk";
import { navFromUrl } from "../src/shell";

const setUrl = (search: string) => {
  window.history.replaceState({}, "", "/" + (search ? "?" + search : ""));
};

describe("corrColor", () => {
  it("is transparent for missing correlation", () => {
    expect(corrColor(null)).toBe("transparent");
    expect(corrColor(undefined)).toBe("transparent");
  });

  it("paints positive correlation red and scales with magnitude", () => {
    expect(corrColor(1)).toContain("220, 80, 70");
    // Stronger correlation -> higher alpha (louder).
    const lo = corrColor(0.2);
    const hi = corrColor(0.9);
    const alpha = (s: string) => Number(s.match(/, ([0-9.]+)\)$/)?.[1]);
    expect(alpha(hi)).toBeGreaterThan(alpha(lo));
  });

  it("paints a genuine hedge (negative correlation) green", () => {
    expect(corrColor(-1)).toContain("70, 170, 110");
  });
});

describe("per-name volatility sorting", () => {
  const positions = [
    { symbol: "AMD", weight_pct: 8, norm_weight_pct: 20, ann_vol_pct: 45 },
    { symbol: "PYPL", weight_pct: 4, norm_weight_pct: 10, ann_vol_pct: 30 },
    { symbol: "CASH", weight_pct: 1, norm_weight_pct: null, ann_vol_pct: null },
  ];

  it("sorts every field and keeps unavailable values last", () => {
    expect(sortRiskPositions(positions, { key: "symbol", dir: "asc" }).map((p) => p.symbol))
      .toEqual(["AMD", "CASH", "PYPL"]);
    expect(sortRiskPositions(positions, { key: "ann_vol_pct", dir: "desc" }).map((p) => p.symbol))
      .toEqual(["AMD", "PYPL", "CASH"]);
    expect(sortRiskPositions(positions, { key: "norm_weight_pct", dir: "asc" }).map((p) => p.symbol))
      .toEqual(["PYPL", "AMD", "CASH"]);
  });

  it("makes all four headers keyboard-operable sort controls", () => {
    document.body.innerHTML = '<div id="risk-result"></div>';
    renderRisk({ positions } as any);
    const symbols = () => [...document.querySelectorAll(".risk-pos-table tbody .risk-pos-sym")]
      .map((cell) => cell.textContent);
    const headers = [...document.querySelectorAll<HTMLElement>("[data-risk-sort]")];

    expect(headers).toHaveLength(4);
    expect(symbols()).toEqual(["AMD", "PYPL", "CASH"]); // default: weight descending

    const vol = document.querySelector<HTMLElement>('[data-risk-sort="ann_vol_pct"]')!;
    vol.click();
    expect(symbols()).toEqual(["AMD", "PYPL", "CASH"]);
    expect(document.querySelector('[data-risk-sort="ann_vol_pct"]')?.getAttribute("aria-sort"))
      .toBe("descending");

    const activeVol = document.querySelector<HTMLElement>('[data-risk-sort="ann_vol_pct"]')!;
    activeVol.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    expect(symbols()).toEqual(["PYPL", "AMD", "CASH"]);
    expect(document.activeElement?.getAttribute("data-risk-sort")).toBe("ann_vol_pct");

    headers.map((header) => header.dataset.riskSort).forEach((key) => {
      document.querySelector<HTMLElement>(`[data-risk-sort="${key}"]`)!.click();
      expect(document.querySelector(`[data-risk-sort="${key}"]`)?.getAttribute("aria-sort"))
        .not.toBe("none");
    });
  });
});

describe("new views are routable", () => {
  beforeEach(() => setUrl(""));

  it("accepts the risk view", () => {
    setUrl("view=risk");
    expect(navFromUrl().view).toBe("risk");
  });

  it("accepts the journal view", () => {
    setUrl("view=journal");
    expect(navFromUrl().view).toBe("journal");
  });
});
