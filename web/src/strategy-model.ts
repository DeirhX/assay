// Pure view-model for the guided Direction -> Rebalance flow. No DOM, no fetch,
// no timers, no module state — every function here is a deterministic function
// of its arguments (a manifest state string, a token, a band), so the stage
// machine, the tone/colour vocabulary, and the band-shift mapping can be
// unit-tested without mounting the view or polling a run. strategy.ts owns the
// DOM/state: it fetches `/api/strategy/{run_id}`, holds `_activeRunId` /
// `_pollTimer` / `_viewStage` / `_lastM`, wires events, and calls these to
// decide which stage is live/reachable and how each cell should read.
import { esc } from "./core";
import { tickerAnchorHtml } from "./analyses/linkify";
import type { BandRow } from "./band-viz";

// A target band, with the rule token that produced it. Shared with strategy.ts,
// which builds its Change/manifest shapes on top of this.
export interface Band {
  low?: number | null;
  high?: number | null;
  rule?: string;
}

// ---- stage machine --------------------------------------------------------
// States in which a background leg is working and the view should keep polling.
const RUNNING = new Set(["draft_running", "synthesis_running", "applying"]);
export const isRunning = (state: string | null | undefined): boolean => RUNNING.has(state || "");

// Progress-tracker stages, in order, mapped from the manifest state.
export const STAGE_ORDER = ["draft", "segment", "research", "synthesize", "review", "done"];
const STATE_STAGE: Record<string, string> = {
  draft_running: "draft",
  awaiting_segment_approval: "segment",
  synthesis_running: "synthesize",
  needs_login: "research",
  awaiting_proposal_approval: "review",
  applying: "review",
  staged: "done",
  done: "done",
  error: "draft",
};

export const STAGE_TITLE: Record<string, string> = {
  draft: "Draft", segment: "Segment", research: "Research",
  synthesize: "Synthesize", review: "Review", done: "Recommendation",
};

// The stage a run is currently on, derived from its state.
export const liveStage = (state: string | null | undefined): string => STATE_STAGE[state || ""] || "draft";

// Stages the user may click into: everything up to and including the live one.
// (Nothing is revisitable while errored — the run never produced those steps.)
export function reachedStages(state: string | null | undefined): string[] {
  if (state === "error") return [];
  const liveIdx = STAGE_ORDER.indexOf(liveStage(state));
  return STAGE_ORDER.slice(0, Math.max(0, liveIdx) + 1);
}

// ---- recent-runs lifecycle labels ----------------------------------------
export function stateLabel(s: string | null | undefined): string {
  return (({
    draft_running: "drafting",
    awaiting_segment_approval: "needs segment approval",
    synthesis_running: "synthesizing",
    needs_login: "needs login",
    awaiting_proposal_approval: "needs approval",
    applying: "applying",
    staged: "staged",
    done: "done",
    error: "failed",
  }) as Record<string, string>)[s || ""] || s || "";
}

// Colour-coded lifecycle pill for the recent-runs list: green = done, red =
// failed, amber = waiting on you, accent (with a pulsing dot) = a leg is working.
const STATE_TONE: Record<string, string> = {
  done: "ok",
  staged: "ok",
  error: "bad",
  awaiting_segment_approval: "warn",
  awaiting_proposal_approval: "warn",
  needs_login: "warn",
  draft_running: "run",
  synthesis_running: "run",
  applying: "run",
};
export function recentStateBadge(state: string | null | undefined): string {
  const tone = STATE_TONE[state || ""] || "muted";
  const running = tone === "run";
  const cls = running ? "accent" : tone;
  const dot = running ? '<span class="strat-recent-dot"></span>' : "";
  return `<span class="abadge ${cls} strat-recent-pill">${dot}${esc(stateLabel(state))}</span>`;
}

// ---- tone vocabulary ------------------------------------------------------
// Semantic tone for an action/rule token so buy- / hold- / sell-leaning cells
// read at a glance (green / grey / amber / red) instead of as identical text.
const TONE: Record<string, string> = {
  accumulate: "pos", add: "pos", buy: "pos",
  hold: "neutral", wait: "neutral",
  reduce: "caution", trim: "caution", trim_only: "caution", do_not_add: "caution",
  avoid: "neg", sell: "neg", exit: "neg",
};
export const toneOf = (token: string | null | undefined): string =>
  TONE[token || ""] || TONE[(token || "").replace("_target", "")] || "neutral";

// Above its band => overweight (trim side, amber); below => underweight (buy side, green).
export const statusTone = (s: string | null | undefined): string => {
  const t = (s || "").toLowerCase();
  if (t.includes("above")) return "caution";
  if (t.includes("below")) return "pos";
  return "neutral";
};

// Positive drift = heavy (trim side); negative = light (buy side). Same colour story.
export const driftTone = (d: number | null | undefined): string =>
  (typeof d === "number" && d > 0 ? "caution" : typeof d === "number" && d < 0 ? "pos" : "neutral");

// directionTag tone (ok/warn/bad) -> the strat-tag colour vocabulary.
export const DIR_TONE: Record<string, string> = { ok: "pos", warn: "caution", bad: "neg" };

// ---- band mapping ---------------------------------------------------------
export const bandStr = (t: Band | null | undefined): string =>
  (t && t.low != null ? `${t.low}–${t.high}%` : "—");

// A defined band or null (for the band-viz track), so a first-time target reads
// as "added" and a dropped one as "removed" rather than a phantom 0–0 bar.
const bandOrNull = (t: Band | null | undefined) =>
  (t && (t.low != null || t.high != null)) ? { low: t.low ?? undefined, high: t.high ?? undefined, rule: t.rule } : null;

// Map a Gate-2 change onto the shared before(ghost)→after(solid) band track.
export function changeBandRow(
  c: { current_target?: Band | null; proposed_target?: Band | null },
): BandRow {
  const before = bandOrNull(c.current_target);
  const after = bandOrNull(c.proposed_target);
  const change = !before && after ? "added" : before && !after ? "removed" : "modified";
  return { change, before, after };
}

// ---- links ----------------------------------------------------------------
// Render a symbol as a deep-dive link. The global a.tlink click handler in shell
// intercepts it and calls openTicker (which live-pulls on a miss); the href is a
// fallback for middle-click / open-in-new-tab.
export const symLink = (sym: string | null | undefined): string =>
  (sym ? tickerAnchorHtml(sym, { bold: true }) : "—");
