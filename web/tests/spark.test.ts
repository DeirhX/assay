// Sparkline component + batch hydration. sparkSvg is pure; hydrateSparks makes
// exactly one /api/spark call for every slot under a root and degrades to empty
// slots (never throws) when a symbol has no cached series or the fetch fails.
import { beforeEach, describe, expect, it, vi } from "vitest";

const { apiMock } = vi.hoisted(() => ({ apiMock: vi.fn() }));
vi.mock("../src/core", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/core")>();
  return { ...actual, api: apiMock };
});

import { sparkSvg, sparkPlaceholder, hydrateSparks } from "../src/spark";

describe("sparkSvg", () => {
  it("returns nothing for fewer than two points (can't draw a line)", () => {
    expect(sparkSvg([])).toBe("");
    expect(sparkSvg([5])).toBe("");
  });

  it("draws a polyline inside a fixed 96x24 box", () => {
    const svg = sparkSvg([1, 2, 3]);
    expect(svg).toContain("<polyline");
    expect(svg).toContain('viewBox="0 0 96 24"');
  });

  it("tones up when last >= first, down otherwise", () => {
    expect(sparkSvg([1, 5])).toContain("spark-up");
    expect(sparkSvg([5, 1])).toContain("spark-down");
  });

  it("puts the signed 3M change in the title when provided", () => {
    expect(sparkSvg([1, 2], { change: 0.123 })).toContain("<title>3M +12.3%</title>");
    expect(sparkSvg([2, 1], { change: -0.05 })).toContain("\u22125.0%");
  });

  it("respects an explicit tone override", () => {
    expect(sparkSvg([5, 1], { tone: "flat" })).toContain("spark-flat");
  });
});

describe("hydrateSparks", () => {
  beforeEach(() => {
    apiMock.mockReset();
    document.body.innerHTML = "";
  });

  it("makes one batch call for all slots and fills them by symbol", async () => {
    document.body.innerHTML =
      sparkPlaceholder("NVDA") + sparkPlaceholder("AMD") + sparkPlaceholder("NVDA");
    apiMock.mockResolvedValue({
      spark: {
        NVDA: { points: [1, 2, 3], change: 0.1 },
        AMD: { points: [3, 2, 1], change: -0.1 },
      },
    });

    await hydrateSparks(document.body);

    expect(apiMock).toHaveBeenCalledTimes(1);
    const path = apiMock.mock.calls[0][0] as string;
    expect(path).toContain("/api/spark?symbols=");
    expect(path).toContain("NVDA");
    expect(path).toContain("AMD");

    const slots = document.querySelectorAll(".spark-slot");
    expect(slots[0].querySelector("svg")).toBeTruthy();          // NVDA filled
    expect(slots[1].innerHTML).toContain("spark-down");          // AMD toned down
    expect(slots[2].querySelector("svg")).toBeTruthy();          // second NVDA slot too
  });

  it("dedups repeated symbols in the query", async () => {
    document.body.innerHTML = sparkPlaceholder("NVDA") + sparkPlaceholder("NVDA");
    apiMock.mockResolvedValue({ spark: {} });
    await hydrateSparks(document.body);
    const path = apiMock.mock.calls[0][0] as string;
    expect(decodeURIComponent(path.split("symbols=")[1])).toBe("NVDA");
  });

  it("leaves a slot empty when its symbol has no series", async () => {
    document.body.innerHTML = sparkPlaceholder("GHOST");
    apiMock.mockResolvedValue({ spark: {} });
    await hydrateSparks(document.body);
    expect(document.querySelector(".spark-slot")!.innerHTML).toBe("");
  });

  it("never throws when the fetch fails", async () => {
    document.body.innerHTML = sparkPlaceholder("NVDA");
    apiMock.mockRejectedValue(new Error("boom"));
    await expect(hydrateSparks(document.body)).resolves.toBeUndefined();
    expect(document.querySelector(".spark-slot")!.innerHTML).toBe("");
  });

  it("no-ops with no slots present", async () => {
    document.body.innerHTML = "<div>nothing here</div>";
    await hydrateSparks(document.body);
    expect(apiMock).not.toHaveBeenCalled();
  });
});
