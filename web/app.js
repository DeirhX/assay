"use strict";

const state = {
  holdings: {},
  nav: null,
  lastSegment: null,
  segSort: { key: "research_score", dir: -1 },
  currentDeepRun: null,
  privacyMode: localStorage.getItem("financeRebalancingPrivacyMode") === "1",
  pplxLoggedIn: false,
  pipeStep: 1,
  segMode: "existing",
  repMode: "current",
  repManual: false,
  promptSegment: null,
  savedRuns: new Set(),
  deepRuns: [],
  analysesRuns: [],
  currentAnalysis: null,
  tickerSet: new Set(),
};

// ---- tiny helpers ---------------------------------------------------------
const $ = (sel, root = document) => root.querySelector(sel);
const el = (tag, cls, html) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html != null) n.innerHTML = html;
  return n;
};
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

// Coarse "x ago" for cache/report freshness labels. Returns "" for junk input.
function relAge(iso) {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const secs = Math.max(0, (Date.now() - then) / 1000);
  if (secs < 90) return "just now";
  const mins = secs / 60;
  if (mins < 90) return `${Math.round(mins)}m ago`;
  const hrs = mins / 60;
  if (hrs < 36) return `${Math.round(hrs)}h ago`;
  return `${Math.round(hrs / 24)}d ago`;
}

// Local date + hour:minute for snapshot/sync stamps (generated_at is ISO UTC).
function fmtStamp(iso) {
  if (!iso) return "n/a";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso).slice(0, 16).replace("T", " ");
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

const fmtPrice = (v) => (v == null ? "n/a" : "$" + Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
const fmtX = (v) => (v == null ? "n/a" : Number(v).toFixed(1) + "x");
const fmtPct = (v) => (v == null ? "n/a" : (v >= 0 ? "+" : "") + Number(v).toFixed(1) + "%");
const fmtB = (v) => {
  if (v == null) return "n/a";
  return Math.abs(v) >= 1000 ? "$" + (v / 1000).toFixed(2) + "T" : "$" + Number(v).toFixed(1) + "B";
};
const fmtShares = (v) => (v == null ? "n/a" : Number(v).toFixed(2) + "B");
const pctClass = (v) => (v == null ? "muted" : v > 0 ? "good" : v < 0 ? "bad" : "muted");
const fmtWeight = (v) => (v == null ? "n/a" : Number(v).toFixed(2) + "%");
const fmtSignedWeight = (v) => (v == null ? "n/a" : (v >= 0 ? "+" : "") + Number(v).toFixed(2) + "%");
const fmtCZK = (v) => {
  if (v == null) return "n/a";
  return Math.abs(v) >= 1000 ? Math.round(v).toLocaleString() : Number(v).toFixed(0);
};
const decisionClass = (v) => {
  if (["add_candidate", "accumulate"].includes(v)) return "good";
  if (["trim", "avoid"].includes(v)) return "bad";
  if (["watch"].includes(v)) return "warn";
  return "muted";
};
const scoreClass = (v) => (v == null ? "muted" : v >= 70 ? "good" : v >= 45 ? "warn" : "bad");
const sensitive = (html, label = "sensitive value") =>
  `<span data-sensitive title="${esc(label)}">${html}</span>`;

function applyPrivacyMode(on) {
  state.privacyMode = !!on;
  document.body.classList.toggle("privacy-mode", state.privacyMode);
  localStorage.setItem("financeRebalancingPrivacyMode", state.privacyMode ? "1" : "0");
  const btn = $("#privacy-toggle");
  if (btn) {
    btn.setAttribute("aria-pressed", state.privacyMode ? "true" : "false");
    btn.textContent = state.privacyMode ? "Privacy: on" : "Privacy: off";
  }
}

async function api(path, method = "GET", body = null) {
  const opt = { method, headers: {} };
  if (body) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const res = await fetch(path, opt);
  const data = await res.json().catch(() => ({ error: "bad response" }));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

// ---- location state --------------------------------------------------------
const VIEWS = new Set(["deepdive", "segment", "pipeline", "analyses", "holdings"]);

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
  } else if (active === "deepdive") {
    await renderViewedTickers();
  }
  if (active === "deepdive") $("#ticker-input").focus();
}

// ---- tabs -----------------------------------------------------------------
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
    await api("/api/holdings/sync", "POST", {});
    await loadHoldings();
  } catch (e) {
    status.textContent = "Sync failed: " + e.message;
    status.classList.add("err");
  } finally {
    btn.disabled = false;
    btn.textContent = prev;
  }
});

// ---- holdings -------------------------------------------------------------
async function loadHoldings() {
  const status = $("#hold-status");
  const out = $("#hold-result");
  status.textContent = "Loading portfolio snapshot...";
  try {
    const h = await api("/api/holdings");
    state.nav = h.net_asset_value;
    state.holdings = {};
    (h.positions || []).forEach((p) => { state.holdings[p.symbol] = p.percent_of_nav; });
    status.innerHTML =
      `NAV ${sensitive(`${Math.round(h.net_asset_value || 0).toLocaleString()} CZK`, "total NAV")} · ` +
      `invested ${sensitive(`${Math.round(h.invested_value || 0).toLocaleString()} CZK`, "invested value")}`;
    const synced = $("#hold-synced");
    if (synced) synced.textContent = h.generated_at ? `Last synced ${fmtStamp(h.generated_at)}` : "No snapshot yet";
    out.innerHTML = "";

    const rows = (h.positions || [])
      .slice()
      .sort((a, b) => (b.percent_of_nav || 0) - (a.percent_of_nav || 0));
    const weights = rows.map((p) => p.percent_of_nav || 0);
    const maxW = Math.max(1e-6, ...weights);
    const cum = (n) => weights.slice(0, n).reduce((s, w) => s + w, 0);

    // Concentration is the single most important fact about this book; state it.
    const banner = el("div", "conc-summary");
    banner.innerHTML =
      `<span>Top 2 <strong>${cum(2).toFixed(1)}%</strong></span>` +
      `<span>Top 5 <strong>${cum(5).toFixed(1)}%</strong></span>` +
      `<span>Top 10 <strong>${cum(10).toFixed(1)}%</strong></span>` +
      `<span class="muted">${rows.length} positions · weights = % of invested</span>`;
    out.appendChild(banner);

    const list = el("div", "pos-list");
    rows.forEach((p) => {
      const isOpt = p.asset_class === "OPT";
      const w = p.percent_of_nav || 0;
      // Tier by absolute concentration (flags the AMD/ARM problem on sight);
      // bar length is relative to the largest holding for visual ranking.
      const tier = isOpt ? "opt" : w >= 10 ? "core" : w >= 5 ? "large" : w >= 1 ? "mid" : "small";
      const barW = isOpt ? 0 : (w / maxW) * 100;
      const right = isOpt ? sensitive(`${fmtCZK(p.base_market_value)} CZK`, "absolute position value") : `${w.toFixed(2)}%`;
      const label = isOpt ? (p.description || p.symbol) : p.symbol;
      const tag = isOpt ? ` <span class="opt-tag">OPT</span>` : "";
      const row = el("div", "pos-row tier-" + tier);
      row.innerHTML =
        `<span class="pos-sym">${esc(label)}${tag}</span>` +
        `<span class="pos-bar-track"><span class="pos-bar" style="width:${barW.toFixed(2)}%"></span></span>` +
        `<span class="pos-w">${right}</span>`;
      row.title =
        (p.description || p.symbol) +
        (isOpt && p.broker_percent_of_nav != null
          ? ` · broker tagged ${p.broker_percent_of_nav}% of NAV (margin/notional artifact, ignored)`
          : ` · ${w.toFixed(2)}% of invested`);
      if (!isOpt) row.addEventListener("click", () => analyzeFromAnywhere(p.symbol));
      list.appendChild(row);
    });
    out.appendChild(list);
    out.appendChild(el("div", "hint",
      "Bar length \u221d weight. Colour = concentration: red >10% (single-name risk), amber 5\u201310%, blue 1\u20135%, grey <1%. Click a row to deep-dive."));
  } catch (e) {
    status.textContent = "Could not load holdings: " + e.message;
    status.classList.add("err");
  }
}

function analyzeFromAnywhere(sym) {
  const ticker = cleanSymbol(sym);
  if (!ticker) return;
  pushNav({ view: "deepdive", ticker });
  setActiveView("deepdive");
  $("#ticker-input").value = ticker;
  pullTicker(ticker, { push: false });
}

// Cache-first open for in-report ticker links: show what we already have
// instantly, and only hit the network (live pull) when there's no cached
// dossier. Browsing a report shouldn't trigger a slow pull per click.
async function openTicker(sym) {
  const ticker = cleanSymbol(sym);
  if (!ticker) return;
  pushNav({ view: "deepdive", ticker });
  setActiveView("deepdive");
  $("#ticker-input").value = ticker;
  const status = $("#dd-status");
  status.classList.remove("err");
  status.textContent = `Loading ${ticker}…`;
  try {
    const rec = await api("/api/research/" + encodeURIComponent(ticker));
    const hist = await api("/api/history/" + encodeURIComponent(ticker)).catch(() => ({ history: [] }));
    rec.history = hist.history || [];
    status.textContent = `Cached ${rec.symbol} from ${new Date(rec.as_of).toLocaleString()} — press Analyze to refresh`;
    renderDeepDive(rec);
  } catch (_e) {
    await pullTicker(ticker, { push: false });  // nothing cached -> pull live
  }
}

// ---- viewed tickers (browser-local recents) -------------------------------
const VIEWED_KEY = "rebal.viewedTickers";
let _viewedSort = "time";  // "time" | "name"

function getViewedMap() {
  try { return JSON.parse(localStorage.getItem(VIEWED_KEY) || "{}"); } catch (_e) { return {}; }
}
function recordView(sym, name) {
  sym = cleanSymbol(sym);
  if (!sym) return;
  const m = getViewedMap();
  m[sym] = { ts: new Date().toISOString(), name: name || (m[sym] && m[sym].name) || "" };
  try { localStorage.setItem(VIEWED_KEY, JSON.stringify(m)); } catch (_e) { /* private mode */ }
}
function relTime(iso) {
  const t = Date.parse(iso);
  if (!t) return "";
  const s = (Date.now() - t) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  const d = Math.floor(s / 86400);
  return d < 30 ? d + "d ago" : new Date(t).toLocaleDateString();
}

async function renderViewedTickers() {
  const out = $("#dd-result");
  out.innerHTML = "";
  $("#dd-status").textContent = "";
  const card = el("div", "card viewed-card");
  const head = el("div", "viewed-head");
  head.appendChild(el("h2", "section", "Viewed tickers"));
  const sortWrap = el("div", "viewed-sort");
  sortWrap.appendChild(el("span", "muted", "sort:"));
  [["time", "Recent"], ["name", "Name"]].forEach(([key, label]) => {
    const b = el("button", "chip" + (_viewedSort === key ? " active" : ""), label);
    b.type = "button";
    b.addEventListener("click", () => { _viewedSort = key; renderViewedTickers(); });
    sortWrap.appendChild(b);
  });
  head.appendChild(sortWrap);
  card.appendChild(head);
  const listWrap = el("div", "viewed-list");
  card.appendChild(listWrap);
  out.appendChild(card);

  let server = [];
  try { server = (await api("/api/ticker-index")).tickers || []; } catch (_e) { /* offline: local only */ }
  const viewed = getViewedMap();
  const bySym = {};
  server.forEach((r) => { bySym[r.symbol] = { ...r }; });
  Object.keys(viewed).forEach((sym) => {
    const row = bySym[sym] || (bySym[sym] = { symbol: sym, name: "", as_of: null, analyzed_at: null, has_analysis: false });
    row.last_viewed = viewed[sym].ts;
    if (!row.name && viewed[sym].name) row.name = viewed[sym].name;
  });
  const rows = Object.values(bySym);
  const timeOf = (r) => r.last_viewed || r.analyzed_at || r.as_of || "";
  if (_viewedSort === "name") rows.sort((a, b) => a.symbol.localeCompare(b.symbol));
  else rows.sort((a, b) => timeOf(b).localeCompare(timeOf(a)));

  if (!rows.length) {
    listWrap.appendChild(el("p", "hint", "No tickers yet. Analyze one above and it'll show up here."));
    return;
  }
  rows.forEach((r) => {
    const when = r.last_viewed ? `viewed ${relTime(r.last_viewed)}`
      : (r.as_of ? `pulled ${relTime(r.as_of)}` : "");
    const ana = r.has_analysis
      ? `<span class="abadge ok">analysis${r.analyzed_at ? " · " + esc(new Date(r.analyzed_at).toLocaleDateString()) : ""}</span>`
      : `<span class="abadge muted">no analysis</span>`;
    const row = el("button", "viewed-row",
      `<span class="viewed-sym">${esc(r.symbol)}</span>` +
      `<span class="viewed-name">${esc(r.name || "")}</span>` +
      `<span class="viewed-when muted">${esc(when)}</span>` + ana);
    row.type = "button";
    row.addEventListener("click", () => openTicker(r.symbol));
    listWrap.appendChild(row);
  });
}

// ---- deep dive ------------------------------------------------------------
$("#ticker-go").addEventListener("click", () => pullTicker($("#ticker-input").value));
$("#ticker-input").addEventListener("keydown", (e) => { if (e.key === "Enter") pullTicker($("#ticker-input").value); });
$("#ticker-recent").addEventListener("click", () => {
  $("#ticker-input").value = "";
  pushNav({ view: "deepdive", ticker: "" });
  setActiveView("deepdive");
  renderViewedTickers();
});

async function loadTickerFromCache(raw) {
  const sym = cleanSymbol(raw);
  if (!sym) return;
  const status = $("#dd-status");
  status.classList.remove("err");
  status.textContent = `Loading cached ${sym}...`;
  try {
    const rec = await api("/api/research/" + encodeURIComponent(sym));
    const hist = await api("/api/history/" + encodeURIComponent(sym)).catch(() => ({ history: [] }));
    rec.history = hist.history || [];
    status.textContent = `Loaded cached ${rec.symbol} from ${new Date(rec.as_of).toLocaleString()}`;
    renderDeepDive(rec);
  } catch (e) {
    status.textContent = `No saved data for ${sym} yet.`;
    status.classList.add("err");
    const out = $("#dd-result");
    out.innerHTML = "";
    const card = el("div", "card empty-ticker");
    card.innerHTML =
      `<h2 class="section">${esc(sym)}</h2>` +
      `<p class="hint">We haven't pulled <strong>${esc(sym)}</strong> yet. If you're sure it's a valid ticker, fetch it live from Yahoo / SEC / FMP.</p>`;
    const btn = el("button", "primary", `Pull live data for ${esc(sym)}`);
    btn.type = "button";
    btn.addEventListener("click", () => pullTicker(sym, { push: false }));
    card.appendChild(btn);
    out.appendChild(card);
  }
}

async function pullTicker(raw, { push = true } = {}) {
  const sym = cleanSymbol(raw);
  if (!sym) return;
  if (push) pushNav({ view: "deepdive", ticker: sym });
  setActiveView("deepdive");
  $("#ticker-input").value = sym;
  const status = $("#dd-status");
  status.classList.remove("err");
  status.innerHTML = `<span class="spinner"></span> Pulling ${esc(sym)} from live sources...`;
  $("#ticker-go").disabled = true;
  try {
    const rec = await api("/api/pull/" + encodeURIComponent(sym), "POST");
    const hist = await api("/api/history/" + encodeURIComponent(sym)).catch(() => ({ history: [] }));
    rec.history = hist.history || [];
    status.textContent = `Fetched ${rec.symbol} at ${new Date(rec.as_of).toLocaleString()}`;
    renderDeepDive(rec);
  } catch (e) {
    status.textContent = "Pull failed: " + e.message;
    status.classList.add("err");
    $("#dd-result").innerHTML = "";
  } finally {
    $("#ticker-go").disabled = false;
  }
}

const METRIC_ROWS = [
  ["market_cap_usd_b", "Market cap", fmtB],
  ["pe_ttm", "P/E (TTM)", fmtX],
  ["pe_fwd", "P/E (fwd)", fmtX],
  ["ps", "P/S", fmtX],
  ["revenue_ttm_usd_b", "Revenue TTM", fmtB],
  ["net_income_ttm_usd_b", "Net income TTM", fmtB],
  ["gross_margin_pct", "Gross margin", (v) => (v == null ? "n/a" : v.toFixed(0) + "%")],
  ["rev_growth_yoy_pct", "Rev growth YoY", fmtPct],
  ["shares_out_b", "Shares out", fmtShares],
];

function renderDeepDive(rec) {
  recordView(rec.symbol, rec.name);
  const out = $("#dd-result");
  out.innerHTML = "";
  const price = rec.price ? rec.price.value : null;
  const portfolio = rec.portfolio || {};
  const target = portfolio.target || {};
  const owned = portfolio.current_weight_pct ?? state.holdings[rec.symbol];
  const decision = rec.decision || "research";

  const card = el("div", "card");
  // header
  const head = el("div", "dd-head");
  head.innerHTML =
    `<span class="sym">${esc(rec.symbol)}</span>` +
    `<span class="name">${esc(rec.name || "")}</span>` +
    `<span class="decision-pill ${decisionClass(decision)}">${esc(decision.replace("_", " "))}</span>` +
    `<span class="price">${fmtPrice(price)} <small class="muted">${esc(rec.currency || "")}</small></span>`;
  card.appendChild(head);

  const sub = el("div", "dd-sub");
  sub.innerHTML =
    `<span>as of ${new Date(rec.as_of).toLocaleString()}</span>` +
    (owned != null ? `<span class="owned-pill">held: ${fmtWeight(owned)} NAV</span>` : `<span class="muted">not held</span>`) +
    (target.rule ? `<span>rule: <strong>${esc(target.rule)}</strong></span>` : `<span class="muted">no target rule</span>`);
  card.appendChild(sub);

  // source badges
  const badges = el("div", "badges");
  ["yahoo", "sec_edgar", "fmp"].forEach((s) => {
    const on = rec.sources && rec.sources[s];
    badges.appendChild(el("span", "badge " + (on ? "on" : "off"), (on ? "✓ " : "· ") + s.replace("_", " ")));
  });
  card.appendChild(badges);
  out.appendChild(card);

  out.appendChild(renderAnalysisCard(rec));
  out.appendChild(renderQaCard(rec));

  const biz = renderBusiness(rec);
  if (biz) out.appendChild(biz);

  const chart = renderPriceChart(rec);
  if (chart) out.appendChild(chart);

  // decision context
  const dcard = el("div", "card");
  dcard.appendChild(el("h2", "section", "Decision context"));
  const dgrid = el("div", "dossier-grid");
  const band = target.low != null && target.high != null ? `${fmtWeight(target.low)} - ${fmtWeight(target.high)}` : "n/a";
  const gap = portfolio.gap_to_band_pct == null ? "n/a" : fmtSignedWeight(portfolio.gap_to_band_pct);
  const targetKind = target.kind === "sleeve" ? `sleeve: ${target.sleeve}` : target.kind === "target" ? "single-name target" : "not modeled";
  [
    ["Current weight", fmtWeight(owned), portfolio.status ? portfolio.status.replace("_", " ") : "not held"],
    ["Target band", band, targetKind],
    ["Band gap", gap, "positive means room to add; negative means trim pressure"],
    ["Research role", decision.replace("_", " "), target.note || "No model note yet."],
  ].forEach(([label, val, note]) => {
    const cell = el("div", "metric-cell");
    cell.innerHTML = `<div class="label">${esc(label)}</div><div class="val">${esc(val)}</div><div class="src">${esc(note)}</div>`;
    dgrid.appendChild(cell);
  });
  dcard.appendChild(dgrid);
  out.appendChild(dcard);

  // cross-checks (the trust layer) -- the console's judgement on the data.
  // Collapsible, but defaults open whenever there is something to read so the
  // findings aren't hidden behind a click.
  const checks = rec.cross_checks || [];
  const hasErrors = !!(rec.errors && rec.errors.length);
  const meta = checks.length ? `${checks.length} check${checks.length === 1 ? "" : "s"}` : "no checks";
  const { details: trust, body: trustBody } = collapsibleCard(
    "Data trust" + dataQualityTag(checks),
    { meta, open: checks.length > 0 || hasErrors },
  );
  const list = el("div", "checks");
  if (!checks.length) {
    list.appendChild(el("div", "check INFO", `<span class="sev">INFO</span><span>No cross-checks produced.</span>`));
  }
  checks.forEach((c) => {
    list.appendChild(el("div", "check " + c.severity,
      `<span class="sev">${c.severity}</span><span><span class="metric">${esc(c.metric)}:</span> ${esc(c.message)}</span>`));
  });
  trustBody.appendChild(list);
  if (hasErrors) {
    trustBody.appendChild(el("div", "status err", "source errors: " + rec.errors.map(esc).join("; ")));
  }
  out.appendChild(trust);

  // metrics
  const mcard = el("div", "card");
  mcard.appendChild(el("h2", "section", "Valuation & fundamentals"));
  const grid = el("div", "metrics-grid");
  METRIC_ROWS.forEach(([key, label, fmt]) => {
    const node = rec.metrics ? rec.metrics[key] : null;
    const cell = el("div", "metric-cell");
    const srcLine = node ? sourceLine(node) : `<span class="muted">no data</span>`;
    cell.innerHTML =
      `<div class="label">${label}</div>` +
      `<div class="val">${node ? esc(fmt(node.value)) : "n/a"}</div>` +
      `<div class="src">${srcLine}</div>`;
    grid.appendChild(cell);
  });
  mcard.appendChild(grid);
  out.appendChild(mcard);

  // momentum
  const mo = rec.momentum || {};
  const mom = el("div", "card");
  mom.appendChild(el("h2", "section", "Momentum"));
  const mgrid = el("div", "metrics-grid");
  [["chg_1m_pct", "1 month"], ["chg_3m_pct", "3 months"], ["chg_6m_pct", "6 months"], ["chg_12m_pct", "12 months"], ["pct_below_52w_high", "vs 52w high"], ["high_52w", "52w high"], ["low_52w", "52w low"]].forEach(([k, lbl]) => {
    const v = mo[k];
    const isPct = k !== "high_52w" && k !== "low_52w";
    const cell = el("div", "metric-cell");
    cell.innerHTML = `<div class="label">${lbl}</div><div class="val ${isPct ? pctClass(v) : ""}">${isPct ? esc(fmtPct(v)) : esc(fmtPrice(v))}</div>`;
    mgrid.appendChild(cell);
  });
  mom.appendChild(mgrid);
  out.appendChild(mom);

  out.appendChild(renderHistory(rec));

  // thesis editor
  out.appendChild(renderThesis(rec));
}

// In-depth, on-demand analysis via the local agent CLIs (Claude -> Cursor).
// Cheap reasoning pass over the deterministic numbers above; Perplexity Deep
// Research stays reserved for whole-segment crawls. Shows the latest saved note
// if one exists, otherwise a button to generate one.
function renderAnalysisCard(rec) {
  const sym = rec.symbol;
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

  async function run(refresh) {
    status.classList.remove("err");
    status.innerHTML = `<span class="spinner"></span> starting&hellip;`;
    body.innerHTML = "";
    try {
      const start = await api("/api/analyze/" + encodeURIComponent(sym), "POST", { refresh: !!refresh });
      await pollDeepJob(start.id, status, async () => { await show(); });
    } catch (e) {
      status.classList.add("err");
      status.textContent = "analysis failed: " + e.message;
    }
  }

  async function show() {
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
      `</div><div class="prose analysis-prose"></div>`;
    const prose = body.querySelector(".analysis-prose");
    prose.innerHTML = mdToHtml(a.report || "");
    linkifyTickers(prose);
    decorateVerdict(prose);
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

// Stances the analysis prompt asks for (Accumulate / Hold / Trim / Avoid) plus
// common synonyms. The earliest occurrence in the verdict block wins, so a
// stance word buried in the justification (e.g. "Hold ... better to avoid
// adding") never overrides the leading call.
const VERDICT_STANCES = [
  { re: /\b(accumulate|accumulating|overweight|add(?:ing)?|buy|buying)\b/i, cls: "v-good", label: "Accumulate" },
  { re: /\b(trim|trimming|reduce|reducing|underweight|lighten)\b/i, cls: "v-warn", label: "Trim" },
  { re: /\b(avoid|sell|selling|exit|exiting)\b/i, cls: "v-bad", label: "Avoid" },
  { re: /\b(hold|holding|neutral|maintain)\b/i, cls: "v-hold", label: "Hold" },
];

// Colour-codes the verdict: a pill on the heading + the inline stance word, so
// the recommendation is unmissable when scanning the analysis.
function decorateVerdict(root) {
  const heads = [...root.querySelectorAll("h1,h2,h3,h4,h5,h6")];
  const vh = heads.find((h) => /^\s*verdict\b/i.test(h.textContent || ""));
  if (!vh) return;
  const block = [];
  for (let n = vh.nextElementSibling; n && !/^H[1-6]$/.test(n.tagName); n = n.nextElementSibling) block.push(n);
  const text = block.map((n) => n.textContent).join(" ");
  let best = null;
  VERDICT_STANCES.forEach((s) => {
    const m = text.match(s.re);
    if (m && (best === null || m.index < best.index)) best = { cls: s.cls, label: s.label, re: s.re, index: m.index };
  });
  if (!best) return;
  const pill = el("span", "verdict-pill " + best.cls, esc(best.label));
  vh.appendChild(document.createTextNode(" "));
  vh.appendChild(pill);
  for (const elBlock of block) {
    if (highlightFirstMatch(elBlock, best.re, "verdict-stance " + best.cls)) break;
  }
}

// Wraps the first regex match found in a text node under `host`. Returns true on
// a hit so callers can stop after the first occurrence.
function highlightFirstMatch(host, re, cls) {
  const walker = document.createTreeWalker(host, NodeFilter.SHOW_TEXT);
  let node;
  while ((node = walker.nextNode())) {
    const m = node.nodeValue.match(re);
    if (!m) continue;
    const tail = node.splitText(m.index);
    tail.nodeValue = tail.nodeValue.slice(m[0].length);
    const span = el("span", cls, esc(m[0]));
    tail.parentNode.insertBefore(span, tail);
    return true;
  }
  return false;
}

// Token + prompt-cache accounting for a Claude Q&A turn. A non-zero "cache read"
// means the heavy prefix (DATA + prior turns) was served from cache, not re-billed.
function qaUsageHtml(u) {
  if (!u || typeof u !== "object") return "";
  const r = u.cache_read_input_tokens, w = u.cache_creation_input_tokens;
  const inp = u.input_tokens, out = u.output_tokens;
  if ([r, w, inp, out].every((v) => v == null)) return "";
  const fmt = (n) => (n == null ? "0" : n >= 1000 ? (n / 1000).toFixed(1).replace(/\.0$/, "") + "k" : String(n));
  const parts = [];
  if (r != null || w != null) {
    const hit = (r || 0) > 0;
    parts.push(`<span class="${hit ? "qa-cache-hit" : ""}">cache ${fmt(r)} read \u00b7 ${fmt(w)} write</span>`);
  }
  if (inp != null) parts.push(`${fmt(inp)} new in`);
  if (out != null) parts.push(`${fmt(out)} out`);
  return `<div class="qa-usage" title="Anthropic prompt-cache + token usage for this turn">${parts.join(" \u00b7 ")}</div>`;
}

// Archived, continuable Q&A about the ticker. Same cheap CLI backends as the
// in-depth note; the whole thread is persisted server-side so it can be resumed
// across sessions. Renders the archive, then an input to ask the next question.
function renderQaCard(rec) {
  const sym = rec.symbol;
  const card = el("div", "card qa-card");
  const head = el("div", "analysis-head");
  head.appendChild(el("h2", "section", "Ask about " + esc(sym)));
  const clearBtn = el("button", "ghost", "Clear thread");
  clearBtn.type = "button";
  clearBtn.title = "Discard the archived Q&A and start fresh";
  head.appendChild(clearBtn);
  card.appendChild(head);

  const thread = el("div", "qa-thread");
  const status = el("div", "dd-status analysis-status");
  const form = el("div", "qa-form");
  const input = el("textarea", "qa-input");
  input.rows = 2;
  input.placeholder = `Ask a follow-up about ${sym} \u2014 grounded in the data above. Ctrl/\u2318+Enter to send.`;
  const askBtn = el("button", "primary", "Ask");
  askBtn.type = "button";
  form.appendChild(input);
  form.appendChild(askBtn);
  card.appendChild(thread);
  card.appendChild(status);
  card.appendChild(form);

  function renderThread(turns) {
    thread.innerHTML = "";
    if (!turns.length) {
      clearBtn.hidden = true;
      thread.appendChild(el("p", "hint",
        "No questions yet. Ask anything about the numbers, momentum, valuation, or how it sits " +
        "in your portfolio. The thread is archived so you can pick it up later."));
      return;
    }
    clearBtn.hidden = false;
    turns.forEach((t) => {
      if (t.role === "user") {
        const q = el("div", "qa-turn qa-q");
        q.appendChild(el("div", "qa-role", "You"));
        q.appendChild(el("div", "qa-text", esc(t.text)));
        thread.appendChild(q);
      } else {
        const a = el("div", "qa-turn qa-a");
        const meta = [t.backend_label, modelLabel(t.model),
                      t.ts ? relTime(t.ts) : null].filter(Boolean).map(esc).join(" \u00b7 ");
        a.appendChild(el("div", "qa-role", "Analyst" + (meta ? ` <span class="muted">${meta}</span>` : "")));
        const prose = el("div", "prose qa-prose");
        prose.innerHTML = mdToHtml(t.text || "");
        linkifyTickers(prose);
        a.appendChild(prose);
        const usage = qaUsageHtml(t.usage);
        if (usage) a.insertAdjacentHTML("beforeend", usage);
        thread.appendChild(a);
      }
    });
  }

  async function load() {
    let data;
    try { data = await api("/api/qa/" + encodeURIComponent(sym)); }
    catch (_e) { data = { turns: [] }; }
    await ensureTickerSet();
    renderThread(data.turns || []);
  }

  async function ask() {
    const q = input.value.trim();
    if (!q) return;
    askBtn.disabled = true;
    input.disabled = true;
    status.classList.remove("err");
    status.innerHTML = `<span class="spinner"></span> thinking&hellip;`;
    try {
      const start = await api("/api/qa/" + encodeURIComponent(sym), "POST", { question: q });
      await pollDeepJob(start.id, status, async () => {
        status.textContent = "";
        input.value = "";
        await load();
      });
    } catch (e) {
      status.classList.add("err");
      status.textContent = "question failed: " + e.message;
    } finally {
      askBtn.disabled = false;
      input.disabled = false;
    }
  }

  askBtn.addEventListener("click", ask);
  input.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); ask(); }
  });
  clearBtn.addEventListener("click", async () => {
    if (!confirm(`Clear the archived Q&A thread for ${sym}?`)) return;
    try {
      const data = await api("/api/qa/" + encodeURIComponent(sym), "POST", { clear: true });
      renderThread(data.turns || []);
    } catch (e) {
      status.classList.add("err");
      status.textContent = "clear failed: " + e.message;
    }
  });

  load();
  return card;
}

// Lightweight modal to edit the CLI backend policy: which agents run, in what
// order (= fallback order), their model override, and whether web tools are on.
async function openAnalysisConfig() {
  let payload;
  try {
    payload = await api("/api/analysis-config");
  } catch (e) {
    alert("Could not load analysis config: " + e.message);
    return;
  }
  const cfg = payload.config;
  const available = payload.available || {};
  const labels = payload.labels || {};
  let models = {};  // provider id -> [{value,label}], filled in async below
  const optsFor = (pid) =>
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
      `<label><input type="checkbox" id="cfg-web" ${cfg.allow_web ? "checked" : ""}> Allow web tools (slower, fresher; off keeps it grounded in the data)</label>` +
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
      panel.querySelectorAll("[data-k]").forEach((inp) => {
        const i = Number(inp.dataset.i);
        if (inp.dataset.k === "enabled") cfg.providers[i].enabled = inp.checked;
        else cfg.providers[i].model = inp.value.trim();
      });
      cfg.allow_web = panel.querySelector("#cfg-web").checked;
      cfg.timeout_sec = Number(panel.querySelector("#cfg-timeout").value) || 300;
      status.classList.remove("err");
      status.innerHTML = `<span class="spinner"></span> saving&hellip;`;
      try {
        await api("/api/analysis-config", "POST", { config: cfg });
        close();
      } catch (e) {
        status.classList.add("err");
        status.textContent = "save failed: " + e.message;
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

function renderBusiness(rec) {
  const p = rec.profile || {};
  if (!p.summary && !p.sector && !p.industry) return null;

  const card = el("div", "card biz-card");
  card.appendChild(el("h2", "section", "Business"));

  const bits = [];
  if (p.sector) bits.push(esc(p.sector));
  if (p.industry) bits.push(esc(p.industry));
  if (p.country) bits.push(esc(p.country));
  if (p.employees) bits.push(`${Number(p.employees).toLocaleString()} employees`);
  if (bits.length) card.appendChild(el("div", "biz-meta", bits.join(" · ")));
  if (p.website) {
    const host = String(p.website).replace(/^https?:\/\//, "").replace(/\/$/, "");
    card.appendChild(el("div", "biz-meta",
      `<a href="${esc(p.website)}" target="_blank" rel="noopener">${esc(host)} \u2197</a>`));
  }

  if (p.summary) {
    const body = el("p", "biz-summary clamp", esc(p.summary));
    card.appendChild(body);
    linkifyTickers(body);
    if (p.summary.length > 320) {
      const toggle = el("button", "linklike biz-toggle", "Show more");
      toggle.type = "button";
      toggle.addEventListener("click", () => {
        const open = body.classList.toggle("expanded");
        toggle.textContent = open ? "Show less" : "Show more";
      });
      card.appendChild(toggle);
    }
  }
  return card;
}

const PRICE_RANGES = [
  ["1mo", "1M"], ["3mo", "3M"], ["6mo", "6M"],
  ["1y", "1Y"], ["5y", "5Y"], ["max", "Max"],
];

function chartSvg(rec, history) {
  const points = (history.points || [])
    .map((p) => ({ date: p.date, close: Number(p.close) }))
    .filter((p) => p.date && Number.isFinite(p.close));
  if (points.length < 2) return null;

  const width = 760, height = 260;
  const pad = { top: 18, right: 18, bottom: 34, left: 58 };
  const innerW = width - pad.left - pad.right;
  const innerH = height - pad.top - pad.bottom;
  let min = Math.min(...points.map((p) => p.close));
  let max = Math.max(...points.map((p) => p.close));
  if (min === max) {
    min *= 0.98;
    max *= 1.02;
  }
  const buffer = (max - min) * 0.06;
  min -= buffer;
  max += buffer;

  const x = (i) => pad.left + (points.length === 1 ? 0 : (i / (points.length - 1)) * innerW);
  const y = (v) => pad.top + ((max - v) / (max - min)) * innerH;
  const line = points.map((p, i) => `${x(i).toFixed(1)},${y(p.close).toFixed(1)}`).join(" ");
  const area = `${pad.left},${height - pad.bottom} ${line} ${width - pad.right},${height - pad.bottom}`;
  const first = points[0], last = points[points.length - 1];
  const change = first.close ? (last.close / first.close - 1) * 100 : null;
  const trend = pctClass(change);
  const spanDays = (new Date(last.date) - new Date(first.date)) / 86400000;
  const dateLabel = (value) => {
    const d = new Date(value + "T00:00:00Z");
    return spanDays > 420
      ? d.toLocaleDateString(undefined, { month: "short", year: "numeric" })
      : d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  };
  const rangeLabel = [history.range, history.interval].filter(Boolean).join(" / ") || "daily closes";
  const sourceLabel = [history.source || "unknown", rangeLabel, `${points.length} points`].join(" · ");

  const lo = min + buffer, hi = max - buffer;
  const yTicks = 4;  // top, two interior, bottom
  const yAxis = Array.from({ length: yTicks }, (_, i) => {
    const v = hi - (i / (yTicks - 1)) * (hi - lo);
    const yp = y(v).toFixed(1);
    const interior = i > 0 && i < yTicks - 1;
    return (
      (interior ? `<line class="chart-grid" x1="${pad.left}" y1="${yp}" x2="${width - pad.right}" y2="${yp}"></line>` : "") +
      `<text class="chart-label" x="${pad.left - 10}" y="${yp}" text-anchor="end" dominant-baseline="middle">${esc(fmtPrice(v))}</text>`
    );
  }).join("");

  const svg =
    `<svg class="price-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="${esc(rec.symbol)} ${esc(rangeLabel)} price history">` +
      `<line class="chart-axis" x1="${pad.left}" y1="${height - pad.bottom}" x2="${width - pad.right}" y2="${height - pad.bottom}"></line>` +
      `<line class="chart-axis" x1="${pad.left}" y1="${pad.top}" x2="${pad.left}" y2="${height - pad.bottom}"></line>` +
      yAxis +
      `<text class="chart-label" x="${pad.left}" y="${height - 9}">${esc(dateLabel(first.date))}</text>` +
      `<text class="chart-label" x="${width - pad.right}" y="${height - 9}" text-anchor="end">${esc(dateLabel(last.date))}</text>` +
      `<polygon class="chart-area ${trend}" points="${area}"></polygon>` +
      `<polyline class="chart-line ${trend}" points="${line}"></polyline>` +
      `<circle class="chart-dot" cx="${x(points.length - 1).toFixed(1)}" cy="${y(last.close).toFixed(1)}" r="3.5"></circle>` +
    `</svg>`;
  const lastHtml = `<span>${esc(fmtPrice(last.close))}</span><strong class="${trend}">${esc(fmtPct(change))}</strong>`;
  return { svg, sourceLabel, lastHtml };
}

function renderPriceChart(rec) {
  const stored = rec.price_history || {};
  if (!chartSvg(rec, stored)) return null;

  const card = el("div", "card price-chart-card");
  const head = el("div", "chart-head");
  head.innerHTML =
    `<div><h2 class="section">Price history</h2><div class="chart-source"></div></div>` +
    `<div class="chart-last"></div>`;
  card.appendChild(head);
  const ranges = el("div", "chart-ranges");
  card.appendChild(ranges);
  const body = el("div", "chart-body");
  // The canvas keeps the last drawn chart while a new range loads, so the card's
  // height never changes; a transparent overlay just dims it and shows a spinner.
  const canvas = el("div", "chart-canvas");
  const overlay = el("div", "chart-overlay", `<span class="spinner"></span>`);
  body.appendChild(canvas);
  body.appendChild(overlay);
  card.appendChild(body);

  const srcEl = head.querySelector(".chart-source");
  const lastEl = head.querySelector(".chart-last");
  const cache = { "1y": stored };  // stored series is the 1y window; reuse it
  let active = "1y";

  function paint(history) {
    const drawn = chartSvg(rec, history);
    if (!drawn) {
      canvas.innerHTML = `<div class="hint">No price data for this range.</div>`;
      srcEl.textContent = "";
      lastEl.innerHTML = "";
      return;
    }
    canvas.innerHTML = drawn.svg;
    srcEl.textContent = drawn.sourceLabel;
    lastEl.innerHTML = drawn.lastHtml;
  }

  async function select(key, label, btn) {
    active = key;
    [...ranges.children].forEach((b) => b.classList.toggle("active", b === btn));
    if (cache[key]) { paint(cache[key]); return; }
    body.classList.add("loading");  // dim + spinner; chart stays put underneath
    try {
      const ph = await api(`/api/price-history/${encodeURIComponent(rec.symbol)}?range=${encodeURIComponent(key)}`);
      cache[key] = ph;
      if (active === key) paint(ph);
    } catch (e) {
      if (active === key) srcEl.innerHTML = `<span class="err">Could not load ${esc(label)}: ${esc(e.message)}</span>`;
    } finally {
      if (active === key) body.classList.remove("loading");
    }
  }

  PRICE_RANGES.forEach(([key, label]) => {
    const btn = el("button", "chart-range" + (key === "1y" ? " active" : ""), esc(label));
    btn.type = "button";
    btn.addEventListener("click", () => select(key, label, btn));
    ranges.appendChild(btn);
  });

  paint(stored);
  return card;
}

// A <details>-based card. `open` decides the initial state; the meta sits on the
// summary line so a collapsed card still tells you what's inside.
function collapsibleCard(titleHtml, { meta = "", open = false } = {}) {
  const details = el("details", "card collapse");
  details.open = !!open;
  const summary = el("summary", "collapse-head");
  summary.innerHTML =
    `<span class="collapse-title">${titleHtml}</span>` +
    (meta ? `<span class="collapse-meta">${meta}</span>` : "") +
    `<span class="collapse-caret" aria-hidden="true">\u203a</span>`;
  details.appendChild(summary);
  const body = el("div", "collapse-body");
  details.appendChild(body);
  return { details, body };
}

function renderHistory(rec) {
  const rows = rec.history || [];
  const meta = rows.length ? `${rows.length} snapshot${rows.length === 1 ? "" : "s"}` : "none yet";
  const { details, body } = collapsibleCard("Recent pulls", { meta });
  if (!rows.length) {
    body.appendChild(el("div", "hint", "No history yet. Pull this ticker again later and this becomes a change log instead of a memory test."));
    return details;
  }
  const table = el("table", "history-table");
  table.innerHTML =
    `<thead><tr><th>As of</th><th class="num">Price</th><th class="num">Fwd P/E</th><th class="num">P/S</th><th class="num">Revenue</th><th>Trust</th></tr></thead>`;
  const tbody = el("tbody");
  rows.slice(0, 8).forEach((h) => {
    const tr = el("tr");
    tr.innerHTML =
      `<td>${esc(h.as_of ? new Date(h.as_of).toLocaleString() : "n/a")}</td>` +
      `<td class="num">${esc(fmtPrice(h.price))}</td>` +
      `<td class="num">${esc(fmtX(h.pe_fwd))}</td>` +
      `<td class="num">${esc(fmtX(h.ps))}</td>` +
      `<td class="num">${esc(fmtB(h.revenue_ttm_usd_b))}</td>` +
      `<td><span class="dot ${esc(h.data_quality || "INFO")}"></span>${esc(h.data_quality || "INFO")}</td>`;
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  body.appendChild(table);
  return details;
}

function dataQualityTag(checks) {
  const sev = checks.some((c) => c.severity === "ERROR") ? "ERROR" : checks.some((c) => c.severity === "WARN") ? "WARN" : "INFO";
  const txt = { ERROR: "conflicts found", WARN: "minor disagreement", INFO: "clean" }[sev];
  return ` &nbsp;<span class="dot ${sev}"></span><span style="font-size:12px;color:var(--muted)">${txt}</span>`;
}

function sourceLine(node) {
  const all = node.all_sources || {};
  const keys = Object.keys(all);
  if (keys.length <= 1) return `source: ${esc(node.source)}`;
  // multiple sources -> show each and flag spread
  const vals = keys.map((k) => all[k]);
  const max = Math.max(...vals.map(Math.abs)), min = Math.min(...vals.map(Math.abs));
  const disagree = max > 0 && (max - min) / max > 0.05;
  const parts = keys.map((k) => `${k}:${Number(all[k]).toPrecision(4)}`).join("  ");
  return `<span class="${disagree ? "disagree" : ""}">${esc(parts)}</span>`;
}

function renderThesis(rec) {
  const t = rec.thesis || {};
  const hasContent = !!(t.summary || t.action || (t.drivers || []).length || (t.downside_triggers || []).length);
  const meta = t.as_of ? "saved " + new Date(t.as_of).toLocaleDateString() : "empty";
  const { details: card, body } = collapsibleCard(
    "Thesis &amp; action — your judgement (kept separate from the numbers)",
    { meta, open: hasContent },
  );
  const g = el("div", "thesis-grid");
  g.innerHTML =
    `<div><label>Summary</label><textarea id="th-summary" rows="4" placeholder="What's the story? Momentum vs valuation.">${esc(t.summary || "")}</textarea></div>` +
    `<div><label>Action</label><textarea id="th-action" rows="4" placeholder="Add / hold / trim / sell / wait — and sizing.">${esc(t.action || "")}</textarea></div>` +
    `<div><label>Drivers (one per line)</label><textarea id="th-drivers" rows="4" placeholder="Real reasons it moved">${esc((t.drivers || []).join("\n"))}</textarea></div>` +
    `<div><label>Downside triggers (one per line)</label><textarea id="th-triggers" rows="4" placeholder="What breaks the thesis">${esc((t.downside_triggers || []).join("\n"))}</textarea></div>`;
  body.appendChild(g);
  const actions = el("div", "thesis-actions");
  const saveBtn = el("button", "primary", "Save thesis");
  const note = el("span", "status", t.as_of ? "last saved " + new Date(t.as_of).toLocaleString() : "");
  saveBtn.addEventListener("click", async () => {
    saveBtn.disabled = true;
    note.classList.remove("err");
    note.textContent = "saving...";
    try {
      const payload = {
        summary: $("#th-summary").value,
        action: $("#th-action").value,
        drivers: $("#th-drivers").value.split("\n").map((s) => s.trim()).filter(Boolean),
        downside_triggers: $("#th-triggers").value.split("\n").map((s) => s.trim()).filter(Boolean),
      };
      const updated = await api("/api/thesis/" + encodeURIComponent(rec.symbol), "POST", payload);
      note.textContent = "saved " + new Date(updated.thesis.as_of).toLocaleString();
    } catch (e) {
      note.textContent = "save failed: " + e.message;
      note.classList.add("err");
    } finally {
      saveBtn.disabled = false;
    }
  });
  actions.appendChild(saveBtn);
  actions.appendChild(note);
  body.appendChild(actions);
  return card;
}

// ---- segment --------------------------------------------------------------
async function loadSegmentList() {
  const sel = $("#segment-select");
  const pipeSel = $("#pipe-segment-select");
  try {
    const { segments } = await api("/api/segments");
    sel.innerHTML = "";
    if (pipeSel) pipeSel.innerHTML = "";
    segments.forEach((s) => {
      const o = el("option");
      o.value = s.name;
      let cacheTag = "";
      if (s.cached) {
        const age = relAge(s.cached_at);
        cacheTag = age ? ` · cached ${age}` : " · cached";
      }
      o.textContent = `${s.title} (${s.count})${s.status === "draft" ? " · draft" : ""}${cacheTag}`;
      sel.appendChild(o);
      if (pipeSel) {
        const p = o.cloneNode(true);
        pipeSel.appendChild(p);
      }
    });
    return segments;
  } catch (e) {
    // Disabled + empty value so a transient /api/segments failure can't poison
    // the dropdown with a selectable bogus name (e.g. "Failed to fetch") that a
    // later load would send to /api/segment/<name>.
    const opt = `<option value="" disabled selected>couldn't load segments: ${esc(e.message)}</option>`;
    sel.innerHTML = opt;
    if (pipeSel) pipeSel.innerHTML = opt;
    return [];
  }
}

$("#segment-select").addEventListener("change", () => {
  if ($("#view-segment").classList.contains("active")) {
    pushNav({ view: "segment", segment: $("#segment-select").value }, { replace: true });
  }
});

$("#pipe-segment-select").addEventListener("change", () => {
  if ($("#view-pipeline").classList.contains("active")) {
    pushNav({ view: "pipeline", segment: $("#pipe-segment-select").value }, { replace: true });
  }
});

async function runSegmentPull(name, { push = true } = {}) {
  const status = $("#seg-status");
  name = cleanSlug(name);
  if (!name) return;
  if (!isSegmentSlug(name)) {
    status.textContent = "Pick a segment from the list first.";
    status.classList.add("err");
    return;
  }
  if (push) pushNav({ view: "segment", segment: name });
  setActiveView("segment");
  $("#segment-select").value = name;
  status.classList.remove("err");
  status.innerHTML = `<span class="spinner"></span> Pulling every peer in "${esc(name)}" live — this takes a bit...`;
  $("#segment-run").disabled = true;
  try {
    const rec = await api("/api/pull-segment/" + encodeURIComponent(name), "POST");
    status.textContent = `Pulled ${rec.members.length} names at ${new Date(rec.as_of).toLocaleString()}`;
    renderSegment(rec);
  } catch (e) {
    status.textContent = "Segment pull failed: " + e.message;
    status.classList.add("err");
  } finally {
    $("#segment-run").disabled = false;
  }
}

async function loadCachedSegment(name, { push = false } = {}) {
  const status = $("#seg-status");
  name = cleanSlug(name);
  if (!name) return;
  if (!isSegmentSlug(name)) {
    status.textContent = "Pick a segment from the list first.";
    status.classList.add("err");
    return;
  }
  if (push) pushNav({ view: "segment", segment: name });
  setActiveView("segment");
  $("#segment-select").value = name;
  status.classList.remove("err");
  status.textContent = "Loading cached segment...";
  try {
    const rec = await api("/api/segment/" + encodeURIComponent(name));
    status.textContent = `Cached ${rec.members.length} names from ${new Date(rec.as_of).toLocaleString()}`;
    renderSegment(rec);
  } catch (e) {
    status.textContent = e.message + " — run a live pull first.";
    status.classList.add("err");
  }
}

$("#segment-run").addEventListener("click", () => runSegmentPull($("#segment-select").value));
$("#segment-load").addEventListener("click", () => loadCachedSegment($("#segment-select").value, { push: true }));

const SEG_COLS = [
  ["symbol", "Symbol", false],
  ["decision", "Decision", false],
  ["research_score", "Score", true],
  ["sleeve", "Sleeve", false],
  ["owned_pct_nav", "Held %", true],
  ["price", "Price", true],
  ["market_cap_usd_b", "Mkt cap", true],
  ["pe_fwd", "Fwd P/E", true],
  ["ps", "P/S", true],
  ["rev_growth_yoy_pct", "Rev g", true],
  ["gross_margin_pct", "GM", true],
  ["chg_3m_pct", "3m", true],
  ["chg_12m_pct", "12m", true],
  ["pct_below_52w_high", "vs 52wH", true],
];

function renderSegment(rec) {
  state.lastSegment = rec;
  const out = $("#seg-result");
  out.innerHTML = "";
  const card = el("div", "card");
  card.appendChild(el("h2", "section", esc(rec.title) + " — peer comparison"));
  const table = el("table", "segment-table");
  const thead = el("thead");
  const htr = el("tr");
  SEG_COLS.forEach(([key, label, num]) => {
    const th = el("th", num ? "num" : "", esc(label));
    th.addEventListener("click", () => {
      const s = state.segSort;
      s.dir = s.key === key ? -s.dir : (num ? -1 : 1);
      s.key = key;
      renderSegment(state.lastSegment);
    });
    if (state.segSort.key === key) th.innerHTML += state.segSort.dir < 0 ? " ↓" : " ↑";
    htr.appendChild(th);
  });
  thead.appendChild(htr);
  table.appendChild(thead);

  const tbody = el("tbody");
  const rows = rec.members.slice().sort((a, b) => {
    const k = state.segSort.key, d = state.segSort.dir;
    let av = a[k], bv = b[k];
    if (typeof av === "string" || typeof bv === "string") return d * String(av ?? "").localeCompare(String(bv ?? ""));
    if (av == null) return 1; if (bv == null) return -1;
    return d * (av - bv);
  });
  rows.forEach((m) => {
    const tr = el("tr");
    const cells = [
      `<span class="dot ${m.data_quality}"></span><strong>${esc(m.symbol)}</strong>`,
      `<span class="decision-pill ${decisionClass(m.decision)}">${esc(String(m.decision || "research").replace("_", " "))}</span>`,
      `<span class="score-pill ${scoreClass(m.research_score)}">${m.research_score == null ? "n/a" : esc(m.research_score)}</span>`,
      `<span class="sleeve-tag">${esc(m.sleeve)}</span>`,
      m.owned_pct_nav != null ? `<span class="owned-pill">${m.owned_pct_nav.toFixed(1)}</span>` : `<span class="muted">–</span>`,
      fmtPrice(m.price),
      fmtB(m.market_cap_usd_b),
      fmtX(m.pe_fwd),
      fmtX(m.ps),
      `<span class="${pctClass(m.rev_growth_yoy_pct)}">${fmtPct(m.rev_growth_yoy_pct)}</span>`,
      m.gross_margin_pct == null ? "n/a" : m.gross_margin_pct.toFixed(0) + "%",
      `<span class="${pctClass(m.chg_3m_pct)}">${fmtPct(m.chg_3m_pct)}</span>`,
      `<span class="${pctClass(m.chg_12m_pct)}">${fmtPct(m.chg_12m_pct)}</span>`,
      `<span class="${pctClass(m.pct_below_52w_high)}">${fmtPct(m.pct_below_52w_high)}</span>`,
    ];
    SEG_COLS.forEach(([key, , num], i) => {
      tr.appendChild(el("td", num ? "num" : "", cells[i]));
    });
    tr.addEventListener("click", () => analyzeFromAnywhere(m.symbol));
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  card.appendChild(table);
  card.appendChild(el("div", "hint", "Score is a rough research queue heuristic from target rule, band gap, growth, valuation, momentum, and data trust. It is not an order signal, because we are not building a robot broker for future regret. Click a row to deep-dive."));
  out.appendChild(card);
}

// ---- pipeline -------------------------------------------------------------
// The pipeline is a strict sequence, not four free-floating panels. You may
// always step BACK to revisit earlier work, but you can only advance to a step
// once its prerequisite exists. The reachable frontier is derived from real
// data, so it stays honest no matter how you got here (URL, reload, back/fwd).
//   1 Segment      -> always available
//   2 Deep Research, 3 Report  -> need a chosen/approved segment
//   4 Review & apply           -> need a saved or loaded report artifact
function pipeCurrentStem() {
  const seg = pipeSegment();
  const date = ($("#pipe-date").value || "").trim();
  return seg && date ? `${seg}-${date}` : "";
}

// Step 4 needs a report saved on disk for THIS exact segment + date — that is
// the only thing the review gate can actually read. Anything weaker (a sticky
// "a run was loaded once" flag) lets you switch to an empty segment and hit a
// dead gate, which is exactly the bug being fixed.
function pipeHasSavedReport() {
  const stem = pipeCurrentStem();
  return !!stem && state.savedRuns.has(stem);
}

function pipeUnlockedMax() {
  if (pipeHasSavedReport()) return 4;
  if (pipeSegment()) return 3;
  return 1;
}

function pipeLockReason(n) {
  if (n >= 4) return "Save or import a report for this segment + date first — the review gate has nothing to read otherwise.";
  if (n >= 2) return "Choose or approve a segment on Step 1 first.";
  return "";
}

let _pipeLockTimer = null;
function showPipeLock(n) {
  const note = $("#pipe-lock-note");
  if (!note) return;
  note.textContent = pipeLockReason(n);
  note.hidden = false;
  clearTimeout(_pipeLockTimer);
  _pipeLockTimer = setTimeout(() => { note.hidden = true; }, 4500);
}

function setPipeStep(n, { silent = false } = {}) {
  n = Math.max(1, Math.min(4, Number(n) || 1));
  const max = pipeUnlockedMax();
  if (n > max) {
    if (!silent) showPipeLock(n);
    n = max;
  } else if (!silent) {
    const note = $("#pipe-lock-note");
    if (note) note.hidden = true;
  }
  state.pipeStep = n;
  document.querySelectorAll("#pipe-wizard .wizard-step").forEach((s) => {
    s.classList.toggle("active", Number(s.dataset.step) === n);
  });
  document.querySelectorAll("#pipe-stepper .step-pill").forEach((p) => {
    const s = Number(p.dataset.step);
    p.classList.toggle("active", s === n);
    p.classList.toggle("done", s < n);
    p.classList.toggle("locked", s > max);
  });
  if (n === 2) { updateStep2LoginGate(); refreshLoginStatus(); updateExistingReportNotice(); }
  if (n === 3) updateRepSubstate();
  const w = $("#pipe-wizard");
  if (w && !silent) w.scrollIntoView({ behavior: "smooth", block: "start" });
}

// Re-evaluate the locked frontier after data changes (segment picked, report
// saved/loaded, pipeline reset) without forcing a navigation.
function refreshPipeLocks() {
  setPipeStep(state.pipeStep, { silent: true });
}

document.querySelectorAll("#pipe-stepper .step-pill").forEach((p) => {
  p.addEventListener("click", () => {
    const s = Number(p.dataset.step);
    if (s > pipeUnlockedMax()) { showPipeLock(s); return; }
    setPipeStep(s);
  });
});
document.querySelectorAll("#pipe-wizard .step-next, #pipe-wizard .step-back").forEach((b) => {
  b.addEventListener("click", () => {
    const goto = Number(b.dataset.goto);
    if (b.classList.contains("step-next") && goto > pipeUnlockedMax()) { showPipeLock(goto); return; }
    setPipeStep(goto);
  });
});

$("#pipe-restart").addEventListener("click", () => {
  state.currentDeepRun = null;
  state.repManual = false;
  ["#pipe-report", "#pipe-sources", "#pipe-source-url", "#pipe-prompt"].forEach((sel) => {
    const elx = $(sel);
    if (elx) elx.value = "";
  });
  updateStep2Actions();
  setRepMode("current");
  pushNav({ view: "pipeline", segment: pipeSegment() }, { replace: true });
  setPipeStep(1);
});

$("#pipe-segment-select").addEventListener("change", () => {
  pushNav({ view: "pipeline", segment: pipeSegment() }, { replace: true });
  refreshPipeLocks();
  updateExistingReportNotice();
});


async function loadPipeline() {
  await loadSegmentList();
  // A launch from the Analyses pane stashes the segment to preselect here, since
  // the dropdown only exists after loadSegmentList has populated it.
  if (state.pipePreselect) {
    setSegmentControls(state.pipePreselect);
    state.pipePreselect = null;
  }
  await refreshDeepRuns();
  refreshLoginStatus();
  setSegMode(state.segMode);
  setRepMode(state.repMode);
  updateStep2Actions();
  setPipeStep(state.pipeStep);
  if (!$("#pipe-date").value) $("#pipe-date").value = new Date().toISOString().slice(0, 10);
}

// Step 1 shows exactly one path at a time: an approved-segment dropdown, or a
// new-segment drafter that only reveals its editor + approve action after a draft.
function setSegMode(mode) {
  mode = mode === "new" ? "new" : "existing";
  state.segMode = mode;
  $("#seg-pane-existing").hidden = mode !== "existing";
  $("#seg-pane-new").hidden = mode !== "new";
  $("#seg-mode-existing").classList.toggle("active", mode === "existing");
  $("#seg-mode-new").classList.toggle("active", mode === "new");
  const cont = $("#pipe-step1-continue");
  const note = $("#pipe-step1-note");
  if (mode === "existing") {
    cont.hidden = false;
    note.textContent = "Pick a segment, then continue.";
  } else {
    // In "new" mode the single forward action is Approve & continue, revealed
    // only after a draft exists -- so the footer Continue is out of the way.
    cont.hidden = true;
    note.textContent = "Draft a theme, review it, then approve to continue.";
    $("#seg-draft-editor").hidden = !$("#pipe-slug").value.trim();
  }
}

$("#seg-mode-existing").addEventListener("click", () => setSegMode("existing"));
$("#seg-mode-new").addEventListener("click", () => setSegMode("new"));

// Step 3 is one-lane too: review the report this run produced (or paste one you
// ran yourself), OR import an existing run. Never both at once.
function setRepMode(mode) {
  mode = mode === "import" ? "import" : "current";
  state.repMode = mode;
  $("#rep-pane-current").hidden = mode !== "current";
  $("#rep-pane-import").hidden = mode !== "import";
  $("#rep-mode-current").classList.toggle("active", mode === "current");
  $("#rep-mode-import").classList.toggle("active", mode === "import");
  updateRepSubstate();
}

// A report "result" is known for the current segment + date once a run/import/
// load has populated the report body and tagged it as the current run.
function pipeHasRunResult() {
  const stem = pipeCurrentStem();
  return !!stem && state.currentDeepRun === stem && !!($("#pipe-report").value || "").trim();
}

// Step 3 "This run's report" is itself step-by-step: until a report actually
// exists, show only the run action and keep the finished-report fields hidden.
// The Perplexity URL is read-only for an automated run (it is filled by the
// run); only a manual "I ran it elsewhere" paste makes it editable. Continue to
// Review stays blocked until a report is saved on disk.
function updateRepSubstate() {
  const pending = $("#rep-current-pending");
  const done = $("#rep-current-done");
  if (!pending || !done) return;
  const hasResult = pipeHasRunResult() || state.repManual;
  pending.hidden = hasResult;
  done.hidden = !hasResult;
  const url = $("#pipe-source-url");
  if (url) {
    const editable = state.repManual && !pipeHasRunResult();
    url.toggleAttribute("readonly", !editable);
  }
  const next = $("#pipe-step3-next");
  if (next) {
    const ok = pipeHasSavedReport();
    next.disabled = !ok;
    next.title = ok ? "" : pipeLockReason(4);
  }
}

$("#rep-mode-current").addEventListener("click", () => setRepMode("current"));
$("#rep-mode-import").addEventListener("click", () => setRepMode("import"));

function pipeSegment() {
  if (state.segMode === "new") return $("#pipe-slug").value.trim() || $("#pipe-segment-select").value;
  return $("#pipe-segment-select").value || $("#pipe-slug").value.trim();
}

function parseJsonField(sel, fallback) {
  const raw = $(sel).value.trim();
  if (!raw) return fallback;
  return JSON.parse(raw);
}

$("#pipe-draft").addEventListener("click", async () => {
  const status = $("#pipe-segment-status");
  status.classList.remove("err");
  status.textContent = "drafting...";
  try {
    const rec = await api("/api/segment-draft", "POST", { query: $("#pipe-query").value });
    $("#pipe-slug").value = rec.slug;
    $("#pipe-segment-json").value = JSON.stringify(rec.definition, null, 2);
    $("#pipe-prompt").value = rec.llm_prompt || "";
    $("#seg-draft-editor").hidden = false;
    status.textContent = rec.warnings && rec.warnings.length ? rec.warnings.join(" ") : "draft ready; review it, then approve to continue";
  } catch (e) {
    status.textContent = "draft failed: " + e.message;
    status.classList.add("err");
  }
});

$("#pipe-save-segment").addEventListener("click", async () => {
  const status = $("#pipe-segment-status");
  status.classList.remove("err");
  status.textContent = "saving segment...";
  try {
    const slug = $("#pipe-slug").value.trim();
    const definition = parseJsonField("#pipe-segment-json", {});
    definition.status = "approved";
    const rec = await api("/api/segment-def/" + encodeURIComponent(slug), "POST", { definition });
    status.textContent = `saved ${rec.name} — continuing to Deep Research`;
    await loadSegmentList();
    $("#pipe-segment-select").value = rec.name;
    setSegMode("existing");
    pushNav({ view: "pipeline", segment: rec.name }, { replace: true });
    setPipeStep(2);
  } catch (e) {
    status.textContent = "save failed: " + e.message;
    status.classList.add("err");
  }
});

// Step 2 shows one primary action at a time: "Build prompt" until a prompt
// exists, then "Run Deep Research". Rebuild + deterministic pull are secondary.
function updateStep2Actions() {
  const hasPrompt = !!$("#pipe-prompt").value.trim();
  $("#pipe-build-prompt").hidden = hasPrompt;
  $("#pipe-run-deep").hidden = !hasPrompt;
  $("#pipe-rebuild-prompt").hidden = !hasPrompt;
}

// Most recent saved run for `seg` that actually has a report on disk. Stems are
// `${seg}-YYYY-MM-DD`; the date check stops a segment like "ai" from matching
// "ai-software-...". Lexical desc sort on the stem orders by date newest-first.
function latestReportForSegment(seg) {
  if (!seg) return null;
  const prefix = seg + "-";
  const matches = (state.deepRuns || [])
    .filter((r) => r.files && r.files.report && r.stem.startsWith(prefix)
      && /^\d{4}-\d{2}-\d{2}$/.test(r.stem.slice(prefix.length)))
    .sort((a, b) => (a.stem < b.stem ? 1 : -1));
  return matches[0] || null;
}

// Deep Research spends quota, so if we already have a report for this segment,
// surface it on Step 2 and let the user reuse it instead of running a new one.
// This needs no login (reuse is read-only), so it sits above the login gate.
function updateExistingReportNotice() {
  const box = $("#pipe-existing");
  if (!box) return;
  const run = latestReportForSegment(pipeSegment());
  if (!run) { box.hidden = true; box.dataset.stem = ""; return; }
  const date = (run.stem.match(/-(\d{4}-\d{2}-\d{2})$/) || [])[1] || "";
  box.dataset.stem = run.stem;
  $("#pipe-existing-text").textContent =
    `This segment already has a saved Deep Research report${date ? ` from ${date}` : ""}. Reuse it instead of spending a new run?`;
  box.hidden = false;
}

$("#pipe-existing-use").addEventListener("click", async () => {
  const stem = $("#pipe-existing").dataset.stem;
  if (!stem) return;
  await loadDeepRun(stem);
  setPipeStep(3);
});

// Deep Research only works through a logged-in Perplexity session. When we are
// not logged in, block the prompt workflow behind the login gate and insist the
// user sets it up first. The deterministic pull and the Step 3 import path stay
// reachable, so this gates the prompt, not the whole step.
function updateStep2LoginGate() {
  const gate = $("#pipe-login-gate");
  const area = $("#pipe-prompt-area");
  const blocked = !state.pplxLoggedIn;
  if (gate) gate.hidden = !blocked;
  if (area) area.hidden = blocked;
  if (!blocked) { updateStep2Actions(); maybeAutoBuildPrompt(); }
}

// Step 2 builds the prompt for you the moment you land on it (and rebuilds it if
// you arrived with a different segment than the one the current prompt is for).
// The textarea is just there to tweak the result before running. "Build prompt"
// stays as a manual fallback for when auto-build fails. A manual edit for the
// same segment is preserved (not clobbered) because the prompt is non-empty and
// not stale.
async function maybeAutoBuildPrompt() {
  if (state.pipeStep !== 2 || !state.pplxLoggedIn || state._autoBuilding) return;
  const seg = pipeSegment();
  if (!seg) return;
  const stale = !!state.promptSegment && state.promptSegment !== seg;
  if ($("#pipe-prompt").value.trim() && !stale) return;
  state._autoBuilding = true;
  try { await buildPrompt(); } finally { state._autoBuilding = false; }
}

async function buildPrompt() {
  const status = $("#pipe-prompt-status");
  const seg = pipeSegment();
  status.classList.remove("err");
  if (!state.pplxLoggedIn) {
    updateStep2LoginGate();
    $("#pipe-login-gate-status").textContent = "Set up the Perplexity login first.";
    return;
  }
  if (!seg) {
    status.classList.add("err");
    status.textContent = "pick or approve a segment on Step 1 first";
    return;
  }
  status.textContent = "building prompt...";
  try {
    const rec = await api("/api/deep-prompt?segment=" + encodeURIComponent(seg));
    $("#pipe-date").value = rec.date;
    $("#pipe-prompt").value = rec.prompt;
    state.promptSegment = rec.segment || seg;
    pushNav({ view: "pipeline", segment: rec.segment || seg }, { replace: true });
    status.textContent = "prompt ready — review it, then run Deep Research";
    updateStep2Actions();
  } catch (e) {
    status.textContent = "prompt failed: " + e.message;
    status.classList.add("err");
  }
}

$("#pipe-build-prompt").addEventListener("click", buildPrompt);
$("#pipe-rebuild-prompt").addEventListener("click", buildPrompt);
$("#pipe-prompt").addEventListener("input", updateStep2Actions);

$("#pipe-run-deterministic").addEventListener("click", async () => {
  const status = $("#pipe-prompt-status");
  const name = pipeSegment();
  status.classList.remove("err");
  status.innerHTML = `<span class="spinner"></span> Pulling deterministic data for ${esc(name)}...`;
  try {
    const rec = await api("/api/pull-segment/" + encodeURIComponent(name), "POST");
    status.textContent = `pulled ${rec.members.length} names`;
    pushNav({ view: "segment", segment: name });
    setActiveView("segment");
    renderSegment(rec);
  } catch (e) {
    status.textContent = "pull failed: " + e.message;
    status.classList.add("err");
  }
});

async function pollDeepJob(jobId, statusEl, onDone) {
  for (;;) {
    await new Promise((r) => setTimeout(r, 4000));
    let job;
    try {
      job = await api("/api/deep-job?id=" + encodeURIComponent(jobId));
    } catch (e) {
      statusEl.classList.add("err");
      statusEl.textContent = "lost the job: " + e.message;
      return;
    }
    if (job.state === "queued" || job.state === "running") {
      statusEl.classList.remove("err");
      statusEl.innerHTML = `<span class="spinner"></span> ${esc(job.message || job.state)}`;
      continue;
    }
    if (job.state === "done") {
      statusEl.classList.remove("err");
      await onDone(job);
      return;
    }
    if (job.state === "needs_login") {
      // The run proved the cached login flag was stale, so resync the gate and
      // hand the user an actual login button instead of an instruction to read.
      state.pplxLoggedIn = false;
      updateStep2LoginGate();
      renderNeedsLogin(statusEl, job.message || job.error);
      return;
    }
    statusEl.classList.add("err");
    statusEl.textContent = job.error || job.message || job.state;
    return;
  }
}

// Render a "not logged in" run/import outcome as an actionable prompt: the
// message plus a real "Set up Perplexity login" button that opens the login
// window in place. After it succeeds, refreshLoginStatus reopens the prompt.
function renderNeedsLogin(statusEl, message) {
  statusEl.classList.remove("err");
  statusEl.innerHTML = "";
  statusEl.appendChild(document.createTextNode((message || "Not logged in.") + " "));
  const btn = el("button", "ghost", "Set up Perplexity login");
  btn.type = "button";
  btn.addEventListener("click", () => runPplxLogin(statusEl));
  statusEl.appendChild(btn);
}

// Shared by the Step 2 run button and the Step 3 "Run Deep Research" action.
// Login and prompt are prerequisites that live on Step 2, so if either is
// missing we bounce the user back there instead of failing in place.
async function runDeepResearch(status) {
  status.classList.remove("err");
  const segment = pipeSegment();
  const date = $("#pipe-date").value.trim() || undefined;
  const prompt = $("#pipe-prompt").value.trim();
  if (!segment) { status.classList.add("err"); status.textContent = "pick or save a segment first"; return; }
  if (!state.pplxLoggedIn) {
    setPipeStep(2);
    updateStep2LoginGate();
    $("#pipe-login-gate-status").textContent = "Set up the Perplexity login first.";
    return;
  }
  if (!prompt) {
    setPipeStep(2);
    const ps = $("#pipe-prompt-status");
    ps.classList.add("err");
    ps.textContent = "Build a prompt on Step 2 first.";
    return;
  }
  status.innerHTML = `<span class="spinner"></span> starting deep research (off-screen browser)...`;
  try {
    const job = await api("/api/deep-research/run", "POST", { segment, date, prompt });
    await pollDeepJob(job.id, status, async (done) => {
      const stem = (done.artifact && done.artifact.stem) || `${segment}-${done.date || date}`;
      const r = done.result || {};
      const n = (r.citations && r.citations.length) || 0;
      status.textContent = `done: ${stem} - ${r.report_chars || 0} chars, ${n} sources. Review the saved report below.`;
      await refreshDeepRuns();
      await loadDeepRun(stem);
      setPipeStep(3);
    });
    await refreshLoginStatus();
  } catch (e) {
    status.classList.add("err");
    status.textContent = "run failed: " + e.message;
    await refreshLoginStatus();
  }
}

$("#pipe-run-deep").addEventListener("click", () => runDeepResearch($("#pipe-prompt-status")));
$("#pipe-run-deep-report").addEventListener("click", () => runDeepResearch($("#pipe-report-run-status")));
$("#rep-paste-manual").addEventListener("click", () => {
  state.repManual = true;
  setRepMode("current");
  const r = $("#pipe-report");
  if (r) r.focus();
});

$("#pipe-import").addEventListener("click", async () => {
  const status = $("#pipe-import-status");
  status.classList.remove("err");
  const url = $("#pipe-import-url").value.trim();
  const segment = pipeSegment();
  const date = $("#pipe-date").value.trim() || undefined;
  if (!segment) { status.classList.add("err"); status.textContent = "pick or save a segment first"; return; }
  if (!url) { status.classList.add("err"); status.textContent = "paste a Perplexity run URL"; return; }
  status.innerHTML = `<span class="spinner"></span> pulling the finished run (off-screen browser)...`;
  try {
    const job = await api("/api/deep-research/import", "POST", { segment, date, url });
    await pollDeepJob(job.id, status, async (done) => {
      const stem = (done.artifact && done.artifact.stem) || `${segment}-${done.date || date}`;
      const r = done.result || {};
      const n = (r.citations && r.citations.length) || 0;
      status.textContent = `imported: ${stem} - ${r.report_chars || 0} chars, ${n} sources.`;
      await refreshDeepRuns();
      await loadDeepRun(stem);
    });
    await refreshLoginStatus();
  } catch (e) {
    status.classList.add("err");
    status.textContent = "import failed: " + e.message;
  }
});

async function refreshLoginStatus() {
  let st;
  try {
    st = await api("/api/deep-research/login-status");
  } catch (e) {
    st = { logged_in: false };
  }
  state.pplxLoggedIn = !!st.logged_in;
  updateStep2LoginGate();
  const txt = $("#settings-login-state");
  if (txt) {
    const when = st.updated_at ? " (confirmed " + st.updated_at.slice(0, 10) + ")" : "";
    txt.classList.remove("err");
    txt.textContent = state.pplxLoggedIn
      ? `Logged in${when}. Re-login only if runs start hitting the login wall.`
      : "Not logged in. Use Re-login to set up the Perplexity session.";
  }
  return state.pplxLoggedIn;
}

async function runPplxLogin(statusEl) {
  statusEl.classList.remove("err");
  statusEl.innerHTML = `<span class="spinner"></span> opening a visible login window...`;
  try {
    const job = await api("/api/deep-research/login", "POST");
    await pollDeepJob(job.id, statusEl, async () => {
      statusEl.textContent = "Perplexity login confirmed. Off-screen runs will reuse it.";
    });
  } catch (e) {
    statusEl.classList.add("err");
    statusEl.textContent = "login failed: " + e.message;
  }
  await refreshLoginStatus();
}

$("#pipe-pplx-login").addEventListener("click", () => runPplxLogin($("#pipe-login-gate-status")));

$("#pipe-login-recheck").addEventListener("click", async () => {
  const txt = $("#pipe-login-gate-status");
  txt.classList.remove("err");
  txt.innerHTML = `<span class="spinner"></span> checking (off-screen browser, ~10s)...`;
  try {
    await api("/api/deep-research/verify-login", "POST");
    await refreshLoginStatus();
    txt.textContent = state.pplxLoggedIn
      ? "Logged in — prompt unlocked."
      : "Still not logged in. Use Set up Perplexity login.";
  } catch (e) {
    txt.classList.add("err");
    txt.textContent = "check failed: " + e.message;
  }
});

$("#settings-toggle").addEventListener("click", () => {
  const panel = $("#settings-panel");
  const opening = panel.hasAttribute("hidden");
  if (opening) {
    panel.removeAttribute("hidden");
    refreshLoginStatus();
  } else {
    panel.setAttribute("hidden", "");
  }
  $("#settings-toggle").setAttribute("aria-expanded", opening ? "true" : "false");
});

$("#settings-close").addEventListener("click", () => {
  $("#settings-panel").setAttribute("hidden", "");
  $("#settings-toggle").setAttribute("aria-expanded", "false");
});

$("#settings-relogin").addEventListener("click", () => runPplxLogin($("#settings-login-state")));

$("#settings-check").addEventListener("click", async () => {
  const txt = $("#settings-login-state");
  txt.classList.remove("err");
  txt.innerHTML = `<span class="spinner"></span> checking (off-screen browser, ~10s)...`;
  try {
    await api("/api/deep-research/verify-login", "POST");
    await refreshLoginStatus();
  } catch (e) {
    txt.classList.add("err");
    txt.textContent = "check failed: " + e.message;
  }
});

$("#pipe-save-report").addEventListener("click", async () => {
  const status = $("#pipe-artifact-status");
  status.classList.remove("err");
  status.textContent = "saving artifacts...";
  try {
    const rec = await api("/api/deep-research/save", "POST", {
      segment: pipeSegment(),
      date: $("#pipe-date").value.trim(),
      source_url: $("#pipe-source-url").value.trim(),
      report: $("#pipe-report").value,
      citations: parseJsonField("#pipe-sources", []),
    });
    status.textContent = `saved ${rec.stem} — continuing to Review`;
    state.currentDeepRun = rec.stem;
    state.repManual = false;
    pushNav({ view: "pipeline", segment: pipeSegment(), run: rec.stem });
    await refreshDeepRuns();
    setPipeStep(4);
  } catch (e) {
    status.textContent = "save failed: " + e.message;
    status.classList.add("err");
  }
});

$("#pipe-run-review").addEventListener("click", async () => {
  const status = $("#pipe-review-status");
  status.classList.remove("err");
  status.textContent = "running review gate...";
  try {
    const segment = pipeSegment();
    const date = $("#pipe-date").value.trim();
    const rec = await api("/api/deep-research/review", "POST", { segment, date });
    state.currentDeepRun = `${segment}-${date}`;
    pushNav({ view: "pipeline", segment, run: state.currentDeepRun });
    status.textContent = `review generated: ${rec.warnings.length} warning(s), ${rec.proposal.changes.length} proposal change(s)`;
    renderReviewGate(rec);
    await refreshDeepRuns();
  } catch (e) {
    status.textContent = "review failed: " + e.message;
    status.classList.add("err");
  }
});

$("#pipe-refresh-runs").addEventListener("click", refreshDeepRuns);

async function refreshDeepRuns() {
  const out = $("#pipe-runs");
  if (!out) return;
  try {
    const { runs } = await api("/api/deep-runs");
    state.deepRuns = runs || [];
    state.savedRuns = new Set(state.deepRuns.map((r) => r.stem));
    refreshPipeLocks();
    updateRepSubstate();
    updateExistingReportNotice();
    out.innerHTML = "";
    const list = el("div", "run-list");
    (runs || []).forEach((run) => {
      const row = el("button", "run-row", "");
      const files = Object.keys(run.files || {}).sort().join(", ");
      row.innerHTML = `<strong>${esc(run.stem)}</strong><span>${esc(files)}</span>`;
      row.addEventListener("click", async () => { await loadDeepRun(run.stem); setPipeStep(3); });
      list.appendChild(row);
    });
    out.appendChild(list);
  } catch (e) {
    out.innerHTML = `<div class="status err">could not load runs: ${esc(e.message)}</div>`;
  }
}

async function loadDeepRun(stem, { push = true } = {}) {
  const rec = await api("/api/deep-run/" + encodeURIComponent(stem));
  state.currentDeepRun = stem;
  state.repManual = false;
  const m = stem.match(/^(.*)-(\d{4}-\d{2}-\d{2})$/);
  if (m) {
    $("#pipe-segment-select").value = m[1];
    $("#pipe-date").value = m[2];
    if (push) pushNav({ view: "pipeline", segment: m[1], run: stem });
  } else if (push) {
    pushNav({ view: "pipeline", run: stem });
  }
  if (rec.report) $("#pipe-report").value = rec.report;
  if (rec.sources) $("#pipe-sources").value = JSON.stringify(rec.sources.citations || [], null, 2);
  if (rec.sources && rec.sources.source_url) $("#pipe-source-url").value = rec.sources.source_url;
  if (rec.markdown || rec.review || rec.proposal) renderReviewGate({
    markdown: rec.review || "",
    proposal: rec.proposal || { changes: [], warnings: [] },
    warnings: (rec.proposal && rec.proposal.warnings) || [],
    rows: [],
    source_summary: rec.proposal ? null : undefined,
  });
  setRepMode("current");
  refreshPipeLocks();
}

function renderReviewGate(rec) {
  const out = $("#pipe-review-output");
  out.innerHTML = "";
  const card = el("div", "card");
  card.appendChild(el("h2", "section", "Review gate output"));
  if (rec.source_summary) {
    const b = rec.source_summary.buckets || {};
    card.appendChild(el("div", "badges",
      Object.keys(b).map((k) => `<span class="badge ${k === "weak" && b[k] ? "off" : "on"}">${esc(k)}: ${b[k]}</span>`).join("")));
  }
  const findings = rec.findings || (rec.proposal && rec.proposal.findings) || null;
  if (findings && findings.length) {
    const cls = { BLOCK: "ERROR", WARN: "WARN", FYI: "INFO" };
    const checks = el("div", "checks");
    findings.forEach((f) => checks.appendChild(
      el("div", `check ${cls[f.level] || "INFO"}`, `<span class="sev">${esc(f.level)}</span><span>${esc(f.message)}</span>`)));
    card.appendChild(checks);
  } else if (rec.warnings && rec.warnings.length) {
    const checks = el("div", "checks");
    rec.warnings.forEach((w) => checks.appendChild(el("div", "check WARN", `<span class="sev">WARN</span><span>${esc(w)}</span>`)));
    card.appendChild(checks);
  }
  if (rec.rows && rec.rows.length) {
    const table = el("table");
    table.innerHTML =
      "<thead><tr><th>Symbol</th><th>Action</th><th>Target</th><th>Data</th><th>Conflict</th></tr></thead>" +
      "<tbody>" + rec.rows.map((r) =>
        `<tr><td><strong>${esc(r.symbol)}</strong></td><td>${esc(r.report_action)}</td><td>${esc(r.target_rule || "")}</td><td>${esc(r.data_quality)}</td><td>${esc(r.conflict || "")}</td></tr>`
      ).join("") + "</tbody>";
    card.appendChild(table);
  }
  const proposal = rec.proposal || {};
  const changes = proposal.changes || [];
  const blocked = rec.blocked_symbols || proposal.blocked_symbols || [];
  const applicable = changes.filter((c) => !blocked.includes(c.symbol));
  card.appendChild(el("h2", "section", "Target-model proposal"));
  if (changes.length) {
    const pre = el("pre", "json-preview", esc(JSON.stringify(changes, null, 2)));
    card.appendChild(pre);
    if (blocked.length) {
      card.appendChild(el("div", "hint",
        `Apply is blocked for ${blocked.map(esc).join(", ")} (ERROR-level data). Re-pull and fix the data first.`));
    }
  } else {
    card.appendChild(el("div", "hint", "No target-model changes proposed."));
  }
  if (rec.markdown) {
    card.appendChild(el("h2", "section", "Review markdown"));
    card.appendChild(el("pre", "markdown-preview", esc(rec.markdown.slice(0, 8000))));
  }
  out.appendChild(card);
  // Apply only becomes available once the review produced a change we're allowed
  // to apply -- i.e. at least one proposed symbol that isn't data-blocked.
  const applyBtn = $("#pipe-apply-proposal");
  if (applyBtn) applyBtn.disabled = !applicable.length;
}

$("#pipe-apply-proposal").addEventListener("click", async () => {
  const status = $("#pipe-apply-status");
  const segment = pipeSegment();
  const date = ($("#pipe-date").value || "").trim();
  if (!segment || !date) {
    status.textContent = "run the review gate first";
    status.classList.add("err");
    return;
  }
  if (!window.confirm("Apply this target-model proposal? This changes target-model.json, not trades.")) return;
  status.classList.remove("err");
  status.textContent = "applying proposal...";
  try {
    const rec = await api("/api/target-proposal/apply", "POST", { segment, date, confirm: true });
    status.textContent = `applied: ${rec.applied.join(", ") || "none"}; skipped: ${rec.skipped.length}`;
  } catch (e) {
    status.textContent = "apply failed: " + e.message;
    status.classList.add("err");
  }
});

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

async function loadAnalyses() {
  const list = $("#analyses-list");
  if (!list) return;
  list.innerHTML = '<div class="hint">Loading…</div>';
  let runs = [];
  let reports = [];
  let segments = [];
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
    const body = el("div", "report-doc-body prose", mdToHtml(rec.report));
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
      try { host = new URL(c.href).hostname.replace(/^www\./, ""); } catch (_e) {}
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

  reader.scrollTop = 0;
}

// ---- boot -----------------------------------------------------------------
applyPrivacyMode(state.privacyMode);
const initialNav = navFromUrl();
window.history.replaceState(initialNav, "", window.location.href);
restoreNav(initialNav);
refreshLoginStatus();
ensureTickerSet();
