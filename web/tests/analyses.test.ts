// Tests for the security-sensitive markdown renderer and the ticker
// auto-linker. The report text comes from Perplexity (untrusted), so the
// escape-first guarantees here are load-bearing.
import { beforeEach, describe, expect, it } from "vitest";
import { linkifyTickers, mdToHtml } from "../src/analyses";
import { state } from "../src/core";

describe("mdToHtml", () => {
  it("escapes raw HTML so script never survives", () => {
    const out = mdToHtml('<script>alert("pwn")</script>');
    expect(out).not.toContain("<script>");
    expect(out).toContain("&lt;script&gt;");
  });

  it("escapes HTML inside emphasis and code spans", () => {
    const out = mdToHtml("**<b>bold</b>** and `<img onerror=x>`");
    expect(out).toContain("<strong>&lt;b&gt;bold&lt;/b&gt;</strong>");
    expect(out).toContain("<code>&lt;img onerror=x&gt;</code>");
  });

  it("allows only http(s) links", () => {
    const ok = mdToHtml("[good](https://example.com/x)");
    expect(ok).toContain('<a href="https://example.com/x"');
    expect(ok).toContain('rel="noopener"');
    const bad = mdToHtml("[evil](javascript:alert(1))");
    expect(bad).not.toContain("<a ");
  });

  it("renders headings one level down from the source", () => {
    expect(mdToHtml("# Title")).toContain("<h2>Title</h2>");
    expect(mdToHtml("#### Deep")).toContain("<h5>Deep</h5>");
  });

  it("renders unordered and ordered lists", () => {
    const ul = mdToHtml("- one\n- two");
    expect(ul).toContain("<ul>");
    expect(ul).toContain("<li>one</li>");
    const ol = mdToHtml("1. first\n2. second");
    expect(ol).toContain("<ol>");
    expect(ol).toContain("<li>second</li>");
  });

  it("renders a well-formed pipe table as a table", () => {
    const out = mdToHtml("| Name | Value |\n| --- | ---: |\n| AMD | 42 |");
    expect(out).toContain('<table class="md-tbl">');
    expect(out).toContain("<th>Name</th>");
    expect(out).toContain("<td>AMD</td>");
  });

  it("links cells in a Ticker-headed column deterministically", () => {
    const out = mdToHtml("| Ticker | Note |\n| --- | --- |\n| AMD | chips |");
    expect(out).toContain('data-ticker="AMD"');
    expect(out).toContain("?view=deepdive&ticker=AMD");
  });

  it("falls back to a pre block for a pipe blob without separator row", () => {
    const out = mdToHtml("| just | some |\n| random | pipes |");
    expect(out).toContain('<pre class="md-table">');
    expect(out).not.toContain("<table");
  });

  it("renders horizontal rules and paragraphs", () => {
    const out = mdToHtml("para one\n\n---\n\npara two");
    expect(out).toContain("<p>para one</p>");
    expect(out).toContain("<hr>");
    expect(out).toContain("<p>para two</p>");
  });

  it("returns empty string for empty input", () => {
    expect(mdToHtml("")).toBe("");
    expect(mdToHtml(null)).toBe("");
  });
});

describe("linkifyTickers", () => {
  let root: HTMLElement;

  const linkify = (html: string): HTMLElement => {
    root = document.createElement("div");
    root.innerHTML = html;
    document.body.appendChild(root);
    linkifyTickers(root);
    return root;
  };

  beforeEach(() => {
    state.tickerSet = new Set(["AMD", "ARM", "TSM"]);
  });

  it("links tickers from the curated set", () => {
    const out = linkify("<p>We like AMD here.</p>");
    const a = out.querySelector("a.tlink");
    expect(a).not.toBeNull();
    expect(a!.getAttribute("data-ticker")).toBe("AMD");
  });

  it("does not link unknown bare tokens", () => {
    const out = linkify("<p>QQQQ is not in the set.</p>");
    expect(out.querySelector("a.tlink")).toBeNull();
  });

  it("links $-prefixed tokens even when stoplisted", () => {
    const out = linkify("<p>Buy $NOW today.</p>");
    const a = out.querySelector("a.tlink");
    expect(a).not.toBeNull();
    expect(a!.getAttribute("data-ticker")).toBe("NOW");
  });

  it("never links bare stoplisted shorthand like AI or CEO", () => {
    const out = linkify("<p>AI will replace the CEO, says the CFO.</p>");
    expect(out.querySelector("a.tlink")).toBeNull();
  });

  it("links parenthetical tokens", () => {
    const out = linkify("<p>ServiceNow (NOWX) reported.</p>");
    const a = out.querySelector("a.tlink");
    expect(a).not.toBeNull();
    expect(a!.textContent).toBe("NOWX");
  });

  it("leaves text inside code and existing anchors alone", () => {
    const out = linkify('<p><code>AMD</code> and <a href="#">ARM</a></p>');
    expect(out.querySelectorAll("a.tlink").length).toBe(0);
  });
});
