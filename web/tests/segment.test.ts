// The peer table's Trend column (Phase 2c): a cached-only sparkline per member,
// rendered as a non-sortable column and filled by one batch /api/spark call.
import { beforeEach, describe, expect, it, vi } from "vitest";

const { apiMock } = vi.hoisted(() => ({ apiMock: vi.fn() }));
vi.mock("../src/core", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/core")>();
  return { ...actual, api: apiMock };
});

import { state } from "../src/core";
import type { SegmentRec } from "../src/segment";
import { renderSegment, SEG_COLS } from "../src/segment";

const REC: SegmentRec = {
  title: "AI Semiconductors",
  segment: "ai-semis",
  members: [
    { symbol: "NVDA", data_quality: "INFO", decision: "hold", sleeve: "core" },
    { symbol: "AMD", data_quality: "INFO", decision: "research", sleeve: "core" },
  ],
};

const flush = async () => {
  for (let i = 0; i < 6; i++) await Promise.resolve();
};

beforeEach(() => {
  apiMock.mockReset();
  apiMock.mockResolvedValue({ spark: {} });
  state.segSort = { key: "symbol", dir: 1 };
  const out = document.querySelector("#seg-result");
  if (out) out.innerHTML = "";
});

describe("segment peer table trend column", () => {
  it("declares a non-sortable Trend pseudo-column", () => {
    expect(SEG_COLS.some(([k]) => k === "__spark")).toBe(true);
    renderSegment(REC);
    const heads = [...document.querySelectorAll("#seg-result th")].map((t) => t.textContent);
    expect(heads).toContain("Trend");
  });

  it("drops one spark placeholder per member, keyed by symbol", () => {
    renderSegment(REC);
    const slots = [...document.querySelectorAll<HTMLElement>("#seg-result .spark-slot")];
    expect(slots).toHaveLength(2);
    expect(slots.map((s) => s.dataset.spark).sort()).toEqual(["AMD", "NVDA"]);
  });

  it("clicking the Trend header does not change the sort (it has no key handler)", () => {
    renderSegment(REC);
    const trendTh = [...document.querySelectorAll<HTMLElement>("#seg-result th")]
      .find((t) => (t.textContent || "").includes("Trend"))!;
    trendTh.click();
    expect(state.segSort.key).toBe("symbol"); // unchanged
  });

  it("fills the column with one batch /api/spark call", async () => {
    renderSegment(REC);
    await flush();
    expect(apiMock).toHaveBeenCalledWith(expect.stringContaining("/api/spark?symbols="));
    expect(apiMock).toHaveBeenCalledTimes(1);
  });
});
