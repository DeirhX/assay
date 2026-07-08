// Minimal, escape-first markdown renderer for untrusted report text, plus the
// slug/table-of-contents helpers that hang off the rendered output. Extracted
// from analyses.ts. Everything is HTML-escaped before a controlled subset of
// markup is re-introduced; links are restricted to http(s) so no javascript:.
import { el, esc } from "../core";
import { tickerAnchorHtml } from "./linkify";

export function mdToHtml(md: string | null | undefined): string {
  if (!md) return "";
  const inline = (s: string) =>
    esc(s)
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/\*([^*\n]+)\*/g, "<em>$1</em>")
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  const out: string[] = [];
  let list: string | null = null;
  let para: string[] = [];
  let table: string[] = [];
  let fenceLang: string | null = null;   // non-null while inside a ``` block
  let fenceLines: string[] = [];
  const flushPara = () => { if (para.length) { out.push(`<p>${inline(para.join(" "))}</p>`); para = []; } };
  const closeList = () => { if (list) { out.push(`</${list}>`); list = null; } };
  // A fenced code block. A ```json block that parses as a price-levels record is
  // rendered as a readable ladder summary; any other JSON is pretty-printed; the
  // rest becomes a plain code block. (Without this, a fence's lines were joined
  // into a paragraph and the inline `code` regex mangled the triple backticks.)
  const flushFence = () => {
    const body = fenceLines.join("\n");
    fenceLines = [];
    const looksJson = fenceLang === "json" || /^\s*[[{]/.test(body);
    if (looksJson) {
      try {
        const data = JSON.parse(body);
        const summary = priceLevelsSummaryHtml(data);
        if (summary) { out.push(summary); return; }
        out.push(`<pre class="md-code md-code-json"><code>${esc(JSON.stringify(data, null, 2))}</code></pre>`);
        return;
      } catch { /* not valid JSON — fall through to a plain code block */ }
    }
    out.push(`<pre class="md-code"><code>${esc(body)}</code></pre>`);
  };
  const flushTable = () => {
    if (!table.length) return;
    const rows = table.map((l) => l.replace(/^\s*\|/, "").replace(/\|\s*$/, "").split("|").map((c) => c.trim()));
    const isSep = (r: string[]) => r.length && r.every((c) => /^:?-+:?$/.test(c.replace(/\s/g, "")));
    if (rows.length >= 2 && isSep(rows[1])) {
      const head = rows[0];
      const body = rows.slice(2).filter((r) => !isSep(r));
      // Columns explicitly headed Ticker/Symbol get deterministic links on every
      // cell -- highest-precision signal, no curated set or guessing required.
      const tickerCols = new Set(
        head.map((h, i) => (/^(ticker|symbol|tickers?|symbols?)$/i.test(h.trim()) ? i : -1)).filter((i) => i >= 0),
      );
      // Link a Ticker/Symbol cell that looks like a symbol: a letter-led US base
      // (optionally exchange-qualified) or a foreign exchange-qualified symbol
      // with a possibly-numeric base (000660.KS). Still rejects junk like "N/A".
      const tickerShape = /^(?:[A-Za-z]{1,5}(?:\.[A-Za-z]{1,3})?|[A-Za-z0-9]{1,6}\.[A-Za-z]{1,3})$/;
      const cell = (c: string, ci: number) =>
        (tickerCols.has(ci) && tickerShape.test(c.trim()))
          ? `<td>${tickerAnchorHtml(c.trim())}</td>`
          : `<td>${inline(c)}</td>`;
      let html = '<table class="md-tbl"><thead><tr>' + head.map((c) => `<th>${inline(c)}</th>`).join("") + "</tr></thead>";
      if (body.length) html += "<tbody>" + body.map((r) => "<tr>" + r.map(cell).join("") + "</tr>").join("") + "</tbody>";
      out.push(html + "</table>");
    } else {
      out.push(`<pre class="md-table">${esc(table.join("\n"))}</pre>`);
    }
    table = [];
  };
  String(md).replace(/\r\n/g, "\n").split("\n").forEach((raw) => {
    const line = raw.replace(/\s+$/, "");
    let m: RegExpMatchArray | null;
    // Fenced code blocks take precedence: swallow every line verbatim until the
    // closing ``` so their contents are never parsed as markdown.
    if (fenceLang !== null) {
      if (/^\s*```\s*$/.test(line)) { flushFence(); fenceLang = null; } else fenceLines.push(raw);
      return;
    }
    if ((m = line.match(/^\s*```(\w*)\s*$/))) { flushPara(); closeList(); flushTable(); fenceLang = (m[1] || "").toLowerCase(); return; }
    if (line.trim().startsWith("|")) { flushPara(); closeList(); table.push(line); return; }
    flushTable();
    if (!line.trim()) { flushPara(); closeList(); return; }
    if (/^-{3,}$/.test(line.trim())) { flushPara(); closeList(); out.push("<hr>"); return; }
    if ((m = line.match(/^(#{1,4})\s+(.*)$/))) {
      flushPara(); closeList();
      out.push(`<h${Math.min(m[1].length + 1, 6)}>${inline(m[2])}</h${Math.min(m[1].length + 1, 6)}>`);
    } else if ((m = line.match(/^\s*[-*]\s+(.*)$/))) {
      flushPara(); if (list !== "ul") { closeList(); list = "ul"; out.push("<ul>"); }
      out.push(`<li>${inline(m[1])}</li>`);
    } else if ((m = line.match(/^\s*\d+\.\s+(.*)$/))) {
      flushPara(); if (list !== "ol") { closeList(); list = "ol"; out.push("<ol>"); }
      out.push(`<li>${inline(m[1])}</li>`);
    } else {
      closeList(); para.push(line);
    }
  });
  if (fenceLang !== null) flushFence();   // unterminated fence: render what we have
  flushPara(); closeList(); flushTable();
  return out.join("\n");
}

// Render a locked/suggested price-levels JSON record as a compact, readable
// ladder instead of raw JSON: a fair-value anchor plus buy/trim tranches, each
// shown as a "margin -> size" chip. Returns null when the object isn't a
// price-levels record so the caller can fall back to a generic JSON block.
function priceLevelsSummaryHtml(d: unknown): string | null {
  if (!d || typeof d !== "object") return null;
  const rec = d as Record<string, unknown>;
  const buy = Array.isArray(rec.buy_ladder) ? (rec.buy_ladder as Record<string, unknown>[]) : null;
  const trim = Array.isArray(rec.trim_ladder) ? (rec.trim_ladder as Record<string, unknown>[]) : null;
  const fv = typeof rec.fair_value === "number" && isFinite(rec.fair_value) ? rec.fair_value : null;
  if (fv == null && !buy && !trim) return null;

  const round2 = (v: number) => Math.round(v * 100) / 100;
  // A fraction (0.15) -> "15%", trimming a trailing ".0".
  const pctOf = (v: unknown): string | null => {
    if (typeof v !== "number" || !isFinite(v)) return null;
    const p = Math.round(v * 1000) / 10;
    return (Number.isInteger(p) ? String(p) : p.toFixed(1)) + "%";
  };
  const chip = (t: Record<string, unknown>, side: "buy" | "trim"): string => {
    const marginRaw = side === "buy" ? t.discount_pct : t.premium_pct;
    let marginLabel: string;
    if (typeof marginRaw === "number" && isFinite(marginRaw)) {
      marginLabel = Math.abs(marginRaw) < 1e-9 ? "at fair" : (side === "buy" ? "\u2212" : "+") + pctOf(Math.abs(marginRaw));
    } else if (typeof t.price === "number" && isFinite(t.price)) {
      marginLabel = String(round2(t.price));
    } else {
      marginLabel = "\u2014";
    }
    const size = pctOf(t.size_pct);
    return `<span class="md-lvl"><span class="md-lvl-m">${esc(marginLabel)}</span>` +
      (size ? `<span class="md-lvl-s">${esc(size)}</span>` : "") + "</span>";
  };
  const sideHtml = (label: string, arr: Record<string, unknown>[] | null, side: "buy" | "trim"): string => {
    if (!arr || !arr.length) return "";
    return `<div class="md-levels-side md-levels-${side}">` +
      `<span class="md-levels-cap">${label}</span>` +
      `<span class="md-lvls">${arr.map((t) => chip(t, side)).join("")}</span></div>`;
  };
  const fvHtml = fv != null
    ? `<div class="md-levels-fv"><span class="md-levels-cap">Fair value</span> <strong>${esc(String(round2(fv)))}</strong></div>`
    : "";
  return `<div class="md-levels">${fvHtml}` +
    `<div class="md-levels-grid">${sideHtml("Buy", buy, "buy")}${sideHtml("Trim", trim, "trim")}</div></div>`;
}

export function slugify(s: string): string {
  return (
    String(s).toLowerCase().trim()
      .replace(/[^\w\s-]/g, "")
      .replace(/\s+/g, "-")
      .replace(/-+/g, "-")
      .replace(/^-+|-+$/g, "") || "section"
  );
}

// Build a clickable table of contents from a rendered report body. Reports are
// prose markdown with section headings (the Deep Research prompt mandates it), so
// mdToHtml emits an h2..h4 outline we can hoist into a nav. Assigns stable,
// de-duplicated ids to the headings as a side effect so the links resolve, and
// returns null when there are too few headings to be worth the chrome.
export function buildReportToc(body: HTMLElement | null): HTMLElement | null {
  if (!body) return null;
  const heads = (Array.from(body.querySelectorAll("h2, h3, h4")) as HTMLElement[])
    .filter((h) => (h.textContent || "").trim());
  if (heads.length < 3) return null;
  const nav = el("nav", "report-toc");
  nav.setAttribute("aria-label", "Report contents");
  const det = el("details", "report-toc-det");
  det.open = true;
  det.innerHTML =
    `<summary class="report-toc-head">` +
    `<span class="report-toc-caret" aria-hidden="true">\u203a</span>` +
    `<span class="report-toc-title">Contents</span>` +
    `<span class="report-toc-count">${heads.length} sections</span></summary>`;
  const ol = el("ol", "report-toc-list");
  heads.forEach((h) => {
    const text = (h.textContent || "").trim();
    if (!h.id) {
      const base = slugify(text);
      let id = base, n = 2;
      while (document.getElementById(id)) id = `${base}-${n++}`;
      h.id = id;
    }
    const li = el("li", "report-toc-item " + h.tagName.toLowerCase());
    const a = el("a", "report-toc-link");
    a.href = "#" + h.id;
    a.textContent = text;
    // The app routes on ?view= query params, not the hash, so suppress the
    // default jump (which would dirty the URL) and scroll the heading into view.
    a.addEventListener("click", (e) => {
      e.preventDefault();
      h.scrollIntoView({ behavior: "smooth", block: "start" });
    });
    li.appendChild(a);
    ol.appendChild(li);
  });
  det.appendChild(ol);
  nav.appendChild(det);
  return nav;
}
