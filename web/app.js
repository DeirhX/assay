"use strict";

const state = { holdings: {}, nav: null, lastSegment: null, segSort: { key: "owned_pct_nav", dir: -1 } };

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
const fmtCZK = (v) => {
  if (v == null) return "n/a";
  return Math.abs(v) >= 1000 ? Math.round(v).toLocaleString() : Number(v).toFixed(0);
};

async function api(path, method = "GET", body = null) {
  const opt = { method, headers: {} };
  if (body) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const res = await fetch(path, opt);
  const data = await res.json().catch(() => ({ error: "bad response" }));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

// ---- tabs -----------------------------------------------------------------
document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
    btn.classList.add("active");
    $("#view-" + btn.dataset.view).classList.add("active");
    if (btn.dataset.view === "holdings") loadHoldings();
  });
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
    status.textContent =
      `NAV ${Math.round(h.net_asset_value || 0).toLocaleString()} CZK · ` +
      `invested ${Math.round(h.invested_value || 0).toLocaleString()} CZK · ` +
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
      const right = isOpt ? `${fmtCZK(p.base_market_value)} CZK` : `${w.toFixed(2)}%`;
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
  document.querySelector('.tab[data-view="deepdive"]').click();
  $("#ticker-input").value = sym;
  pullTicker(sym);
}

// ---- deep dive ------------------------------------------------------------
$("#ticker-go").addEventListener("click", () => pullTicker($("#ticker-input").value));
$("#ticker-input").addEventListener("keydown", (e) => { if (e.key === "Enter") pullTicker($("#ticker-input").value); });

async function pullTicker(raw) {
  const sym = (raw || "").trim().toUpperCase();
  if (!sym) return;
  const status = $("#dd-status");
  status.classList.remove("err");
  status.innerHTML = `<span class="spinner"></span> Pulling ${esc(sym)} from live sources...`;
  $("#ticker-go").disabled = true;
  try {
    const rec = await api("/api/pull/" + encodeURIComponent(sym), "POST");
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
  const owned = state.holdings[rec.symbol];

  const card = el("div", "card");
  // header
  const head = el("div", "dd-head");
  head.innerHTML =
    `<span class="sym">${esc(rec.symbol)}</span>` +
    `<span class="name">${esc(rec.name || "")}</span>` +
    `<span class="price">${fmtPrice(price)} <small class="muted">${esc(rec.currency || "")}</small></span>`;
  card.appendChild(head);

  const sub = el("div", "dd-sub");
  sub.innerHTML =
    `<span>as of ${new Date(rec.as_of).toLocaleString()}</span>` +
    (owned != null ? `<span class="owned-pill">held: ${owned.toFixed(2)}% NAV</span>` : `<span class="muted">not held</span>`);
  card.appendChild(sub);

  // source badges
  const badges = el("div", "badges");
  ["yahoo", "sec_edgar", "fmp"].forEach((s) => {
    const on = rec.sources && rec.sources[s];
    badges.appendChild(el("span", "badge " + (on ? "on" : "off"), (on ? "✓ " : "· ") + s.replace("_", " ")));
  });
  card.appendChild(badges);
  out.appendChild(card);

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
  mcard.appendChild(el("h2", "section", "Fundamentals"));
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

  // thesis editor
  out.appendChild(renderThesis(rec));
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
  try {
    const { segments } = await api("/api/segments");
    sel.innerHTML = "";
    segments.forEach((s) => {
      const o = el("option");
      o.value = s.name;
      o.textContent = `${s.title} (${s.count})${s.cached ? " · cached" : ""}`;
      sel.appendChild(o);
    });
  } catch (e) {
    sel.innerHTML = `<option>${esc(e.message)}</option>`;
  }
}

$("#segment-run").addEventListener("click", async () => {
  const name = $("#segment-select").value;
  const status = $("#seg-status");
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
});

$("#segment-load").addEventListener("click", async () => {
  const name = $("#segment-select").value;
  const status = $("#seg-status");
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
});

const SEG_COLS = [
  ["symbol", "Symbol", false],
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
  card.appendChild(el("div", "hint", "Green/amber/red dot = data-trust level of that name's last pull. Click a row to deep-dive. Held % is your current NAV weight — empty means a peer you don't own."));
  out.appendChild(card);
}

// ---- boot -----------------------------------------------------------------
loadHoldings();
loadSegmentList();
$("#ticker-input").focus();
