import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { caveatBanner } from "../src/display/chrome";
import { executionPlanHtml } from "../src/execution-plan-ui";
import { exposureGaps } from "../src/leaderboard";
import type { LeaderboardRow } from "../src/leaderboard";

function sampleRow(overrides: Partial<LeaderboardRow> = {}): LeaderboardRow {
  return {
    segment: "ai-infra",
    title: "AI infrastructure",
    member_count: 12,
    momentum_3m_med: 0.12,
    momentum_12m_med: 0.2,
    breadth_3m: 0.7,
    val_growth_med: 1.2,
    val_growth_coverage: 8,
    exposure_pct: 2,
    held_count: 1,
    cached_at: "2026-07-01T00:00:00Z",
    age_days: 1,
    stale: false,
    overlap_allowed: true,
    score: 9,
    ...overrides,
  };
}

const indexHtml = readFileSync(resolve(__dirname, "../index.html"), "utf8");

describe("display primitives — emitted classes", () => {
  it("caveatBanner dual-classes semantic banner primitives", () => {
    const banner = caveatBanner(["thin sample"], { always: true })!;
    expect(banner.className).toBe("banner banner-warn risk-caveat");
  });

  it("executionPlanHtml lifecycle chips include tone-chip", () => {
    const html = executionPlanHtml({
      schema_version: 1,
      version: 1,
      items: [{
        id: "x",
        symbol: "NVDA",
        source: "rebalance",
        direction: "increase",
        delta_czk: 1,
        delta_pct: 1,
        desired_weight_pct: 5,
        route_policy: "auto_put",
        status: "queued",
      }],
    });
    expect(html).toContain('class="chip tone-chip good"');
  });

  it("index.html page heads and segment controls use primitive classes", () => {
    expect(indexHtml).toContain('class="page-head page-head--spaced reb-head"');
    expect(indexHtml).toContain('class="action-row seg-controls"');
    expect(indexHtml).toContain('class="ui-segment ui-segment--spaced seg-mode"');
    expect(indexHtml).toContain('class="ui-segment-btn seg-mode-btn');
    expect(indexHtml).toContain('class="ui-segment-pills act-filters"');
  });

  it("exposureGaps still surfaces hot underweight rows for callout rendering", () => {
    const rows = [
      sampleRow({ segment: "a", score: 10, exposure_pct: 1 }),
      sampleRow({ segment: "b", score: 9, exposure_pct: 2 }),
      sampleRow({ segment: "c", score: 8, exposure_pct: 3 }),
      sampleRow({ segment: "d", score: 7, exposure_pct: 4 }),
    ];
    expect(exposureGaps(rows).hot.length).toBeGreaterThan(0);
  });
});
