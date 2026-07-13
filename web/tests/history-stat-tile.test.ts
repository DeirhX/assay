import { describe, expect, it } from "vitest";
import { esc, sensitive, statTile } from "../src/core";
import { ccyTag } from "../src/history/format";

describe("history stat tiles", () => {
  it("matches the former local card builder markup and classes", () => {
    const legacy = (label: string, valueHtml: string, cls = "muted") => {
      const c = document.createElement("div");
      c.className = "risk-stat";
      c.innerHTML =
        `<span class="risk-stat-k">${esc(label)}</span>` +
        `<span class="risk-stat-v ${esc(cls)}">${valueHtml}</span>`;
      return c;
    };

    const value = sensitive("1,234", "net asset value") + ccyTag("CZK");
    expect(legacy("Latest NAV", value).outerHTML).toBe(
      statTile("Latest NAV", value, { family: "risk-stat", html: true, cls: "muted" }).outerHTML,
    );
    expect(legacy("Change", value, "good").outerHTML).toBe(
      statTile("Change", value, { family: "risk-stat", html: true, cls: "good" }).outerHTML,
    );
  });
});
