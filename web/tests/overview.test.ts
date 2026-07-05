// Tests for the Today cockpit's pure HTML builders: the next-step banner's
// tone/CTA, card visibility rules (in-flight cards hide when empty), and the
// research triage/queue rendering.
import { describe, expect, it } from "vitest";
import {
  basketTriageCard, draftCard, journalCard, nextStepHtml, planCard,
  segmentsCard, snapshotCard, stagedBasketCard,
} from "../src/overview";
import type { Overview } from "../src/overview";

type Research = Overview["research"];

describe("nextStepHtml", () => {
  it("renders an urgent step with a CTA targeting its view", () => {
    const html = nextStepHtml({ id: "commit-draft", view: "working-draft", label: "Review the working draft", reason: "3 uncommitted changes" });
    expect(html).toContain("today-next-warn");
    expect(html).toContain('data-goto="working-draft"');
    expect(html).toContain("3 uncommitted changes");
  });

  it("all-clear has no CTA button", () => {
    const html = nextStepHtml({ id: "all-clear", view: "rebalance", label: "All caught up", reason: "Everything in band." });
    expect(html).toContain("today-next-ok");
    expect(html).not.toContain("data-goto");
  });

  it("a research-queue step carries the ticker for the deep-dive", () => {
    const html = nextStepHtml({ id: "research-queue", view: "deepdive", symbol: "FIND", label: "Look at FIND", reason: "top score" });
    expect(html).toContain('data-ticker="FIND"');
  });
});

describe("snapshotCard", () => {
  it("flags a stale snapshot and offers a resync", () => {
    const html = snapshotCard({ exists: true, age_days: 12, stale: true, positions: 30 });
    expect(html).toContain("today-warn");
    expect(html).toContain('data-action="resync"');
    expect(html).toContain("12 days ago");
  });

  it("points a missing snapshot at Setup", () => {
    const html = snapshotCard({ exists: false, positions: 0 });
    expect(html).toContain("today-bad");
    expect(html).toContain('data-goto="setup"');
  });
});

describe("planCard", () => {
  it("summarises drift with buy/trim/review counts", () => {
    const html = planCard({ rows: 10, out_of_band: 3, buy: 2, trim: 1, review: 0, actionable: 3, conflicts: 1, gates_waiting: 1, gates_open: 0, untargeted: 2, untargeted_pct: 5.5 });
    expect(html).toContain("2 buy, 1 trim, 0 review");
    expect(html).toContain("conflict");
    expect(html).toContain('data-goto="rebalance"');
  });

  it("routes a missing model to the Planner/Optimizer", () => {
    const html = planCard(null);
    expect(html).toContain('data-goto="strategy"');
    expect(html).toContain('data-goto="optimizer"');
  });

  it("flags a breached cash band", () => {
    const html = planCard({
      rows: 5, out_of_band: 0, buy: 0, trim: 0, review: 0, actionable: 0,
      conflicts: 0, gates_waiting: 0, gates_open: 0, untargeted: 0,
      cash: { pct_of_nav: 1.4, target_pct: 5, low: 3, high: 7, status: "BELOW" },
    });
    expect(html).toContain("Cash is 1.4% of NAV");
    expect(html).toContain("under its 3–7% band");
  });
});

describe("in-flight cards hide when there is nothing in flight", () => {
  it("draft, staged basket, and journal render empty strings at zero", () => {
    expect(draftCard({ has_draft: false, pending: 0 })).toBe("");
    expect(stagedBasketCard({ count: 0, buys: 0, sells: 0, total_abs_czk: 0 })).toBe("");
    expect(journalCard({ total: 5, pending_outcomes: 0, review_due: 0 })).toBe("");
  });

  it("a pending draft renders a primary commit CTA", () => {
    const html = draftCard({ has_draft: true, pending: 4 });
    expect(html).toContain("4 pending");
    expect(html).toContain('data-goto="working-draft"');
  });
});

describe("research lane", () => {
  const research = (over: Partial<Research>): Research => ({
    basket: { count: 0, unresearched_count: 0, aging_count: 0, unresearched: [] },
    segments: { total: 0, cached: 0, stale: [], stale_count: 0 },
    queue: [],
    ...over,
  });

  it("lists unresearched picks with aging highlighted", () => {
    const html = basketTriageCard(research({
      basket: {
        count: 3, unresearched_count: 2, aging_count: 1,
        unresearched: [
          { symbol: "OLDP", tier: "curious", age_days: 47 },
          { symbol: "NEWP", tier: "want", age_days: 2 },
        ],
      },
    }));
    expect(html).toContain("OLDP");
    expect(html).toContain("47d");
    expect(html).toContain("1 sitting for 30+ days");
  });

  it("stale segments render re-open buttons with their slug", () => {
    const html = segmentsCard(research({
      segments: { total: 2, cached: 2, stale_count: 1, stale: [{ name: "semis", title: "Semiconductors", age_days: 60 }] },
    }));
    expect(html).toContain('data-segment="semis"');
    expect(html).toContain("60d old");
  });
});
