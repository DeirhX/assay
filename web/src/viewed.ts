import { $$, api, el, esc } from "./core";
import { detectStance } from "./deepdive/decorate";
import { openTicker } from "./ticker-nav";
import { cleanSymbol, navFromUrl, replaceViewState } from "./shell";

// ---- viewed tickers (browser-local recents) -------------------------------
const VIEWED_KEY = "rebal.viewedTickers";
let _viewedSort = "time";  // "time" | "name"

// Merge of the server's /api/ticker-index rows and browser-local recents; every
// field is optional because a row may come from either source alone.
interface ViewedRow {
  symbol: string;
  name?: string;
  as_of?: string | null;
  analyzed_at?: string | null;
  has_analysis?: boolean;
  last_viewed?: string;
  verdict?: string;
}

// Browser-local recents: { SYM: { ts, name } }, persisted in localStorage.
interface ViewedEntry {
  ts: string;
  name: string;
}

function getViewedMap(): Record<string, ViewedEntry> {
  try { return JSON.parse(localStorage.getItem(VIEWED_KEY) || "{}"); } catch (_e) { return {}; }
}
function recordView(sym: string, name?: string) {
  sym = cleanSymbol(sym);
  if (!sym) return;
  const m = getViewedMap();
  m[sym] = { ts: new Date().toISOString(), name: name || (m[sym] && m[sym].name) || "" };
  try { localStorage.setItem(VIEWED_KEY, JSON.stringify(m)); } catch (_e) { /* private mode */ }
  // Mirror to the durable server-side Activity feed (best-effort; the server
  // debounces repeat views, so this stays quiet even if called on every render).
  api("/api/activity/view", "POST", { symbol: sym, name: m[sym].name }).catch(() => { /* offline: local-only */ });
}
function relTime(iso: string | number | null | undefined): string {
  const t = Date.parse(String(iso ?? ""));
  if (!t) return "";
  const s = (Date.now() - t) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  const d = Math.floor(s / 86400);
  return d < 30 ? d + "d ago" : new Date(t).toLocaleDateString();
}

async function renderViewedTickers() {
  _viewedSort = navFromUrl().sort === "name" ? "name" : "time";
  const out = $$("#dd-result");
  out.innerHTML = "";
  $$("#dd-status").textContent = "";
  const card = el("div", "card viewed-card");
  const head = el("div", "page-head page-head--toolbar viewed-head");
  head.appendChild(el("h2", "section", "Viewed tickers"));
  const sortWrap = el("div", "ui-segment-pills viewed-sort");
  sortWrap.appendChild(el("span", "muted", "sort:"));
  [["time", "Recent"], ["name", "Name"]].forEach(([key, label]) => {
    const b = el("button", "chip tone-chip ui-segment-pill" + (_viewedSort === key ? " active" : ""), label);
    b.type = "button";
    b.addEventListener("click", () => {
      _viewedSort = key;
      replaceViewState({ sort: key === "time" ? "" : key });
      renderViewedTickers();
    });
    sortWrap.appendChild(b);
  });
  head.appendChild(sortWrap);
  card.appendChild(head);
  const listWrap = el("div", "viewed-list");
  card.appendChild(listWrap);
  out.appendChild(card);

  let server: ViewedRow[] = [];
  try { server = (await api<{ tickers?: ViewedRow[] }>("/api/ticker-index")).tickers || []; } catch (_e) { /* offline: local only */ }
  const viewed = getViewedMap();
  const bySym: Record<string, ViewedRow> = {};
  server.forEach((r) => { bySym[r.symbol] = { ...r }; });
  Object.keys(viewed).forEach((sym) => {
    const row = bySym[sym] || (bySym[sym] = { symbol: sym, name: "", as_of: null, analyzed_at: null, has_analysis: false });
    row.last_viewed = viewed[sym].ts;
    if (!row.name && viewed[sym].name) row.name = viewed[sym].name;
  });
  const rows = Object.values(bySym);
  const timeOf = (r: ViewedRow) => r.last_viewed || r.analyzed_at || r.as_of || "";
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
    let stancePill = "", verdictText = "";
    if (r.verdict) {
      const st = detectStance(r.verdict);
      if (st) {
        stancePill = `<span class="verdict-stance ${st.cls}">${esc(st.label)}</span>`;
        // Drop the matched stance word, then any leading separators it left behind
        // -- including the "/" / "|" that delimit "Avoid / high confidence: …".
        verdictText = r.verdict.replace(st.re, "").replace(/^[\s/|,:;.\u2014\u2013-]+/, "");
      } else {
        verdictText = r.verdict;
      }
    }
    const html =
      `<span class="viewed-sym">${esc(r.symbol)}</span>` +
      `<span class="viewed-name">${esc(r.name || "")}</span>` +
      `<span class="viewed-stance-cell">${stancePill}</span>` +
      `<span class="viewed-when muted">${esc(when)}</span>` +
      `<span class="viewed-badge">${ana}</span>` +
      (verdictText ? `<span class="viewed-verdict-text">${esc(verdictText)}</span>` : "");
    const row = el("button", "viewed-row", html);
    row.type = "button";
    row.addEventListener("click", () => openTicker(r.symbol));
    listWrap.appendChild(row);
  });
}

export {
  VIEWED_KEY,
  _viewedSort,
  getViewedMap,
  recordView,
  relTime,
  renderViewedTickers,
};
