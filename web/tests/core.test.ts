import { describe, expect, it } from "vitest";

import { decisionPill } from "../src/core";

describe("decisionPill", () => {
  it("spaces every underscore, not just the first", () => {
    // The bug this consolidation fixes: `.replace('_', ' ')` only spaced the
    // first underscore, so `do_not_add` used to render "do not_add".
    expect(decisionPill("do_not_add")).toContain(">do not add<");
    expect(decisionPill("add_candidate")).toContain(">add candidate<");
  });

  it("colours from the raw token", () => {
    expect(decisionPill("accumulate")).toContain("decision-pill good");
    expect(decisionPill("trim")).toContain("decision-pill bad");
    expect(decisionPill("watch")).toContain("decision-pill warn");
  });

  it("uses the fallback label when the decision is absent", () => {
    expect(decisionPill(null, { fallback: "research" })).toContain(">research<");
    expect(decisionPill("", { fallback: "research" })).toContain(">research<");
  });

  it("renders an empty label with no decision and no fallback", () => {
    expect(decisionPill(undefined)).toBe('<span class="decision-pill muted"></span>');
  });

  it("escapes the token", () => {
    expect(decisionPill("<b>x")).toContain("&lt;b&gt;x");
  });
});
