// @ts-nocheck
import { $, api, el, esc, relAge, state } from "./core";
import { cleanSlug, navFromUrl, pushNav, setActiveView } from "./shell";
import { pollDeepJob } from "./errors";

// ---- analyses -------------------------------------------------------------

// ---- ticker auto-linking --------------------------------------------------
// All-caps tokens that are common finance/English shorthand, not tickers. Bare
// matches are additionally gated by the curated ticker set; this stoplist guards
// the structural ($X, parenthetical) paths and trims obvious noise.
const TICKER_STOP = new Set([
  "US", "EU", "UK", "USA", "EV", "AI", "AR", "VR", "ML", "LLM", "GPU", "CPU", "API", "SDK",
  "UI", "UX", "CEO", "CFO", "CTO", "COO", "IPO", "ETF", "ETFS", "NAV", "EPS", "PE", "PEG",
  "ROE", "ROI", "ROIC", "FCF", "GAAP", "YOY", "QOQ", "CAGR", "ARR", "MRR", "TAM", "SAM", "SOM",
  "FY", "H1", "H2", "Q1", "Q2", "Q3", "Q4", "USD", "EUR", "GBP", "JPY", "KPI", "OEM", "ESG",
  "IRR", "WACC", "DCF", "EBITDA", "IT", "OK", "NO", "AND", "THE", "FOR", "WITH", "FROM",
  "THAT", "THIS", "ARE", "NOT", "ALL", "ANY", "OS", "PC", "TV", "IOT", "SAAS", "B2B", "B2C",
  "RD", "IP", "ID", "VS", "ETC", "CES", "FDA", "SEC", "GDP", "API",
]);

let _tickerSetLoaded = false;
async function ensureTickerSet() {
  if (_tickerSetLoaded) return state.tickerSet;
  try {
    const d = await api("/api/tickers");
    state.tickerSet = new Set(d.tickers || []);
  } catch (_e) { state.tickerSet = new Set(); }
  _tickerSetLoaded = true;
  return state.tickerSet;
}

function tickerAnchorHtml(raw) {
  const s = String(raw).toUpperCase();
  return `<a class="tlink" data-ticker="${esc(s)}" href="?view=deepdive&ticker=${encodeURIComponent(s)}" title="Open ${esc(s)} deep-dive">${esc(raw)}</a>`;
}

// Walk text nodes and turn ticker-shaped tokens into deep-dive links. Skips text
// already inside <a>/<code>/<pre>. A token links if it's $-prefixed, wrapped in
// (parens), or present in the curated set -- and never if in the stoplist.
const _TICKER_TOKEN = /\b[A-Z]{2,5}(?:\.[A-Z]{1,2})?\b/g;
function linkifyTextNode(node, set) {
  const text = node.nodeValue;
  let m, last = 0, frag = null;
  _TICKER_TOKEN.lastIndex = 0;
  while ((m = _TICKER_TOKEN.exec(text))) {
    const tok = m[0];
    const base = tok.split(".")[0];
    const i = m.index;
    const prev = text[i - 1] || "";
    const after = text[i + tok.length] || "";
    const dollar = prev === "$";  // explicit author intent -- overrides the stoplist
    // A "$NOW" must link even though NOW is a stoplisted English word; bare and
    // parenthetical tokens still respect the stoplist.
    if (!dollar && (TICKER_STOP.has(tok) || TICKER_STOP.has(base))) continue;
    const linkable = dollar || (prev === "(" && after === ")") || set.has(tok) || set.has(base);
    if (!linkable) continue;
    frag = frag || document.createDocumentFragment();
    if (i > last) frag.appendChild(document.createTextNode(text.slice(last, i)));
    const a = document.createElement("a");
    a.className = "tlink";
    a.dataset.ticker = tok;
    a.href = `?view=deepdive&ticker=${encodeURIComponent(tok)}`;
    a.title = `Open ${tok} deep-dive`;
    a.textContent = tok;
    frag.appendChild(a);
    last = i + tok.length;
  }
  if (frag) {
    if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
    node.parentNode.replaceChild(frag, node);
  }
}

function linkifyTickers(root) {
  if (!root) return;
  const set = state.tickerSet || new Set();
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(n) {
      if (!n.nodeValue || !/[A-Z]{2}/.test(n.nodeValue)) return NodeFilter.FILTER_REJECT;
      for (let p = n.parentElement; p && p !== root.parentElement; p = p.parentElement) {
        const tag = p.tagName;
        if (tag === "A" || tag === "CODE" || tag === "PRE") return NodeFilter.FILTER_REJECT;
      }
      return NodeFilter.FILTER_ACCEPT;
    },
  });
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  nodes.forEach((n) => linkifyTextNode(n, set));
}

// Minimal, escape-first markdown renderer. The report text is from Perplexity
// (untrusted), so everything is HTML-escaped before a controlled subset of
// markup is re-introduced; links are restricted to http(s) so no javascript:.
function mdToHtml(md) {
  if (!md) return "";
  const inline = (s) =>
    esc(s)
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/\*([^*\n]+)\*/g, "<em>$1</em>")
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  const out = [];
  let list = null;
  let para = [];
  let table = [];
  const flushPara = () => { if (para.length) { out.push(`<p>${inline(para.join(" "))}</p>`); para = []; } };
  const closeList = () => { if (list) { out.push(`</${list}>`); list = null; } };
  const flushTable = () => {
    if (!table.length) return;
    const rows = table.map((l) => l.replace(/^\s*\|/, "").replace(/\|\s*$/, "").split("|").map((c) => c.trim()));
    const isSep = (r) => r.length && r.every((c) => /^:?-+:?$/.test(c.replace(/\s/g, "")));
    if (rows.length >= 2 && isSep(rows[1])) {
      const head = rows[0];
      const body = rows.slice(2).filter((r) => !isSep(r));
      // Columns explicitly headed Ticker/Symbol get deterministic links on every
      // cell -- highest-precision signal, no curated set or guessing required.
      const tickerCols = new Set(
        head.map((h, i) => (/^(ticker|symbol|tickers?|symbols?)$/i.test(h.trim()) ? i : -1)).filter((i) => i >= 0),
      );
      const cell = (c, ci) =>
        (tickerCols.has(ci) && /^[A-Za-z][A-Za-z0-9.]{0,5}$/.test(c.trim()))
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
    let m;
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
  flushPara(); closeList(); flushTable();
  return out.join("\n");
}

function analysisBadges(r) {
  const parts = [];
  if (r.has_review) parts.push('<span class="abadge ok">reviewed</span>');
  if (r.change_count) parts.push(`<span class="abadge">${r.change_count} proposed</span>`);
  if (r.blocked_symbols && r.blocked_symbols.length)
    parts.push(`<span class="abadge bad">blocked: ${esc(r.blocked_symbols.join(", "))}</span>`);
  return parts.join(" ");
}

function markActiveAnalysis(stem) {
  document.querySelectorAll("#analyses-list .analysis-row").forEach((row) =>
    row.classList.toggle("active", row.dataset.stem === stem));
}

// A labelled card for the parts the console synthesizes on top of the raw run
// (prompt, review gate, citations) -- visually distinct from the report itself.
function synthBox(title, note) {
  const box = el("section", "synth-box");
  box.innerHTML =
    `<div class="synth-box-head"><span class="synth-box-title">${esc(title)}</span>` +
    (note ? `<span class="synth-box-note">${esc(note)}</span>` : "") +
    `</div><div class="synth-box-body"></div>`;
  return box;
}

// Jump into the Pipeline wizard at step 1, optionally pre-selecting a segment.
// The pipeline (a gated multi-step flow) stays the single home for running
// research; the Analyses pane is just the launchpad into it.
function startPipeline(segment) {
  const seg = cleanSlug(segment || "");
  state.pipeStep = 1;
  state.segMode = "existing";
  state.currentDeepRun = null;
  state.pipePreselect = seg || null;
  pushNav({ view: "pipeline", segment: seg || undefined });
  setActiveView("pipeline");
}

// Open a saved run in this (the canonical) reader from anywhere else in the app
// -- e.g. the Pipeline routes here instead of rendering the report a second time.
function openRunInAnalyses(stem) {
  pushNav({ view: "analyses", run: cleanSlug(stem) });
  setActiveView("analyses");
}

function fmtConfidence(c) {
  if (c == null || c === "") return "";
  if (typeof c === "number") return c <= 1 ? Math.round(c * 100) + "%" : String(c);
  return String(c);
}

// Some reports are a structured segment document (title/comment/sleeves/members)
// saved as JSON rather than narrative markdown. Rendering that through mdToHtml
// is an unreadable JSON wall, so detect and lay it out as a table. Returns null
// when the text isn't such a document (the caller falls back to markdown).
function renderStructuredReport(raw) {
  let data;
  try { data = JSON.parse(raw); } catch { return null; }
  if (!data || typeof data !== "object" || Array.isArray(data)) return null;
  if (!data.members && !data.sleeves && !data.comment) return null;
  let html = "";
  if (data.title) html += `<h3 class="rep-title">${esc(data.title)}</h3>`;
  if (data.comment) html += `<p class="rep-lead">${esc(data.comment)}</p>`;
  if (Array.isArray(data.sleeves) && data.sleeves.length) {
    html += `<div class="rep-section-h">Sleeves</div><div class="rep-sleeves">`;
    data.sleeves.forEach((s) => {
      html += `<div class="rep-sleeve"><span class="rep-sleeve-name">${esc(s.name || "")}</span>` +
        (s.description ? `<span class="rep-sleeve-desc">${esc(s.description)}</span>` : "") + `</div>`;
    });
    html += `</div>`;
  }
  if (Array.isArray(data.members) && data.members.length) {
    html += `<div class="rep-section-h">Members <span class="rep-count">${data.members.length}</span></div>`;
    html += `<table class="rep-members"><thead><tr>` +
      `<th>Symbol</th><th>Sleeve</th><th class="num">Conf.</th><th>Rationale</th></tr></thead><tbody>`;
    data.members.forEach((m) => {
      html += `<tr>` +
        `<td class="rep-sym">${esc(m.symbol || "")}</td>` +
        `<td class="rep-sleeve-cell">${esc(m.sleeve || "")}</td>` +
        `<td class="num">${esc(fmtConfidence(m.confidence))}</td>` +
        `<td class="rep-rat">${esc(m.rationale || "")}</td>` +
        `</tr>`;
    });
    html += `</tbody></table>`;
  }
  return html || null;
}

async function loadAnalyses() {
  const list = $("#analyses-list");
  if (!list) return;
  list.innerHTML = '<div class="hint">Loading…</div>';
  let runs, reports, segments;
  try {
    [runs, reports, segments] = await Promise.all([
      api("/api/deep-runs").then((d) => d.runs || []),
      api("/api/reports").then((d) => d.reports || []).catch(() => []),
      api("/api/segments").then((d) => d.segments || []).catch(() => []),
    ]);
  } catch (e) {
    list.innerHTML = `<div class="status err">could not load analyses: ${esc(e.message)}</div>`;
    return;
  }
  state.analysesRuns = runs;

  // Group runs under their segment so each segment shows once (no more duplicate
  // Segments + Deep Research lists). Runs arrive newest-first, so [0] is latest.
  const runsBySeg = {};
  runs.forEach((r) => { (runsBySeg[r.segment] = runsBySeg[r.segment] || []).push(r); });
  const knownSegs = new Set(segments.map((s) => s.name));

  list.innerHTML = "";

  const runRow = (r, cls) => {
    const row = el("button", "analysis-row" + (cls ? " " + cls : ""));
    row.dataset.stem = r.stem;
    const age = relAge(r.generated_at);
    const meta = `${esc(r.date || "")}${age ? " · " + esc(age) : ""} · ${r.source_count || 0} sources`;
    const badges = analysisBadges(r) ? `<div class="analysis-row-badges">${analysisBadges(r)}</div>` : "";
    row.innerHTML = cls === "sub-run"
      ? `<div class="analysis-row-meta">${meta}</div>${badges}`
      : `<div class="analysis-row-title">${esc(r.title || r.stem)}</div><div class="analysis-row-meta">${meta}</div>${badges}`;
    row.addEventListener("click", () => loadAnalysis(r.stem));
    return row;
  };

  if (segments.length) {
    list.appendChild(el("div", "analyses-group-label", "Segments"));
    segments.forEach((s) => {
      const segRuns = runsBySeg[s.name] || [];
      const latest = segRuns[0];
      const row = el("button", "analysis-row seg-row");
      row.dataset.segment = s.name;
      if (latest) row.dataset.stem = latest.stem;
      const runCount = segRuns.length ? `${segRuns.length} run${segRuns.length === 1 ? "" : "s"}` : "no runs yet";
      const cover = latest
        ? `<span class="abadge ok">analysed · ${esc(latest.date)}</span>`
        : `<span class="abadge muted">not analysed</span>`;
      const moreBadges = latest && analysisBadges(latest) ? " " + analysisBadges(latest) : "";
      row.innerHTML =
        `<div class="analysis-row-title">${esc(s.title || s.name)}` +
          `<span class="seg-run" role="button" tabindex="0" title="Run a new Deep Research for this segment">+ run</span></div>` +
        `<div class="analysis-row-meta">${s.count} name${s.count === 1 ? "" : "s"} · ${runCount}${s.status === "draft" ? " · draft" : ""}</div>` +
        `<div class="analysis-row-badges">${cover}${moreBadges}</div>`;
      row.addEventListener("click", () => { if (latest) loadAnalysis(latest.stem); else startPipeline(s.name); });
      const runBtn = row.querySelector(".seg-run");
      runBtn.addEventListener("click", (ev) => { ev.stopPropagation(); startPipeline(s.name); });
      runBtn.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); ev.stopPropagation(); startPipeline(s.name); }
      });
      list.appendChild(row);
      segRuns.slice(1).forEach((r) => list.appendChild(runRow(r, "sub-run")));  // older runs, nested
    });
  }

  // Runs whose segment no longer has a definition (renamed/removed) -- keep them
  // reachable rather than dropping them on the floor.
  const orphanRuns = runs.filter((r) => !knownSegs.has(r.segment));
  if (orphanRuns.length) {
    list.appendChild(el("div", "analyses-group-label", "Other runs"));
    orphanRuns.forEach((r) => list.appendChild(runRow(r)));
  }
  if (reports.length) {
    list.appendChild(el("div", "analyses-group-label", "Written reports"));
    reports.forEach((rp) => {
      const a = el("a", "analysis-row report-row");
      a.href = rp.href;
      const tag = rp.kind === "ticker" ? (rp.symbol || "ticker") : "thematic";
      a.innerHTML =
        `<div class="analysis-row-title">${esc(rp.title)} <span class="open-ext">↗</span></div>` +
        `<div class="analysis-row-meta">${esc(tag)} · static page</div>`;
      list.appendChild(a);
    });
  }
  if (!runs.length && !reports.length && !segments.length) {
    list.innerHTML = '<div class="hint">No analyses or segments yet. Use “+ New analysis” to start one.</div>';
    $("#analyses-reader").innerHTML = '<div class="hint">Nothing to read yet.</div>';
    return;
  }

  if (runs.length) {
    const urlRun = navFromUrl().run;
    const toOpen = (urlRun && runs.some((r) => r.stem === urlRun)) ? urlRun : runs[0].stem;
    await loadAnalysis(toOpen, { push: false });
  } else {
    $("#analyses-reader").innerHTML =
      '<div class="hint">No Deep Research runs yet — pick a segment to run one, choose a written report, or hit “+ New analysis”.</div>';
  }
}

async function loadAnalysis(stem, { push = true } = {}) {
  const reader = $("#analyses-reader");
  if (!reader) return;
  await ensureTickerSet();
  state.currentAnalysis = stem;
  markActiveAnalysis(stem);
  if (push) pushNav({ view: "analyses", run: stem }, { replace: true });
  reader.innerHTML = '<div class="hint">Loading…</div>';
  let rec;
  try {
    rec = await api("/api/deep-run/" + encodeURIComponent(stem));
  } catch (e) {
    reader.innerHTML = `<div class="status err">${esc(e.message)}</div>`;
    return;
  }
  const meta = state.analysesRuns.find((r) => r.stem === stem) || {};
  const sources = rec.sources || {};
  const citations = sources.citations || [];
  const age = relAge(meta.generated_at);

  let prompt = "";
  if (meta.segment) {
    try {
      prompt = (await api("/api/deep-prompt?segment=" + encodeURIComponent(meta.segment))).prompt || "";
    } catch (_e) { /* prompt is best-effort context */ }
  }

  reader.innerHTML = "";

  // Synthesized summary header (title + metadata the console attaches on top).
  const head = el("div", "analysis-header synth");
  let sub = `Deep Research${meta.date ? " · " + esc(meta.date) : ""}${age ? " · " + esc(age) : ""} · ${citations.length} sources`;
  if (sources.source_url)
    sub += ` · <a href="${esc(sources.source_url)}" target="_blank" rel="noopener">open in Perplexity ↗</a>`;
  head.innerHTML =
    `<div class="synth-tag">Console summary</div>` +
    `<h2>${esc(meta.title || stem)}</h2>` +
    `<div class="analysis-sub">${sub}</div>` +
    (analysisBadges(meta) ? `<div class="analysis-row-badges">${analysisBadges(meta)}</div>` : "");
  reader.appendChild(head);

  // The report itself — verbatim Perplexity output, framed as a document.
  if (rec.report) {
    const doc = el("section", "report-doc");
    doc.innerHTML =
      `<div class="report-doc-head"><span class="report-doc-title">Deep Research report</span>` +
      `<span class="report-doc-note">Verbatim Perplexity output — treat numbers as claims to verify</span></div>`;
    const body = el("div", "report-doc-body prose");
    body.innerHTML = renderStructuredReport(rec.report) || mdToHtml(rec.report);
    doc.appendChild(body);
    reader.appendChild(doc);
    linkifyTickers(body);
  }

  // Everything below this line is generated/extracted by the console, not the report.
  reader.appendChild(el("div", "synth-divider", "<span>Synthesized by the console</span>"));

  if (prompt) {
    const box = synthBox("Prompt", "What the console asks Perplexity for this segment");
    const det = el("details", "prompt-details");
    det.innerHTML = `<summary>Show prompt</summary><pre class="prompt-text">${esc(prompt)}</pre>`;
    box.querySelector(".synth-box-body").appendChild(det);
    reader.appendChild(box);
  }

  if (rec.review) {
    const box = synthBox("Review gate", "Local cross-check of the report against your holdings");
    box.querySelector(".synth-box-body").appendChild(el("div", "prose", mdToHtml(rec.review)));
    reader.appendChild(box);
  }

  if (citations.length) {
    const box = synthBox(`Sources (${citations.length})`, "Citations extracted from the run");
    const ul = el("ol", "cite-list");
    citations.forEach((c) => {
      const li = el("li", "cite");
      let host = c.href || "";
      try { host = new URL(c.href).hostname.replace(/^www\./, ""); } catch { /* not a URL: keep raw href */ }
      const parts = String(c.label || "").split("\n").map((s) => s.trim()).filter(Boolean);
      const name = parts.find((p) => !/^https?:/i.test(p)) || host;
      const desc = parts.find((p) => !/^https?:/i.test(p) && p !== name) || "";
      li.innerHTML =
        (c.href ? `<a href="${esc(c.href)}" target="_blank" rel="noopener">${esc(name)}</a>` : esc(name)) +
        `<span class="cite-host">${esc(host)}</span>` +
        (desc ? `<div class="cite-desc">${esc(desc)}</div>` : "");
      ul.appendChild(li);
    });
    box.querySelector(".synth-box-body").appendChild(ul);
    reader.appendChild(box);
  }

  // Follow-up Q&A, grounded in this report. Only meaningful once a report exists.
  if (rec.report) {
    reader.appendChild(renderDeepQaCard(stem, meta.title || stem));
  }

  reader.scrollTop = 0;
}

// Continuable follow-up Q&A about a Deep Research run. The thread is archived
// server-side (next to the run artifacts) so it survives reloads. Mirrors the
// per-ticker Q&A card but grounds the model in the report + its citations.
// Shared archive-and-continue Q&A card used by both the per-ticker dossier and
// the Deep Research reader. The shell (collapsible thread, ask/cancel form,
// background-job polling, clear-with-confirm) is identical; callers pass the copy
// and the three endpoints the turns live behind, plus optional per-turn meta,
// token-usage HTML, and a pre-render hook (e.g. loading the ticker set).
function createQaCard(opts) {
  const card = el("div", "card qa-card");
  const head = el("div", "analysis-head");
  head.appendChild(el("h2", "section", esc(opts.title)));
  const clearBtn = el("button", "ghost", "Clear thread");
  clearBtn.type = "button";
  clearBtn.title = "Discard the archived Q&A and start fresh";
  head.appendChild(clearBtn);
  card.appendChild(head);

  const emptyHint = el("p", "hint", opts.emptyHint);
  const threadWrap = el("details", "qa-collapse");
  threadWrap.open = true;
  const threadSummary = el("summary", "qa-collapse-head collapse-head");
  const thread = el("div", "qa-thread");
  threadWrap.appendChild(threadSummary);
  threadWrap.appendChild(thread);
  const status = el("div", "dd-status analysis-status");
  const form = el("div", "qa-form");
  const input = el("textarea", "qa-input");
  input.rows = 2;
  input.placeholder = opts.placeholder;
  const askBtn = el("button", "primary", "Ask");
  askBtn.type = "button";
  form.appendChild(input);
  form.appendChild(askBtn);
  card.appendChild(emptyHint);
  card.appendChild(threadWrap);
  card.appendChild(form);
  card.appendChild(status);

  function renderThread(turns) {
    thread.innerHTML = "";
    if (!turns.length) {
      clearBtn.hidden = true;
      emptyHint.hidden = false;
      threadWrap.hidden = true;
      return;
    }
    clearBtn.hidden = false;
    emptyHint.hidden = true;
    threadWrap.hidden = false;
    const exchanges = turns.filter((t) => t.role === "user").length;
    threadSummary.innerHTML =
      `<span class="collapse-title">Conversation history</span>` +
      `<span class="collapse-meta">${exchanges} question${exchanges === 1 ? "" : "s"}</span>` +
      `<span class="collapse-caret" aria-hidden="true">\u203a</span>`;
    turns.forEach((t) => {
      if (t.role === "user") {
        const q = el("div", "qa-turn qa-q");
        q.appendChild(el("div", "qa-role", "You"));
        q.appendChild(el("div", "qa-text", esc(t.text)));
        thread.appendChild(q);
      } else {
        const a = el("div", "qa-turn qa-a");
        const meta = (opts.turnMeta ? opts.turnMeta(t) : []).filter(Boolean).map(esc).join(" \u00b7 ");
        a.appendChild(el("div", "qa-role", "Analyst" + (meta ? ` <span class="muted">${meta}</span>` : "")));
        const prose = el("div", "prose qa-prose");
        prose.innerHTML = mdToHtml(t.text || "");
        linkifyTickers(prose);
        a.appendChild(prose);
        const usage = opts.usageHtml ? opts.usageHtml(t) : "";
        if (usage) a.insertAdjacentHTML("beforeend", usage);
        thread.appendChild(a);
      }
    });
  }

  async function load() {
    let data;
    try { data = await opts.loadThread(); }
    catch (_e) { data = { turns: [] }; }
    if (opts.prepare) await opts.prepare();
    renderThread(data.turns || []);
  }

  let currentJobId = null;
  let busy = false;

  function setBusy(on) {
    busy = on;
    if (on) {
      askBtn.textContent = "Cancel";
      askBtn.classList.remove("primary");
      askBtn.classList.add("ghost");
      askBtn.title = "Stop this question and ask something else";
    } else {
      askBtn.textContent = "Ask";
      askBtn.classList.add("primary");
      askBtn.classList.remove("ghost");
      askBtn.title = "";
      currentJobId = null;
    }
  }

  async function ask() {
    if (busy) return;
    const q = input.value.trim();
    if (!q) return;
    setBusy(true);
    status.classList.remove("err");
    status.innerHTML = `<span class="spinner"></span> thinking&hellip;`;
    try {
      const start = await opts.postQuestion(q);
      currentJobId = start.id;
      await pollDeepJob(start.id, status, async () => {
        status.textContent = "";
        input.value = "";
        await load();
      }, opts.pollLabel);
    } catch (e) {
      status.classList.add("err");
      status.textContent = "question failed: " + e.message;
    } finally {
      setBusy(false);
    }
  }

  // Cancel the in-flight question (kills the CLI subprocess server-side). The
  // poll loop observes the cancelled state on its next tick and winds down,
  // re-enabling the Ask button so a different question can be asked.
  async function cancelAsk() {
    if (!currentJobId) return;
    status.classList.remove("err");
    status.innerHTML = `<span class="spinner"></span> cancelling&hellip;`;
    try {
      await api("/api/deep-job/cancel", "POST", { id: currentJobId });
    } catch (_e) { /* poll loop still winds the job down */ }
  }

  askBtn.addEventListener("click", () => (busy ? cancelAsk() : ask()));
  input.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); if (!busy) ask(); }
  });
  clearBtn.addEventListener("click", async () => {
    if (!confirm(opts.confirmMsg)) return;
    try {
      const data = await opts.clearThread();
      renderThread(data.turns || []);
    } catch (e) {
      status.classList.add("err");
      status.textContent = "clear failed: " + e.message;
    }
  });

  load();
  return card;
}

function renderDeepQaCard(stem, title) {
  return createQaCard({
    title: "Ask about this report",
    emptyHint:
      "No questions yet. Ask anything about the report \u2014 a company's positioning, a claim worth " +
      "verifying, or how a name fits your portfolio. The thread is archived so you can pick it up later.",
    placeholder: `Ask a follow-up about "${title}" \u2014 grounded in the report above. Ctrl/\u2318+Enter to send.`,
    pollLabel: `Q&A \u00b7 ${stem}`,
    confirmMsg: "Clear the archived Q&A thread for this report?",
    loadThread: () => api("/api/deep-qa?stem=" + encodeURIComponent(stem)),
    postQuestion: (q) => api("/api/deep-qa", "POST", { stem, question: q }),
    clearThread: () => api("/api/deep-qa", "POST", { stem, clear: true }),
    turnMeta: (t) => [t.backend_label, t.ts ? relAge(t.ts) : null],
  });
}

export {
  TICKER_STOP,
  _tickerSetLoaded,
  ensureTickerSet,
  tickerAnchorHtml,
  _TICKER_TOKEN,
  linkifyTextNode,
  linkifyTickers,
  mdToHtml,
  analysisBadges,
  markActiveAnalysis,
  synthBox,
  createQaCard,
  startPipeline,
  openRunInAnalyses,
  loadAnalyses,
  loadAnalysis,
};
