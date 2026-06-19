// @ts-nocheck
import { createQaCard, ensureTickerSet, linkifyTickers, mdToHtml } from "./analyses";
import { $, api, decisionClass, el, esc, fmtB, fmtPct, fmtPrice, fmtShares, fmtSignedWeight, fmtWeight, fmtX, freshnessNote, instrumentBadge, pctClass, sectionCard, simpleTable, state } from "./core";
import { pollDeepJob } from "./errors";
import { cleanSymbol, downloadText, modelLabel, pushNav, setActiveView } from "./shell";
import { recordView, relTime, renderViewedTickers } from "./viewed";
import { activeFraction, fairValueStale, laddersMatch, marginFromPrice, priceFromMargin, sizeSum, sortLadder } from "./ladder";
import { decorateAnalysis, decorateSources } from "./deepdive/decorate";

// ---- deep dive ------------------------------------------------------------
$("#ticker-go").addEventListener("click", () => pullTicker($("#ticker-input").value));
$("#ticker-input").addEventListener("keydown", (e) => { if (e.key === "Enter") pullTicker($("#ticker-input").value); });
// Return to the viewed-tickers overview (the deep-dive landing list).
function goToOverview() {
  $("#ticker-input").value = "";
  pushNav({ view: "deepdive", ticker: "" });
  setActiveView("deepdive");
  renderViewedTickers();
}

// Sticky "back to overview" bar. The single way back to the viewed-tickers list
// from any dossier state (loaded dossier OR a no-market-data card), so it stays
// one click away even after scrolling. Replaces the old search-bar button.
function overviewBackBar() {
  const backBar = el("div", "dd-backbar");
  const back = el("button", "ghost dd-back", "\u2190 All tickers");
  back.type = "button";
  back.title = "Back to your viewed tickers";
  back.addEventListener("click", goToOverview);
  backBar.appendChild(back);
  return backBar;
}

const EXCHANGE_SUFFIXES = [".L", ".AS", ".DE", ".PA", ".BR", ".SW", ".HK", ".TO", ".PR"];

function exchangeCandidates(sym) {
  const base = cleanSymbol(sym).replace(/\s+/g, "");
  if (!base || base.includes(".") || base.includes("-") || base.includes("=")) return [];
  const candidates = EXCHANGE_SUFFIXES.map((suffix) => base + suffix);
  if (/^\d+$/.test(base)) candidates.unshift(base.padStart(4, "0") + ".HK");
  return [...new Set(candidates)];
}

function hasUsableMarketData(rec) {
  if (!rec || typeof rec !== "object") return false;
  if (rec.price && rec.price.value != null) return true;
  return METRIC_ROWS.some(([key]) => rec[key] != null);
}

async function saveSymbolAlias(inputSymbol, providerSymbol) {
  return api("/api/symbol-alias", "POST", {
    input_symbol: inputSymbol,
    provider_symbol: providerSymbol,
  });
}

// Magnifier-with-minus: "we searched and found no market data" — themed via
// currentColor so it inherits the badge tint.
const NODATA_ICON_SVG =
  `<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" ` +
  `stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">` +
  `<circle cx="11" cy="11" r="7"></circle><line x1="20.5" y1="20.5" x2="16.4" y2="16.4"></line>` +
  `<line x1="8" y1="11" x2="14" y2="11"></line></svg>`;

// One rich, clickable suggestion row: bold symbol, company name, exchange/type
// meta, and an open affordance. Used for both name matches and exchange guesses.
function symbolSuggestRow({ symbol, name, meta }, onClick) {
  const btn = el("button", "symbol-suggest");
  btn.type = "button";
  btn.title = `Analyze ${symbol}`;
  btn.innerHTML =
    `<span class="sx-sym">${esc(symbol)}</span>` +
    (name ? `<span class="sx-name">${esc(name)}</span>` : `<span class="sx-name"></span>`) +
    (meta ? `<span class="sx-meta">${esc(meta)}</span>` : "") +
    `<span class="sx-go" aria-hidden="true">\u2197</span>`;
  btn.addEventListener("click", onClick);
  return btn;
}

function renderNoMarketData(rec) {
  const sym = cleanSymbol(rec?.input_symbol || rec?.alias_candidate_for || rec?.symbol || "");
  const provider = rec?.provider_symbol || rec?.symbol || sym;
  const out = $("#dd-result");
  out.innerHTML = "";
  out.appendChild(overviewBackBar());
  const card = el("div", "card empty-ticker nodata-card");
  const errors = rec?.provider_errors || rec?.errors || rec?.error;
  const detail = Array.isArray(errors)
    ? errors.join("; ")
    : typeof errors === "object" && errors
      ? Object.entries(errors).map(([k, v]) => `${k}: ${v}`).join("; ")
      : String(errors || "No usable quote, fundamentals, or market-data fields were returned.");

  const head = el("div", "nodata-head");
  head.innerHTML =
    `<span class="nodata-icon">${NODATA_ICON_SVG}</span>` +
    `<h2 class="section">No market data for ${esc(provider)}</h2>` +
    `<p class="nodata-lead">No usable quote or fundamentals came back for <strong>${esc(sym || provider)}</strong>. ` +
    `Broker symbols often need an exchange suffix — pick a real match below.</p>`;
  card.appendChild(head);

  // Lead with the useful action: company-name / near-miss search. Maybe they
  // typed a name or a broker symbol that maps to a real listing.
  const queryStr = sym || provider;
  if (queryStr) {
    const sec = el("div", "nodata-suggest");
    sec.innerHTML = `<div class="nodata-suggest-label"><span class="spinner"></span> Searching the market for "${esc(queryStr)}"\u2026</div>`;
    card.appendChild(sec);
    loadNameSearch(sec, queryStr);
  }

  // Then the deterministic exchange-suffix guesses (LSE, TSX, …).
  const candidates = exchangeCandidates(sym || provider);
  if (candidates.length) {
    const sec = el("div", "nodata-suggest");
    sec.innerHTML = `<div class="nodata-suggest-label"><span class="spinner"></span> Checking exchange-qualified candidates\u2026</div>`;
    card.appendChild(sec);
    loadCandidateSuggestions(sec, sym || provider, candidates);
  }

  // The raw provider error is debugging detail, not the headline — tuck it into
  // a collapsed, de-emphasized panel so it stops dominating the card.
  const det = el("details", "nodata-detail");
  det.innerHTML =
    `<summary>Provider response details</summary>` +
    `<div class="nodata-detail-body">${esc(detail)}</div>`;
  card.appendChild(det);

  out.appendChild(card);
}

async function loadNameSearch(sec, query) {
  try {
    const result = await api("/api/symbol-search?q=" + encodeURIComponent(query));
    const wanted = cleanSymbol(query);
    const matches = (result.results || []).filter((m) => cleanSymbol(m.symbol) !== wanted);
    sec.innerHTML = "";
    if (!matches.length) {
      sec.appendChild(el("div", "nodata-suggest-label", `No market symbols matched "${esc(query)}".`));
      return;
    }
    sec.appendChild(el("div", "nodata-suggest-label", "Matching symbols"));
    const list = el("div", "symbol-suggest-list");
    matches.forEach((m) => list.appendChild(symbolSuggestRow(
      { symbol: m.symbol, name: m.name, meta: [m.exchange, m.type].filter(Boolean).join(" \u00b7 ") },
      () => pullTicker(m.symbol, { push: false }))));
    sec.appendChild(list);
  } catch (e) {
    sec.innerHTML = "";
    sec.classList.add("err");
    sec.appendChild(el("div", "nodata-suggest-label", `Symbol search failed: ${esc(e.message)}`));
  }
}

async function loadCandidateSuggestions(sec, inputSymbol, candidates) {
  try {
    const result = await api("/api/symbol-candidates", "POST", {
      input_symbol: inputSymbol,
      candidates,
    });
    const valid = result.candidates || [];
    sec.innerHTML = "";
    if (!valid.length) {
      sec.appendChild(el("div", "nodata-suggest-label", "No working exchange-qualified alternatives found."));
      return;
    }
    sec.appendChild(el("div", "nodata-suggest-label", "Exchange-qualified alternatives"));
    const list = el("div", "symbol-suggest-list");
    valid.forEach((c) => list.appendChild(symbolSuggestRow(
      { symbol: c.symbol, name: "", meta: [c.exchange, c.currency].filter(Boolean).join(" \u00b7 ") },
      () => pullTicker(c.symbol, { push: false, aliasFor: inputSymbol }))));
    sec.appendChild(list);
  } catch (e) {
    sec.innerHTML = "";
    sec.classList.add("err");
    sec.appendChild(el("div", "nodata-suggest-label", `Could not validate alternate tickers: ${esc(e.message)}`));
  }
}

async function loadTickerFromCache(raw) {
  const sym = cleanSymbol(raw);
  if (!sym) return;
  const status = $("#dd-status");
  status.classList.remove("err");
  status.textContent = `Loading cached ${sym}...`;
  try {
    const rec = await api("/api/research/" + encodeURIComponent(sym));
    status.textContent = `Loaded cached ${rec.symbol} from ${new Date(rec.as_of).toLocaleString()}`;
    if (hasUsableMarketData(rec)) {
      renderDeepDive(rec);
      hydrateHistory(rec);
    } else {
      renderNoMarketData(rec);
    }
  } catch {
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

async function pullTicker(raw, { push = true, aliasFor = "" } = {}) {
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
    status.textContent = `Fetched ${rec.symbol} at ${new Date(rec.as_of).toLocaleString()}`;
    if (aliasFor) rec.alias_candidate_for = cleanSymbol(aliasFor);
    if (hasUsableMarketData(rec)) {
      renderDeepDive(rec);
      hydrateHistory(rec);
    } else {
      renderNoMarketData(rec);
    }
  } catch (e) {
    status.textContent = "Pull failed: " + e.message;
    status.classList.add("err");
    renderNoMarketData({ symbol: sym, error: e.message });
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

const METRIC_FMT = Object.fromEntries(METRIC_ROWS.map(([k, , f]) => [k, f]));

function ordinal(n) {
  const s = ["th", "st", "nd", "rd"], v = n % 100;
  return n + (s[(v - 20) % 10] || s[v] || s[0]);
}

// Qualitative rank for samples too small to carry a percentile.
function rankWord(pct) {
  if (pct <= 0) return "lowest";
  if (pct >= 1) return "highest";
  if (pct < 0.5) return "below median";
  if (pct > 0.5) return "above median";
  return "at median";
}

// A slim track (low -> high across the segment peers) with the subject's marker
// at its rank-percentile and a centre tick marking the peer median. With one
// segment the caption names it; with several it shows the aggregate and the
// per-segment breakdown lives in the tooltip.
//
// A rank-percentile over a handful of peers is noise (n=3 -> only 0/50/100), so
// when too few segment members have data the backend marks the metric
// unreliable: we then drop the precise-looking "Nth pctile" for an honest rank +
// coverage ("lowest · 3 of 19 peers") and de-emphasize the bar.
function peerBar(key, m) {
  const fmt = METRIC_FMT[key] || ((v) => String(v));
  const pct = Math.max(0, Math.min(1, m.aggregate.pct));
  const segs = m.per_segment || [];
  const multi = segs.length > 1;
  const best = segs.reduce((a, s) => (s.n > a.n ? s : a), segs[0] || { n: 0, members_total: 0 });
  // Backend flag is authoritative; fall back to sample size for older payloads.
  const reliable = m.reliable != null ? m.reliable : best.n >= 5;
  const coverage = best.members_total
    ? `${best.n} of ${best.members_total} peers`
    : `${best.n} peers`;

  let cap, tip;
  if (reliable) {
    cap = multi
      ? `vs ${segs.length} sectors \u00b7 ${ordinal(Math.round(pct * 100))} pctile`
      : `vs ${segs[0].title} \u00b7 ${ordinal(Math.round(pct * 100))} pctile`;
    tip = segs
      .map((s) => `${s.title}: ${ordinal(Math.round(s.pct * 100))} pctile of ${s.n}` +
        ` (median ${fmt(s.median)}, range ${fmt(s.min)}\u2013${fmt(s.max)})`)
      .join("\n");
  } else {
    cap = `${rankWord(pct)} \u00b7 ${coverage}`;
    tip = "Too few peers have data for a meaningful percentile.\n" + segs
      .map((s) => `${s.title}: ${rankWord(s.pct)} of ${s.n}` +
        (s.members_total ? ` of ${s.members_total}` : "") +
        ` (median ${fmt(s.median)}, range ${fmt(s.min)}\u2013${fmt(s.max)})`)
      .join("\n");
  }

  const wrap = el("div", reliable ? "metric-peer" : "metric-peer sparse");
  wrap.title = tip;
  wrap.innerHTML =
    `<div class="mp-track"><span class="mp-median"></span>` +
    `<span class="mp-marker" style="left:${(pct * 100).toFixed(1)}%"></span></div>` +
    `<div class="mp-cap">${esc(cap)}</div>`;
  return wrap;
}

async function loadPeerStats(symbol, grid) {
  let data;
  try { data = await api("/api/peer-stats?symbol=" + encodeURIComponent(symbol)); }
  catch (_e) { return; }  // best-effort enrichment; tiles already rendered
  const metrics = (data && data.metrics) || {};
  if (!Object.keys(metrics).length) return;
  grid.querySelectorAll(".metric-cell").forEach((cell) => {
    const m = metrics[cell.dataset.metric];
    if (m) cell.appendChild(peerBar(cell.dataset.metric, m));
  });
}

function renderDeepDive(rec) {
  recordView(rec.symbol, rec.name);
  const out = $("#dd-result");
  out.innerHTML = "";
  out.appendChild(overviewBackBar());

  const price = rec.price ? rec.price.value : null;
  const portfolio = rec.portfolio || {};
  const target = portfolio.target || {};
  const owned =
    portfolio.current_weight_pct ??
    state.holdings[rec.symbol] ??
    state.holdings[rec.input_symbol] ??
    state.holdings[rec.alias_candidate_for] ??
    state.holdings[rec.provider_symbol];
  const decision = rec.decision || "research";

  const card = el("div", "card");
  // header
  const head = el("div", "dd-head");
  head.innerHTML =
    `<span class="sym">${esc(rec.symbol)}</span>` +
    `<span class="name">${esc(rec.name || "")}</span>` +
    instrumentBadge(rec.instrument_type) +
    `<span class="decision-pill ${decisionClass(decision)}">${esc(decision.replace("_", " "))}</span>` +
    `<span class="price">${fmtPrice(price)} <small class="muted">${esc(rec.currency || "")}</small></span>`;
  card.appendChild(head);

  const sub = el("div", "dd-sub");
  sub.innerHTML =
    `<span>as of ${freshnessNote(rec.as_of) || esc(new Date(rec.as_of).toLocaleString())}</span>` +
    (owned != null ? `<span class="owned-pill">held: ${fmtWeight(owned)} NAV</span>` : `<span class="muted">not held</span>`) +
    (target.rule ? `<span>rule: <strong>${esc(target.rule)}</strong></span>` : `<span class="muted">no target rule</span>`);
  const refreshBtn = el("button", "ghost dd-refresh", "\u21bb Refresh");
  refreshBtn.type = "button";
  refreshBtn.title = "Re-pull live price history, price, metrics, and profile from Yahoo / SEC / FMP";
  refreshBtn.addEventListener("click", () => pullTicker(rec.symbol, { push: false }));
  sub.appendChild(refreshBtn);
  card.appendChild(sub);

  // source badges
  const badges = el("div", "badges");
  ["yahoo", "sec_edgar", "fmp"].forEach((s) => {
    const on = rec.sources && rec.sources[s];
    badges.appendChild(el("span", "badge " + (on ? "on" : "off"), (on ? "✓ " : "· ") + s.replace("_", " ")));
  });
  card.appendChild(badges);
  if (rec.input_symbol && rec.provider_symbol && rec.input_symbol !== rec.provider_symbol) {
    card.appendChild(el("div", "alias-suggestion", `Resolved ${esc(rec.input_symbol)} to ${esc(rec.provider_symbol)}.`));
  } else if (rec.alias_candidate_for && rec.alias_candidate_for !== rec.symbol) {
    const row = el("div", "alias-suggestion");
    row.innerHTML = `<span>${esc(rec.symbol)} worked. Save it as the provider symbol for ${esc(rec.alias_candidate_for)}?</span>`;
    const btn = el("button", "primary", "Save alias");
    btn.type = "button";
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      try {
        await saveSymbolAlias(rec.alias_candidate_for, rec.symbol);
        row.innerHTML = `<span>Saved alias ${esc(rec.alias_candidate_for)} \u2192 ${esc(rec.symbol)}.</span>`;
      } catch (e) {
        btn.disabled = false;
        row.appendChild(el("span", "status err", ` save failed: ${esc(e.message)}`));
      }
    });
    row.appendChild(btn);
    card.appendChild(row);
  }
  out.appendChild(card);

  out.appendChild(renderAnalysisCard(rec));
  out.appendChild(renderDeepResearchCard(rec));
  out.appendChild(renderQaCard(rec));

  const biz = renderBusiness(rec);
  if (biz) out.appendChild(biz);

  const chart = renderPriceChart(rec);
  if (chart) out.appendChild(chart);

  // decision context
  const dcard = sectionCard("Decision context");
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
  const mcard = sectionCard("Valuation & fundamentals");
  const grid = el("div", "metrics-grid");
  METRIC_ROWS.forEach(([key, label, fmt]) => {
    const node = rec.metrics ? rec.metrics[key] : null;
    const cell = el("div", "metric-cell");
    cell.dataset.metric = key;
    const srcLine = node ? sourceLine(node) : `<span class="muted">no data</span>`;
    cell.innerHTML =
      `<div class="label">${label}</div>` +
      `<div class="val">${node ? esc(fmt(node.value)) : "n/a"}</div>` +
      `<div class="src">${srcLine}</div>`;
    grid.appendChild(cell);
  });
  mcard.appendChild(grid);
  out.appendChild(mcard);
  // Peer-comparison bars load off the critical path (they read every segment
  // member's cached metrics server-side) and slot into the tiles when ready.
  loadPeerStats(rec.symbol, grid);

  // momentum
  const mo = rec.momentum || {};
  const mom = sectionCard("Momentum");
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

  // Recent-pulls change log lives in a stable slot so the background history
  // fetch can swap it in place without disturbing the rest of the dossier.
  const histSlot = el("div", "dd-slot");
  histSlot.dataset.slot = "history";
  histSlot.dataset.symbol = rec.symbol;
  histSlot.appendChild(renderHistory(rec));
  out.appendChild(histSlot);

  // thesis editor
  out.appendChild(renderThesis(rec));
}

// Fetch the recent-pulls change log out of band and drop it into its slot. Kept
// off the critical render path so a cached dossier paints immediately; guarded by
// symbol so a fast re-navigation to another ticker can't get the wrong table.
async function hydrateHistory(rec) {
  try {
    const hist = await api("/api/history/" + encodeURIComponent(rec.symbol));
    rec.history = hist.history || [];
  } catch (_e) {
    rec.history = [];
  }
  const slot = $("#dd-result [data-slot='history']");
  if (!slot || slot.dataset.symbol !== rec.symbol) return;
  slot.innerHTML = "";
  slot.appendChild(renderHistory(rec));
}

// A ticker_analysis job for this symbol may already be in flight — started on
// this page, then navigated away and back (the backend allows only one per
// symbol). It's surfaced via /api/jobs so the deep-dive can re-attach to its
// progress instead of falsely showing the idle "Run" state.
async function runningAnalysisJob(symbol) {
  try {
    const res = await api("/api/jobs");
    return (res.jobs || []).find(
      (j) => j.kind === "ticker_analysis" && j.symbol === symbol &&
             (j.state === "running" || j.state === "queued")) || null;
  } catch (_e) {
    return null;
  }
}

// Price-level triggers: the analysis suggests buy-below / trim-above prices;
// the user edits and LOCKS them. Once locked, the level gates the rebalance
// suggestion (downstream slice) and becomes the limit price on synthesized
// orders. All in the instrument's trading currency. Editable inline under the
// analysis so the human confirms every trigger before it can move money.
function num(v) {
  return typeof v === "number" && isFinite(v) ? v : null;
}

// Read a side's ladder off a locked/suggested record, upgrading a legacy
// {buy_below}/{trim_above} single level to a 1-tranche ladder so the editor
// always works in ladder terms.
function ladderOf(rec, side) {
  if (!rec) return [];
  const arr = side === "buy" ? rec.buy_ladder : rec.trim_ladder;
  if (Array.isArray(arr) && arr.length) {
    return arr.map((t) => ({
      price: num(t.price),
      size: num(t.size_pct),
      margin: num(side === "buy" ? t.discount_pct : t.premium_pct),
    }));
  }
  const legacy = num(side === "buy" ? rec.buy_below : rec.trim_above);
  return legacy != null ? [{ price: legacy, size: 1, margin: null }] : [];
}

// A locked, valuation-anchored ladder editor: a fair-value anchor plus buy/trim
// tranches (each margin% <-> price <-> size%). Locking sends the full ladder;
// the backend grades the rebalance by how many tranches the live price unlocks
// and uses the outermost tranche as the order's limit. You confirm every order.
function priceLevelsBlock(rec, analysis, initialLocked) {
  const sym = rec.symbol;
  const meta = (analysis && analysis.meta) || {};
  const suggested = meta.price_levels_suggested || {};
  const currency = (suggested.currency || meta.currency || rec.currency || "").toUpperCase();
  const spot = num(rec.price && rec.price.value);
  let locked = initialLocked || null;

  const ccyPrefix = currency ? currency + " " : "";
  const fmtLvl = (v) => (v == null ? "\u2014" : ccyPrefix + (Math.round(v * 100) / 100));
  const r2 = (v) => (v == null ? null : Math.round(v * 100) / 100);

  // Editor state (rebuilt from the locked level if present, else the suggestion).
  let fairValue = null;
  let buyRows = [];
  let trimRows = [];
  function seedFrom(src) {
    fairValue = num(src && src.fair_value);
    buyRows = ladderOf(src, "buy");
    trimRows = ladderOf(src, "trim");
  }
  seedFrom(locked && (locked.buy_ladder || locked.trim_ladder || locked.buy_below != null || locked.trim_above != null) ? locked : suggested);

  const sugBuy = sortLadder((suggested.buy_ladder || []).map((t) => ({ price: num(t.price), size: num(t.size_pct), margin: num(t.discount_pct) })), "buy");
  const sugTrim = sortLadder((suggested.trim_ladder || []).map((t) => ({ price: num(t.price), size: num(t.size_pct), margin: num(t.premium_pct) })), "trim");
  const sugFair = num(suggested.fair_value);

  const block = el("div", "price-levels");

  // Recompute a row's price from its margin when the fair value is known, and
  // update its bound input in place (avoids a full rebuild while typing).
  function repriceFromFair() {
    for (const side of [["buy", buyRows], ["trim", trimRows]]) {
      for (const row of side[1]) {
        if (row.margin != null && fairValue != null) {
          row.price = priceFromMargin(fairValue, row.margin, side[0]);
          if (row._priceIn) row._priceIn.value = row.price != null ? String(row.price) : "";
        }
      }
    }
  }

  function trancheRow(side, row, rows) {
    const wrap = el("div", "pl-tranche pl-tranche--" + side);
    const marginIn = numInput("pl-input pl-tr-num", row.margin != null ? r2(row.margin * 100) : null, side === "buy" ? "disc %" : "prem %");
    const priceIn = numInput("pl-input pl-tr-num", row.price != null ? r2(row.price) : null, "price");
    const sizeIn = numInput("pl-input pl-tr-num", row.size != null ? r2(row.size * 100) : null, "size %");
    row._priceIn = priceIn;

    marginIn.addEventListener("input", () => {
      row.margin = marginIn.value.trim() === "" ? null : Number(marginIn.value) / 100;
      if (fairValue != null && row.margin != null) {
        row.price = priceFromMargin(fairValue, row.margin, side);
        priceIn.value = row.price != null ? String(row.price) : "";
      }
      sync();
    });
    priceIn.addEventListener("input", () => {
      row.price = priceIn.value.trim() === "" ? null : Number(priceIn.value);
      if (fairValue != null && row.price != null) {
        row.margin = marginFromPrice(fairValue, row.price, side);
        marginIn.value = row.margin != null ? String(r2(row.margin * 100)) : "";
      }
      sync();
    });
    sizeIn.addEventListener("input", () => {
      row.size = sizeIn.value.trim() === "" ? null : Number(sizeIn.value) / 100;
      sync();
    });

    const dist = el("span", "pl-tr-dist", "");
    row._distEl = dist;
    const del = el("button", "pl-tr-del", "\u00d7");
    del.type = "button";
    del.title = "Remove this tranche";
    del.addEventListener("click", () => {
      const i = rows.indexOf(row);
      if (i >= 0) rows.splice(i, 1);
      render();
    });

    wrap.appendChild(numField(side === "buy" ? "discount" : "premium", marginIn, "%"));
    wrap.appendChild(numField("price", priceIn, currency));
    wrap.appendChild(numField("size", sizeIn, "%"));
    wrap.appendChild(dist);
    wrap.appendChild(del);
    return wrap;
  }

  function numField(label, input, suffix) {
    const f = el("label", "pl-tr-field");
    f.appendChild(el("span", "pl-tr-label", esc(label) + (suffix ? " (" + esc(suffix) + ")" : "")));
    f.appendChild(input);
    return f;
  }

  // Per-tranche distance-to-spot + a live/pending dot, recomputed on every edit.
  function updateDistances() {
    for (const [side, rows] of [["buy", buyRows], ["trim", trimRows]]) {
      for (const row of rows) {
        if (!row._distEl) continue;
        if (spot == null || row.price == null) { row._distEl.textContent = ""; row._distEl.className = "pl-tr-dist"; continue; }
        const live = side === "buy" ? spot <= row.price : spot >= row.price;
        if (live) {
          row._distEl.className = "pl-tr-dist live";
          row._distEl.textContent = "\u25cf live";
        } else {
          const away = Math.abs(spot - row.price) / spot;
          row._distEl.className = "pl-tr-dist pending";
          row._distEl.textContent = "\u25cb " + (Math.round(away * 1000) / 10) + "% away";
        }
      }
    }
  }

  function sideSummary(side, rows, host) {
    host.innerHTML = "";
    const sizes = rows.map((r) => r.size);
    const sum = sizeSum(sizes);
    const tranches = rows.filter((r) => r.price != null);
    if (!tranches.length) {
      host.appendChild(el("span", "muted", "no " + side + " tranches"));
      return;
    }
    const frac = activeFraction(tranches.map((r) => ({ price: r.price, size_pct: r.size, })), spot, side);
    const live = tranches.filter((r) => (side === "buy" ? spot != null && spot <= r.price : spot != null && spot >= r.price)).length;
    if (spot != null) {
      host.appendChild(el("span", "pl-sum-live" + (frac > 0 ? " on" : ""), `${live}/${tranches.length} live`));
      host.appendChild(el("span", "pl-sum-frac", `${Math.round(frac * 100)}% sized`));
    }
    const ok = Math.abs(sum - 1) <= 0.02;
    host.appendChild(el("span", "pl-sum-size" + (ok ? " ok" : " warn"),
      `sizes ${Math.round(sum * 100)}%${ok ? "" : " \u2014 will normalize to 100%"}`));
  }

  // Compare the current editor ladder to the analysis suggestion, per side.
  function matchLine(side, rows, sug, host) {
    host.innerHTML = "";
    if (!sug.length) { host.appendChild(el("span", "muted", "no suggestion")); return; }
    const mine = sortLadder(rows.filter((r) => r.price != null), side);
    if (laddersMatch(mine, sug)) {
      const ok = el("span", "pl-cmp pl-cmp-ok");
      ok.appendChild(el("span", "pl-cmp-ico", "\u2713"));
      ok.appendChild(el("span", "pl-cmp-lead", "matches analysis"));
      host.appendChild(ok);
      return;
    }
    const diff = el("span", "pl-cmp pl-cmp-diff");
    diff.appendChild(el("span", "pl-cmp-ico", "\u2260"));
    diff.appendChild(el("span", "pl-cmp-lead", "suggested:"));
    sug.forEach((t, i) => {
      if (i) diff.appendChild(el("span", "pl-cmp-sep", "\u00b7"));
      diff.appendChild(el("span", "pl-cmp-field", `${fmtLvl(t.price)} @ ${Math.round((t.size || 0) * 100)}%`));
    });
    const apply = el("button", "pl-cmp-apply", "Use suggested");
    apply.type = "button";
    apply.addEventListener("click", () => {
      if (side === "buy") buyRows = sug.map((t) => ({ ...t }));
      else trimRows = sug.map((t) => ({ ...t }));
      render();
    });
    diff.appendChild(apply);
    host.appendChild(diff);
  }

  let summaryHosts = null;

  // Light refresh: distances + summaries + match lines (no rebuild, keeps focus).
  function sync() {
    updateDistances();
    if (summaryHosts) {
      sideSummary("buy", buyRows, summaryHosts.buySum);
      sideSummary("trim", trimRows, summaryHosts.trimSum);
      matchLine("buy", buyRows, sugBuy, summaryHosts.buyMatch);
      matchLine("trim", trimRows, sugTrim, summaryHosts.trimMatch);
    }
  }

  function sideColumn(side, rows) {
    const col = el("div", "pl-side pl-side--" + side);
    const head = el("div", "pl-side-head");
    head.appendChild(el("span", "pl-side-title", side === "buy" ? "Buy ladder" : "Trim ladder"));
    const sum = el("span", "pl-side-sum");
    head.appendChild(sum);
    col.appendChild(head);
    const list = el("div", "pl-tranches");
    rows.forEach((row) => list.appendChild(trancheRow(side, row, rows)));
    col.appendChild(list);
    const matchHost = el("div", "pl-side-match");
    col.appendChild(matchHost);
    const add = el("button", "ghost pl-add", "+ Add tranche");
    add.type = "button";
    add.addEventListener("click", () => { rows.push({ price: null, size: null, margin: null }); render(); });
    col.appendChild(add);
    return { col, sum, matchHost };
  }

  function render() {
    block.innerHTML = "";
    block.classList.toggle("pl-is-locked", !!locked);
    const head = el("div", "pl-head");
    head.appendChild(el("h3", "pl-title", "Price levels"));
    if (locked) head.appendChild(el("span", "abadge ok pl-locked", "Locked"));
    block.appendChild(head);
    block.appendChild(el("p", "hint pl-intro",
      "A valuation-anchored ladder in the instrument's trading currency. Set a fair value, then " +
      "buy/trim tranches \u2014 each a price (or a margin vs fair value) and a size. Once locked, the " +
      "rebalance scales each trade by how many tranches the live price unlocks, and the outermost " +
      "tranche becomes the order's limit. You confirm every order before it places."));

    // Staleness banner: a newer analysis fair value differs from the locked one.
    if (locked && fairValueStale(num(locked.fair_value), sugFair)) {
      const banner = el("div", "pl-stale");
      banner.appendChild(el("span", "pl-stale-ico", "\u26a0"));
      banner.appendChild(el("span", "pl-stale-text",
        `Locked on fair value ${fmtLvl(num(locked.fair_value))}; latest analysis says ${fmtLvl(sugFair)}.`));
      const re = el("button", "pl-cmp-apply", "Re-anchor");
      re.type = "button";
      re.title = "Set the fair value to the latest analysis and re-derive tranche prices from their margins";
      re.addEventListener("click", () => { fairValue = sugFair; repriceFromFair(); render(); });
      banner.appendChild(re);
      block.appendChild(banner);
    }

    // Fair value anchor.
    const fvRow = el("div", "pl-fair");
    const fvField = el("label", "pl-tr-field");
    fvField.appendChild(el("span", "pl-tr-label", "Fair value" + (currency ? " (" + currency + ")" : "")));
    const fvIn = numInput("pl-input pl-fv-input", fairValue != null ? r2(fairValue) : null, "anchor");
    fvIn.addEventListener("input", () => {
      fairValue = fvIn.value.trim() === "" ? null : Number(fvIn.value);
      repriceFromFair();
      sync();
    });
    fvField.appendChild(fvIn);
    fvRow.appendChild(fvField);
    if (sugFair != null) {
      const hint = el("span", "pl-fair-hint muted", `analysis: ${fmtLvl(sugFair)}`);
      fvRow.appendChild(hint);
    }
    if (spot != null) fvRow.appendChild(el("span", "pl-fair-hint muted", `spot: ${fmtLvl(spot)}`));
    block.appendChild(fvRow);

    // Two ladders side by side.
    const cols = el("div", "pl-cols");
    const buyCol = sideColumn("buy", buyRows);
    const trimCol = sideColumn("trim", trimRows);
    cols.appendChild(buyCol.col);
    cols.appendChild(trimCol.col);
    block.appendChild(cols);

    summaryHosts = { buySum: buyCol.sum, trimSum: trimCol.sum, buyMatch: buyCol.matchHost, trimMatch: trimCol.matchHost };
    sync();

    if (locked && locked.locked_at) {
      block.appendChild(el("p", "muted pl-when", `Locked ${esc(relTime(locked.locked_at))}`));
    }

    const msg = el("p", "hint pl-msg", "");
    const actions = el("div", "analysis-actions pl-actions");
    const lockBtn = el("button", "primary", locked ? "Update lock" : "Lock in");
    lockBtn.type = "button";
    lockBtn.addEventListener("click", async () => {
      const payload = buildLockPayload();
      const err = validate(payload);
      if (err) { msg.className = "hint pl-msg err"; msg.textContent = err; return; }
      lockBtn.disabled = true;
      msg.className = "hint pl-msg";
      msg.textContent = "Locking\u2026";
      try {
        const res = await api("/api/price-levels/lock", "POST", payload);
        locked = res.level;
        seedFrom(locked);
        render();
      } catch (e) {
        lockBtn.disabled = false;
        msg.className = "hint pl-msg err";
        msg.textContent = "Lock failed: " + e.message;
      }
    });
    actions.appendChild(lockBtn);
    if (locked) {
      const clearBtn = el("button", "ghost", "Clear");
      clearBtn.type = "button";
      clearBtn.addEventListener("click", async () => {
        clearBtn.disabled = true;
        msg.className = "hint pl-msg";
        msg.textContent = "Clearing\u2026";
        try {
          await api("/api/price-levels/clear", "POST", { symbol: sym });
          locked = null;
          seedFrom(suggested);
          render();
        } catch (e) {
          clearBtn.disabled = false;
          msg.className = "hint pl-msg err";
          msg.textContent = "Clear failed: " + e.message;
        }
      });
      actions.appendChild(clearBtn);
    }
    block.appendChild(actions);
    block.appendChild(msg);
  }

  function buildLockPayload() {
    const mapSide = (rows, side) => rows
      .filter((r) => num(r.price) != null)
      .map((r) => {
        const t = { price: num(r.price), size_pct: num(r.size) };
        const m = num(r.margin);
        if (m != null) t[side === "buy" ? "discount_pct" : "premium_pct"] = m;
        return t;
      });
    return {
      symbol: sym,
      fair_value: num(fairValue),
      buy_ladder: mapSide(buyRows, "buy"),
      trim_ladder: mapSide(trimRows, "trim"),
      currency,
      source: {
        kind: "ticker_analysis",
        stem: analysis && analysis.stem,
        suggested: {
          fair_value: sugFair,
          buy_ladder: suggested.buy_ladder || [],
          trim_ladder: suggested.trim_ladder || [],
        },
      },
    };
  }

  // Mirror the backend validation so we fail fast with a friendly message.
  function validate(p) {
    if (!p.buy_ladder.length && !p.trim_ladder.length) {
      return "Add at least one buy or trim tranche (or Clear to remove).";
    }
    const buyPrices = p.buy_ladder.map((t) => t.price);
    const trimPrices = p.trim_ladder.map((t) => t.price);
    if (buyPrices.length && trimPrices.length && Math.max(...buyPrices) >= Math.min(...trimPrices)) {
      return "Every buy price must be below every trim price.";
    }
    if (p.fair_value != null) {
      if (buyPrices.length && Math.max(...buyPrices) > p.fair_value) return "Buy prices must be at or below fair value.";
      if (trimPrices.length && Math.min(...trimPrices) < p.fair_value) return "Trim prices must be at or above fair value.";
    }
    return null;
  }

  render();
  return block;
}

// A bare numeric input for the ladder editor (currency/percent decorated by the
// surrounding field label).
function numInput(cls, value, placeholder) {
  const inp = document.createElement("input");
  inp.type = "number";
  inp.step = "any";
  inp.min = "0";
  inp.className = cls;
  if (placeholder) inp.placeholder = placeholder;
  if (value != null) inp.value = String(value);
  return inp;
}

// In-depth, on-demand analysis via the local agent CLIs (Claude -> Cursor).
// The cheap tier: a reasoning pass over the deterministic numbers above, no web
// crawl. The expensive, web-sourced tier is the Deep Research card below. Shows
// the latest saved note if one exists, otherwise a button to generate one.
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

  function renderRetry(refresh) {
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

  async function run(refresh) {
    status.classList.remove("err");
    status.innerHTML = `<span class="spinner"></span> starting&hellip;`;
    body.innerHTML = "";
    try {
      const start = await api("/api/analyze/" + encodeURIComponent(sym), "POST", { refresh: !!refresh });
      await pollDeepJob(start.id, status, async () => { await show(); }, `Analyzing ${sym}`,
        () => renderRetry(refresh));
    } catch (e) {
      status.classList.add("err");
      status.textContent = "analysis failed: " + e.message;
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
      `</div><div class="prose analysis-prose"></div>`;
    const prose = body.querySelector(".analysis-prose");
    prose.innerHTML = mdToHtml(a.report || "");
    linkifyTickers(prose);
    decorateAnalysis(prose);
    decorateSources(prose, rec);
    // Price-level triggers go right under the meta bar (above the prose) so the
    // accept/lock affordance is the first thing seen after the verdict.
    let lockedMap;
    try {
      lockedMap = (await api("/api/price-levels")).levels || {};
    } catch (_e) {
      lockedMap = {};
    }
    body.insertBefore(priceLevelsBlock(rec, a, lockedMap[sym]), prose);
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
function renderDeepResearchCard(rec) {
  const sym = rec.symbol;
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
  const norm = (s) => String(s || "").replace(/[^a-z0-9]/gi, "").toUpperCase();
  const want = norm(sym);

  function openRun(stem) {
    pushNav({ view: "analyses", run: stem });
    setActiveView("analyses");
  }

  function goLogin() {
    pushNav({ view: "pipeline" });
    setActiveView("pipeline");
  }

  function runRowEl(r) {
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
      status.textContent = "deep research failed: " + e.message;
    }
  }

  function renderIdle(loggedIn, runs) {
    status.textContent = "";
    status.classList.remove("err");
    body.innerHTML = "";
    if (runs.length) {
      const list = el("div", "dr-runs");
      runs.forEach((r) => list.appendChild(runRowEl(r)));
      body.appendChild(list);
    } else {
      body.appendChild(el("p", "hint",
        `No Deep Research for <strong>${esc(sym)}</strong> yet. The in-depth analysis ` +
        `above reasons over the data on this page; this spends a Perplexity Deep ` +
        `Research crawl for a fuller, web-sourced single-name report &mdash; a few ` +
        `minutes, and quota-limited, so it's opt-in.`));
    }
    const actions = el("div", "analysis-actions");
    if (loggedIn === false) {
      body.appendChild(el("p", "hint muted",
        "A logged-in Perplexity session is required to run a new one."));
      const a = el("button", "primary", "Set up Perplexity login");
      a.type = "button";
      a.addEventListener("click", goLogin);
      actions.appendChild(a);
    } else {
      const btn = el("button", runs.length ? "ghost" : "primary",
        runs.length ? "\u21bb Run new Deep Research" : "Run Deep Research");
      btn.type = "button";
      btn.addEventListener("click", startRun);
      actions.appendChild(btn);
    }
    body.appendChild(actions);
  }

  async function show() {
    status.innerHTML = `<span class="spinner"></span> loading&hellip;`;
    body.innerHTML = "";
    let runs = [];
    let loggedIn = null;
    let live = null;
    try {
      const [runsRes, loginRes, jobsRes] = await Promise.all([
        api("/api/deep-runs").then((d) => d.runs || []).catch(() => []),
        api("/api/deep-research/login-status").catch(() => null),
        api("/api/jobs").then((d) => d.jobs || []).catch(() => []),
      ]);
      runs = runsRes
        .filter((r) => r.kind === "ticker" && norm(r.symbol) === want)
        .sort((a, b) => (a.stem < b.stem ? 1 : -1));
      loggedIn = loginRes ? !!loginRes.logged_in : null;
      live = jobsRes.find((j) => j.kind === "deep_research" &&
        (j.state === "running" || j.state === "queued") &&
        norm(String(j.segment || "").replace(/^ticker-/, "")) === want) || null;
    } catch (_e) { /* fall through to idle */ }

    if (live) {
      status.innerHTML = `<span class="spinner"></span> Deep Research running&hellip;`;
      await pollDeepJob(live.id, status, async () => { await show(); },
        `Deep Research \u00b7 ${sym}`, async () => { await show(); });
      return;
    }
    renderIdle(loggedIn, runs);
  }

  show();
  return card;
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
  return createQaCard({
    title: "Ask about " + sym,
    emptyHint:
      "No questions yet. Ask anything about the numbers, momentum, valuation, or how it sits " +
      "in your portfolio. The thread is archived so you can pick it up later.",
    placeholder: `Ask a follow-up about ${sym} \u2014 grounded in the data above. Ctrl/\u2318+Enter to send.`,
    pollLabel: `Q&A \u00b7 ${sym}`,
    confirmMsg: `Clear the archived Q&A thread for ${sym}?`,
    // The ticker set must be loaded before linkifyTickers runs in the thread.
    prepare: ensureTickerSet,
    loadThread: () => api("/api/qa/" + encodeURIComponent(sym)),
    postQuestion: (q) => api("/api/qa/" + encodeURIComponent(sym), "POST", { question: q }),
    clearThread: () => api("/api/qa/" + encodeURIComponent(sym), "POST", { clear: true }),
    deleteTurn: (idx) => api("/api/qa/" + encodeURIComponent(sym), "POST", { delete: idx }),
    turnMeta: (t) => [t.backend_label, modelLabel(t.model), t.ts ? relTime(t.ts) : null],
    usageHtml: (t) => qaUsageHtml(t.usage),
  });
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

  const card = sectionCard("Business", "biz-card");

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
  ["1d", "1D"], ["1w", "1W"], ["1mo", "1M"], ["3mo", "3M"], ["6mo", "6M"],
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
  const parseStamp = (value) => new Date(value.length > 10 ? value : value + "T00:00:00Z");
  const spanDays = (parseStamp(last.date) - parseStamp(first.date)) / 86400000;
  const dateLabel = (value) => {
    const d = parseStamp(value);
    if (spanDays < 2) return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
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

  // Evenly spaced x-axis date labels (previously just first + last). Aim for one
  // label per ~120px, snapped to real data points so labels line up with the
  // plotted line. End ticks anchor inward; interior ticks center and get a faint
  // vertical gridline to match the y-axis grid. Set() dedupes when there are
  // fewer points than ticks.
  const xTickCount = Math.min(Math.max(2, Math.round(innerW / 120)), points.length);
  const xIndices = Array.from(new Set(
    Array.from({ length: xTickCount }, (_, i) =>
      Math.round((i / (xTickCount - 1)) * (points.length - 1))),
  ));
  const xAxis = xIndices.map((idx) => {
    const xp = x(idx).toFixed(1);
    const anchor = idx === 0 ? "start" : idx === points.length - 1 ? "end" : "middle";
    const interior = idx > 0 && idx < points.length - 1;
    return (
      (interior ? `<line class="chart-grid" x1="${xp}" y1="${pad.top}" x2="${xp}" y2="${height - pad.bottom}"></line>` : "") +
      `<text class="chart-label" x="${xp}" y="${height - 9}" text-anchor="${anchor}">${esc(dateLabel(points[idx].date))}</text>`
    );
  }).join("");

  // Vertical "mountain" gradient: saturated at the price line, fading to nothing
  // at the baseline. A flat-opacity fill (the old approach) reads as a featureless
  // slab whose top — the volatile line — smears into a band; the fade ties the
  // fill to the line and keeps the baseline unambiguous.
  const fillId = "price-area-fill";
  const svg =
    `<svg class="price-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="${esc(rec.symbol)} ${esc(rangeLabel)} price history">` +
      `<defs><linearGradient id="${fillId}" class="chart-fill-grad ${trend}" x1="0" y1="0" x2="0" y2="1">` +
        `<stop class="cf-top" offset="0%"></stop><stop class="cf-bot" offset="100%"></stop>` +
      `</linearGradient></defs>` +
      `<line class="chart-axis" x1="${pad.left}" y1="${height - pad.bottom}" x2="${width - pad.right}" y2="${height - pad.bottom}"></line>` +
      `<line class="chart-axis" x1="${pad.left}" y1="${pad.top}" x2="${pad.left}" y2="${height - pad.bottom}"></line>` +
      yAxis +
      xAxis +
      `<polygon class="chart-area" fill="url(#${fillId})" points="${area}"></polygon>` +
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
  // undefined == not fetched yet (streaming in). Show the section shell with a
  // progress bar overlaid so the rest of the dossier isn't held hostage to it.
  if (rec.history === undefined) {
    const card = el("div", "card section-loading");
    card.appendChild(el("h2", "section", "Recent pulls"));
    const body = el("div", "section-body");
    body.appendChild(el("div", "hint", "Fetching the change log\u2026"));
    body.appendChild(el("div", "section-overlay", `<div class="progress-bar"><span></span></div>`));
    card.appendChild(body);
    return card;
  }
  const rows = rec.history || [];
  const meta = rows.length ? `${rows.length} snapshot${rows.length === 1 ? "" : "s"}` : "none yet";
  const { details, body } = collapsibleCard("Recent pulls", { meta });
  if (!rows.length) {
    body.appendChild(el("div", "hint", "No history yet. Pull this ticker again later and this becomes a change log instead of a memory test."));
    return details;
  }
  const table = simpleTable({
    className: "history-table",
    head: `<tr><th>As of</th><th class="num">Price</th><th class="num">Fwd P/E</th><th class="num">P/S</th><th class="num">Revenue</th><th>Trust</th><th></th></tr>`,
    rows: rows.slice(0, 8),
    cells: (h) =>
      `<td>${esc(h.as_of ? new Date(h.as_of).toLocaleString() : "n/a")}</td>` +
      `<td class="num">${esc(fmtPrice(h.price))}</td>` +
      `<td class="num">${esc(fmtX(h.pe_fwd))}</td>` +
      `<td class="num">${esc(fmtX(h.ps))}</td>` +
      `<td class="num">${esc(fmtB(h.revenue_ttm_usd_b))}</td>` +
      `<td><span class="dot ${esc(h.data_quality || "INFO")}"></span>${esc(h.data_quality || "INFO")}</td>`,
    onRow: (tr, h) => {
      // The delete column carries a per-row listener, so it's appended as real
      // DOM after the string cells rather than baked into the cells() HTML.
      const delCell = el("td", "history-del-cell");
      if (h.stamp) {
        const del = el("button", "history-del", "\u2715");
        del.type = "button";
        del.title = "Delete this snapshot";
        del.setAttribute("aria-label", "Delete this snapshot");
        del.addEventListener("click", () => {
          const when = h.as_of ? new Date(h.as_of).toLocaleString() : "this";
          if (!confirm(`Delete the ${when} snapshot for ${rec.symbol}? This cannot be undone.`)) return;
          del.disabled = true;
          deleteHistorySnapshot(rec, h.stamp).catch((e) => {
            del.disabled = false;
            alert("Delete failed: " + e.message);
          });
        });
        delCell.appendChild(del);
      }
      tr.appendChild(delCell);
    },
  });
  body.appendChild(table);
  return details;
}

async function deleteHistorySnapshot(rec, stamp) {
  const res = await api("/api/history/delete", "POST", { symbol: rec.symbol, stamp });
  rec.history = res.history || [];
  const slot = $("#dd-result [data-slot='history']");
  if (slot) {
    slot.innerHTML = "";
    slot.appendChild(renderHistory(rec));
  }
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

export {
  loadTickerFromCache,
  pullTicker,
  METRIC_ROWS,
  renderDeepDive,
  hydrateHistory,
  renderAnalysisCard,
  qaUsageHtml,
  renderQaCard,
  openAnalysisConfig,
  renderBusiness,
  PRICE_RANGES,
  chartSvg,
  renderPriceChart,
  collapsibleCard,
  renderHistory,
  dataQualityTag,
  sourceLine,
  renderThesis,
};
