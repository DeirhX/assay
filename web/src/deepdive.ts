// @ts-nocheck
import { $, api, decisionClass, el, esc, fmtPct, fmtPrice, fmtSignedWeight, fmtWeight, freshnessNote, instrumentBadge, pctClass, sectionCard, state } from "./core";
import { cleanSymbol, pushNav, setActiveView } from "./shell";
import { recordView, renderViewedTickers } from "./viewed";
import { renderPriceChart } from "./deepdive/price-chart";
import { collapsibleCard, dataQualityTag, sourceLine, renderBusiness } from "./deepdive/cards";
import { renderQaCard } from "./deepdive/qa";
import { METRIC_ROWS, loadPeerStats } from "./deepdive/metrics";
import { renderAnalysisCard, renderDeepResearchCard } from "./deepdive/analysis-card";
import { renderHistory } from "./deepdive/history-card";
import { renderThesis } from "./deepdive/thesis";

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

export {
  loadTickerFromCache,
  pullTicker,
  renderDeepDive,
  hydrateHistory,
};
