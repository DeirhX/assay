// @ts-nocheck
import { createQaCard, ensureTickerSet, linkifyTickers, mdToHtml } from "./analyses";
import { $, api, decisionClass, el, esc, fmtB, fmtPct, fmtPrice, fmtShares, fmtSignedWeight, fmtWeight, fmtX, pctClass, sectionCard, simpleTable, state } from "./core";
import { pollDeepJob } from "./errors";
import { cleanSymbol, downloadText, modelLabel, pushNav, setActiveView } from "./shell";
import { recordView, relTime, renderViewedTickers } from "./viewed";

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
    `<span class="decision-pill ${decisionClass(decision)}">${esc(decision.replace("_", " "))}</span>` +
    `<span class="price">${fmtPrice(price)} <small class="muted">${esc(rec.currency || "")}</small></span>`;
  card.appendChild(head);

  const sub = el("div", "dd-sub");
  sub.innerHTML =
    `<span>as of ${new Date(rec.as_of).toLocaleString()}</span>` +
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

// Earliest-match stance detection over arbitrary verdict text. Shared by the
// analysis card and the recents list. Returns {cls,label,re} or null.
function detectStance(text) {
  if (!text) return null;
  let best = null;
  VERDICT_STANCES.forEach((s) => {
    const m = text.match(s.re);
    if (m && (best === null || m.index < best.index)) best = { cls: s.cls, label: s.label, re: s.re, index: m.index };
  });
  return best;
}

// Colour-codes the verdict: a pill on the heading + the inline stance word, so
// the recommendation is unmissable when scanning the analysis.
function decorateVerdict(root) {
  const heads = [...root.querySelectorAll("h1,h2,h3,h4,h5,h6")];
  const vh = heads.find((h) => /^\s*verdict\b/i.test(h.textContent || ""));
  if (!vh) return;
  const block = [];
  for (let n = vh.nextElementSibling; n && !/^H[1-6]$/.test(n.tagName); n = n.nextElementSibling) block.push(n);
  const text = block.map((n) => n.textContent).join(" ");
  const best = detectStance(text);
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
  VERDICT_STANCES,
  detectStance,
  decorateVerdict,
  highlightFirstMatch,
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
