// Tests for analysis colour-coding: the verdict gets a stance pill and the
// Bull/Bear/trigger sections get tinted rails so a reader can find the call,
// the upside, and the downside at a glance.
import { describe, expect, it } from "vitest";
import { mdToHtml } from "../src/analyses";
import { decorateAnalysis, decorateSources } from "../src/deepdive/decorate";

function render(md: string): HTMLElement {
  const root = document.createElement("div");
  root.className = "prose analysis-prose";
  root.innerHTML = mdToHtml(md);
  decorateAnalysis(root);
  return root;
}

const SAMPLE = [
  "## Verdict",
  "Accumulate on weakness; the risk/reward is favourable.",
  "## Bull case",
  "Margins are expanding and the backlog is growing.",
  "## Bear case",
  "Cyclical demand could roll over and compress multiples.",
  "## What would change the thesis",
  "A sustained order slowdown or a dividend cut.",
].join("\n\n");

describe("decorateAnalysis", () => {
  it("pins a stance pill on the verdict heading", () => {
    const root = render(SAMPLE);
    const pill = root.querySelector(".verdict-pill");
    expect(pill).not.toBeNull();
    expect(pill?.classList.contains("v-good")).toBe(true);
    expect(pill?.textContent).toBe("Accumulate");
  });

  it("wraps the bull section in a green rail", () => {
    const root = render(SAMPLE);
    const bull = root.querySelector(".case-section.case-bull");
    expect(bull).not.toBeNull();
    expect(bull?.querySelector(".case-head")?.textContent).toContain("Bull case");
    expect(bull?.textContent).toContain("backlog is growing");
  });

  it("wraps the bear section in a red rail", () => {
    const root = render(SAMPLE);
    const bear = root.querySelector(".case-section.case-bear");
    expect(bear).not.toBeNull();
    expect(bear?.textContent).toContain("compress multiples");
  });

  it("flags the thesis-trigger section", () => {
    const root = render(SAMPLE);
    expect(root.querySelector(".case-section.case-flip")).not.toBeNull();
  });

  it("never tints the verdict section as a case", () => {
    const root = render(SAMPLE);
    const verdictHead = [...root.querySelectorAll("h1,h2,h3,h4,h5,h6")].find((h) => /verdict/i.test(h.textContent || ""));
    expect(verdictHead).toBeTruthy();
    expect(verdictHead?.closest(".case-section")).toBeNull();
  });

  it("is idempotent", () => {
    const root = render(SAMPLE);
    decorateAnalysis(root);
    expect(root.querySelectorAll(".case-section.case-bull").length).toBe(1);
    expect(root.querySelectorAll(".verdict-pill").length).toBe(1);
  });
});

describe("inline stance words", () => {
  const TRIGGERS = [
    "## What would change the thesis",
    "Upgrade to Accumulate if growth turns positive while margins hold or improve.",
    "Downgrade to Trim on a rally to the 52-week high.",
    "Downgrade to Avoid if the cheapness disappears.",
  ].join("\n\n");

  function stanceFor(root: HTMLElement, word: string): string | undefined {
    const span = [...root.querySelectorAll(".verdict-stance")].find((s) => s.textContent === word);
    return span?.className;
  }

  it("colours each capitalised decision word with its stance class", () => {
    const root = render(TRIGGERS);
    expect(stanceFor(root, "Accumulate")).toContain("v-good");
    expect(stanceFor(root, "Trim")).toContain("v-warn");
    expect(stanceFor(root, "Avoid")).toContain("v-bad");
  });

  it("does NOT colour the lowercase verb 'hold' (margins hold or improve)", () => {
    const root = render(TRIGGERS);
    const colouredHold = [...root.querySelectorAll(".verdict-stance")].some((s) => s.textContent === "hold");
    expect(colouredHold).toBe(false);
  });

  it("is idempotent across decision words", () => {
    const root = render(TRIGGERS);
    decorateAnalysis(root);
    const accs = [...root.querySelectorAll(".verdict-stance")].filter((s) => s.textContent === "Accumulate");
    expect(accs.length).toBe(1);
  });
});

function renderWithSources(md: string, rec: unknown): HTMLElement {
  const root = document.createElement("div");
  root.className = "prose analysis-prose";
  root.innerHTML = mdToHtml(md);
  decorateSources(root, rec as Parameters<typeof decorateSources>[1]);
  return root;
}

describe("decorateSources", () => {
  const NONE_MD = "## Sources\n\nNone — analysis is from the provided data only.";
  const rec = { as_of: "2026-06-14T10:00:00Z", sources: { yahoo: true, sec_edgar: true, fmp: false } };

  it("replaces a bare 'None' with the real provenance", () => {
    const root = renderWithSources(NONE_MD, rec);
    const note = root.querySelector(".sources-provenance");
    expect(note).not.toBeNull();
    expect(note?.textContent).toContain("Yahoo Finance");
    expect(note?.textContent).toContain("SEC EDGAR");
    expect(note?.textContent).not.toContain("FMP");
    expect(root.textContent).not.toContain("analysis is from the provided data only");
  });

  it("surfaces snapshot freshness", () => {
    const root = renderWithSources(NONE_MD, rec);
    expect(root.querySelector(".sources-provenance .fresh-note")).not.toBeNull();
  });

  it("leaves a real web citation list untouched", () => {
    const md = "## Sources\n\n- [Reuters](https://reuters.com/x) — guidance cut";
    const root = renderWithSources(md, rec);
    expect(root.querySelector(".sources-provenance")).toBeNull();
    expect(root.querySelector('a[href="https://reuters.com/x"]')).not.toBeNull();
  });

  it("falls back gracefully when no providers are flagged", () => {
    const root = renderWithSources(NONE_MD, { as_of: null, sources: {} });
    expect(root.querySelector(".sources-provenance")?.textContent).toContain("cached data snapshot");
  });
});
