// @ts-nocheck
// core is intentionally a dependency-free leaf: it must finish initializing
// (defining $, el, api, ...) before any other module body runs, otherwise the
// import cycle would hit it mid-init (TDZ on `$`). The error center registers
// its sink here at boot instead of core importing it.
let _errorSink = null;
function setErrorSink(fn) { _errorSink = fn; }

const state = {
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
  savedRuns: new Set(),
  deepRuns: [],
  analysesRuns: [],
  currentAnalysis: null,
  tickerSet: new Set(),
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

// Coarse "x ago" for cache/report freshness labels. Returns "" for junk input.
function relAge(iso) {
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
function fmtStamp(iso) {
  if (!iso) return "n/a";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso).slice(0, 16).replace("T", " ");
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

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
  let res;
  try {
    res = await fetch(path, opt);
  } catch (netErr) {
    // The server is down / unreachable -- always a genuine failure worth the
    // central error center, regardless of who (if anyone) catches it locally.
    const e = new Error(`can't reach the server (${method} ${path})`);
    e._recorded = _errorSink && _errorSink("network", e.message, { detail: String(netErr && netErr.message || netErr) });
    throw e;
  }
  const data = await res.json().catch(() => ({ error: "bad response" }));
  if (!res.ok) {
    const e = new Error(data.error || `HTTP ${res.status}`);
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
};
