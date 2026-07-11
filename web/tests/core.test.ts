import { afterEach, describe, expect, it, vi } from "vitest";

import { api, decisionPill, setErrorSink } from "../src/core";

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

describe("api timeout", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    setErrorSink(() => undefined);
  });

  it("aborts a stalled request and reports a timeout instead of hanging", async () => {
    // A fetch that never answers until the AbortController fires -- exactly a
    // wedged IBKR gateway that accepts the socket but never responds.
    vi.stubGlobal("fetch", (_url: string, opt: RequestInit) => new Promise((_resolve, reject) => {
      opt.signal?.addEventListener("abort", () => {
        const e = new Error("aborted");
        (e as { name: string }).name = "AbortError";
        reject(e);
      });
    }));
    await expect(api("/api/trade/preview", "POST", {}, { timeoutMs: 20 }))
      .rejects.toThrow(/timed out after/);
  });

  it("resolves normally and clears the timer when the server answers", async () => {
    vi.stubGlobal("fetch", () => Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ ok: 1 }),
    } as Response));
    await expect(api("/api/trade/status", "GET", null, { timeoutMs: 5_000 }))
      .resolves.toEqual({ ok: 1 });
  });

  it("does not abort when no timeout is set", async () => {
    let sawSignal = false;
    vi.stubGlobal("fetch", (_url: string, opt: RequestInit) => {
      sawSignal = opt.signal != null;
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) } as Response);
    });
    await api("/api/holdings");
    expect(sawSignal).toBe(false);
  });

  it("can suppress expected background-poll errors", async () => {
    const sink = vi.fn();
    setErrorSink(sink);
    vi.stubGlobal("fetch", () => Promise.resolve({
      ok: false,
      status: 502,
      json: () => Promise.resolve({ error: "bad response" }),
    } as Response));

    await expect(api(
      "/api/jobs", "GET", null, { reportError: false },
    )).rejects.toThrow("bad response");
    expect(sink).not.toHaveBeenCalled();
  });
});
