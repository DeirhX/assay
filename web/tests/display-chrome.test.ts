import { describe, expect, it } from "vitest";
import { analyticsSection, caveatBanner, metaStrip } from "../src/display/chrome";

describe("display/chrome helpers", () => {
  it("metaStrip wraps pre-built span HTML in reb-meta", () => {
    const meta = metaStrip(["window 1y", "source cache"]);
    expect(meta.className).toBe("reb-meta");
    expect(meta.innerHTML).toContain("<span>window 1y</span>");
    expect(meta.innerHTML).toContain("<span>source cache</span>");
  });

  it("caveatBanner matches the risk/attribution markup", () => {
    const banner = caveatBanner(["thin sample", "stale prices"], { always: true })!;
    expect(banner.className).toBe("banner banner-warn risk-caveat");
    expect(banner.innerHTML).toContain("Read this before you trust a number below.");
    expect(banner.innerHTML).toContain("<li>thin sample</li>");
  });

  it("caveatBanner stays absent unless always or caveats exist", () => {
    expect(caveatBanner([])).toBeNull();
    expect(caveatBanner([], { always: true })?.querySelector("ul")?.children.length).toBe(0);
  });

  it("analyticsSection builds a titled risk-section with optional hint", () => {
    const sec = analyticsSection("Portfolio value", "Hover markers for fills.");
    expect(sec.className).toBe("risk-section");
    expect(sec.querySelector("h3")?.textContent).toBe("Portfolio value");
    expect(sec.querySelector("p.hint")?.textContent).toBe("Hover markers for fills.");
  });
});
