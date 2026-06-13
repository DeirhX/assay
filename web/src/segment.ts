// @ts-nocheck
import { $, api, decisionClass, el, emptyState, esc, fmtB, fmtPct, fmtPrice, fmtX, loadError, pctClass, relAge, scoreClass, sectionCard, spinner, state } from "./core";
import { analyzeFromAnywhere } from "./rebalance";
import { cleanSlug, isSegmentSlug, pushNav, setActiveView } from "./shell";

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
  status.innerHTML = `${spinner()} Pulling every peer in "${esc(name)}" live — this takes a bit...`;
  $("#segment-run").disabled = true;
  try {
    const rec = await api("/api/pull-segment/" + encodeURIComponent(name), "POST");
    status.textContent = `Pulled ${rec.members.length} names at ${new Date(rec.as_of).toLocaleString()}`;
    renderSegment(rec);
  } catch (e) {
    loadError(status, "Segment pull failed", e);
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

// Seed the result area so an un-loaded Segment tab is a clear prompt, not a void.
// Any pull/cache load replaces this; a deep-link to ?segment= loads over it.
emptyState($("#seg-result"),
  "<strong>No segment loaded</strong>" +
  "Pick a peer universe above, then <em>Run live pull</em> (~30-60s for ~20 names) " +
  "or <em>Load cached</em> for the last saved table.");

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
  const card = sectionCard(esc(rec.title) + " — peer comparison");
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
    const av = a[k], bv = b[k];
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
    SEG_COLS.forEach(([, , num], i) => {
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

export {
  loadSegmentList,
  runSegmentPull,
  loadCachedSegment,
  SEG_COLS,
  renderSegment,
};
