// core is intentionally a dependency-free leaf: it must finish initializing
// (defining $, el, api, ...) before any other module body runs, otherwise the
// import cycle would hit it mid-init (TDZ on `$`). The error center registers
// its sink here at boot instead of core importing it.
//
// `import type` is erased at build time, so re-exporting the API DTOs from
// ./api-types — and pulling in a couple of view-owned record shapes for the
// shared store below — keeps core a runtime leaf while giving one source of
// truth. (Type-only edges can't form an import cycle; they vanish at build.)
import type { DeepRun, PriceLevel } from "./api-types";
import type { AnalysisRun } from "./analyses";
import type { SegmentRec } from "./segment";

// Optional numeric inputs from JSON (often null/undefined for missing data).
type Num = number | null | undefined;

// The error center registers recordError here; core never imports it directly.
type ErrorSink = (source: string, message: string, opts?: { detail?: string }) => unknown;
let _errorSink: ErrorSink | null = null;
function setErrorSink(fn: ErrorSink): void { _errorSink = fn; }

interface AppState {
  // Symbol (and provider-symbol alias) -> percent-of-NAV, seeded from the
  // holdings snapshot; read by the deep dive to show "owned" weight.
  holdings: Record<string, number | null>;
  nav: number | null;
  lastSegment: SegmentRec | null;
  segSort: { key: string; dir: number };
  // Stems pinning the active deep-research run / analysis being viewed.
  currentDeepRun: string | null;
  privacyMode: boolean;
  pplxLoggedIn: boolean;
  pipeStep: number;
  segMode: string;
  repMode: string;
  repManual: boolean;
  promptSegment: string | null;
  savedRuns: Set<string>;
  deepRuns: DeepRun[];
  analysesRuns: AnalysisRun[];
  currentAnalysis: string | null;
  tickerSet: Set<string>;
  stagedBasket: Array<{ symbol: string; delta_czk: number }>;
  // Set lazily by views; absent in the initial literal.
  pipePreselect?: string | null;
  _autoBuilding?: boolean;
}

const state: AppState = {
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
  savedRuns: new Set<string>(),
  deepRuns: [],
  analysesRuns: [],
  currentAnalysis: null,
  tickerSet: new Set<string>(),
  stagedBasket: [],
};

// ---- tiny helpers ---------------------------------------------------------
// Defaults to HTMLElement since nearly every call site reads element props;
// pass an explicit type param for SVG/other nodes.
const $ = <T extends Element = HTMLElement>(sel: string, root: ParentNode = document): T | null =>
  root.querySelector<T>(sel);

// Overloaded so a literal tag ("button", "a", ...) yields the precise element
// type (HTMLButtonElement, HTMLAnchorElement, ...) and call sites can touch
// tag-specific props without a cast; a dynamic string tag falls back to
// HTMLElement.
function el<K extends keyof HTMLElementTagNameMap>(tag: K, cls?: string, html?: string): HTMLElementTagNameMap[K];
function el(tag: string, cls?: string, html?: string): HTMLElement;
function el(tag: string, cls?: string, html?: string): HTMLElement {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html != null) n.innerHTML = html;
  return n;
}

const ESC_MAP: Record<string, string> = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" };
const esc = (s: unknown): string => String(s ?? "").replace(/[&<>"]/g, (c) => ESC_MAP[c]);

// Coarse "x ago" for cache/report freshness labels. Returns "" for junk input.
function relAge(iso: string | null | undefined): string {
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
function fmtStamp(iso: string | null | undefined): string {
  if (!iso) return "n/a";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso).slice(0, 16).replace("T", " ");
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

// Color-coded "x ago" chip for sync/snapshot stamps so staleness is obvious at a
// glance: green <=2d, amber <=14d, red older. Extends relAge to weeks/months and
// returns an HTML string (title = exact local time). "" for junk/missing input.
function freshnessNote(iso: string | null | undefined): string {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const secs = Math.max(0, (Date.now() - then) / 1000);
  const mins = secs / 60, hrs = mins / 60, days = hrs / 24;
  let text: string;
  if (secs < 90) text = "just now";
  else if (mins < 90) text = `${Math.round(mins)}m ago`;
  else if (hrs < 36) text = `${Math.round(hrs)}h ago`;
  else if (days < 14) text = `${Math.round(days)}d ago`;
  else if (days < 60) text = `${Math.round(days / 7)}w ago`;
  else text = `${Math.round(days / 30)}mo ago`;
  const bucket = days <= 2 ? "fresh" : days <= 14 ? "aging" : "stale";
  return `<span class="fresh-note ${bucket}" title="${esc(new Date(then).toLocaleString())}">${esc(text)}</span>`;
}

// Human label per canonical instrument_type emitted by the backend
// (tools/instruments.py). Keep keys in lockstep with that module.
const INSTRUMENT_LABELS: Record<string, string> = {
  stock: "Stock",
  etf: "ETF",
  futures: "Futures",
  index: "Index",
  fund: "Fund",
  crypto: "Crypto",
  fx: "FX",
  other: "Other",
};

// Small pill that calls out what an instrument actually is (ETF vs single stock
// vs futures...). Returns "" for unknown/missing so callers can omit it cleanly.
function instrumentBadge(type: string | null | undefined): string {
  const kind = (type || "").toLowerCase();
  const label = INSTRUMENT_LABELS[kind];
  if (!label) return "";
  return `<span class="inst-pill inst-${esc(kind)}" title="Instrument type: ${esc(label)}">${esc(label)}</span>`;
}

const fmtPrice = (v: Num) => (v == null ? "n/a" : "$" + Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
const fmtX = (v: Num) => (v == null ? "n/a" : Number(v).toFixed(1) + "x");
const fmtPct = (v: Num) => (v == null ? "n/a" : (v >= 0 ? "+" : "") + Number(v).toFixed(1) + "%");
const fmtB = (v: Num) => {
  if (v == null) return "n/a";
  return Math.abs(v) >= 1000 ? "$" + (v / 1000).toFixed(2) + "T" : "$" + Number(v).toFixed(1) + "B";
};
const fmtShares = (v: Num) => (v == null ? "n/a" : Number(v).toFixed(2) + "B");
const pctClass = (v: Num) => (v == null ? "muted" : v > 0 ? "good" : v < 0 ? "bad" : "muted");
const fmtWeight = (v: Num) => (v == null ? "n/a" : Number(v).toFixed(2) + "%");
const fmtSignedWeight = (v: Num) => (v == null ? "n/a" : (v >= 0 ? "+" : "") + Number(v).toFixed(2) + "%");
const fmtCZK = (v: Num) => {
  if (v == null) return "n/a";
  return Math.abs(v) >= 1000 ? Math.round(v).toLocaleString() : Number(v).toFixed(0);
};
const decisionClass = (v: string) => {
  if (["add_candidate", "accumulate"].includes(v)) return "good";
  if (["trim", "avoid"].includes(v)) return "bad";
  if (["watch"].includes(v)) return "warn";
  return "muted";
};
const scoreClass = (v: Num) => (v == null ? "muted" : v >= 70 ? "good" : v >= 45 ? "warn" : "bad");
const sensitive = (html: string, label = "sensitive value") =>
  `<span data-sensitive title="${esc(label)}">${html}</span>`;

function applyPrivacyMode(on: boolean): void {
  state.privacyMode = !!on;
  document.body.classList.toggle("privacy-mode", state.privacyMode);
  localStorage.setItem("financeRebalancingPrivacyMode", state.privacyMode ? "1" : "0");
  const btn = $("#privacy-toggle");
  if (btn) {
    btn.setAttribute("aria-pressed", state.privacyMode ? "true" : "false");
    btn.textContent = state.privacyMode ? "Privacy: on" : "Privacy: off";
  }
}

// Errors carry ad-hoc fields the rest of the app reads (status for HTTP code,
// _recorded so the global handler doesn't double-count what api() already logged).
type AppError = Error & { _recorded?: unknown; status?: number };

// Generic so typed call sites can pin a response shape from ./api-types, e.g.
// `await api<HoldingsPayload>("/api/holdings")`. Defaults to any for the call
// sites that haven't pinned a DTO yet; prefer passing an explicit type param.
async function api<T = any>(path: string, method: string = "GET", body: unknown = null): Promise<T> {
  const opt: RequestInit = { method, headers: {} };
  if (body) {
    (opt.headers as Record<string, string>)["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  let res: Response;
  try {
    res = await fetch(path, opt);
  } catch (netErr) {
    // The server is down / unreachable -- always a genuine failure worth the
    // central error center, regardless of who (if anyone) catches it locally.
    const e = new Error(`can't reach the server (${method} ${path})`) as AppError;
    e._recorded = _errorSink && _errorSink("network", e.message, { detail: String((netErr as any) && (netErr as any).message || netErr) });
    throw e;
  }
  const data = await res.json().catch(() => ({ error: "bad response" }));
  if (!res.ok) {
    const e = new Error(data.error || `HTTP ${res.status}`) as AppError;
    e.status = res.status;
    // 5xx is the server blowing up -- record centrally. 4xx is usually expected
    // control flow (missing cache, validation) that callers handle inline, so we
    // leave those to local status; if a 4xx goes uncaught it still surfaces via
    // the global unhandledrejection handler below.
    if (res.status >= 500) {
      e._recorded = _errorSink && _errorSink("api", `${method} ${path}: ${e.message}`, { detail: `HTTP ${res.status}` });
    }
    throw e;
  }
  return data;
}

// ---- shared load / status / empty-state choreography ----------------------
// Every data view used to hand-roll the same dance: set a "Loading…" status,
// clear the result container, await an api() call, render, blank the status, and
// on failure write "Could not load X: <msg>" with the .err class. These helpers
// centralize that so a view is one declarative call, and empty states stop being
// an ad-hoc blank div in one view and a friendly message in the next.

const spinner = (): string => '<span class="spinner"></span>';

// Loading text into a status line (optionally with the spinner). Clears any
// prior error styling so a retry doesn't stay red.
function setLoading(statusEl: HTMLElement | null, msg: string, spin = false): void {
  if (!statusEl) return;
  statusEl.classList.remove("err");
  statusEl.innerHTML = (spin ? spinner() + " " : "") + esc(msg);
}

// The canonical failure line: "<label>: <message>" in the error colour.
function loadError(statusEl: HTMLElement | null, label: string, err: unknown): void {
  if (!statusEl) return;
  statusEl.textContent = `${label}: ${(err as Error)?.message ?? String(err)}`;
  statusEl.classList.add("err");
}

// A consistent, centered placeholder for "nothing here yet" containers.
function emptyState(out: HTMLElement | null, html: string): void {
  if (out) out.innerHTML = `<div class="empty-state">${html}</div>`;
}

// A labelled KPI tile (uppercase key over a big value), shared by the rebalance,
// journal, and risk headlines. `family` selects the CSS class set ("reb-stat" vs
// "risk-stat") since those differ in size/affordance; `cls` colours the value
// (good/warn/bad/muted). Pass html:true when the value is pre-built markup
// (e.g. a sensitive() wrapper) that must not be escaped.
interface StatTileOpts {
  cls?: string;
  title?: string;
  html?: boolean;
  family?: string;
}

function statTile(label: string, value: string, opts: StatTileOpts = {}): HTMLElement {
  const family = opts.family || "reb-stat";
  const c = el("div", family);
  if (opts.title) c.title = opts.title;
  const valHtml = opts.html ? value : esc(value);
  c.innerHTML =
    `<span class="${family}-k">${esc(label)}</span>` +
    `<span class="${family}-v ${esc(opts.cls || "")}">${valHtml}</span>`;
  return c;
}

// A plain panel: a `.card` with a `.section` heading, content appended by the
// caller. `extra` adds further classes to the card (e.g. "biz-card"). `title` is
// inserted as-is (callers pass esc()'d text when it's dynamic), matching the
// hand-rolled sites this replaces. Cards with a toolbar/button beside the title
// (analysis-head) or a collapsible body keep their own builders -- they're a
// different shape, not this one.
function sectionCard(title: string, extra = ""): HTMLElement {
  const card = el("div", extra ? "card " + extra : "card");
  card.appendChild(el("h2", "section", title));
  return card;
}

// A non-sortable HTML table: a <thead> from a fixed header row plus a <tbody>
// whose rows come from `cells(item)` (returns the row's inner HTML). `onRow` is
// the escape hatch for rows that need imperative DOM (e.g. a delete button with
// a listener) appended after the string cells. The sortable segment peer table
// is deliberately not built on this -- its column/sort machinery is richer.
interface SimpleTableOpts<T> {
  className?: string;
  head: string;
  rows: T[];
  cells: (item: T, index: number) => string;
  onRow?: (tr: HTMLTableRowElement, item: T, index: number) => void;
}

function simpleTable<T>(o: SimpleTableOpts<T>): HTMLTableElement {
  const tbl = el("table", o.className);
  tbl.innerHTML = `<thead>${o.head}</thead>`;
  const body = el("tbody");
  o.rows.forEach((item, i) => {
    const tr = el("tr");
    tr.innerHTML = o.cells(item, i);
    if (o.onRow) o.onRow(tr, item, i);
    body.appendChild(tr);
  });
  tbl.appendChild(body);
  return tbl;
}

// ---- per-view request tokens ----------------------------------------------
// Views re-fetch on every (re)entry and on param changes (ticker switches,
// account changes, tab flips). A slow response from a request the user has
// already moved past must not clobber the current render. Each view bumps a
// monotonic token when it (re)loads and checks it after the await; a stale
// token means "a newer load for this view started — drop this response."
const _viewTokens: Record<string, number> = {};
function nextToken(view: string): number {
  return (_viewTokens[view] = (_viewTokens[view] || 0) + 1);
}
function isStaleToken(view: string, token: number): boolean {
  return _viewTokens[view] !== token;
}

interface ApiLoadOpts<T> {
  path: string;
  render: (data: T) => void;
  status?: HTMLElement | null;
  // Containers to blank before the request and again on failure.
  clear?: (HTMLElement | null | undefined)[];
  loading?: string;
  errorLabel?: string;
  method?: string;
  body?: unknown;
  spin?: boolean;
  // Returns true when this load has been superseded by a newer one for the same
  // view; the response (success or error) is then dropped so it can't paint over
  // fresher data. Pair with nextToken/isStaleToken.
  stale?: () => boolean;
}

// Convenience wrapper for the uniform "status + clear + fetch + render" views
// (risk, rebalance, journal). Views whose status line becomes a persistent
// summary (holdings, segment) should compose setLoading/loadError directly.
async function apiLoad<T = any>(o: ApiLoadOpts<T>): Promise<void> {
  const status = o.status ?? null;
  setLoading(status, o.loading ?? "Loading…", o.spin);
  (o.clear || []).forEach((c) => { if (c) c.innerHTML = ""; });
  try {
    const data = await api<T>(o.path, o.method, o.body);
    if (o.stale && o.stale()) return;  // a newer load won; don't paint stale data
    o.render(data);
    if (status) status.textContent = "";
  } catch (e) {
    if (o.stale && o.stale()) return;  // superseded request failing late; ignore
    (o.clear || []).forEach((c) => { if (c) c.innerHTML = ""; });
    if (status) loadError(status, o.errorLabel ?? "Could not load", e);
    else throw e;
  }
}

export {
  state,
  $,
  el,
  esc,
  relAge,
  fmtStamp,
  freshnessNote,
  instrumentBadge,
  fmtPrice,
  fmtX,
  fmtPct,
  fmtB,
  fmtShares,
  pctClass,
  fmtWeight,
  fmtSignedWeight,
  fmtCZK,
  decisionClass,
  scoreClass,
  sensitive,
  applyPrivacyMode,
  api,
  setErrorSink,
  spinner,
  setLoading,
  loadError,
  emptyState,
  statTile,
  sectionCard,
  simpleTable,
  apiLoad,
  nextToken,
  isStaleToken,
};
export type { AppState, Num, ErrorSink, AppError, ApiLoadOpts, StatTileOpts, SimpleTableOpts, PriceLevel };
