"use strict";

const state = {
  holdings: {},
  nav: null,
  lastSegment: null,
  segSort: { key: "research_score", dir: -1 },
  currentDeepRun: null,
  privacyMode: localStorage.getItem("financeRebalancingPrivacyMode") === "1",
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
const VIEWS = new Set(["deepdive", "segment", "pipeline", "holdings"]);

const cleanSymbol = (raw) => (raw || "").trim().toUpperCase();
const cleanSlug = (raw) => (raw || "").trim();

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
  return nav;
}

function setActiveView(view) {
  const active = VIEWS.has(view) ? view : "deepdive";
  document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.view === active));
  document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
  $("#view-" + active).classList.add("active");
  if (active === "holdings") loadHoldings();
  if (active === "pipeline") loadPipeline();
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
      `invested ${sensitive(`${Math.round(h.invested_value || 0).toLocaleString()} CZK`, "invested value")} · ` +
      `snapshot ${(h.generated_at || "").slice(0, 10)}`;
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

// ---- deep dive ------------------------------------------------------------
$("#ticker-go").addEventListener("click", () => pullTicker($("#ticker-input").value));
$("#ticker-input").addEventListener("keydown", (e) => { if (e.key === "Enter") pullTicker($("#ticker-input").value); });

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
    status.textContent = `No cached research for ${sym}; press Analyze to pull live data.`;
    status.classList.add("err");
    $("#dd-result").innerHTML = "";
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

  // cross-checks (the trust layer)
  const checks = rec.cross_checks || [];
  const trust = el("div", "card");
  trust.appendChild(el("h2", "section", "Data trust" + dataQualityTag(checks)));
  const list = el("div", "checks");
  if (!checks.length) {
    list.appendChild(el("div", "check INFO", `<span class="sev">INFO</span><span>No cross-checks produced.</span>`));
  }
  checks.forEach((c) => {
    list.appendChild(el("div", "check " + c.severity,
      `<span class="sev">${c.severity}</span><span><span class="metric">${esc(c.metric)}:</span> ${esc(c.message)}</span>`));
  });
  trust.appendChild(list);
  if (rec.errors && rec.errors.length) {
    trust.appendChild(el("div", "status err", "source errors: " + rec.errors.map(esc).join("; ")));
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

function renderHistory(rec) {
  const card = el("div", "card");
  card.appendChild(el("h2", "section", "Recent pulls"));
  const rows = rec.history || [];
  if (!rows.length) {
    card.appendChild(el("div", "hint", "No history yet. Pull this ticker again later and this becomes a change log instead of a memory test."));
    return card;
  }
  const table = el("table");
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
  card.appendChild(table);
  return card;
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
  const card = el("div", "card");
  card.appendChild(el("h2", "section", "Thesis &amp; action — your judgement (kept separate from the numbers)"));
  const g = el("div", "thesis-grid");
  g.innerHTML =
    `<div><label>Summary</label><textarea id="th-summary" rows="4" placeholder="What's the story? Momentum vs valuation.">${esc(t.summary || "")}</textarea></div>` +
    `<div><label>Action</label><textarea id="th-action" rows="4" placeholder="Add / hold / trim / sell / wait — and sizing.">${esc(t.action || "")}</textarea></div>` +
    `<div><label>Drivers (one per line)</label><textarea id="th-drivers" rows="4" placeholder="Real reasons it moved">${esc((t.drivers || []).join("\n"))}</textarea></div>` +
    `<div><label>Downside triggers (one per line)</label><textarea id="th-triggers" rows="4" placeholder="What breaks the thesis">${esc((t.downside_triggers || []).join("\n"))}</textarea></div>`;
  card.appendChild(g);
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
  card.appendChild(actions);
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
      o.textContent = `${s.title} (${s.count})${s.status === "draft" ? " · draft" : ""}${s.cached ? " · cached" : ""}`;
      sel.appendChild(o);
      if (pipeSel) {
        const p = o.cloneNode(true);
        pipeSel.appendChild(p);
      }
    });
    return segments;
  } catch (e) {
    sel.innerHTML = `<option>${esc(e.message)}</option>`;
    if (pipeSel) pipeSel.innerHTML = `<option>${esc(e.message)}</option>`;
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
  const table = el("table");
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
async function loadPipeline() {
  await loadSegmentList();
  await refreshDeepRuns();
  if (!$("#pipe-date").value) $("#pipe-date").value = new Date().toISOString().slice(0, 10);
}

function pipeSegment() {
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
    status.textContent = rec.warnings && rec.warnings.length ? rec.warnings.join(" ") : "draft ready; edit and approve when sane";
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
    status.textContent = `saved ${rec.name}`;
    await loadSegmentList();
    $("#pipe-segment-select").value = rec.name;
    pushNav({ view: "pipeline", segment: rec.name }, { replace: true });
  } catch (e) {
    status.textContent = "save failed: " + e.message;
    status.classList.add("err");
  }
});

$("#pipe-build-prompt").addEventListener("click", async () => {
  const status = $("#pipe-prompt-status");
  status.classList.remove("err");
  status.textContent = "building prompt...";
  try {
    const rec = await api("/api/deep-prompt?segment=" + encodeURIComponent(pipeSegment()));
    $("#pipe-date").value = rec.date;
    $("#pipe-prompt").value = rec.prompt;
    pushNav({ view: "pipeline", segment: rec.segment || pipeSegment() }, { replace: true });
    status.textContent = "prompt ready";
  } catch (e) {
    status.textContent = "prompt failed: " + e.message;
    status.classList.add("err");
  }
});

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
    status.textContent = `saved ${rec.stem}`;
    state.currentDeepRun = rec.stem;
    pushNav({ view: "pipeline", segment: pipeSegment(), run: rec.stem });
    await refreshDeepRuns();
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
    out.innerHTML = "";
    const list = el("div", "run-list");
    (runs || []).forEach((run) => {
      const row = el("button", "run-row", "");
      const files = Object.keys(run.files || {}).sort().join(", ");
      row.innerHTML = `<strong>${esc(run.stem)}</strong><span>${esc(files)}</span>`;
      row.addEventListener("click", () => loadDeepRun(run.stem));
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
  if (rec.warnings && rec.warnings.length) {
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
  card.appendChild(el("h2", "section", "Target-model proposal"));
  if (changes.length) {
    const pre = el("pre", "json-preview", esc(JSON.stringify(changes, null, 2)));
    card.appendChild(pre);
  } else {
    card.appendChild(el("div", "hint", "No target-model changes proposed."));
  }
  if (rec.markdown) {
    card.appendChild(el("h2", "section", "Review markdown"));
    card.appendChild(el("pre", "markdown-preview", esc(rec.markdown.slice(0, 8000))));
  }
  out.appendChild(card);
}

$("#pipe-apply-proposal").addEventListener("click", async () => {
  const status = $("#pipe-apply-status");
  const m = (state.currentDeepRun || "").match(/^(.*)-(\d{4}-\d{2}-\d{2})$/);
  if (!m) {
    status.textContent = "select or generate a run first";
    status.classList.add("err");
    return;
  }
  if (!window.confirm("Apply this target-model proposal? This changes target-model.json, not trades.")) return;
  status.classList.remove("err");
  status.textContent = "applying proposal...";
  try {
    const rec = await api("/api/target-proposal/apply", "POST", { segment: m[1], date: m[2], confirm: true });
    status.textContent = `applied: ${rec.applied.join(", ") || "none"}; skipped: ${rec.skipped.length}`;
  } catch (e) {
    status.textContent = "apply failed: " + e.message;
    status.classList.add("err");
  }
});

// ---- boot -----------------------------------------------------------------
applyPrivacyMode(state.privacyMode);
const initialNav = navFromUrl();
window.history.replaceState(initialNav, "", window.location.href);
restoreNav(initialNav);
