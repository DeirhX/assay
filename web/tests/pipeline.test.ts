// Tests for the pipeline wizard's gating logic: which steps are reachable is
// derived from real data (segment chosen, report saved on disk), and the
// stem-prefix discipline must not let "ai" match "ai-software" runs.
import { beforeEach, describe, expect, it } from "vitest";
import type { DeepRun } from "../src/api-types";
import { $, state } from "../src/core";
import { latestReportForSegment, pipeCurrentStem, pipeHasSavedReport, pipeLockReason, pipeUnlockedMax } from "../src/pipeline";

const setSlug = (slug: string) => {
  // "new" mode reads #pipe-slug, which avoids needing <option> entries in the
  // segment <select> just to set its value.
  state.segMode = "new";
  ($("#pipe-slug") as HTMLInputElement).value = slug;
};
const setDate = (date: string) => {
  ($("#pipe-date") as HTMLInputElement).value = date;
};

beforeEach(() => {
  setSlug("");
  setDate("");
  state.savedRuns = new Set();
  state.deepRuns = [];
});

describe("pipeUnlockedMax", () => {
  it("only step 1 is reachable with no segment", () => {
    expect(pipeUnlockedMax()).toBe(1);
  });

  it("steps 2-3 unlock once a segment exists", () => {
    setSlug("semis");
    expect(pipeUnlockedMax()).toBe(3);
  });

  it("step 4 unlocks only when a report is saved for this exact segment+date", () => {
    setSlug("semis");
    setDate("2026-06-01");
    state.savedRuns = new Set(["semis-2026-06-01"]);
    expect(pipeUnlockedMax()).toBe(4);
  });

  it("a saved report for another date does not unlock the review gate", () => {
    setSlug("semis");
    setDate("2026-06-02");
    state.savedRuns = new Set(["semis-2026-06-01"]);
    expect(pipeUnlockedMax()).toBe(3);
  });
});

describe("pipeCurrentStem / pipeHasSavedReport", () => {
  it("stem needs both segment and date", () => {
    expect(pipeCurrentStem()).toBe("");
    setSlug("semis");
    expect(pipeCurrentStem()).toBe("");
    setDate("2026-06-01");
    expect(pipeCurrentStem()).toBe("semis-2026-06-01");
  });

  it("saved-report check matches the exact stem", () => {
    setSlug("semis");
    setDate("2026-06-01");
    expect(pipeHasSavedReport()).toBe(false);
    state.savedRuns.add("semis-2026-06-01");
    expect(pipeHasSavedReport()).toBe(true);
  });
});

describe("pipeLockReason", () => {
  it("explains each locked frontier", () => {
    expect(pipeLockReason(4)).toMatch(/report/i);
    expect(pipeLockReason(2)).toMatch(/segment/i);
    expect(pipeLockReason(1)).toBe("");
  });
});

describe("latestReportForSegment", () => {
  // Minimal fixture: latestReportForSegment only reads `stem` + `files.report`,
  // so the other DeepRun fields are deliberately omitted (asserted, not built).
  const run = (stem: string, hasReport = true): DeepRun => ({
    stem,
    files: hasReport ? { report: "report.md" } : {},
  } as DeepRun);

  it("picks the newest run with a report on disk", () => {
    state.deepRuns = [
      run("semis-2026-05-01"),
      run("semis-2026-06-01"),
      run("semis-2026-05-15", false),
    ];
    expect(latestReportForSegment("semis")!.stem).toBe("semis-2026-06-01");
  });

  it("does not let a short segment match a longer one's runs", () => {
    state.deepRuns = [run("ai-software-2026-06-01")];
    expect(latestReportForSegment("ai")).toBeNull();
  });

  it("returns null with no segment or no matching runs", () => {
    expect(latestReportForSegment("")).toBeNull();
    state.deepRuns = [run("other-2026-06-01")];
    expect(latestReportForSegment("semis")).toBeNull();
  });
});
