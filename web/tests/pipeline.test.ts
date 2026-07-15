// Tests for the pipeline wizard's gating logic: which steps are reachable is
// derived from real data (segment chosen, report saved on disk), and the
// stem-prefix discipline must not let "ai" match "ai-software" runs.
import { beforeEach, describe, expect, it } from "vitest";
import type { DeepRun } from "../src/api-types";
import { $, state } from "../src/core";
import {
  latestReportForSegment,
  pipeCurrentStem,
  pipeHasSavedReport,
  pipeLockReason,
  pipeUnlockedMax,
  renderReviewGate,
} from "../src/pipeline";

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

describe("Step 4 decision gate", () => {
  beforeEach(() => {
    ($("#pipe-review-output") as HTMLElement).innerHTML = "";
    ($("#pipe-report") as HTMLTextAreaElement).value = "Saved report";
    ($("#pipe-apply-status") as HTMLElement).textContent = "";
  });

  it("refuses to stage a legacy review-only proposal", () => {
    renderReviewGate({
      proposal: {
        changes: [{
          symbol: "NVDA",
          action: "add_target",
          proposed_target: { low: 4, high: 5.4, rule: "accumulate" },
        }],
      },
      rows: [{ symbol: "NVDA" }],
    });

    const apply = $("#pipe-apply-proposal") as HTMLButtonElement;
    expect(apply.disabled).toBe(true);
    expect(apply.hidden).toBe(true);
    expect($("#pipe-next-action")!.textContent).toMatch(/legacy review/i);
    expect(($("#pipe-run-review") as HTMLButtonElement).textContent).toMatch(/sized proposal/i);
  });

  it("makes a constructed proposal's next action and target bands explicit", () => {
    renderReviewGate({
      proposal: {
        construct_meta: {
          book_reconciliation: {
            targeted_mid_pct: 74.7,
            cash_target_pct: 5,
            over_allocated: false,
          },
        },
        changes: [{
          symbol: "NVDA",
          action: "add_target",
          rationale: "Below the approved strategic floor.",
          proposed_target: { low: 4, high: 5.4, rule: "accumulate" },
        }],
      },
      rows: [{ symbol: "NVDA" }],
    });

    const apply = $("#pipe-apply-proposal") as HTMLButtonElement;
    expect(apply.disabled).toBe(false);
    expect(apply.hidden).toBe(false);
    expect(apply.textContent).toBe("Add 1 sized change to Pending model");
    expect($("#pipe-next-action")!.textContent).toMatch(/review 1 sized change/i);
    expect($("#pipe-review-output")!.textContent).toContain("4–5.4%");
    expect($("#pipe-review-output")!.textContent).toContain("Fits budget");
  });

  it("shows how a standing exit decision resolves bullish research", () => {
    renderReviewGate({
      proposal: {
        construct_meta: {},
        changes: [{
          symbol: "PYPL",
          action: "modify_target",
          rationale: "Standing exit decision.",
          resolution: "Resolved automatically: standing exit intent overrides this report.",
          report_conviction: "high",
          standing_intent: "avoid",
          proposed_target: { low: 0, high: 0, rule: "avoid" },
        }],
      },
      rows: [{ symbol: "PYPL", report_action: "add" }],
    });

    const output = $("#pipe-review-output")!;
    expect(output.textContent).toContain("0–0%");
    expect(output.textContent).toContain("avoid");
    expect(output.textContent).toContain(
      "Resolved automatically: standing exit intent overrides this report.",
    );
    expect(output.querySelector(".proposal-resolution")).not.toBeNull();
  });
});
