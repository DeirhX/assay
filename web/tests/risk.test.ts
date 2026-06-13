// Tests for the risk lens helper and that the new tabs are routable views.
import { beforeEach, describe, expect, it } from "vitest";
import { corrColor } from "../src/risk";
import { navFromUrl } from "../src/shell";

const setUrl = (search: string) => {
  window.history.replaceState({}, "", "/" + (search ? "?" + search : ""));
};

describe("corrColor", () => {
  it("is transparent for missing correlation", () => {
    expect(corrColor(null)).toBe("transparent");
    expect(corrColor(undefined)).toBe("transparent");
  });

  it("paints positive correlation red and scales with magnitude", () => {
    expect(corrColor(1)).toContain("220, 80, 70");
    // Stronger correlation -> higher alpha (louder).
    const lo = corrColor(0.2);
    const hi = corrColor(0.9);
    const alpha = (s: string) => Number(s.match(/, ([0-9.]+)\)$/)?.[1]);
    expect(alpha(hi)).toBeGreaterThan(alpha(lo));
  });

  it("paints a genuine hedge (negative correlation) green", () => {
    expect(corrColor(-1)).toContain("70, 170, 110");
  });
});

describe("new views are routable", () => {
  beforeEach(() => setUrl(""));

  it("accepts the risk view", () => {
    setUrl("view=risk");
    expect(navFromUrl().view).toBe("risk");
  });

  it("accepts the journal view", () => {
    setUrl("view=journal");
    expect(navFromUrl().view).toBe("journal");
  });
});
