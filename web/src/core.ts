// core is intentionally a dependency-free leaf: it must finish initializing
// (defining $, el, api, ...) before any other module body runs, otherwise the
// import cycle would hit it mid-init (TDZ on `$`). The error center registers
// its sink here at boot instead of core importing it.

// Optional numeric inputs from JSON (often null/undefined for missing data).
type Num = number | null | undefined;

// The error center registers recordError here; core never imports it directly.
type ErrorSink = (source: string, message: string, opts?: { detail?: string }) => unknown;
let _errorSink: ErrorSink | null = null;
function setErrorSink(fn: ErrorSink): void { _errorSink = fn; }

interface AppState {
  holdings: Record<string, any>;
  nav: any;
  lastSegment: any;
  segSort: { key: string; dir: number };
  currentDeepRun: any;
  privacyMode: boolean;
  pplxLoggedIn: boolean;
  pipeStep: number;
  segMode: string;
  repMode: string;
  repManual: boolean;
  promptSegment: any;
  savedRuns: Set<string>;
  deepRuns: any[];
  analysesRuns: any[];
  currentAnalysis: any;
  tickerSet: Set<string>;
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
// `await api<HoldingsPayload>("/api/holdings")`. Defaults to any, so the many
// still-untyped (@ts-nocheck) callers are unaffected.
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
    o.render(data);
    if (status) status.textContent = "";
  } catch (e) {
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
  apiLoad,
};
export type { AppState, Num, ErrorSink, AppError, ApiLoadOpts };
