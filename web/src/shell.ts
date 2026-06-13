// @ts-nocheck
import { loadAnalyses, startPipeline } from "./analyses";
import { $, api, applyPrivacyMode, el, state } from "./core";
import { loadTickerFromCache } from "./deepdive";
import { loadDeepRun } from "./errors";
import { loadHoldings } from "./holdings";
import { initJournalControls, loadJournal } from "./journal";
import { loadPipeline, setPipeStep } from "./pipeline";
import { loadRebalance, openTicker } from "./rebalance";
import { initRiskControls, loadRisk } from "./risk";
import { loadCachedSegment, loadSegmentList } from "./segment";
import { loadSetup } from "./setup";
import { renderViewedTickers } from "./viewed";

// ---- location state --------------------------------------------------------
const VIEWS = new Set(["deepdive", "segment", "pipeline", "analyses", "rebalance", "risk", "journal", "holdings", "setup"]);

const cleanSymbol = (raw) => (raw || "").trim().toUpperCase();
const cleanSlug = (raw) => (raw || "").trim();
// Segment names are server slugs: lowercase alphanumerics + hyphens. Guards
// against junk (e.g. a "Failed to fetch" error string) being used as a segment.
const isSegmentSlug = (s) => /^[a-z0-9][a-z0-9-]*$/.test(s || "");

// Always surface which model produced an output. When no model was pinned the
// backend used its own default, which we can't name precisely, so say so.
const modelLabel = (m) => (m && m !== "(default)" ? m : "default model");

// Trigger a client-side download of text content as a file.
function downloadText(filename, text) {
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

function navFromUrl() {
  const params = new URLSearchParams(window.location.search);
  const view = VIEWS.has(params.get("view")) ? params.get("view") : "deepdive";
  return {
    view,
    ticker: cleanSymbol(params.get("ticker")),
    segment: cleanSlug(params.get("segment")),
    run: cleanSlug(params.get("run")),
  };
}

function urlForNav(nav) {
  const url = new URL(window.location.href);
  url.search = "";
  url.hash = "";
  if (nav.view && nav.view !== "deepdive") url.searchParams.set("view", nav.view);
  if (nav.ticker) url.searchParams.set("ticker", cleanSymbol(nav.ticker));
  if (nav.segment) url.searchParams.set("segment", cleanSlug(nav.segment));
  if (nav.run) url.searchParams.set("run", cleanSlug(nav.run));
  return url;
}

function pushNav(partial, { replace = false } = {}) {
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

function navForView(view) {
  const nav = { view };
  if (view === "deepdive") nav.ticker = cleanSymbol($("#ticker-input").value);
  if (view === "segment") nav.segment = cleanSlug($("#segment-select").value);
  if (view === "pipeline") {
    nav.segment = cleanSlug($("#pipe-segment-select").value || $("#pipe-slug").value);
    if (state.currentDeepRun) nav.run = state.currentDeepRun;
  }
  if (view === "analyses" && state.currentAnalysis) nav.run = state.currentAnalysis;
  return nav;
}

function setActiveView(view) {
  const active = VIEWS.has(view) ? view : "deepdive";
  document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.view === active));
  document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
  $("#view-" + active).classList.add("active");
  if (active === "holdings") loadHoldings();
  if (active === "pipeline") loadPipeline();
  if (active === "analyses") loadAnalyses();
  if (active === "rebalance") loadRebalance();
  if (active === "risk") { initRiskControls(); loadRisk(); }
  if (active === "journal") { initJournalControls(); loadJournal(); }
  if (active === "setup") loadSetup();
  return active;
}

function setSegmentControls(segment) {
  if (!segment) return;
  const seg = $("#segment-select");
  const pipe = $("#pipe-segment-select");
  if (seg && Array.from(seg.options).some((o) => o.value === segment)) seg.value = segment;
  if (pipe && Array.from(pipe.options).some((o) => o.value === segment)) pipe.value = segment;
  const slug = $("#pipe-slug");
  if (slug && !slug.value) slug.value = segment;
}

async function restoreNav(nav) {
  const active = setActiveView(nav.view);
  if (nav.ticker) $("#ticker-input").value = nav.ticker;
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
  if (active === "deepdive") $("#ticker-input").focus();
}

// ---- tabs / app shell wiring ----------------------------------------------
// Select-to-analyze: highlighting a ticker-shaped token in a report/summary pops
// a chip to open it -- the escape hatch for symbols we never auto-linked and have
// no data for. The user asserts it's a ticker; openTicker live-pulls on a miss.
let _selChip = null;
function hideSelChip() { if (_selChip) _selChip.hidden = true; }
function maybeShowSelChip() {
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed || !sel.rangeCount) return hideSelChip();
  const raw = sel.toString().trim();
  if (!/^[A-Za-z][A-Za-z.\-]{0,6}$/.test(raw)) return hideSelChip();  // ticker-shaped only
  const node = sel.anchorNode;
  const host = node && (node.nodeType === 3 ? node.parentElement : node);
  if (!host || !host.closest(".report-doc-body, .biz-summary, .prose")) return hideSelChip();
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
  document.querySelectorAll(".tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      pushNav(navForView(btn.dataset.view));
      restoreNav(navFromUrl());
    });
  });

  window.addEventListener("popstate", (event) => {
    restoreNav(event.state || navFromUrl());
  });

  $("#privacy-toggle").addEventListener("click", () => applyPrivacyMode(!state.privacyMode));

  $("#analyses-new").addEventListener("click", () => startPipeline());

  // Ticker links inside rendered reports / summaries are SPA-internal: intercept
  // and route to the deep-dive instead of a full navigation.
  document.addEventListener("click", (e) => {
    const a = e.target.closest ? e.target.closest("a.tlink") : null;
    if (!a) return;
    e.preventDefault();
    if (a.dataset.ticker) openTicker(a.dataset.ticker);
  });

  document.addEventListener("mouseup", () => setTimeout(maybeShowSelChip, 0));
  document.addEventListener("keyup", (e) => { if (e.shiftKey || e.key === "Shift") setTimeout(maybeShowSelChip, 0); });
  document.addEventListener("scroll", hideSelChip, true);
  window.addEventListener("resize", hideSelChip);
  document.addEventListener("mousedown", (e) => {
    if (_selChip && !_selChip.hidden && !(e.target.closest && e.target.closest(".sel-analyze"))) hideSelChip();
  });

  $("#hold-sync").addEventListener("click", async () => {
    const btn = $("#hold-sync");
    const status = $("#hold-status");
    const prev = btn.textContent;
    btn.disabled = true;
    btn.textContent = "Syncing…";
    status.classList.remove("err");
    status.textContent = "Re-pulling portfolio from IBKR (read-only, can take a minute)…";
    try {
      const res = await api("/api/holdings/sync", "POST", {});
      await loadHoldings();
      status.textContent = "Synced. " + planMsg(res && res.site);
    } catch (e) {
      status.textContent = "Sync failed: " + e.message;
      status.classList.add("err");
    } finally {
      btn.disabled = false;
      btn.textContent = prev;
    }
  });

  const regen = $("#plan-regen");
  if (regen) {
    regen.addEventListener("click", async () => {
      const status = $("#hold-status");
      const prev = regen.textContent;
      regen.disabled = true;
      regen.textContent = "Regenerating…";
      status.classList.remove("err");
      try {
        const res = await api("/api/site/regenerate", "POST", {});
        status.textContent = planMsg(res);
      } catch (e) {
        status.textContent = "Regenerate failed: " + e.message;
        status.classList.add("err");
      } finally {
        regen.disabled = false;
        regen.textContent = prev;
      }
    });
  }
}

// Human-readable summary of a generate_site.regenerate() result.
function planMsg(site) {
  if (!site) return "Plan not regenerated.";
  if (site.ok === false) return "Plan not regenerated: " + (site.error || "unknown error");
  const n = (site.written || []).length;
  if (!site.has_model && n === 0) return "Plan unchanged (no target model found).";
  return n ? `Plan regenerated (${n} file${n === 1 ? "" : "s"} updated).` : "Plan already up to date.";
}

export {
  initShell,
  VIEWS,
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
