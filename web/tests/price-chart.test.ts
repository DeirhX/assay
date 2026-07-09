// The x-axis labels are spaced by array index, so an intraday range (many
// points per calendar day) used to render the same day twice — the classic
// "Jul 8 appears twice on the axis". These lock in the label-dedupe.
import { describe, expect, it } from "vitest";
import { chartSvg } from "../src/deepdive/price-chart";

// Pull the <text class="chart-label"> nodes that sit on the x-axis (they carry
// the date/time labels; the y-axis price labels are text-anchor="end" at the
// left gutter, distinguishable by their x position near pad.left - 10 = 48).
function xLabels(svg: string): string[] {
  const out: string[] = [];
  const re = /<text class="chart-label" x="([\d.]+)" y="\d+" text-anchor="[^"]*">([^<]*)<\/text>/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(svg))) {
    if (Number(m[1]) > 48) out.push(m[2]);   // x > left gutter => an x-axis tick
    else out.push("");                        // y-axis label placeholder (skip)
  }
  return out.filter(Boolean);
}

// Build an intraday-ish series: several timestamps per day across a few days.
function intraday(days: string[], perDay: number): { date: string; close: number }[] {
  const pts: { date: string; close: number }[] = [];
  for (const d of days) {
    for (let h = 0; h < perDay; h++) {
      const hh = String(9 + h).padStart(2, "0");
      pts.push({ date: `${d}T${hh}:30:00`, close: 100 + pts.length });
    }
  }
  return pts;
}

describe("price-chart x-axis labels", () => {
  it("never renders the same day label twice on an intraday range", () => {
    const points = intraday(
      ["2026-07-01", "2026-07-02", "2026-07-06", "2026-07-07", "2026-07-08"], 6,
    );
    const drawn = chartSvg({ symbol: "TEST" }, { points, range: "1w", interval: "30m", source: "test" });
    expect(drawn).not.toBeNull();
    const labels = xLabels(drawn!.svg);
    expect(labels.length).toBeGreaterThan(1);
    expect(new Set(labels).size).toBe(labels.length);   // all distinct — no "Jul 8, Jul 8"
  });

  it("keeps the latest date anchored at the right edge after a collision", () => {
    // >2-day span => date labels; the busy last day is where the old bug put a
    // second "Jul 8" tick just left of the right edge.
    const points = intraday(
      ["2026-07-01", "2026-07-02", "2026-07-06", "2026-07-07", "2026-07-08"], 6,
    );
    const drawn = chartSvg({ symbol: "TEST" }, { points, range: "1w", interval: "30m", source: "test" });
    const labels = xLabels(drawn!.svg);
    const lastDay = new Date("2026-07-08T00:00:00").toLocaleDateString(undefined, { month: "short", day: "numeric" });
    expect(labels.filter((l) => l === lastDay)).toHaveLength(1);   // once, not twice
    expect(labels[labels.length - 1]).toBe(lastDay);               // and it's the right edge
  });

  it("leaves a clean daily series (one point per day) untouched", () => {
    const points = ["2026-07-01", "2026-07-02", "2026-07-03", "2026-07-06", "2026-07-07"]
      .map((d, i) => ({ date: d, close: 100 + i }));
    const drawn = chartSvg({ symbol: "TEST" }, { points, range: "1w", interval: "1d", source: "test" });
    const labels = xLabels(drawn!.svg);
    expect(new Set(labels).size).toBe(labels.length);
  });
});
