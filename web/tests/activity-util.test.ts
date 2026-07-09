// Tests for the Activity view's pure helpers: reconstructing the JobListing
// shape from a flattened feed event (so Task Center routing works), and the
// day-bucket labels used for group headers.
import { describe, expect, it } from "vitest";
import { asJob, dayLabel } from "../src/activity-util";
import type { ActivityEvent } from "../src/api-types";

describe("asJob", () => {
  it("re-nests artifact_stem -> artifact.stem and slug -> result.slug", () => {
    const ev: ActivityEvent = {
      ts: "2026-07-09T00:00:00+00:00", type: "task", id: "abc",
      kind: "deep_research", state: "done", artifact_stem: "ai-chips-2026-07-09",
      slug: "ai-chips",
    };
    const job = asJob(ev);
    expect(job.artifact).toEqual({ stem: "ai-chips-2026-07-09" });
    expect(job.result).toEqual({ slug: "ai-chips" });
    expect(job.kind).toBe("deep_research");
    expect(job.cancelled).toBe(false);
  });

  it("leaves artifact/result null when the feed has no stem/slug", () => {
    const ev: ActivityEvent = {
      ts: "2026-07-09T00:00:00+00:00", type: "task", kind: "ticker_analysis",
      state: "done", symbol: "RTX",
    };
    const job = asJob(ev);
    expect(job.artifact).toBeNull();
    expect(job.result).toBeNull();
    expect(job.symbol).toBe("RTX");
  });

  it("flags a cancelled state", () => {
    const ev: ActivityEvent = { ts: "x", type: "task", kind: "ticker_qa", state: "cancelled" };
    expect(asJob(ev).cancelled).toBe(true);
  });
});

describe("dayLabel", () => {
  const now = new Date("2026-07-09T12:00:00");
  it("labels same-day as Today", () => {
    expect(dayLabel("2026-07-09T01:00:00", now)).toBe("Today");
  });
  it("labels the prior day as Yesterday", () => {
    expect(dayLabel("2026-07-08T23:00:00", now)).toBe("Yesterday");
  });
  it("labels within a week as N days ago", () => {
    expect(dayLabel("2026-07-06T09:00:00", now)).toBe("3 days ago");
  });
  it("falls back to a date string past a week", () => {
    const out = dayLabel("2026-06-01T09:00:00", now);
    expect(out).not.toBe("Today");
    expect(out).not.toMatch(/days ago/);
  });
  it("returns Earlier for an unparseable timestamp", () => {
    expect(dayLabel("not-a-date", now)).toBe("Earlier");
  });
});
