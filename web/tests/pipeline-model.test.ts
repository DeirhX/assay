// Tests for the pure pipeline view-model (extracted from pipeline.ts so the
// wizard's gating math, segment-draft validation, stem <-> (segment, date)
// mapping, and saved-run selection are testable without mounting the DOM). The
// invariants that matter: the stem carries a date suffix so a short segment
// can't steal a longer one's runs, the frontier only opens a step when its
// prerequisite exists, and the draft gate rejects an empty/skeleton/placeholder
// definition. The DOM-coupled wrappers stay covered by pipeline.test.ts.
import { describe, expect, it } from "vitest";
import type { DeepRun } from "../src/api-types";
import {
  blankSegmentDef,
  clampStep,
  latestReportForSegment,
  parseStem,
  pipeLockReason,
  pipeStem,
  proposalReadiness,
  reviewTagClass,
  segDraftValid,
  segSlugify,
  unlockedMax,
} from "../src/pipeline-model";

describe("pipeStem / parseStem", () => {
  it("needs both a segment and a date to form a stem", () => {
    expect(pipeStem("semis", "2026-06-01")).toBe("semis-2026-06-01");
    expect(pipeStem("semis", "")).toBe("");
    expect(pipeStem("", "2026-06-01")).toBe("");
    expect(pipeStem(null, null)).toBe("");
  });

  it("trims surrounding whitespace before joining", () => {
    expect(pipeStem("  semis ", " 2026-06-01 ")).toBe("semis-2026-06-01");
  });

  it("round-trips a stem back into its parts", () => {
    expect(parseStem("semis-2026-06-01")).toEqual({ segment: "semis", date: "2026-06-01" });
  });

  it("keeps hyphenated segments intact — only the date suffix is split off", () => {
    expect(parseStem("ai-software-2026-06-01")).toEqual({ segment: "ai-software", date: "2026-06-01" });
  });

  it("returns null when there is no trailing YYYY-MM-DD", () => {
    expect(parseStem("semis")).toBeNull();
    expect(parseStem("semis-2026-06")).toBeNull();
    expect(parseStem(null)).toBeNull();
  });
});

describe("unlockedMax", () => {
  it("opens step 4 only with a saved report", () => {
    expect(unlockedMax(true, true)).toBe(4);
  });

  it("opens steps 2-3 with a segment but no saved report", () => {
    expect(unlockedMax(false, true)).toBe(3);
  });

  it("keeps only step 1 with no segment", () => {
    expect(unlockedMax(false, false)).toBe(1);
  });

  it("a saved report without a segment still ranks as the report frontier", () => {
    // hasSavedReport dominates by design — the DOM layer only ever reports it
    // true for the current segment+date, so this ordering is what callers rely on.
    expect(unlockedMax(true, false)).toBe(4);
  });
});

describe("pipeLockReason", () => {
  it("explains each locked frontier and stays silent for step 1", () => {
    expect(pipeLockReason(4)).toMatch(/report/i);
    expect(pipeLockReason(2)).toMatch(/segment/i);
    expect(pipeLockReason(1)).toBe("");
  });
});

describe("clampStep", () => {
  it("clamps into the 1..4 wizard range", () => {
    expect(clampStep(0)).toBe(1);
    expect(clampStep(9)).toBe(4);
    expect(clampStep(3)).toBe(3);
  });

  it("falls back to step 1 on garbage input", () => {
    expect(clampStep("nope")).toBe(1);
    expect(clampStep(undefined)).toBe(1);
    expect(clampStep(NaN)).toBe(1);
  });
});

describe("segSlugify", () => {
  it("lowercases, collapses non-alphanumerics to single hyphens, trims edges", () => {
    expect(segSlugify("  AI & Space Exploration!! ")).toBe("ai-space-exploration");
  });

  it("caps the slug at 60 characters", () => {
    expect(segSlugify("a".repeat(80)).length).toBe(60);
  });

  it("is empty for input with no alphanumerics", () => {
    expect(segSlugify("---")).toBe("");
  });
});

describe("blankSegmentDef", () => {
  it("titlecases the theme and carries exactly one placeholder member", () => {
    const def = blankSegmentDef("space exploration");
    expect(def.title).toBe("Space Exploration");
    expect(def.status).toBe("approved");
    expect(def.members).toHaveLength(1);
    expect(def.members[0].symbol).toBe("TICKER");
  });

  it("falls back to a generic title with no theme", () => {
    expect(blankSegmentDef("").title).toBe("New segment");
  });
});

describe("segDraftValid", () => {
  const good = JSON.stringify({ members: [{ symbol: "NVDA" }] });

  it("accepts a slug plus JSON with at least one real ticker", () => {
    expect(segDraftValid("semis", good)).toBe(true);
  });

  it("rejects a missing slug even with valid JSON", () => {
    expect(segDraftValid("", good)).toBe(false);
    expect(segDraftValid("   ", good)).toBe(false);
  });

  it("rejects empty, non-object, or member-less definitions", () => {
    expect(segDraftValid("semis", "")).toBe(false);
    expect(segDraftValid("semis", "[]")).toBe(false);
    expect(segDraftValid("semis", "{}")).toBe(false);
    expect(segDraftValid("semis", JSON.stringify({ members: [] }))).toBe(false);
  });

  it("rejects unparseable JSON", () => {
    expect(segDraftValid("semis", "{not json")).toBe(false);
  });

  it("rejects the untouched placeholder ticker (case-insensitively)", () => {
    expect(segDraftValid("semis", JSON.stringify({ members: [{ symbol: "TICKER" }] }))).toBe(false);
    expect(segDraftValid("semis", JSON.stringify({ members: [{ symbol: "ticker" }] }))).toBe(false);
  });

  it("requires every member to carry a real symbol, not just one", () => {
    expect(segDraftValid("semis", JSON.stringify({ members: [{ symbol: "NVDA" }, { symbol: "" }] }))).toBe(false);
  });
});

describe("latestReportForSegment", () => {
  // Minimal fixture: only `stem` + `files.report` are read, so the rest of the
  // DeepRun shape is deliberately omitted (asserted, not built).
  const run = (stem: string, hasReport = true): DeepRun => ({
    stem,
    files: hasReport ? { report: "report.md" } : {},
  } as DeepRun);

  it("picks the newest run with a report on disk", () => {
    const runs = [
      run("semis-2026-05-01"),
      run("semis-2026-06-01"),
      run("semis-2026-05-15", false),
    ];
    expect(latestReportForSegment(runs, "semis")!.stem).toBe("semis-2026-06-01");
  });

  it("does not let a short segment match a longer one's runs", () => {
    expect(latestReportForSegment([run("ai-software-2026-06-01")], "ai")).toBeNull();
  });

  it("returns null with no segment, no runs, or no match", () => {
    expect(latestReportForSegment([run("other-2026-06-01")], "semis")).toBeNull();
    expect(latestReportForSegment([], "semis")).toBeNull();
    expect(latestReportForSegment(null, "semis")).toBeNull();
    expect(latestReportForSegment([run("semis-2026-06-01")], "")).toBeNull();
  });
});

describe("reviewTagClass", () => {
  it("escalates blockers and conflicts to bad", () => {
    expect(reviewTagClass("BLOCKED")).toBe("bad");
    expect(reviewTagClass("data conflict")).toBe("bad");
  });

  it("flags warnings and weak sources", () => {
    expect(reviewTagClass("WARN")).toBe("warn");
    expect(reviewTagClass("weak")).toBe("warn");
  });

  it("greens ok / good / primary / strong", () => {
    expect(reviewTagClass("primary")).toBe("good");
    expect(reviewTagClass("strong")).toBe("good");
  });

  it("is neutral for anything else", () => {
    expect(reviewTagClass("INFO")).toBe("");
    expect(reviewTagClass(undefined)).toBe("");
  });
});

describe("proposalReadiness", () => {
  it("refuses review-only proposal scraps that were never sized", () => {
    expect(proposalReadiness({
      changes: [{ symbol: "NVDA" }],
      blocked_symbols: [],
    })).toMatchObject({
      phase: "needs_sizing",
      constructed: false,
      total: 1,
      applicable: 1,
    });
  });

  it("marks a constructed proposal ready when at least one change is applicable", () => {
    expect(proposalReadiness({
      construct_meta: { book_reconciliation: {} },
      changes: [{ symbol: "NVDA" }, { symbol: "NU" }],
      blocked_symbols: ["NU"],
    })).toEqual({
      phase: "ready",
      constructed: true,
      total: 2,
      applicable: 1,
      blocked: ["NU"],
    });
  });

  it("distinguishes fully blocked and empty constructed proposals", () => {
    expect(proposalReadiness({
      construct_meta: {},
      changes: [{ symbol: "NU" }],
      blocked_symbols: ["NU"],
    }).phase).toBe("blocked");
    expect(proposalReadiness({
      construct_meta: {},
      changes: [],
    }).phase).toBe("empty");
  });
});
