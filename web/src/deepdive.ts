// @ts-nocheck
import { ensureTickerSet, linkifyTickers, mdToHtml } from "./analyses";
import { $, api, decisionClass, el, esc, fmtB, fmtPct, fmtPrice, fmtSignedWeight, fmtWeight, fmtX, freshnessNote, instrumentBadge, pctClass, sectionCard, simpleTable, state } from "./core";
import { pollDeepJob } from "./errors";
import { cleanSymbol, downloadText, modelLabel, pushNav, setActiveView } from "./shell";
import { recordView, renderViewedTickers } from "./viewed";
import { decorateAnalysis, decorateSources } from "./deepdive/decorate";
import { renderPriceChart } from "./deepdive/price-chart";
import { collapsibleCard, dataQualityTag, sourceLine, renderBusiness } from "./deepdive/cards";
import { renderQaCard } from "./deepdive/qa";
import { priceLevelsBlock } from "./deepdive/price-levels";
import { METRIC_ROWS, loadPeerStats } from "./deepdive/metrics";

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
  renderDeepDive,
  hydrateHistory,
  renderAnalysisCard,
  openAnalysisConfig,
  renderHistory,
  renderThesis,
};
