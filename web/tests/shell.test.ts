// Tests for URL <-> nav-state mapping: deep links are the app's persistence
// layer, so parse/serialize must round-trip and reject junk.
import { beforeEach, describe, expect, it } from "vitest";
import { isSegmentSlug, navFromUrl, urlForNav } from "../src/shell";

const setUrl = (search: string) => {
  window.history.replaceState({}, "", "/" + (search ? "?" + search : ""));
};

describe("navFromUrl", () => {
  beforeEach(() => setUrl(""));

  it("defaults to the deepdive view", () => {
    expect(navFromUrl()).toEqual({ view: "deepdive", ticker: "", segment: "", run: "" });
  });

  it("parses a full deep link", () => {
    setUrl("view=segment&segment=semiconductors");
    const nav = navFromUrl();
    expect(nav.view).toBe("segment");
    expect(nav.segment).toBe("semiconductors");
  });

  it("rejects unknown views", () => {
    setUrl("view=adminpanel");
    expect(navFromUrl().view).toBe("deepdive");
  });

  it("uppercases and trims tickers", () => {
    setUrl("view=deepdive&ticker=%20amd%20");
    expect(navFromUrl().ticker).toBe("AMD");
  });
});

describe("urlForNav", () => {
  beforeEach(() => setUrl(""));

  it("omits the default view from the URL", () => {
    const url = urlForNav({ view: "deepdive", ticker: "", segment: "", run: "" });
    expect(url.search).toBe("");
  });

  it("round-trips through navFromUrl", () => {
    const nav = { view: "pipeline", ticker: "", segment: "semis", run: "semis-2026-06-01" };
    window.history.replaceState({}, "", urlForNav(nav));
    expect(navFromUrl()).toEqual(nav);
  });

  it("serializes the ticker uppercased", () => {
    const url = urlForNav({ view: "deepdive", ticker: "amd" });
    expect(url.searchParams.get("ticker")).toBe("AMD");
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
