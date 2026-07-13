// Tests for URL <-> nav-state mapping: deep links are the app's persistence
// layer, so parse/serialize must round-trip and reject junk.
import { beforeEach, describe, expect, it } from "vitest";
import {
  isSegmentSlug, navFromUrl, parseSearch, pushNav, replaceViewState, urlForNav,
} from "../src/shell";

const setUrl = (search: string) => {
  window.history.replaceState({}, "", "/" + (search ? "?" + search : ""));
};

describe("navFromUrl", () => {
  beforeEach(() => setUrl(""));

  it("defaults to the Today cockpit", () => {
    expect(navFromUrl()).toEqual(expect.objectContaining({
      view: "today", ticker: "", segment: "", run: "", tab: "", step: "",
    }));
  });

  it("parses a full deep link", () => {
    setUrl("view=segment&segment=semiconductors");
    const nav = navFromUrl();
    expect(nav.view).toBe("segment");
    expect(nav.segment).toBe("semiconductors");
  });

  it("accepts the stable Orders pipeline destination", () => {
    setUrl("view=orders");
    expect(navFromUrl().view).toBe("orders");
  });

  it("rejects unknown views", () => {
    setUrl("view=adminpanel");
    expect(navFromUrl().view).toBe("today");
  });

  it("uppercases and trims tickers", () => {
    setUrl("view=deepdive&ticker=%20amd%20");
    expect(navFromUrl().ticker).toBe("AMD");
  });

  it("recovers a deep link whose separators got percent-encoded", () => {
    // External encoders (chat/markdown) turn "?view=strategy&run=X" into
    // "?view%3Dstrategy%26run%3DX", which would otherwise parse as one junk key.
    setUrl("view%3Dstrategy%26run%3D18f100b5");
    const nav = navFromUrl();
    expect(nav.view).toBe("strategy");
    expect(nav.run).toBe("18f100b5");
  });
});

describe("parseSearch", () => {
  it("decodes a fully percent-encoded query", () => {
    const p = parseSearch("?view%3Dstrategy%26run%3D18f100b5");
    expect(p.get("view")).toBe("strategy");
    expect(p.get("run")).toBe("18f100b5");
  });

  it("leaves a normal query untouched", () => {
    const p = parseSearch("?view=segment&segment=semis");
    expect(p.get("view")).toBe("segment");
    expect(p.get("segment")).toBe("semis");
  });

  it("does not over-decode a value that legitimately contains %3D", () => {
    // A real '=' pair is present, so we must NOT decode the whole string and
    // mangle an encoded '=' that lives inside a value.
    const p = parseSearch("?ticker=AMD&q=a%3Db");
    expect(p.get("ticker")).toBe("AMD");
    expect(p.get("q")).toBe("a=b");
  });
});

describe("urlForNav", () => {
  beforeEach(() => setUrl(""));

  it("omits the default view from the URL", () => {
    const url = urlForNav({ view: "today", ticker: "", segment: "", run: "" });
    expect(url.search).toBe("");
  });

  it("round-trips through navFromUrl", () => {
    const nav = {
      view: "pipeline", ticker: "", segment: "semis", run: "semis-2026-06-01",
      tab: "", step: "4", segmode: "existing", repmode: "current", stage: "",
      filter: "", sort: "", range: "", benchmark: "", soon: "",
      sec: "",
    };
    window.history.replaceState({}, "", urlForNav(nav));
    expect(navFromUrl()).toEqual(nav);
  });

  it("serializes view-scoped tabs and controls", () => {
    const url = urlForNav({
      view: "trade", tab: "orders", sort: "age-desc", range: "1y",
    });
    expect(url.searchParams.get("tab")).toBe("orders");
    expect(url.searchParams.get("sort")).toBe("age-desc");
    expect(url.searchParams.get("range")).toBe("1y");
  });

  it("serializes the ticker uppercased", () => {
    const url = urlForNav({ view: "deepdive", ticker: "amd" });
    expect(url.searchParams.get("ticker")).toBe("AMD");
  });
});

describe("view-scoped URL state", () => {
  beforeEach(() => setUrl("view=trade&tab=orders&sort=age-desc"));

  it("retains in-view state when one control changes", () => {
    replaceViewState({ tab: "review" });
    const nav = navFromUrl();
    expect(nav.tab).toBe("review");
    expect(nav.sort).toBe("age-desc");
  });

  it("clears prior-view state when navigating elsewhere", () => {
    pushNav({ view: "risk", range: "3y" });
    const nav = navFromUrl();
    expect(nav.view).toBe("risk");
    expect(nav.range).toBe("3y");
    expect(nav.tab).toBe("");
    expect(nav.sort).toBe("");
  });
});

describe("isSegmentSlug", () => {
  it("accepts server-style slugs", () => {
    expect(isSegmentSlug("semiconductors")).toBe(true);
    expect(isSegmentSlug("fintech-payments")).toBe(true);
    expect(isSegmentSlug("ai2")).toBe(true);
  });

  it("rejects junk that is clearly not a slug", () => {
    expect(isSegmentSlug("")).toBe(false);
    expect(isSegmentSlug("Failed to fetch")).toBe(false);
    expect(isSegmentSlug("-leading-dash")).toBe(false);
    expect(isSegmentSlug("UPPER")).toBe(false);
    expect(isSegmentSlug(undefined)).toBe(false);
  });
});
