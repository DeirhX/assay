// Tests for URL <-> nav-state mapping: deep links are the app's persistence
// layer, so parse/serialize must round-trip and reject junk.
import { beforeEach, describe, expect, it } from "vitest";
import { isSegmentSlug, navFromUrl, parseSearch, urlForNav } from "../src/shell";

const setUrl = (search: string) => {
  window.history.replaceState({}, "", "/" + (search ? "?" + search : ""));
};

describe("navFromUrl", () => {
  beforeEach(() => setUrl(""));

  it("defaults to the guided Plan (strategy) view", () => {
    expect(navFromUrl()).toEqual({ view: "strategy", ticker: "", segment: "", run: "" });
  });

  it("parses a full deep link", () => {
    setUrl("view=segment&segment=semiconductors");
    const nav = navFromUrl();
    expect(nav.view).toBe("segment");
    expect(nav.segment).toBe("semiconductors");
  });

  it("rejects unknown views", () => {
    setUrl("view=adminpanel");
    expect(navFromUrl().view).toBe("strategy");
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
    const url = urlForNav({ view: "strategy", ticker: "", segment: "", run: "" });
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
