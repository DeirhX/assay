import { describe, expect, it } from "vitest";
import {
  bandZoneGeom,
  connectorGeom,
  positionTrackHtml,
  POSITION_TRACK_SEL,
} from "../src/band-viz";

describe("positionTrackHtml", () => {
  it("draws zone, connector, and both marks for a static trade preview", () => {
    const { html, refs } = positionTrackHtml({
      scaleMax: 10,
      band: { low: 5, high: 7 },
      current: 8.2,
      projected: 6.9,
      ariaLabel: "AAPL: 8.2% to 6.9% vs band 5–7%",
      opts: { connTone: "auto", inBand: true },
    });
    expect(html).toContain(`class="${POSITION_TRACK_SEL.track}"`);
    expect(html).toContain(POSITION_TRACK_SEL.zone);
    expect(html).toContain("reb-conn sell");
    expect(html).toContain("reb-cur-mark");
    expect(html).toContain("reb-proj-mark in");
    expect(html).toContain('title="current 8.20%"');
    expect(html).toContain('title="projected 6.90%"');
    expect(refs.track).toBe("reb-track");
  });

  it("omits projected tick and connector for unchanged target-state rows", () => {
    const { html } = positionTrackHtml({
      scaleMax: 10,
      band: { low: 4, high: 6 },
      current: 5,
      projected: 5,
      ariaLabel: "STILL: now 5.0%, after 5.0%",
      opts: { showProjected: false, showConn: false },
    });
    expect(html).toContain("reb-cur-mark");
    expect(html).not.toContain("reb-proj-mark");
    expect(html).not.toContain("reb-conn");
  });

  it("supports draggable planner rows with axis legend", () => {
    const { html, geom } = positionTrackHtml({
      scaleMax: 15,
      band: { low: 4, high: 6 },
      current: 5,
      projected: 5.5,
      ariaLabel: "NVDA: current 5.0%, target band 4.0 to 6.0%",
      opts: {
        role: "group",
        connTone: "none",
        showConn: true,
        showAxis: true,
        inBand: true,
      },
    });
    expect(html).toContain('role="group"');
    expect(html).toContain("reb-axis");
    expect(html).toContain("reb-conn");
    expect(html).not.toContain("reb-conn buy");
    expect(geom.curP).toBeCloseTo(33.3, 1);
    expect(geom.projP).toBeCloseTo(36.7, 1);
  });
});

describe("bandZoneGeom / connectorGeom", () => {
  it("keeps a hairline band visible and spans connectors between ticks", () => {
    expect(bandZoneGeom({ low: 5, high: 5 }, 20).width).toBe(1.5);
    expect(connectorGeom(20, 35)).toEqual({ left: 20, width: 15 });
  });
});
