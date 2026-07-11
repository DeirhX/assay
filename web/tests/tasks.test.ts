// Tests for the central Task Center: the kind -> result-view routing map (the
// crux of "navigate to a task's result"), the human labels, and the panel's
// active/recent grouping + newest-first ordering fed from GET /api/jobs.
import { afterEach, describe, expect, it, vi } from "vitest";
import type { JobListing } from "../src/api-types";
import { kindLabel, navForTask, startTaskCenter, taskTitle, toggleTaskPanel } from "../src/tasks";

const mk = (p: Partial<JobListing>): JobListing =>
  ({ id: "x", kind: "x", state: "done", message: "", cancelled: false, ...p } as JobListing);

describe("navForTask", () => {
  it("routes ticker analysis + Q&A to the deep dive", () => {
    expect(navForTask(mk({ kind: "ticker_analysis", symbol: "AMD" }))).toEqual({ view: "deepdive", ticker: "AMD" });
    expect(navForTask(mk({ kind: "ticker_qa", symbol: "TSM" }))).toEqual({ view: "deepdive", ticker: "TSM" });
  });

  it("routes a finished deep run to the analyses reader by artifact stem", () => {
    expect(navForTask(mk({ kind: "deep_research", artifact: { stem: "ai-2026-06-01" } })))
      .toEqual({ view: "analyses", run: "ai-2026-06-01" });
    expect(navForTask(mk({ kind: "import", artifact: { stem: "space-2026-06-02" } })))
      .toEqual({ view: "analyses", run: "space-2026-06-02" });
  });

  it("falls back to the pipeline segment when a deep run has no stem yet", () => {
    expect(navForTask(mk({ kind: "deep_research", segment: "ai-infra" })))
      .toEqual({ view: "pipeline", segment: "ai-infra" });
  });

  it("routes deep-report Q&A by stem", () => {
    expect(navForTask(mk({ kind: "deep_qa", stem: "ai-2026-06-01" })))
      .toEqual({ view: "analyses", run: "ai-2026-06-01" });
  });

  it("routes a segment draft to the pipeline by its result slug", () => {
    expect(navForTask(mk({ kind: "segment_draft", result: { slug: "nuclear-supply" } })))
      .toEqual({ view: "pipeline", segment: "nuclear-supply" });
  });

  it("routes IBKR jobs to their portfolio views", () => {
    expect(navForTask(mk({ kind: "ibkr_sync" }))).toEqual({ view: "holdings" });
    expect(navForTask(mk({ kind: "ibkr_history" }))).toEqual({ view: "history" });
    expect(navForTask(mk({ kind: "ibkr_sectors" }))).toEqual({ view: "history" });
  });

  it("routes a guided strategy run by run_id", () => {
    expect(navForTask(mk({ kind: "strategy", run_id: "run-123" })))
      .toEqual({ view: "strategy", run: "run-123" });
  });

  it("returns null for non-navigable or incomplete jobs", () => {
    expect(navForTask(mk({ kind: "login" }))).toBeNull();
    expect(navForTask(mk({ kind: "ticker_analysis" }))).toBeNull();  // no symbol
    expect(navForTask(mk({ kind: "strategy" }))).toBeNull();         // no run_id
    expect(navForTask(mk({ kind: "deep_qa" }))).toBeNull();          // no stem
  });
});

describe("kindLabel / taskTitle", () => {
  it("labels known kinds and passes unknown kinds through", () => {
    expect(kindLabel("deep_research")).toBe("Deep research");
    expect(kindLabel("ibkr_sync")).toBe("IBKR sync");
    expect(kindLabel("totally_new_kind")).toBe("totally_new_kind");
  });

  it("composes '<kind> · <subject>' and degrades to the bare label", () => {
    expect(taskTitle(mk({ kind: "ticker_analysis", symbol: "AMD" }))).toBe("Analysis \u00b7 AMD");
    expect(taskTitle(mk({ kind: "deep_research", artifact: { stem: "ai-2026-06-01" } }))).toBe("Deep research \u00b7 ai-2026-06-01");
    expect(taskTitle(mk({ kind: "ibkr_sync" }))).toBe("IBKR sync");
  });
});

describe("task panel scope", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("shows only in-progress work and sends completed history to Activity", async () => {
    const jobs: JobListing[] = [
      mk({ id: "j3", kind: "ticker_analysis", symbol: "AMD", state: "running", message: "thinking", created_at: "2026-06-16T10:00:03+00:00", updated_at: "2026-06-16T10:00:03+00:00" }),
      mk({ id: "j2", kind: "deep_research", state: "done", artifact: { stem: "ai-2026-06-16" }, created_at: "2026-06-16T10:00:02+00:00", updated_at: "2026-06-16T10:00:02+00:00" }),
      mk({ id: "j1", kind: "ibkr_sync", state: "error", error: "boom", created_at: "2026-06-16T10:00:01+00:00", updated_at: "2026-06-16T10:00:01+00:00" }),
    ];
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => ({ jobs }) })));

    startTaskCenter();          // begins the poller; first poll runs immediately
    toggleTaskPanel(true);      // open the panel so it renders

    const list = document.getElementById("task-list") as HTMLElement;
    await vi.waitFor(() => expect(list.querySelectorAll(".task-item").length).toBe(1));

    const items = [...list.querySelectorAll(".task-item")];
    expect(items.map((i) => i.getAttribute("data-open"))).toEqual(["j3"]);
    expect((list.querySelector(".task-group-head") as HTMLElement).textContent).toBe("In progress");
    expect(list.textContent).toContain("Completed work is kept in the durable Activity log");
    // the running ticker analysis routes to a result, so its row is navigable
    expect(items[0].classList.contains("navigable")).toBe(true);
  });
});
