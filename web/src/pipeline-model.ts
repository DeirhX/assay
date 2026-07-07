// Pure view-model for the research pipeline wizard. No DOM, no fetch, no timers,
// no module state — every function here is a deterministic function of its
// arguments, so the step-gating math, the segment-draft validation, the
// stem <-> (segment, date) mapping, and the saved-run selection can be
// unit-tested without mounting the wizard. pipeline.ts owns the DOM/state: it
// reads the inputs, holds `state.*` and the lock timer, wires the events, and
// calls these to decide what is reachable and what to show. Keep it that way —
// if a new gate or derivation is needed, express it here first so it stays
// testable apart from where its inputs happen to come from.
import type { DeepRun } from "./api-types";

// A run stem is `${segment}-YYYY-MM-DD`. These two functions are the single
// source of truth for moving between the stem and its parts; the date suffix is
// what stops a segment like "ai" from matching an "ai-software-..." run.
const STEM_DATE_RE = /^(.*)-(\d{4}-\d{2}-\d{2})$/;

export function pipeStem(segment: string | null | undefined, date: string | null | undefined): string {
  const seg = String(segment || "").trim();
  const d = String(date || "").trim();
  return seg && d ? `${seg}-${d}` : "";
}

export function parseStem(stem: string | null | undefined): { segment: string; date: string } | null {
  const m = String(stem || "").match(STEM_DATE_RE);
  return m ? { segment: m[1], date: m[2] } : null;
}

// The reachable step given what data actually exists. A pure function of two
// booleans so the frontier logic is testable apart from where they come from
// (DOM inputs + the saved-run set):
//   4 Review   -> a report is saved on disk for this exact segment + date
//   2-3        -> a segment is chosen/approved
//   1 Segment  -> always available
export function unlockedMax(hasSavedReport: boolean, hasSegment: boolean): number {
  if (hasSavedReport) return 4;
  if (hasSegment) return 3;
  return 1;
}

export function pipeLockReason(n: number): string {
  if (n >= 4) return "Save or import a report for this segment + date first — the review gate has nothing to read otherwise.";
  if (n >= 2) return "Choose or approve a segment on Step 1 first.";
  return "";
}

// Coerce an arbitrary requested step into the 1..4 wizard range.
export function clampStep(n: unknown): number {
  return Math.max(1, Math.min(4, Number(n) || 1));
}

// The example member's symbol in the manual template. segDraftValid() rejects
// it, so the user must replace it with a real ticker before continuing.
export const SEG_PLACEHOLDER_SYM = "TICKER";

export function segSlugify(s: string): string {
  return String(s || "").toLowerCase().trim()
    .replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 60);
}

// A minimal but structurally valid segment definition for the manual path: it
// shows the real shape with one example member. The placeholder symbol is
// intentionally rejected by segDraftValid() so Approve & continue stays blocked
// until it is replaced.
export function blankSegmentDef(theme: string) {
  const title = theme
    ? theme.replace(/\s+/g, " ").trim().replace(/\b\w/g, (c: string) => c.toUpperCase())
    : "New segment";
  return {
    title,
    kind: "research",
    status: "approved",
    comment: "Manual draft — replace the example member with real tickers and refine the rationales.",
    members: [
      { symbol: SEG_PLACEHOLDER_SYM, rationale: "Why this company belongs in the segment." },
    ],
  };
}

// A draft is good enough to continue only when the slug is set and the JSON
// parses into an object with at least one member carrying a real ticker (not
// the manual-template placeholder). This gates Approve & continue so you can
// never advance to Deep Research on an empty or skeleton segment.
export function segDraftValid(slug: string, rawJson: string): boolean {
  if (!String(slug || "").trim()) return false;
  const raw = String(rawJson || "").trim();
  if (!raw) return false;
  let def: unknown;
  try { def = JSON.parse(raw); } catch { return false; }
  if (!def || typeof def !== "object" || Array.isArray(def)) return false;
  const members = Array.isArray((def as { members?: unknown }).members)
    ? (def as { members: unknown[] }).members : [];
  if (!members.length) return false;
  return members.every((m) => {
    const sym = m && typeof (m as { symbol?: unknown }).symbol === "string"
      ? (m as { symbol: string }).symbol.trim() : "";
    return !!sym && sym.toUpperCase() !== SEG_PLACEHOLDER_SYM;
  });
}

// Most recent saved run for `seg` that actually has a report on disk. Stems are
// `${seg}-YYYY-MM-DD`; the date check stops a segment like "ai" from matching
// "ai-software-...". Lexical desc sort on the stem orders by date newest-first.
export function latestReportForSegment(
  runs: DeepRun[] | null | undefined, seg: string | null | undefined,
): DeepRun | null {
  if (!seg) return null;
  const prefix = seg + "-";
  const matches = (runs || [])
    .filter((r) => r.files && r.files.report && r.stem.startsWith(prefix)
      && /^\d{4}-\d{2}-\d{2}$/.test(r.stem.slice(prefix.length)))
    .sort((a, b) => (a.stem < b.stem ? 1 : -1));
  return matches[0] || null;
}

// Data-quality / source-strength -> tag color. Most rows are "INFO" (neutral);
// only escalate color when the gate flags something worth a second look.
export function reviewTagClass(v: unknown): string {
  const s = String(v).toLowerCase();
  if (s.includes("block") || s.includes("bad") || s.includes("conflict")) return "bad";
  if (s.includes("warn") || s.includes("weak")) return "warn";
  if (s.includes("ok") || s.includes("good") || s.includes("primary") || s.includes("strong")) return "good";
  return "";
}
