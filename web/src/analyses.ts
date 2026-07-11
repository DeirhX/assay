import type { SegmentSummary } from "./api-types";
import { $$, api, el, esc, relAge, state } from "./core";
import { cleanSlug, navFromUrl, pushNav, setActiveView } from "./shell";
import { ensureTickerSet, linkifyTickers, tickerAnchorHtml } from "./analyses/linkify";
import { buildReportToc, mdToHtml } from "./analyses/markdown";
import { starHtml } from "./basket";

// Re-export the report-formatting helpers so existing importers (and tests) can
// keep pulling them from "./analyses" while the implementations live in seams.
export {
  TICKER_STOP, _tickerSetLoaded, ensureTickerSet, tickerAnchorHtml, _TICKER_TOKEN,
  collectReportTickers, linkifyTextNode, linkifyTickers,
} from "./analyses/linkify";
export { mdToHtml, slugify, buildReportToc } from "./analyses/markdown";
import { renderDeepQaCard } from "./analyses/qa-card";
export { createQaCard } from "./analyses/qa-card";

// ---- analyses -------------------------------------------------------------

// A saved Deep Research run as the list/reader read it. Mirrors DeepRun from
// api-types but adds `kind` (ticker vs segment) and keeps every field the row
// renderer touches optional, since older nested re-runs are sparser.
export interface AnalysisRun {
  stem: string;
  segment: string;
  date?: string;
  title?: string;
  kind?: string;
  source_count?: number;
  generated_at?: string;
  has_review?: boolean;
  has_proposal?: boolean;
  change_count?: number;
  blocked_symbols?: string[];
  files?: Record<string, string>;
}

// One extracted source in a run's citation list.
interface Citation {
  href?: string;
  label?: string;
}

// A name the report discusses beyond the segment's own members (server-extracted
// in deep_runs.discovered_for). Starrable into the optimizer pool with its
// provenance so the sizer can credit the run's stance later.
interface DiscoveredCandidate {
  symbol: string;
  action?: string;
  context?: string;
  segment?: string;
  run?: string;
}

// Tone the action chip the way the rest of the app reads buy/sell signals.
const _ACTION_TONE: Record<string, string> = {
  add: "ok", hold: "", wait: "warn", trim: "warn", sell: "bad",
};

function discoveredRow(c: DiscoveredCandidate): string {
  const action = (c.action || "mentioned").toLowerCase();
  const tone = _ACTION_TONE[action] || "";
  const link = tickerAnchorHtml(c.symbol, { bold: true });
  const star = starHtml(c.symbol, "analyses", { tier: "curious", segment: c.segment, run: c.run });
  return `<div class="disc-row">` +
    `<span class="disc-sym">${link}</span>` +
    `<span class="disc-action ${tone ? "strat-tag-" + tone : ""}">${esc(action)}</span>` +
    (c.context ? `<span class="disc-ctx">${esc(c.context)}</span>` : "") +
    `<span class="disc-star">${star}</span>` +
    `</div>`;
}

// A report saved as a structured segment document rather than narrative markdown.
interface StructuredSleeve {
  name?: string;
  description?: string;
}
interface StructuredMember {
  symbol?: string;
  sleeve?: string;
  confidence?: unknown;
  rationale?: string;
}
interface StructuredDoc {
  title?: string;
  comment?: string;
  sleeves?: StructuredSleeve[];
  members?: StructuredMember[];
}

function analysisBadges(r: Partial<AnalysisRun>) {
  const parts = [];
  if (r.has_review) parts.push('<span class="abadge ok">reviewed</span>');
  if (r.change_count) parts.push(`<span class="abadge">${r.change_count} proposed</span>`);
  if (r.blocked_symbols && r.blocked_symbols.length)
    parts.push(`<span class="abadge bad">blocked: ${esc(r.blocked_symbols.join(", "))}</span>`);
  return parts.join(" ");
}

// "2026-06-18" -> "Jun 18, 2026". Falls back to the raw string on a bad/empty date.
function fmtRunDate(d: string | null | undefined) {
  if (!d) return "";
  const dt = new Date(d + "T00:00:00");
  return isNaN(dt.getTime()) ? d : dt.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

// A run is either a single-ticker dive or a segment study; a small leading glyph
// lets you tell the two apart without reading the title.
const KIND_ICON = {
  ticker: '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 17l5-5 4 4 7-8"/><path d="M16 8h5v5"/></svg>',
  segment: '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3l9 5-9 5-9-5 9-5z"/><path d="M3 14l9 5 9-5"/></svg>',
};
function kindIcon(r: AnalysisRun) {
  const k = r.kind === "ticker" ? "ticker" : "segment";
  const title = k === "ticker" ? "Single-ticker deep research" : "Segment deep research";
  return `<span class="ar-kind ar-kind--${k}" title="${esc(title)}">${KIND_ICON[k]}</span>`;
}

// One coherent state read per run: a lifecycle stage chip (draft -> report ready
// -> reviewed) plus optional proposal / blocked chips so the row's status is
// legible at a glance, including for older nested re-runs.
function runStateBadges(r: AnalysisRun) {
  const parts = [];
  const hasReport = !!(r.files && r.files.report);
  if (r.has_review || r.change_count) parts.push('<span class="abadge ok">reviewed</span>');
  else if (hasReport) parts.push('<span class="abadge">report ready</span>');
  else parts.push('<span class="abadge muted">draft</span>');
  if (r.change_count)
    parts.push(`<span class="abadge accent">${r.change_count} change${r.change_count === 1 ? "" : "s"} proposed</span>`);
  if (r.blocked_symbols && r.blocked_symbols.length)
    parts.push(`<span class="abadge bad">blocked: ${esc(r.blocked_symbols.join(", "))}</span>`);
  return parts.join(" ");
}

function markActiveAnalysis(stem: string) {
  document.querySelectorAll<HTMLElement>("#analyses-list .analysis-row").forEach((row) =>
    row.classList.toggle("active", row.dataset.stem === stem));
}

// A labelled card for the parts the console synthesizes on top of the raw run
// (prompt, review gate, citations) -- visually distinct from the report itself.
function synthBox(title: string, note?: string) {
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
function startPipeline(segment?: string) {
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
function openRunInAnalyses(stem: string) {
  pushNav({ view: "analyses", run: cleanSlug(stem) });
  setActiveView("analyses");
}

function fmtConfidence(c: unknown) {
  if (c == null || c === "") return "";
  if (typeof c === "number") return c <= 1 ? Math.round(c * 100) + "%" : String(c);
  return String(c);
}

// Ticker runs use a synthetic `ticker-XYZ` grouping key for list organization;
// it is not a segment definition and must never be sent to /api/deep-prompt.
export function promptSegmentFor(meta: Partial<AnalysisRun>): string | null {
  return meta.segment && meta.kind !== "ticker" ? meta.segment : null;
}

// Some reports are a structured segment document (title/comment/sleeves/members)
// saved as JSON rather than narrative markdown. Rendering that through mdToHtml
// is an unreadable JSON wall, so detect and lay it out as a table. Returns null
// when the text isn't such a document (the caller falls back to markdown).
function renderStructuredReport(raw: string) {
  let data: StructuredDoc;
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

// Delete a saved Deep Research run (its report, sidecars, and Q&A archive) after
// confirmation, then refresh the list. If the deleted run was open in the reader,
// loadAnalyses re-renders it to the next run (or an empty state).
async function deleteAnalysis(stem: string) {
  if (!stem) return;
  if (!confirm(
    "Delete this Deep Research analysis?\n\n" +
    "This removes the report, its sources, the review gate, any target proposal, " +
    "and the follow-up Q&A thread. This cannot be undone.")) return;
  try {
    await api("/api/deep-run/delete", "POST", { stem });
  } catch (e) {
    alert("Delete failed: " + (e as Error).message);
    return;
  }
  if (state.currentAnalysis === stem) state.currentAnalysis = null;
  await loadAnalyses();
}

async function loadAnalyses() {
  const list = $$("#analyses-list");
  if (!list) return;
  list.innerHTML = '<div class="hint">Loading…</div>';
  let runs: AnalysisRun[];
  let segments: SegmentSummary[];
  try {
    [runs, segments] = await Promise.all([
      api<{ runs?: AnalysisRun[] }>("/api/deep-runs").then((d) => d.runs || []),
      api<{ segments?: SegmentSummary[] }>("/api/segments").then((d) => d.segments || []).catch((): SegmentSummary[] => []),
    ]);
  } catch (e) {
    list.innerHTML = `<div class="status err">could not load analyses: ${esc((e as Error).message)}</div>`;
    return;
  }
  state.analysesRuns = runs;

  // Group runs under their segment so each segment shows once (no more duplicate
  // Segments + Deep Research lists). Runs arrive newest-first, so [0] is latest.
  const runsBySeg: Record<string, AnalysisRun[]> = {};
  runs.forEach((r) => { (runsBySeg[r.segment] = runsBySeg[r.segment] || []).push(r); });
  const knownSegs = new Set(segments.map((s) => s.name));

  list.innerHTML = "";

  const delHtml = `<span class="analysis-row-del" role="button" tabindex="0" title="Delete this analysis" aria-label="Delete this analysis">\u00d7</span>`;
  const wireDelete = (row: HTMLElement, stem: string) => {
    const del = row.querySelector<HTMLElement>(".analysis-row-del");
    if (!del) return;
    del.addEventListener("click", (ev) => { ev.stopPropagation(); deleteAnalysis(stem); });
    del.addEventListener("keydown", (ev: KeyboardEvent) => {
      if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); ev.stopPropagation(); deleteAnalysis(stem); }
    });
  };

  const runRow = (r: AnalysisRun, cls?: string) => {
    const row = el("button", "analysis-row" + (cls ? " " + cls : ""));
    row.dataset.stem = r.stem;
    const isSub = cls === "sub-run";
    const age = relAge(r.generated_at);
    const dateLabel = fmtRunDate(r.date) || r.date || "";
    // A sub-run is an older run of the same segment, so its identity is its date;
    // give it a real title ("Re-run · Jun 18, 2026") instead of a headerless meta line.
    const title = isSub ? `Re-run · ${esc(dateLabel)}` : esc(r.title || r.stem);
    const metaBits = [];
    if (!isSub && dateLabel) metaBits.push(esc(dateLabel));
    if (age) metaBits.push(esc(age));
    metaBits.push(`${r.source_count || 0} source${r.source_count === 1 ? "" : "s"}`);
    row.innerHTML =
      `${delHtml}` +
      `<div class="analysis-row-title">${kindIcon(r)}<span class="ar-title-text">${title}</span></div>` +
      `<div class="analysis-row-meta">${metaBits.join(" · ")}</div>` +
      `<div class="analysis-row-badges">${runStateBadges(r)}</div>`;
    row.addEventListener("click", () => loadAnalysis(r.stem));
    wireDelete(row, r.stem);
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
      const segDel = latest
        ? `<span class="seg-del" role="button" tabindex="0" title="Delete the latest analysis (${esc(latest.date)}) for this segment" aria-label="Delete the latest analysis for this segment">\u00d7 analysis</span>`
        : "";
      row.innerHTML =
        `<div class="analysis-row-title"><span class="seg-title-text">${esc(s.title || s.name)}</span>` +
          `<span class="seg-actions">${segDel}` +
          `<span class="seg-run" role="button" tabindex="0" title="Run a new Deep Research for this segment">+ run</span></span></div>` +
        `<div class="analysis-row-meta">${s.count} name${s.count === 1 ? "" : "s"} · ${runCount}${s.status === "draft" ? " · draft" : ""}</div>` +
        `<div class="analysis-row-badges">${cover}${moreBadges}</div>`;
      row.addEventListener("click", () => { if (latest) loadAnalysis(latest.stem); else startPipeline(s.name); });
      const runBtn = row.querySelector<HTMLElement>(".seg-run");
      runBtn?.addEventListener("click", (ev) => { ev.stopPropagation(); startPipeline(s.name); });
      runBtn?.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); ev.stopPropagation(); startPipeline(s.name); }
      });
      if (latest) {
        const segDelBtn = row.querySelector<HTMLElement>(".seg-del");
        segDelBtn?.addEventListener("click", (ev) => { ev.stopPropagation(); deleteAnalysis(latest.stem); });
        segDelBtn?.addEventListener("keydown", (ev) => {
          if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); ev.stopPropagation(); deleteAnalysis(latest.stem); }
        });
      }
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
  if (!runs.length && !segments.length) {
    list.innerHTML = '<div class="hint">No analyses or segments yet. Use “+ New run” to start one.</div>';
    $$("#analyses-reader").innerHTML = '<div class="hint">Nothing to read yet.</div>';
    return;
  }

  if (runs.length) {
    const urlRun = navFromUrl().run;
    const toOpen = (urlRun && runs.some((r) => r.stem === urlRun)) ? urlRun : runs[0].stem;
    await loadAnalysis(toOpen, { push: false, openReport: !!urlRun });
  } else {
    $$("#analyses-reader").innerHTML =
      '<div class="hint">No Deep Research runs yet — pick a segment to run one, choose a written report, or hit “+ New run”.</div>';
  }
}

async function loadAnalysis(
  stem: string,
  { push = true, openReport = push }: { push?: boolean; openReport?: boolean } = {},
) {
  const reader = $$("#analyses-reader");
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
    reader.innerHTML = `<div class="status err">${esc((e as Error).message)}</div>`;
    return;
  }
  const meta: Partial<AnalysisRun> = state.analysesRuns.find((r) => r.stem === stem) || {};
  const sources = rec.sources || {};
  const citations = sources.citations || [];
  const age = relAge(meta.generated_at);

  let prompt = "";
  const promptSegment = promptSegmentFor(meta);
  if (promptSegment) {
    try {
      prompt = (await api("/api/deep-prompt?segment=" + encodeURIComponent(promptSegment))).prompt || "";
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
  const delBtn = el("button", "ghost analysis-delete", "Delete analysis");
  delBtn.type = "button";
  delBtn.title = "Delete this Deep Research run and all its artifacts";
  delBtn.addEventListener("click", () => deleteAnalysis(stem));
  head.appendChild(delBtn);
  reader.appendChild(head);

  // The report itself — verbatim Perplexity output, framed as a collapsible
  // document so a long report can be folded away while reading the Q&A below.
  if (rec.report) {
    const doc = el("details", "report-doc");
    // Keep the automatically selected newest report folded so the Reports
    // landing page is a useful index, not a 14,000px wall. Explicit selections
    // and deep links still open the document immediately.
    doc.open = openReport;
    const sum = el("summary", "report-doc-head");
    sum.innerHTML =
      `<span class="report-doc-caret" aria-hidden="true">\u203a</span>` +
      `<span class="report-doc-title">Deep Research report</span>` +
      `<span class="report-doc-note">Verbatim Perplexity output — treat numbers as claims to verify</span>`;
    doc.appendChild(sum);
    const body = el("div", "report-doc-body prose");
    body.innerHTML = renderStructuredReport(rec.report) || mdToHtml(rec.report);
    const toc = buildReportToc(body);
    if (toc) body.insertBefore(toc, body.firstChild);
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
    box.querySelector(".synth-box-body")?.appendChild(det);
    reader.appendChild(box);
  }

  if (rec.review) {
    const box = synthBox("Review gate", "Local cross-check of the report against your holdings");
    box.querySelector(".synth-box-body")?.appendChild(el("div", "prose", mdToHtml(rec.review)));
    reader.appendChild(box);
  }

  // Names the report discusses beyond the segment's member list. Star one to
  // pull it into the optimizer pool (as a "curious" pick) with its provenance.
  const discovered: DiscoveredCandidate[] = rec.discovered_candidates || [];
  if (discovered.length) {
    const box = synthBox(
      `Candidates discovered (${discovered.length})`,
      "Tickers this report names beyond the segment — star to add to your pool");
    const list = el("div", "disc-list");
    list.innerHTML = discovered.map(discoveredRow).join("");
    box.querySelector(".synth-box-body")?.appendChild(list);
    reader.appendChild(box);
  }

  if (citations.length) {
    const box = synthBox(`Sources (${citations.length})`, "Citations extracted from the run");
    const ul = el("ol", "cite-list");
    citations.forEach((c: Citation) => {
      const li = el("li", "cite");
      let host = c.href || "";
      try { host = new URL(c.href ?? "").hostname.replace(/^www\./, ""); } catch { /* not a URL: keep raw href */ }
      const parts = String(c.label || "").split("\n").map((s) => s.trim()).filter(Boolean);
      const name = parts.find((p) => !/^https?:/i.test(p)) || host;
      const desc = parts.find((p) => !/^https?:/i.test(p) && p !== name) || "";
      li.innerHTML =
        (c.href ? `<a href="${esc(c.href)}" target="_blank" rel="noopener">${esc(name)}</a>` : esc(name)) +
        `<span class="cite-host">${esc(host)}</span>` +
        (desc ? `<div class="cite-desc">${esc(desc)}</div>` : "");
      ul.appendChild(li);
    });
    box.querySelector(".synth-box-body")?.appendChild(ul);
    reader.appendChild(box);
  }

  // Follow-up Q&A, grounded in this report. Only meaningful once a report exists.
  if (rec.report) {
    reader.appendChild(renderDeepQaCard(stem, meta.title || stem));
  }

  reader.scrollTop = 0;
}

export {
  analysisBadges,
  markActiveAnalysis,
  synthBox,
  startPipeline,
  openRunInAnalyses,
  loadAnalyses,
  loadAnalysis,
};
