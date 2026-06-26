// Tests for the working-draft (staging) view's pure render helpers: band
// formatting, provenance lineage labels (legacy vs pinned vs research-derived),
// and the whole-book reconciliation bar (which must flag over-allocation).
import { describe, expect, it } from "vitest";
import { bandText, provLabel, reconHtml } from "../src/staging";

describe("bandText", () => {
  it("formats a band with rule and optional sleeve", () => {
    expect(bandText({ low: 3, high: 5, rule: "accumulate" })).toBe("3–5% accumulate");
    expect(bandText({ low: 3, high: 5, rule: "hold", sleeve: "semis-compute" }))
      .toContain("· semis-compute");
  });

  it("renders an em-dash for a missing band (added/removed side)", () => {
    expect(bandText(null)).toBe("—");
  });
});

describe("provLabel", () => {
  it("labels a pinned band as standing intent", () => {
    expect(provLabel({ source: "user-pin", stance: "accumulate" })).toContain("pinned");
  });

  it("flags a legacy hand-set band", () => {
    expect(provLabel({ source: "legacy-plan", set_at: "2026-01-01" })).toContain("legacy plan");
  });

  it("attributes a research-derived band to its run/segment", () => {
    const out = provLabel({ source: "strategy", run_id: "abc123", segment: "add-ai", conviction: "high" });
    expect(out).toContain("abc123");
    expect(out).toContain("add-ai");
  });
});

describe("reconHtml", () => {
  it("shows the free-to-allocate headroom when the book fits", () => {
    const html = reconHtml({ targeted_mid_pct: 40, cash_target_pct: 5, available_pct: 55, over_allocated: false });
    expect(html).toContain("Free to allocate");
    expect(html).not.toContain("Over budget");
    expect(html).not.toContain("stage-recon-bad");
  });

  it("flags an over-allocated book in red with a warning", () => {
    const html = reconHtml({ targeted_mid_pct: 110, cash_target_pct: 5, available_pct: -15, over_allocated: true });
    expect(html).toContain("stage-recon-bad");
    expect(html).toContain("Over budget by");
    expect(html).toContain("exceed 100%");
  });
});
