import { loadAnalyses, startPipeline } from "./analyses";
import { initBasket, loadBasket } from "./basket";
import { $, api, applyPrivacyMode, el, esc, instrumentBadge, state } from "./core";
import { loadTickerFromCache } from "./deepdive";
import { loadDeepRun, pollDeepJob } from "./errors";
import { initHistoryControls, loadHistory } from "./history";
import { loadHoldings } from "./holdings";
import { initJournalControls, loadJournal } from "./journal";
import { loadPipeline, setPipeStep } from "./pipeline";
import { initOptimizer, loadOptimizer } from "./optimizer";
import { loadRebalance, openTicker } from "./rebalance";
import { initRiskControls, loadRisk } from "./risk";
import { loadCachedSegment, loadSegmentList } from "./segment";
import { loadSetup } from "./setup";
import { initStaging, loadStaging } from "./staging";
import { initStrategy, loadStrategy } from "./strategy";
import { loadTrade } from "./trade";
import { getViewedMap, renderViewedTickers } from "./viewed";

// ---- header ticker autocomplete --------------------------------------------
// Suggestions are drawn ONLY from data we already have locally: the server's
// cached ticker index (/api/ticker-index -- symbols we've pulled/analysed) plus
// browser-local recents. The index is fetched once with a short TTL and filtered
// in-memory, so typing never hits the network. We intentionally do NOT call the
// live /api/symbol-search here -- that's for discovering symbols we DON'T have.
let _tkIndex: any[] | null = null;
let _tkIndexAt = 0;

async function tickerSuggestRows() {
  if (!_tkIndex || Date.now() - _tkIndexAt > 60000) {
    try { _tkIndex = (await api("/api/ticker-index")).tickers || []; }
    catch (_e) { _tkIndex = _tkIndex || []; }
    _tkIndexAt = Date.now();
  }
  const map: Record<string, any> = {};
  (_tkIndex || []).forEach((r) => {
    map[r.symbol] = {
      symbol: r.symbol, name: r.name || "", type: r.type || "",
      has_analysis: !!r.has_analysis, viewed: "",
    };
  });
  // Local recents fold in (and win on freshness ordering) so a ticker you just
  // looked at is offered immediately, even before the server index refreshes.
  const recents = getViewedMap();
  Object.keys(recents).forEach((sym) => {
    const m = map[sym] || (map[sym] = { symbol: sym, name: "", has_analysis: false });
    if (!m.name && recents[sym].name) m.name = recents[sym].name;
    m.viewed = recents[sym].ts || "";
  });
  return Object.values(map);
}

// Prefix-on-symbol beats prefix-on-name beats substring; empty query shows the
// most recently viewed. Capped so the dropdown never becomes a wall.
function rankTickerRows(rows: any[], q: string) {
  q = (q || "").trim().toUpperCase();
  if (!q) {
    return rows.filter((r) => r.viewed)
      .sort((a, b) => (b.viewed || "").localeCompare(a.viewed || ""))
      .slice(0, 8);
  }
  const scored: [number, any][] = [];
  rows.forEach((r) => {
    const sym = r.symbol.toUpperCase();
    const name = (r.name || "").toUpperCase();
    let s = -1;
    if (sym === q) s = 100;
    else if (sym.startsWith(q)) s = 80;
    else if (name.startsWith(q)) s = 55;
    else if (sym.includes(q)) s = 40;
    else if (name.includes(q)) s = 20;
    if (s >= 0) scored.push([s, r]);
  });
  scored.sort((a, b) => b[0] - a[0] || a[1].symbol.localeCompare(b[1].symbol));
  return scored.slice(0, 8).map((x) => x[1]);
}

function tickerMenuHtml(rows: any[]) {
  return rows.map((r) =>
    `<div class="topsearch-item" role="option" data-sym="${esc(r.symbol)}">` +
      `<span class="ts-sym">${esc(r.symbol)}</span>` +
      `<span class="ts-name">${esc(r.name || "")}</span>` +
      instrumentBadge(r.type) +
      (r.has_analysis ? `<span class="ts-badge">analysis</span>` : "") +
    `</div>`).join("");
}

function wireTickerSearch(input: HTMLInputElement) {
  const menu = $("#top-ticker-menu");
  if (!menu) return;

  const render = async (showRecents: boolean) => {
    const list = rankTickerRows(await tickerSuggestRows(), showRecents ? "" : input.value);
    menu.innerHTML = tickerMenuHtml(list);
    menu.hidden = !list.length;
    input.setAttribute("aria-expanded", list.length ? "true" : "false");
  };
  const close = () => { menu.hidden = true; input.setAttribute("aria-expanded", "false"); };
  const items = () => [...menu.querySelectorAll(".topsearch-item")];
  const setActive = (its: Element[], idx: number) => {
    its.forEach((it) => it.classList.remove("active"));
    if (idx >= 0 && its[idx]) { its[idx].classList.add("active"); its[idx].scrollIntoView({ block: "nearest" }); }
  };
  const choose = (raw: string) => {
    const sym = cleanSymbol(raw);
    if (!sym) return;
    close();
    input.value = "";
    input.blur();
    openTicker(sym);
  };
  const submit = () => {
    const active = menu.hidden ? null : menu.querySelector<HTMLElement>(".topsearch-item.active");
    choose(active ? active.dataset.sym : input.value);
  };

  input.addEventListener("focus", () => render(true));
  input.addEventListener("input", () => render(false));
  input.addEventListener("blur", () => setTimeout(close, 120));
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); return submit(); }
    if (e.key === "Escape") return close();
    const its = items();
    if (menu.hidden || !its.length) return;
    const idx = its.findIndex((it) => it.classList.contains("active"));
    if (e.key === "ArrowDown") { e.preventDefault(); setActive(its, Math.min(its.length - 1, idx + 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setActive(its, Math.max(-1, idx - 1)); }
  });
  // mousedown (not click) so the pick lands before the input's blur closes us.
  menu.addEventListener("mousedown", (e) => {
    const it = (e.target as HTMLElement).closest<HTMLElement>(".topsearch-item");
    if (!it) return;
    e.preventDefault();
    choose(it.dataset.sym);
  });
}

// ---- location state --------------------------------------------------------
const VIEWS = new Set(["strategy", "deepdive", "segment", "pipeline", "analyses", "optimizer", "rebalance", "working-draft", "trade", "risk", "journal", "holdings", "history", "basket", "setup"]);

// Two-level navigation: the header exposes three top-level GROUPS, each of which
// fans out to a set of VIEWS via a secondary sub-tab bar. The URL still carries
// the flat `view` (so deep links + history stay stable); the group is derived.
// `segment` is intentionally absent from any sub-tab bar -- it's folded into the
// research flow (reached via the pipeline's deterministic pull or a segment row)
// rather than being a destination of its own. `setup` (the gear) sits outside
// the group bar entirely.
const VIEW_GROUP: Record<string, string> = {
  strategy: "strategy",
  deepdive: "deepdive",
  analyses: "research", pipeline: "research", segment: "research",
  holdings: "portfolio", history: "portfolio", optimizer: "portfolio", rebalance: "portfolio", "working-draft": "portfolio", trade: "portfolio", risk: "portfolio", journal: "portfolio",
  basket: "basket",
  setup: "setup",
};
// Which sub-tab lights up for a given view. Holdings + History are merged behind
// one "positions" sub-tab (toggled Now/Over-time inside the views themselves).
const VIEW_SUBTAB: Record<string, string> = {
  analyses: "analyses", pipeline: "pipeline",
  holdings: "positions", history: "positions", optimizer: "optimizer", rebalance: "rebalance", "working-draft": "working-draft", trade: "trade", risk: "risk", journal: "journal",
};
const GROUP_DEFAULT: Record<string, string> = { strategy: "strategy", deepdive: "deepdive", research: "analyses", portfolio: "holdings", basket: "basket" };
// Remember the last view visited within each group so re-clicking a group header
// returns you where you were, not always to the group's default.
const lastViewByGroup: Record<string, string> = { strategy: "strategy", deepdive: "deepdive", research: "analyses", portfolio: "holdings", basket: "basket" };

const cleanSymbol = (raw: string | null | undefined) => (raw || "").trim().toUpperCase();
const cleanSlug = (raw: string | null | undefined) => (raw || "").trim();
// Segment names are server slugs: lowercase alphanumerics + hyphens. Guards
// against junk (e.g. a "Failed to fetch" error string) being used as a segment.
const isSegmentSlug = (s: string | null | undefined) => /^[a-z0-9][a-z0-9-]*$/.test(s || "");

// Always surface which model produced an output. When no model was pinned the
// backend used its own default, which we can't name precisely, so say so.
const modelLabel = (m: string | null | undefined) => (m && m !== "(default)" ? m : "default model");

// A resolved location: the flat view plus its optional target identifiers.
interface NavState {
  view?: string;
  ticker?: string;
  segment?: string;
  run?: string;
}

// Trigger a client-side download of text content as a file.
function downloadText(filename: string, text: string) {
  const blob = new Blob([text], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = el("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

// Parse the query string, tolerating links whose separators got percent-encoded
// by an external encoder (chat/markdown renderers love to turn "?a=b&c=d" into
// "?a%3Db%26c%3Dd"). That mangled form parses as a single junk key with no value,
// so every param reads back null and we'd silently fall back to the default view.
// Detect it (encoded '=' present but no real '=' pair) and decode once.
function parseSearch(search = window.location.search) {
  let raw = (search || "").replace(/^\?/, "");
  if (raw && !raw.includes("=") && /%3d/i.test(raw)) {
    try { raw = decodeURIComponent(raw); } catch { /* malformed escape: use as-is */ }
  }
  return new URLSearchParams(raw);
}

function navFromUrl() {
  const params = parseSearch();
  const view = VIEWS.has(params.get("view")) ? params.get("view") : "deepdive";
  return {
    view,
    ticker: cleanSymbol(params.get("ticker")),
    segment: cleanSlug(params.get("segment")),
    run: cleanSlug(params.get("run")),
  };
}

function urlForNav(nav: NavState) {
  const url = new URL(window.location.href);
  url.search = "";
  url.hash = "";
  if (nav.view && nav.view !== "deepdive") url.searchParams.set("view", nav.view);
  if (nav.ticker) url.searchParams.set("ticker", cleanSymbol(nav.ticker));
  if (nav.segment) url.searchParams.set("segment", cleanSlug(nav.segment));
  if (nav.run) url.searchParams.set("run", cleanSlug(nav.run));
  return url;
}

function pushNav(partial: Partial<NavState>, { replace = false }: { replace?: boolean } = {}) {
  const next = {
    ...navFromUrl(),
    ticker: "",
    segment: "",
    run: "",
    ...partial,
  };
  const method = replace ? "replaceState" : "pushState";
  window.history[method](next, "", urlForNav(next));
  return next;
}

function navForView(view: string) {
  const nav: { view: string; ticker?: string; segment?: string; run?: string } = { view };
  if (view === "deepdive") nav.ticker = cleanSymbol($<HTMLInputElement>("#ticker-input").value);
  if (view === "segment") nav.segment = cleanSlug($<HTMLSelectElement>("#segment-select").value);
  if (view === "pipeline") {
    nav.segment = cleanSlug($<HTMLSelectElement>("#pipe-segment-select").value || $<HTMLInputElement>("#pipe-slug").value);
    if (state.currentDeepRun) nav.run = state.currentDeepRun;
  }
  if (view === "analyses" && state.currentAnalysis) nav.run = state.currentAnalysis;
  return nav;
}

// Sync the header chrome (group buttons, sub-tab bar, positions toggle) to the
// active view. Kept separate from data loading so navigation logic stays legible.
function updateChrome(active: string) {
  const group = VIEW_GROUP[active] || "deepdive";
  // Research has no sub-tab bar: "New run" (pipeline) is a sub-page reached via the
  // button and left via its Back button, so the Research group header always
  // returns to Reports rather than remembering pipeline as the last view.
  if (lastViewByGroup[group]) lastViewByGroup[group] = (group === "research") ? "analyses" : active;

  document.querySelectorAll<HTMLElement>(".group").forEach((b) => b.classList.toggle("active", b.dataset.group === group));
  document.querySelectorAll<HTMLElement>(".tab").forEach((b) => b.classList.toggle("active", b.dataset.view === active));

  // The sub-tab bar only exists for groups that fan out (research, portfolio).
  const subbar = $("#subbar");
  const groupHasSubtabs = group === "portfolio";
  if (subbar) subbar.hidden = !groupHasSubtabs;
  document.querySelectorAll<HTMLElement>(".subtabs").forEach((s) => { s.hidden = s.dataset.group !== group; });
  const wantSub = VIEW_SUBTAB[active];
  document.querySelectorAll<HTMLElement>(".subtab").forEach((b) => b.classList.toggle("active", VIEW_SUBTAB[b.dataset.view] === wantSub));

  // Positions (holdings + history) share a Now / Over-time toggle in-content.
  document.querySelectorAll<HTMLElement>(".pos-toggle button").forEach((b) => {
    const isNow = b.dataset.pos === "now";
    b.classList.toggle("active", (isNow && active === "holdings") || (!isNow && active === "history"));
  });
}

function setActiveView(view: string) {
  const active = VIEWS.has(view) ? view : "deepdive";
  updateChrome(active);
  document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
  $("#view-" + active).classList.add("active");
  if (active === "strategy") loadStrategy();
  if (active === "holdings") loadHoldings();
  if (active === "history") { initHistoryControls(); loadHistory(); }
  if (active === "pipeline") loadPipeline();
  if (active === "analyses") loadAnalyses();
  if (active === "optimizer") loadOptimizer();
  if (active === "rebalance") loadRebalance();
  if (active === "working-draft") loadStaging();
  if (active === "trade") loadTrade();
  if (active === "risk") { initRiskControls(); loadRisk(); }
  if (active === "journal") { initJournalControls(); loadJournal(); }
  if (active === "basket") loadBasket();
  if (active === "setup") loadSetup();
  return active;
}

function setSegmentControls(segment: string | null | undefined) {
  if (!segment) return;
  const seg = $<HTMLSelectElement>("#segment-select");
  const pipe = $<HTMLSelectElement>("#pipe-segment-select");
  if (seg && Array.from(seg.options).some((o) => o.value === segment)) seg.value = segment;
  if (pipe && Array.from(pipe.options).some((o) => o.value === segment)) pipe.value = segment;
  const slug = $<HTMLInputElement>("#pipe-slug");
  if (slug && !slug.value) slug.value = segment;
}

// Open a saved Deep Research run in the pipeline view, landing on the review
// gate (Step 4) with the report/review loaded. Cross-view "open the full run"
// buttons must use this rather than setActiveView("pipeline") alone, which
// fires loadPipeline() and strands the user on the Step 1 segment chooser.
//
// Ordering is load-bearing: loadPipeline's refreshDeepRuns rebuilds
// state.savedRuns from the standalone deep-runs list, which can omit an
// orchestrated run's artifact. If that runs *after* loadDeepRun, it drops the
// stem we just registered and re-locks Step 4. So we await loadPipeline to full
// settle FIRST, then loadDeepRun (which re-adds the stem + refreshPipeLocks),
// then setPipeStep(4) last.
export async function openDeepRunInPipeline(stem: string): Promise<void> {
  const m = stem.match(/^(.*)-(\d{4}-\d{2}-\d{2})$/);
  pushNav(m ? { view: "pipeline", segment: m[1], run: stem } : { view: "pipeline", run: stem });
  setActiveView("pipeline");
  await loadPipeline();
  await loadDeepRun(stem, { push: false });
  setPipeStep(4);
}

async function restoreNav(nav: NavState) {
  const active = setActiveView(nav.view ?? "deepdive");
  if (nav.ticker) $<HTMLInputElement>("#ticker-input").value = nav.ticker;
  if (nav.segment || nav.run || active === "segment" || active === "pipeline") {
    await loadSegmentList();
    setSegmentControls(nav.segment);
  }
  if (active === "deepdive" && nav.ticker) {
    await loadTickerFromCache(nav.ticker);
  } else if (active === "segment" && nav.segment) {
    await loadCachedSegment(nav.segment);
  } else if (active === "pipeline" && nav.run) {
    await loadDeepRun(nav.run, { push: false });
    // Deep-linking to a run means "show me this run" -- land on the review gate
    // (Step 4) with its loaded report/review, not back on Step 1.
    setPipeStep(4);
  } else if (active === "deepdive") {
    await renderViewedTickers();
  }
  if (active === "deepdive") $<HTMLInputElement>("#ticker-input").focus();
}

// ---- tabs / app shell wiring ----------------------------------------------
// Select-to-analyze: highlighting a ticker-shaped token in a report/summary pops
// a chip to open it -- the escape hatch for symbols we never auto-linked and have
// no data for. The user asserts it's a ticker; openTicker live-pulls on a miss.
let _selChip: HTMLButtonElement | null = null;
function hideSelChip() { if (_selChip) _selChip.hidden = true; }
function maybeShowSelChip() {
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed || !sel.rangeCount) return hideSelChip();
  const raw = sel.toString().trim();
  if (!/^[A-Za-z][A-Za-z.-]{0,6}$/.test(raw)) return hideSelChip();  // ticker-shaped only
  const node = sel.anchorNode;
  const host = node && (node.nodeType === 3 ? node.parentElement : node);
  if (!host || !(host as Element).closest(".report-doc-body, .biz-summary, .prose")) return hideSelChip();
  const rect = sel.getRangeAt(0).getBoundingClientRect();
  if (!rect || (!rect.width && !rect.height)) return hideSelChip();
  if (!_selChip) {
    _selChip = el("button", "sel-analyze");
    _selChip.type = "button";
    _selChip.addEventListener("mousedown", (e) => e.preventDefault());  // keep selection
    _selChip.addEventListener("click", () => { const t = _selChip.dataset.ticker; hideSelChip(); if (t) openTicker(t); });
    document.body.appendChild(_selChip);
  }
  const sym = raw.toUpperCase();
  _selChip.dataset.ticker = sym;
  _selChip.textContent = `Analyze ${sym} \u2197`;
  _selChip.hidden = false;
  _selChip.style.top = `${Math.max(8, rect.top - 36)}px`;
  _selChip.style.left = `${Math.min(window.innerWidth - 150, Math.max(8, rect.left))}px`;
}
// All DOM wiring lives here and is invoked once from main()'s boot. It must NOT
// run at import time: the core<->errors<->shell import cycle can evaluate shell
// before core's `$`/`el`/`api` consts are initialized, throwing a TDZ error.
function initShell() {
  const goToView = (view: string) => {
    pushNav(navForView(view));
    restoreNav(navFromUrl());
  };

  // Direct view targets: the settings gear (.tab) and the secondary sub-tabs.
  document.querySelectorAll<HTMLElement>(".tab, .subtab").forEach((btn) => {
    btn.addEventListener("click", () => goToView(btn.dataset.view));
  });

  // Top-level group headers jump to wherever you last were in that group (or its
  // default on first visit), giving the three-item nav some memory.
  document.querySelectorAll<HTMLElement>(".group").forEach((btn) => {
    btn.addEventListener("click", () => {
      const group = btn.dataset.group;
      goToView(lastViewByGroup[group] || GROUP_DEFAULT[group] || "deepdive");
    });
  });

  // Persistent header search with autocomplete over tickers we already have.
  const topTicker = $<HTMLInputElement>("#top-ticker");
  if (topTicker) wireTickerSearch(topTicker);

  // Guided strategy view wiring (deferred here to dodge the import-cycle TDZ).
  initStrategy();
  initStaging();
  initBasket();
  initOptimizer();

  // Positions Now / Over-time toggle: two views (holdings, history) behind one
  // sub-tab. Delegated so it works for the toggle in either section.
  document.addEventListener("click", (e) => {
    const tgt = e.target as HTMLElement;
    const b = tgt.closest ? tgt.closest<HTMLElement>(".pos-toggle button") : null;
    if (!b) return;
    goToView(b.dataset.pos === "over" ? "history" : "holdings");
  });

  window.addEventListener("popstate", (event) => {
    restoreNav(event.state || navFromUrl());
  });

  $("#privacy-toggle").addEventListener("click", () => applyPrivacyMode(!state.privacyMode));

  $("#analyses-new").addEventListener("click", () => startPipeline());

  // New run is a sub-page of Reports now (no sub-tab); its Back button returns there.
  const pipeBack = $("#pipe-back");
  if (pipeBack) pipeBack.addEventListener("click", () => goToView("analyses"));

  // Ticker links inside rendered reports / summaries are SPA-internal: intercept
  // and route to the deep-dive instead of a full navigation.
  document.addEventListener("click", (e) => {
    const tgt = e.target as HTMLElement;
    const a = tgt.closest ? tgt.closest<HTMLElement>("a.tlink") : null;
    if (!a) return;
    e.preventDefault();
    if (a.dataset.ticker) openTicker(a.dataset.ticker);
  });

  document.addEventListener("mouseup", () => setTimeout(maybeShowSelChip, 0));

  // Publish the (variable) sticky topbar height as --topbar-h so sticky elements
  // below it (e.g. the deep-dive back bar) can pin flush without magic numbers.
  const topbar = document.querySelector<HTMLElement>("header.topbar");
  if (topbar) {
    const syncTopbarH = () =>
      document.documentElement.style.setProperty("--topbar-h", topbar.offsetHeight + "px");
    syncTopbarH();
    window.addEventListener("resize", syncTopbarH);
    if (window.ResizeObserver) new ResizeObserver(syncTopbarH).observe(topbar);
  }
  document.addEventListener("keyup", (e) => { if (e.shiftKey || e.key === "Shift") setTimeout(maybeShowSelChip, 0); });
  document.addEventListener("scroll", hideSelChip, true);
  window.addEventListener("resize", hideSelChip);
  document.addEventListener("mousedown", (e) => {
    const tgt = e.target as HTMLElement;
    if (_selChip && !_selChip.hidden && !(tgt.closest && tgt.closest(".sel-analyze"))) hideSelChip();
  });

  $<HTMLButtonElement>("#hold-sync").addEventListener("click", async () => {
    const btn = $<HTMLButtonElement>("#hold-sync");
    const status = $("#hold-status");
    if (btn.disabled) return;
    const prev = btn.textContent;
    btn.disabled = true;
    btn.textContent = "Syncing…";
    status.classList.remove("err");
    status.innerHTML = `<span class="spinner"></span> Re-pulling portfolio from IBKR (read-only, can take a minute)…`;
    try {
      // The sync now runs as a registered background job: start it, then poll the
      // shared job loop so it survives navigation and shows in the global pill.
      const job = await api("/api/holdings/sync", "POST", {});
      await pollDeepJob(job.id, status, async (done) => {
        await loadHoldings();
        status.textContent = "Synced. " + siteMsg((done.result as Record<string, any>)?.site);
      }, "IBKR sync");
    } catch (e) {
      status.textContent = "Sync failed: " + e.message;
      status.classList.add("err");
    } finally {
      btn.disabled = false;
      btn.textContent = prev;
    }
  });

}

// One generate_site.regenerate() result (now: the markdown holdings summary).
interface SiteRegen {
  ok?: boolean;
  error?: string;
  written?: string[];
}

// Human-readable tail for the sync status line, reporting whether the derived
// holdings summary was refreshed alongside the snapshot.
function siteMsg(site: SiteRegen | null | undefined) {
  if (!site) return "Summary not refreshed.";
  if (site.ok === false) return "Summary not refreshed: " + (site.error || "unknown error");
  return (site.written || []).length ? "Holdings summary refreshed." : "Holdings summary already up to date.";
}

export {
  initShell,
  VIEWS,
  parseSearch,
  cleanSymbol,
  cleanSlug,
  isSegmentSlug,
  modelLabel,
  downloadText,
  navFromUrl,
  urlForNav,
  pushNav,
  navForView,
  setActiveView,
  setSegmentControls,
  restoreNav,
  _selChip,
  hideSelChip,
  maybeShowSelChip,
};
