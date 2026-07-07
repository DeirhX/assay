// Tests for the pure strategy view-model (extracted from strategy.ts so the
// stage machine, the tone/colour vocabulary, and the band-shift mapping are
// testable without mounting the guided flow or polling a run). The invariants
// that matter: which stages are reachable follows the state, an errored run
// exposes no revisitable steps, running states drive polling, and a first-time
// vs dropped band maps to added/removed on the shared track.
import { describe, expect, it } from "vitest";
import {
  bandStr,
  changeBandRow,
  driftTone,
  isRunning,
  liveStage,
  reachedStages,
  recentStateBadge,
  stateLabel,
  statusTone,
  symLink,
  toneOf,
} from "../src/strategy-model";

describe("isRunning", () => {
  it("is true only while a background leg works", () => {
    expect(isRunning("draft_running")).toBe(true);
    expect(isRunning("synthesis_running")).toBe(true);
    expect(isRunning("applying")).toBe(true);
  });

  it("is false for gates, terminal, and unknown states", () => {
    expect(isRunning("awaiting_segment_approval")).toBe(false);
    expect(isRunning("done")).toBe(false);
    expect(isRunning("error")).toBe(false);
    expect(isRunning(null)).toBe(false);
  });
});

describe("liveStage", () => {
  it("maps each manifest state onto its progress stage", () => {
    expect(liveStage("draft_running")).toBe("draft");
    expect(liveStage("awaiting_segment_approval")).toBe("segment");
    expect(liveStage("synthesis_running")).toBe("synthesize");
    expect(liveStage("needs_login")).toBe("research");
    expect(liveStage("awaiting_proposal_approval")).toBe("review");
    expect(liveStage("applying")).toBe("review");
    expect(liveStage("staged")).toBe("done");
    expect(liveStage("done")).toBe("done");
  });

  it("defaults unknown/errored states to draft", () => {
    expect(liveStage("error")).toBe("draft");
    expect(liveStage("wat")).toBe("draft");
    expect(liveStage(undefined)).toBe("draft");
  });
});

describe("reachedStages", () => {
  it("exposes every stage up to and including the live one", () => {
    expect(reachedStages("awaiting_proposal_approval")).toEqual(
      ["draft", "segment", "research", "synthesize", "review"]);
  });

  it("opens only the first stage at the very start", () => {
    expect(reachedStages("draft_running")).toEqual(["draft"]);
  });

  it("reveals the whole track when done", () => {
    expect(reachedStages("done")).toEqual(
      ["draft", "segment", "research", "synthesize", "review", "done"]);
  });

  it("makes nothing revisitable while errored", () => {
    expect(reachedStages("error")).toEqual([]);
  });
});

describe("stateLabel / recentStateBadge", () => {
  it("gives human labels and echoes unknown states verbatim", () => {
    expect(stateLabel("synthesis_running")).toBe("synthesizing");
    expect(stateLabel("error")).toBe("failed");
    expect(stateLabel("mystery")).toBe("mystery");
    expect(stateLabel(null)).toBe("");
  });

  it("tones the pill and adds a pulsing dot only while running", () => {
    const running = recentStateBadge("draft_running");
    expect(running).toContain("accent");
    expect(running).toContain("strat-recent-dot");
    const done = recentStateBadge("done");
    expect(done).toContain("ok");
    expect(done).not.toContain("strat-recent-dot");
    expect(recentStateBadge("what")).toContain("muted");
  });
});

describe("toneOf", () => {
  it("colours buy / hold / trim / sell tokens", () => {
    expect(toneOf("accumulate")).toBe("pos");
    expect(toneOf("hold")).toBe("neutral");
    expect(toneOf("trim")).toBe("caution");
    expect(toneOf("sell")).toBe("neg");
  });

  it("strips a _target suffix before looking up the tone", () => {
    expect(toneOf("buy_target")).toBe("pos");
    expect(toneOf("avoid_target")).toBe("neg");
  });

  it("falls back to neutral for unknown tokens", () => {
    expect(toneOf("wobble")).toBe("neutral");
    expect(toneOf(null)).toBe("neutral");
  });
});

describe("statusTone / driftTone", () => {
  it("reads above-band as trim and below-band as buy", () => {
    expect(statusTone("above band")).toBe("caution");
    expect(statusTone("below band")).toBe("pos");
    expect(statusTone("in band")).toBe("neutral");
  });

  it("reads positive drift as heavy and negative as light", () => {
    expect(driftTone(3.2)).toBe("caution");
    expect(driftTone(-1.1)).toBe("pos");
    expect(driftTone(0)).toBe("neutral");
    expect(driftTone(null)).toBe("neutral");
  });
});

describe("bandStr", () => {
  it("formats a defined band and dashes a missing one", () => {
    expect(bandStr({ low: 5, high: 7 })).toBe("5–7%");
    expect(bandStr(null)).toBe("—");
    expect(bandStr({ low: null, high: 7 })).toBe("—");
  });
});

describe("changeBandRow", () => {
  it("labels a first-time target as added", () => {
    const row = changeBandRow({ current_target: null, proposed_target: { low: 4, high: 6 } });
    expect(row.change).toBe("added");
    expect(row.before).toBeNull();
    expect(row.after).toEqual({ low: 4, high: 6, rule: undefined });
  });

  it("labels a dropped target as removed", () => {
    const row = changeBandRow({ current_target: { low: 4, high: 6 }, proposed_target: null });
    expect(row.change).toBe("removed");
    expect(row.after).toBeNull();
  });

  it("labels a shifted band as modified and converts null edges to undefined", () => {
    const row = changeBandRow({ current_target: { low: 4, high: null }, proposed_target: { low: 5, high: 8 } });
    expect(row.change).toBe("modified");
    expect(row.before).toEqual({ low: 4, high: undefined, rule: undefined });
  });

  it("treats an empty 0-edge band (all null) as absent", () => {
    const row = changeBandRow({ current_target: { low: null, high: null }, proposed_target: { low: 5, high: 8 } });
    expect(row.change).toBe("added");
    expect(row.before).toBeNull();
  });
});

describe("symLink", () => {
  it("dashes a missing symbol and links a present one", () => {
    expect(symLink(null)).toBe("—");
    expect(symLink("")).toBe("—");
    expect(symLink("NVDA")).toContain("NVDA");
  });
});
