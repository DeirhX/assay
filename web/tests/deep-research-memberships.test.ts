import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  deepAnalysisRelationLabel,
  renderRelatedDeepAnalyses,
} from "../src/deepdive/analysis-card";


describe("ticker deep-analysis memberships", () => {
  beforeEach(() => {
    document.body.innerHTML = "";
  });

  it("explains member and mention relationships plainly", () => {
    expect(deepAnalysisRelationLabel("member+mentioned"))
      .toBe("segment member · discussed");
    expect(deepAnalysisRelationLabel("member")).toBe("segment member");
    expect(deepAnalysisRelationLabel("mentioned")).toBe("mentioned in report");
  });

  it("renders every overlapping sector analysis as a direct link", () => {
    const open = vi.fn();
    const view = renderRelatedDeepAnalyses([
      {
        stem: "ai-infrastructure-2026-07-02",
        title: "AI Infrastructure",
        date: "2026-07-02",
        source_count: 5,
        relationship: "mentioned",
        has_review: true,
      },
      {
        stem: "semiconductors-2026-07-01",
        title: "Semiconductors",
        date: "2026-07-01",
        source_count: 3,
        relationship: "member+mentioned",
      },
    ], open);

    expect(view).not.toBeNull();
    document.body.appendChild(view!);
    expect(view!.textContent).toContain("Appears in 2 sector analyses");
    expect(view!.textContent).toContain("mentioned in report");
    expect(view!.textContent).toContain("segment member · discussed");
    expect(view!.textContent).toContain("5 sources · reviewed");

    view!.querySelectorAll<HTMLButtonElement>("button")[1].click();
    expect(open).toHaveBeenCalledWith("semiconductors-2026-07-01");
  });

  it("omits the section when no sector report contains the ticker", () => {
    expect(renderRelatedDeepAnalyses([], vi.fn())).toBeNull();
  });

  it("keeps a heavily overlapping ticker compact until expanded", () => {
    const view = renderRelatedDeepAnalyses(
      Array.from({ length: 7 }, (_, index) => ({
        stem: `sector-${index}-2026-07-01`,
        title: `Sector ${index}`,
        relationship: "mentioned" as const,
      })),
      vi.fn(),
    )!;
    document.body.appendChild(view);

    expect(view.querySelectorAll(".dr-related-row")).toHaveLength(4);
    const more = view.querySelector<HTMLButtonElement>(".dr-related-more")!;
    expect(more.textContent).toBe("Show 3 more sector analyses");
    more.click();
    expect(view.querySelectorAll(".dr-related-row")).toHaveLength(7);
    expect(view.querySelector(".dr-related-more")).toBeNull();
  });
});
