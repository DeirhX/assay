import { starHtml } from "./basket";
import { $$, api, decisionPill, el, emptyState, esc, fmtB, fmtPct, fmtPrice, fmtX, loadError, pctClass, relAge, scoreClass, sectionCard, spinner, state } from "./core";
import type { SegmentSummary } from "./api-types";
import { sparkPlaceholder, hydrateSparks } from "./spark";
import { startPipeline } from "./analyses";
import { analyzeFromAnywhere } from "./ticker-nav";
import { cleanSlug, isSegmentSlug, navFromUrl, pushNav, replaceViewState, setActiveView } from "./shell";

// ---- segment --------------------------------------------------------------
async function loadSegmentList() {
  const sel = $$("#segment-select");
  const pipeSel = $$("#pipe-segment-select");
  try {
    const { segments } = await api<{ segments: SegmentSummary[] }>("/api/segments");
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
    const opt = `<option value="" disabled selected>couldn't load segments: ${esc((e as Error).message)}</option>`;
    sel.innerHTML = opt;
    if (pipeSel) pipeSel.innerHTML = opt;
    return [];
  }
}

async function runSegmentPull(name: string, { push = true }: { push?: boolean } = {}) {
  const status = $$("#seg-status");
  name = cleanSlug(name);
  if (!name) return;
  if (!isSegmentSlug(name)) {
    status.textContent = "Pick a segment from the list first.";
    status.classList.add("err");
    return;
  }
  if (push) pushNav({ view: "segment", segment: name });
  setActiveView("segment");
  $$<HTMLSelectElement>("#segment-select").value = name;
  status.classList.remove("err");
  status.innerHTML = `${spinner()} Pulling every peer in "${esc(name)}" live — this takes a bit...`;
  $$<HTMLButtonElement>("#segment-run").disabled = true;
  try {
    const rec = await api("/api/pull-segment/" + encodeURIComponent(name), "POST");
    status.textContent = `Pulled ${rec.members.length} names at ${new Date(rec.as_of).toLocaleString()}`;
    renderSegment(rec);
  } catch (e) {
    loadError(status, "Segment pull failed", e);
  } finally {
    $$<HTMLButtonElement>("#segment-run").disabled = false;
  }
}

async function loadCachedSegment(name: string, { push = false }: { push?: boolean } = {}) {
  const requestedSort = navFromUrl().sort.match(/^([^:]+):(asc|desc)$/);
  const validSortKeys = new Set(SEG_COLS.map(([key]) => key).filter((key) => key !== "__spark"));
  state.segSort = requestedSort && validSortKeys.has(requestedSort[1])
    ? { key: requestedSort[1], dir: requestedSort[2] === "asc" ? 1 : -1 }
    : { key: "research_score", dir: -1 };
  const status = $$("#seg-status");
  name = cleanSlug(name);
  if (!name) return;
  if (!isSegmentSlug(name)) {
    status.textContent = "Pick a segment from the list first.";
    status.classList.add("err");
    return;
  }
  if (push) pushNav({ view: "segment", segment: name });
  setActiveView("segment");
  $$<HTMLSelectElement>("#segment-select").value = name;
  status.classList.remove("err");
  status.textContent = "Loading cached segment...";
  try {
    const rec = await api("/api/segment/" + encodeURIComponent(name));
    status.textContent = `Cached ${rec.members.length} names from ${new Date(rec.as_of).toLocaleString()}`;
    renderSegment(rec);
  } catch (e) {
    status.textContent = (e as Error).message + " — run a live pull first.";
    status.classList.add("err");
  }
}

let _initialized = false;
function initSegment(): void {
  if (_initialized) return;
  _initialized = true;

  $$("#segment-select").addEventListener("change", () => {
    if ($$("#view-segment").classList.contains("active")) {
      pushNav({ view: "segment", segment: $$<HTMLSelectElement>("#segment-select").value }, { replace: true });
    }
  });
  $$("#pipe-segment-select").addEventListener("change", () => {
    if ($$("#view-pipeline").classList.contains("active")) {
      pushNav({ view: "pipeline", segment: $$<HTMLSelectElement>("#pipe-segment-select").value }, { replace: true });
    }
  });
  $$("#segment-run").addEventListener("click", () =>
    runSegmentPull($$<HTMLSelectElement>("#segment-select").value));
  $$("#segment-load").addEventListener("click", () =>
    loadCachedSegment($$<HTMLSelectElement>("#segment-select").value, { push: true }));
  $$("#segment-deep").addEventListener("click", () =>
    startPipeline($$<HTMLSelectElement>("#segment-select").value));

  // Seed the result area so an un-loaded Segment tab is a clear prompt, not a void.
  // Any pull/cache load replaces this; a deep-link to ?segment= loads over it.
  emptyState($$("#seg-result"),
    "<strong>No segment loaded</strong>" +
    "Pick a peer universe above, then <em>Run live pull</em> (~30-60s for ~20 names) " +
    "or <em>Load cached</em> for the last saved table.");
}

// Leading ★ column: the segment table is the prime discovery surface, so a
// find must be shortlistable in place (basket, "curious" tier, segment
// provenance) instead of forcing a deep-dive round-trip per name. Not in
// SEG_COLS — it's not a sortable metric.
const SEG_COLS: [string, string, boolean][] = [
  ["symbol", "Symbol", false],
  ["__spark", "Trend", false],   // cached-only sparkline; not a sortable metric
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

// A peer-comparison row: the named metric columns the table renders, plus an
// index signature so the click-to-sort can read an arbitrary column key.
interface SegMember {
  symbol: string;
  data_quality?: string;
  decision?: string;
  research_score?: number | null;
  sleeve?: string;
  owned_pct_nav?: number | null;
  price?: number | null;
  market_cap_usd_b?: number | null;
  pe_fwd?: number | null;
  ps?: number | null;
  rev_growth_yoy_pct?: number | null;
  gross_margin_pct?: number | null;
  chg_3m_pct?: number | null;
  chg_12m_pct?: number | null;
  pct_below_52w_high?: number | null;
  [key: string]: unknown;
}

export interface SegmentRec {
  title: string;
  segment?: string;   // the slug, carried on every cached/live pull record
  members: SegMember[];
  [key: string]: unknown;
}

function renderSegment(rec: SegmentRec) {
  state.lastSegment = rec;
  const out = $$("#seg-result");
  out.innerHTML = "";
  const card = sectionCard(esc(rec.title) + " — peer comparison");
  const table = el("table", "segment-table");
  const thead = el("thead");
  const htr = el("tr");
  htr.appendChild(el("th", "seg-star-th", "")); // ★ column — not sortable
  SEG_COLS.forEach(([key, label, num]) => {
    const th = el("th", num ? "num" : "", esc(label));
    // The Trend column is a rendered sparkline, not a metric: no sort, no arrow.
    if (key !== "__spark") {
      th.addEventListener("click", () => {
        const s = state.segSort;
        s.dir = s.key === key ? -s.dir : (num ? -1 : 1);
        s.key = key;
        replaceViewState({
          sort: key === "research_score" && s.dir < 0
            ? "" : `${key}:${s.dir < 0 ? "desc" : "asc"}`,
        });
        if (state.lastSegment) renderSegment(state.lastSegment);
      });
      if (state.segSort.key === key) th.innerHTML += state.segSort.dir < 0 ? " ↓" : " ↑";
    }
    htr.appendChild(th);
  });
  thead.appendChild(htr);
  table.appendChild(thead);

  const tbody = el("tbody");
  const rows = rec.members.slice().sort((a: SegMember, b: SegMember) => {
    const k = state.segSort.key, d = state.segSort.dir;
    const av = a[k], bv = b[k];
    if (typeof av === "string" || typeof bv === "string") return d * String(av ?? "").localeCompare(String(bv ?? ""));
    if (av == null) return 1; if (bv == null) return -1;
    return d * (Number(av) - Number(bv));
  });
  const slug = typeof rec.segment === "string" ? rec.segment : "";
  rows.forEach((m: SegMember) => {
    const tr = el("tr");
    // Discovery default: a segment star is bare interest, so it lands in the
    // basket as a "curious" pick carrying which universe it was found in.
    const star = el("td", "seg-star-cell", starHtml(m.symbol, "segment", { tier: "curious", segment: slug || undefined }));
    tr.appendChild(star);
    const cells = [
      `<span class="dot ${m.data_quality}"></span><strong>${esc(m.symbol)}</strong>`,
      sparkPlaceholder(m.symbol),
      decisionPill(m.decision, { fallback: "research" }),
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
  // The peer table has ~16 nowrap columns and is routinely wider than the card;
  // a scroll wrapper keeps that overflow inside the panel instead of letting it
  // bleed past the rounded border and drag the whole page sideways.
  const scroll = el("div", "seg-table-scroll");
  scroll.appendChild(table);
  card.appendChild(scroll);
  // One batch /api/spark call fills the Trend column; cached-only, so members
  // without a cached dossier just show an empty slot.
  void hydrateSparks(table);
  card.appendChild(el("div", "hint", "Score is a rough research queue heuristic from target rule, band gap, growth, valuation, momentum, and data trust. It is not an order signal, because we are not building a robot broker for future regret. Click a row to deep-dive."));
  out.appendChild(card);
}

export {
  initSegment,
  loadSegmentList,
  runSegmentPull,
  loadCachedSegment,
  SEG_COLS,
  renderSegment,
};
