// The two on-demand research cards plus the backend-config modal.
//   - renderAnalysisCard: the cheap tier -- a local agent-CLI reasoning pass over
//     the deterministic numbers, with the price-level editor and export/regen.
//   - renderDeepResearchCard: the expensive tier -- an on-demand Perplexity Deep
//     Research crawl that reuses the segment run/save/Q&A machinery.
//   - openAnalysisConfig: the modal to edit CLI backend order/models/web policy.
// Extracted from deepdive.ts; all I/O via /api/*, all rendering self-contained.
import { api, el, esc, pplxRequiredNotice } from "../core";
import { pollDeepJob } from "../jobs";
import { ensureTickerSet, linkifyTickers, mdToHtml } from "../analyses";
import { modelLabel, downloadText, pushNav, setActiveView } from "../shell";
import { decorateAnalysis, decorateSources } from "./decorate";
import { pinBlock } from "./pin";
import { priceLevelsBlock } from "./price-levels";

interface Rec {
  symbol?: string;
  currency?: string;
  price?: { value?: number | null } | null;
  sources?: Record<string, unknown> | null;
  as_of?: string | null;
  deep_analyses?: RelatedDeepAnalysis[];
}

interface Job {
  id: string;
  kind?: string;
  symbol?: string;
  segment?: string;
  state?: string;
}

interface DeepRun {
  stem: string;
  date?: string;
  source_count?: number;
  kind?: string;
  symbol?: string;
}

interface RelatedDeepAnalysis extends DeepRun {
  title?: string;
  segment?: string;
  relationship?: "member" | "mentioned" | "member+mentioned";
  has_review?: boolean;
}

interface ProviderCfg {
  id: string;
  enabled?: boolean;
  model?: string;
}

interface BackendConfig {
  providers: ProviderCfg[];
  allow_web?: boolean;
  timeout_sec?: number;
}

export function deepAnalysisRelationLabel(
  relationship: RelatedDeepAnalysis["relationship"],
): string {
  if (relationship === "member+mentioned") return "segment member · discussed";
  if (relationship === "member") return "segment member";
  return "mentioned in report";
}

export function renderRelatedDeepAnalyses(
  runs: RelatedDeepAnalysis[],
  openRun: (stem: string) => void,
): HTMLElement | null {
  if (!runs.length) return null;
  const section = el("section", "dr-related");
  section.innerHTML =
    `<div class="dr-related-head"><strong>Appears in ${runs.length} sector ` +
    `analys${runs.length === 1 ? "is" : "es"}</strong>` +
    `<span>Overlapping reports can carry different theses — open each for context.</span></div>`;
  const list = el("div", "dr-runs dr-related-runs");
  const appendRun = (run: RelatedDeepAnalysis) => {
    const btn = el("button", "dr-run-row dr-related-row");
    btn.type = "button";
    btn.title = `Open ${run.title || run.segment || run.stem} in Reports`;
    const relation = deepAnalysisRelationLabel(run.relationship);
    const sourceText = run.source_count
      ? `${run.source_count} source${run.source_count === 1 ? "" : "s"}`
      : "sources unavailable";
    btn.innerHTML =
      `<span class="dr-related-main"><strong>${esc(run.title || run.segment || run.stem)}</strong>` +
      `<span>${esc(relation)} · ${esc(run.date || "saved report")}</span></span>` +
      `<span class="dr-related-meta">${esc(sourceText)}${run.has_review ? " · reviewed" : ""}</span>` +
      `<span class="sx-go" aria-hidden="true">\u2197</span>`;
    btn.addEventListener("click", () => openRun(run.stem));
    list.appendChild(btn);
  };
  runs.slice(0, 4).forEach(appendRun);
  section.appendChild(list);
  if (runs.length > 4) {
    const more = el(
      "button",
      "ghost dr-related-more",
      `Show ${runs.length - 4} more sector analyses`,
    );
    more.type = "button";
    more.addEventListener("click", () => {
      runs.slice(4).forEach(appendRun);
      more.remove();
    });
    section.appendChild(more);
  }
  return section;
}

async function runningAnalysisJob(symbol: string): Promise<Job | null> {
  try {
    const res = await api(
      "/api/jobs", "GET", null, { reportError: false },
    );
    return (res.jobs || []).find(
      (j: Job) => j.kind === "ticker_analysis" && j.symbol === symbol &&
             (j.state === "running" || j.state === "queued")) || null;
  } catch (_e) {
    return null;
  }
}

// In-depth, on-demand analysis via the local agent CLIs (Claude -> Cursor).
// The cheap tier: a reasoning pass over the deterministic numbers above, no web
// crawl. The expensive, web-sourced tier is the Deep Research card below. Shows
// the latest saved note if one exists, otherwise a button to generate one.
export function renderAnalysisCard(rec: Rec): HTMLElement {
  const sym = rec.symbol || "";
  const card = el("div", "card analysis-card");
  const head = el("div", "analysis-head");
  head.appendChild(el("h2", "section", "In-depth analysis"));
  const cfgBtn = el("button", "ghost", "&#9881; Backends");
  cfgBtn.type = "button";
  cfgBtn.title = "Configure analysis backends";
  cfgBtn.addEventListener("click", openAnalysisConfig);
  head.appendChild(cfgBtn);
  card.appendChild(head);

  const status = el("div", "dd-status analysis-status");
  const body = el("div", "analysis-body");
  card.appendChild(status);
  card.appendChild(body);

  function renderRetry(refresh: boolean) {
    // The job died (bad response / timeout / lost). Leave the error in `status`
    // and give the user a way to run it again instead of a dead-end card.
    body.innerHTML = "";
    body.appendChild(el("p", "hint",
      "The analysis didn't finish. Backends fall back automatically " +
      "(Cursor, then Claude) \u2014 you can just run it again."));
    const actions = el("div", "analysis-actions");
    const retry = el("button", "primary", "\u21bb Try again");
    retry.type = "button";
    retry.addEventListener("click", () => run(refresh));
    actions.appendChild(retry);
    if (!refresh) {
      const reFresh = el("button", "ghost", "\u21bb Refresh data + analyse");
      reFresh.type = "button";
      reFresh.addEventListener("click", () => run(true));
      actions.appendChild(reFresh);
    }
    body.appendChild(actions);
  }

  async function run(refresh: boolean) {
    status.classList.remove("err");
    status.innerHTML = `<span class="spinner"></span> starting&hellip;`;
    body.innerHTML = "";
    try {
      const start = await api("/api/analyze/" + encodeURIComponent(sym), "POST", { refresh: !!refresh });
      await pollDeepJob(start.id, status, async () => { await show(); }, `Analyzing ${sym}`,
        () => renderRetry(refresh));
    } catch (e) {
      status.classList.add("err");
      status.textContent = "analysis failed: " + (e as Error).message;
      renderRetry(refresh);
    }
  }

  async function show() {
    // Re-attach to an already-running analysis (e.g. navigated away and back) so
    // the page keeps visualizing its progress rather than offering to start over.
    const live = await runningAnalysisJob(sym);
    if (live) {
      status.classList.remove("err");
      status.innerHTML = `<span class="spinner"></span> analysing&hellip;`;
      body.innerHTML = "";
      await pollDeepJob(live.id, status, async () => { await show(); }, `Analyzing ${sym}`,
        () => renderRetry(false));
      return;
    }
    let a;
    try {
      a = await api("/api/analysis/" + encodeURIComponent(sym));
    } catch (_e) {
      status.textContent = "";
      status.classList.remove("err");
      body.innerHTML = "";
      body.appendChild(el("p", "hint",
        `No in-depth analysis for <strong>${esc(sym)}</strong> yet. ` +
        `Runs locally via your agent CLI (Claude, then Cursor) over the data above &mdash; ` +
        `a skeptical, portfolio-aware note in ~30&ndash;60s.`));
      const btn = el("button", "primary", "Run in-depth analysis");
      btn.type = "button";
      btn.addEventListener("click", () => run(false));
      body.appendChild(btn);
      return;
    }
    await ensureTickerSet();
    const meta = a.meta || {};
    const when = meta.generated_at ? new Date(meta.generated_at).toLocaleString() : "";
    status.textContent = "";
    status.classList.remove("err");
    body.innerHTML =
      `<div class="analysis-meta">` +
      `<span class="abadge ok">${esc(meta.backend_label || "CLI")}</span>` +
      `<span class="muted">${esc(modelLabel(meta.model))}</span>` +
      (when ? `<span class="muted">${esc(when)}</span>` : "") +
      `</div><div class="prose analysis-prose prose-clamp"></div>`;
    const prose = body.querySelector(".analysis-prose") as HTMLElement;
    prose.innerHTML = mdToHtml(a.report || "");
    linkifyTickers(prose);
    decorateAnalysis(prose);
    decorateSources(prose, rec);
    // Long analyses dominate the page; clamp by default with an expander so the
    // verdict + price levels stay above the fold and the wall is opt-in.
    if ((a.report || "").length > 600) {
      const toggle = el("button", "linklike analysis-toggle", "Read full analysis \u25be");
      toggle.type = "button";
      toggle.addEventListener("click", () => {
        const open = prose.classList.toggle("expanded");
        toggle.textContent = open ? "Show less \u25b4" : "Read full analysis \u25be";
      });
      prose.insertAdjacentElement("afterend", toggle);
    } else {
      prose.classList.remove("prose-clamp");
    }
    // Price-level triggers go right under the meta bar (above the prose) so the
    // accept/lock affordance is the first thing seen after the verdict.
    let lockedMap;
    try {
      lockedMap = (await api("/api/price-levels")).levels || {};
    } catch (_e) {
      lockedMap = {};
    }
    let existingPin = null;
    try {
      const tm = await api("/api/target-model");
      const p = tm && tm.provenance && tm.provenance[sym];
      if (p && p.source === "user-pin") existingPin = p;
    } catch (_e) { /* no model yet — pinning still seeds one */ }
    body.insertBefore(priceLevelsBlock(rec, a, lockedMap[sym]), prose);
    body.insertBefore(pinBlock(rec, existingPin), prose);
    const actions = el("div", "analysis-actions");
    const re = el("button", "ghost", "&#8635; Regenerate");
    re.type = "button";
    re.addEventListener("click", () => run(false));
    const reFresh = el("button", "ghost", "&#8635; Refresh data + analyse");
    reFresh.type = "button";
    reFresh.addEventListener("click", () => run(true));
    const exportBtn = el("button", "ghost", "&#8615; Export .md");
    exportBtn.type = "button";
    exportBtn.title = "Download this analysis as a Markdown file";
    exportBtn.addEventListener("click", () => {
      const gen = meta.generated_at ? new Date(meta.generated_at) : new Date();
      const day = gen.toISOString().slice(0, 10);
      const footer = `\n\n---\n*Generated by ${meta.backend_label || "CLI"} (${modelLabel(meta.model)})` +
        `${meta.generated_at ? " on " + gen.toLocaleString() : ""}.*\n`;
      downloadText(`${sym}-analysis-${day}.md`, (a.report || "").trimEnd() + footer);
    });
    actions.appendChild(re);
    actions.appendChild(reFresh);
    actions.appendChild(exportBtn);
    body.appendChild(actions);
  }

  show();
  return card;
}

// The expensive tier: a single-name Perplexity Deep Research crawl, run on
// demand. It reuses the segment pipeline's run/save/Q&A machinery with a
// `ticker-<sym>` subject, so it never spends quota unless you ask, surfaces any
// past runs for reuse, and opens the full report (with follow-up Q&A) in the
// Reports reader. This is the systematic replacement for the old hand-authored
// "<sym> Detail" static pages.
export function renderDeepResearchCard(rec: Rec): HTMLElement {
  const sym = rec.symbol || "";
  const card = el("div", "card deepresearch-card");
  const head = el("div", "analysis-head");
  head.appendChild(el("h2", "section", "Deep Research"));
  head.appendChild(el("span", "muted dr-sub", "Web-sourced \u00b7 Perplexity \u00b7 on demand"));
  card.appendChild(head);

  const status = el("div", "dd-status dr-status");
  const body = el("div", "analysis-body");
  card.appendChild(status);
  card.appendChild(body);

  // Strip non-alphanumerics so a dossier symbol like "TUI1.DE" matches a saved
  // run's slug-derived symbol "TUI1-DE" without reimplementing the backend slug.
  const norm = (s: unknown) => String(s || "").replace(/[^a-z0-9]/gi, "").toUpperCase();
  const want = norm(sym);
  const relatedRuns = rec.deep_analyses || [];

  function openRun(stem: string) {
    pushNav({ view: "analyses", run: stem });
    setActiveView("analyses");
  }

  function runRowEl(r: DeepRun): HTMLElement {
    const btn = el("button", "dr-run-row");
    btn.type = "button";
    btn.title = "Open the full report and follow-up Q&A in Reports";
    const srcs = r.source_count
      ? ` \u00b7 ${r.source_count} source${r.source_count === 1 ? "" : "s"}` : "";
    btn.innerHTML =
      `<span class="dr-run-date">${esc(r.date || "saved report")}</span>` +
      `<span class="dr-run-meta">deep research${esc(srcs)}</span>` +
      `<span class="sx-go" aria-hidden="true">\u2197</span>`;
    btn.addEventListener("click", () => openRun(r.stem));
    return btn;
  }

  async function startRun() {
    status.classList.remove("err");
    body.innerHTML = "";
    status.innerHTML = `<span class="spinner"></span> building prompt&hellip;`;
    try {
      const p = await api("/api/deep-prompt?ticker=" + encodeURIComponent(sym));
      status.innerHTML =
        `<span class="spinner"></span> running Deep Research for ${esc(sym)}&hellip; ` +
        `this can take a few minutes`;
      const job = await api("/api/deep-research/run", "POST",
        { segment: p.segment, date: p.date, prompt: p.prompt });
      await pollDeepJob(job.id, status, async () => { await show(); },
        `Deep Research \u00b7 ${sym}`, async () => { await show(); });
    } catch (e) {
      status.classList.add("err");
      status.textContent = "deep research failed: " + (e as Error).message;
    }
  }

  function renderIdle(
    enabled: boolean,
    available: boolean | null,
    loggedIn: boolean | null,
    runs: DeepRun[],
  ) {
    status.textContent = "";
    status.classList.remove("err");
    body.innerHTML = "";
    const related = renderRelatedDeepAnalyses(relatedRuns, openRun);
    if (related) body.appendChild(related);
    if (runs.length) {
      body.appendChild(el("div", "dr-own-head", "Ticker-specific Deep Research"));
      const list = el("div", "dr-runs");
      runs.forEach((r) => list.appendChild(runRowEl(r)));
      body.appendChild(list);
    } else {
      body.appendChild(el("p", "hint",
        `No ticker-specific Deep Research for <strong>${esc(sym)}</strong> yet. The in-depth analysis ` +
        `above reasons over the data on this page; this spends a Perplexity Deep ` +
        `Research crawl for a fuller, web-sourced single-name report &mdash; a few ` +
        `minutes, and quota-limited, so it's opt-in.`));
    }
    // Perplexity is optional: with no session, don't offer a run at all -- just
    // a passive notice (past runs above stay readable, they need no login).
    if (!enabled || available === false || loggedIn === false) {
      const reason = !enabled ? "disabled" : available === false ? "unavailable" : "login";
      body.appendChild(pplxRequiredNotice("Deep Research", reason));
      return;
    }
    const actions = el("div", "analysis-actions");
    const btn = el("button", runs.length ? "ghost" : "primary",
      runs.length ? "\u21bb Run new Deep Research" : "Run Deep Research");
    btn.type = "button";
    btn.addEventListener("click", startRun);
    actions.appendChild(btn);
    body.appendChild(actions);
  }

  async function show() {
    status.innerHTML = `<span class="spinner"></span> loading&hellip;`;
    body.innerHTML = "";
    let runs: DeepRun[] = [];
    let enabled = true;
    let available: boolean | null = null;
    let loggedIn: boolean | null = null;
    let live: Job | null = null;
    try {
      const [runsRes, loginRes, jobsRes] = await Promise.all([
        api<{ runs?: DeepRun[] }>("/api/deep-runs").then((d) => d.runs || []).catch((): DeepRun[] => []),
        api<{ enabled?: boolean; logged_in?: boolean; deep_research_available?: boolean | null } | null>(
          "/api/deep-research/login-status",
        ).catch((): {
          enabled?: boolean;
          logged_in?: boolean;
          deep_research_available?: boolean | null;
        } | null => null),
        api<{ jobs?: Job[] }>(
          "/api/jobs", "GET", null, { reportError: false },
        ).then((d) => d.jobs || []).catch((): Job[] => []),
      ]);
      runs = runsRes
        .filter((r: DeepRun) => r.kind === "ticker" && norm(r.symbol) === want)
        .sort((a: DeepRun, b: DeepRun) => (a.stem < b.stem ? 1 : -1));
      enabled = loginRes?.enabled !== false;
      available = loginRes?.deep_research_available ?? null;
      loggedIn = loginRes ? !!loginRes.logged_in : null;
      live = jobsRes.find((j: Job) => j.kind === "deep_research" &&
        (j.state === "running" || j.state === "queued") &&
        norm(String(j.segment || "").replace(/^ticker-/, "")) === want) || null;
    } catch (_e) { /* fall through to idle */ }

    if (live) {
      status.innerHTML = `<span class="spinner"></span> Deep Research running&hellip;`;
      await pollDeepJob(live.id, status, async () => { await show(); },
        `Deep Research \u00b7 ${sym}`, async () => { await show(); });
      return;
    }
    renderIdle(enabled, available, loggedIn, runs);
  }

  show();
  return card;
}

// Lightweight modal to edit the CLI backend policy: which agents run, in what
// order (= fallback order), their model override, and whether web tools are on.
async function openAnalysisConfig(): Promise<void> {
  let payload;
  try {
    payload = await api("/api/analysis-config");
  } catch (e) {
    alert("Could not load analysis config: " + (e as Error).message);
    return;
  }
  const cfg: BackendConfig = payload.config;
  const available: Record<string, boolean> = payload.available || {};
  const labels: Record<string, string> = payload.labels || {};
  let models: Record<string, { value: string; label?: string }[]> = {};  // provider id -> options, filled async below
  const optsFor = (pid: string) =>
    (models[pid] || []).map((m) => `<option value="${esc(m.value)}">${esc(m.label || m.value)}</option>`).join("");

  const overlay = el("div", "modal-overlay");
  const panel = el("div", "modal");
  overlay.appendChild(panel);
  const close = () => overlay.remove();
  overlay.addEventListener("click", (e) => { if (e.target === overlay) close(); });

  function render() {
    panel.innerHTML =
      `<div class="modal-head"><h2 class="section">Analysis backends</h2></div>` +
      `<p class="hint">Tried top-to-bottom; the first that succeeds wins, and a quota/auth miss falls through to the next. Perplexity Deep Research is separate (whole-segment runs).</p>`;
    const list = el("div", "backend-list");
    cfg.providers.forEach((p, i) => {
      const row = el("div", "backend-row");
      const ok = available[p.id];
      row.innerHTML =
        `<div class="backend-rank">${i + 1}</div>` +
        `<label class="backend-name"><input type="checkbox" ${p.enabled ? "checked" : ""} data-k="enabled" data-i="${i}"> ${esc(labels[p.id] || p.id)}</label>` +
        `<span class="abadge ${ok ? "ok" : "bad"}">${ok ? "available" : "not found"}</span>` +
        `<input class="backend-model" type="text" placeholder="model (default)" value="${esc(p.model || "")}" data-k="model" data-i="${i}" list="bk-models-${esc(p.id)}" autocomplete="off">` +
        `<datalist id="bk-models-${esc(p.id)}">${optsFor(p.id)}</datalist>`;
      const up = el("button", "ghost backend-up", "&#8593;");
      up.type = "button";
      up.disabled = i === 0;
      up.title = "Move up (try sooner)";
      up.addEventListener("click", () => {
        [cfg.providers[i - 1], cfg.providers[i]] = [cfg.providers[i], cfg.providers[i - 1]];
        render();
      });
      row.appendChild(up);
      list.appendChild(row);
    });
    panel.appendChild(list);

    const opts = el("div", "backend-opts");
    opts.innerHTML =
      `<label><input type="checkbox" id="cfg-web" ${cfg.allow_web ? "checked" : ""}> Allow web research (Claude + Cursor, cited; slower &amp; fresher \u2014 off keeps it grounded purely in the data)</label>` +
      `<label>Timeout <input type="number" id="cfg-timeout" min="30" max="1200" value="${Number(cfg.timeout_sec) || 300}"> s</label>`;
    panel.appendChild(opts);

    const status = el("div", "dd-status");
    const actions = el("div", "modal-actions");
    const save = el("button", "primary", "Save");
    const cancel = el("button", "ghost", "Cancel");
    cancel.type = "button";
    cancel.addEventListener("click", close);
    save.type = "button";
    save.addEventListener("click", async () => {
      panel.querySelectorAll<HTMLInputElement>("[data-k]").forEach((inp) => {
        const i = Number(inp.dataset.i);
        if (inp.dataset.k === "enabled") cfg.providers[i].enabled = inp.checked;
        else cfg.providers[i].model = inp.value.trim();
      });
      cfg.allow_web = (panel.querySelector("#cfg-web") as HTMLInputElement).checked;
      cfg.timeout_sec = Number((panel.querySelector("#cfg-timeout") as HTMLInputElement).value) || 300;
      status.classList.remove("err");
      status.innerHTML = `<span class="spinner"></span> saving&hellip;`;
      try {
        await api("/api/analysis-config", "POST", { config: cfg });
        close();
      } catch (e) {
        status.classList.add("err");
        status.textContent = "save failed: " + (e as Error).message;
      }
    });
    actions.appendChild(cancel);
    actions.appendChild(save);
    panel.appendChild(actions);
    panel.appendChild(status);
  }

  render();
  document.body.appendChild(overlay);

  // Fill the autocomplete lists without re-rendering, so any in-progress edits
  // and the current row order survive.
  api("/api/analysis-models").then((r) => {
    models = r.models || {};
    cfg.providers.forEach((p) => {
      const dl = panel.querySelector("#bk-models-" + CSS.escape(p.id));
      if (dl) dl.innerHTML = optsFor(p.id);
    });
  }).catch(() => {});
}
