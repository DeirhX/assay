// Segment leaderboard (Phase 1b): the ranked "which segment is hottest, and am
// I in it?" screen. Covers the pure builders (rankSegments sort modes,
// exposureGaps callouts, leaderboardTileHtml rendering incl. sensitive exposure
// and tone) and a render pass over the cached-only payload.
import { beforeEach, describe, expect, it, vi } from "vitest";

const { apiMock } = vi.hoisted(() => ({ apiMock: vi.fn() }));
vi.mock("../src/core", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/core")>();
  return { ...actual, api: apiMock };
});

import type { LeaderboardPayload, LeaderboardRow } from "../src/leaderboard";
import { exposureGaps, leaderboardTileHtml, loadLeaderboard, rankSegments } from "../src/leaderboard";

function row(segment: string, o: Partial<LeaderboardRow> = {}): LeaderboardRow {
  return {
    segment,
    title: o.title ?? segment.replace(/-/g, " "),
    member_count: o.member_count ?? 5,
    momentum_3m_med: o.momentum_3m_med ?? 0,
    momentum_12m_med: o.momentum_12m_med ?? 0,
    breadth_3m: o.breadth_3m ?? 0.5,
    val_growth_med: o.val_growth_med ?? null,
    val_growth_coverage: o.val_growth_coverage ?? 0,
    exposure_pct: o.exposure_pct ?? 0,
    held_count: o.held_count ?? 0,
    cached_at: o.cached_at ?? "2026-07-01T00:00:00+00:00",
    age_days: o.age_days ?? 3,
    stale: o.stale ?? false,
    overlap_allowed: o.overlap_allowed ?? true,
    score: o.score ?? 0,
  };
}

const flush = async () => { for (let i = 0; i < 6; i++) await Promise.resolve(); };

describe("rankSegments", () => {
  const rows = [
    row("a", { score: 2, momentum_3m_med: 5, breadth_3m: 0.4, exposure_pct: 30 }),
    row("b", { score: 8, momentum_3m_med: 40, breadth_3m: 1.0, exposure_pct: 1 }),
    row("c", { score: 5, momentum_3m_med: 20, breadth_3m: 0.7, exposure_pct: 10 }),
  ];

  it("promise: descending score", () => {
    expect(rankSegments(rows, "promise").map((r) => r.segment)).toEqual(["b", "c", "a"]);
  });

  it("momentum: descending 3M median", () => {
    expect(rankSegments(rows, "momentum").map((r) => r.segment)).toEqual(["b", "c", "a"]);
  });

  it("breadth: descending breadth", () => {
    expect(rankSegments(rows, "breadth").map((r) => r.segment)).toEqual(["b", "c", "a"]);
  });

  it("gap: hot-but-unowned floats up, cold-but-owned sinks", () => {
    // b: top score, tiny exposure -> biggest positive gap. a: low score, big
    // exposure -> most negative gap. c sits between.
    expect(rankSegments(rows, "gap").map((r) => r.segment)).toEqual(["b", "c", "a"]);
  });

  it("does not mutate the input array", () => {
    const before = rows.map((r) => r.segment);
    rankSegments(rows, "momentum");
    expect(rows.map((r) => r.segment)).toEqual(before);
  });

  it("sorts null momentum last", () => {
    const withNull = [row("x", { momentum_3m_med: null }), row("y", { momentum_3m_med: 3 })];
    expect(rankSegments(withNull, "momentum").map((r) => r.segment)).toEqual(["y", "x"]);
  });
});

describe("exposureGaps", () => {
  it("flags top-promise underweight and bottom-promise overweight", () => {
    const rows = [
      row("hot", { score: 10, momentum_3m_med: 40, exposure_pct: 1 }),
      row("mid1", { score: 6, exposure_pct: 12 }),
      row("mid2", { score: 5, exposure_pct: 9 }),
      row("cold", { score: 1, exposure_pct: 25 }),
    ];
    const { hot, cold } = exposureGaps(rows, { underweightPct: 5, overweightPct: 15 });
    expect(hot.map((r) => r.segment)).toEqual(["hot"]);
    expect(cold.map((r) => r.segment)).toEqual(["cold"]);
  });

  it("stays quiet when a well-positioned book tracks promise", () => {
    const rows = [
      row("hot", { score: 10, exposure_pct: 30 }),   // hot AND owned -> no flag
      row("cold", { score: 1, exposure_pct: 0 }),     // cold AND unowned -> no flag
    ];
    const { hot, cold } = exposureGaps(rows, { underweightPct: 5, overweightPct: 15 });
    expect(hot).toHaveLength(0);
    expect(cold).toHaveLength(0);
  });

  it("returns empty for no segments", () => {
    expect(exposureGaps([])).toEqual({ hot: [], cold: [] });
  });
});

describe("leaderboardTileHtml", () => {
  it("shows the rank badge and title", () => {
    const html = leaderboardTileHtml(row("semis", { title: "Semiconductors", score: 7 }), 3);
    expect(html).toContain("#3");
    expect(html).toContain("Semiconductors");
    expect(html).toContain('data-segment="semis"');
  });

  it("tone-colors momentum by sign", () => {
    const up = leaderboardTileHtml(row("u", { momentum_3m_med: 12 }), 1);
    const down = leaderboardTileHtml(row("d", { momentum_3m_med: -8 }), 1);
    expect(up).toContain('class="lb-v good"');
    expect(down).toContain('class="lb-v bad"');
  });

  it("wraps exposure in a sensitive span and shows held/total", () => {
    const html = leaderboardTileHtml(row("s", { exposure_pct: 12.5, held_count: 3, member_count: 8 }), 1);
    expect(html).toContain("data-sensitive");
    expect(html).toContain("3/8");
  });

  it("labels an unheld segment instead of a zero exposure", () => {
    const html = leaderboardTileHtml(row("s", { exposure_pct: 0, held_count: 0, member_count: 6 }), 1);
    expect(html).toContain("not held");
  });

  it("marks a stale tile", () => {
    const html = leaderboardTileHtml(row("s", { stale: true }), 1);
    expect(html).toContain("lb-tile-stale");
    expect(html).toContain("lb-dot-stale");
  });

  it("draws the breadth bar to the right width", () => {
    const html = leaderboardTileHtml(row("s", { breadth_3m: 0.75 }), 1);
    expect(html).toContain("width:75%");
    expect(html).toContain("75% up");
  });
});

describe("loadLeaderboard render", () => {
  const payload: LeaderboardPayload = {
    segments: [
      row("hot", { title: "Hot", score: 10, momentum_3m_med: 40, breadth_3m: 1, exposure_pct: 1 }),
      row("owned", { title: "Owned", score: 4, momentum_3m_med: 10, exposure_pct: 30, held_count: 4 }),
    ],
    overlap: true,
    as_of: "2026-07-07T00:00:00+00:00",
    stale_days: 45,
  };

  beforeEach(() => {
    apiMock.mockReset();
    apiMock.mockResolvedValue(payload);
    const body = document.querySelector("#lb-body");
    if (body) body.innerHTML = "";
  });

  it("renders one tile per segment and the overlap note", async () => {
    await loadLeaderboard();
    await flush();
    expect(apiMock).toHaveBeenCalledWith("/api/segments/leaderboard");
    expect(document.querySelectorAll("#lb-body .lb-tile")).toHaveLength(2);
    expect(document.querySelector("#lb-body .lb-overlap")).not.toBeNull();
  });

  it("surfaces the hot-but-underweight callout", async () => {
    await loadLeaderboard();
    await flush();
    const callout = document.querySelector("#lb-body .lb-callout-hot");
    expect(callout).not.toBeNull();
    expect(callout!.textContent).toContain("Hot");
  });

  it("switching sort re-renders and moves the active toggle", async () => {
    await loadLeaderboard();
    await flush();
    const momentumBtn = [...document.querySelectorAll<HTMLElement>("#lb-body .lb-sort")]
      .find((b) => b.dataset.sort === "momentum")!;
    momentumBtn.click();
    const active = document.querySelector<HTMLElement>("#lb-body .lb-sort.active");
    expect(active!.dataset.sort).toBe("momentum");
  });
});
