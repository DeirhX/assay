import type { Job } from "./api-types";
import { $$, api, el, esc } from "./core";
import { pollDeepJob } from "./jobs";

let _wired = false;

const LLM_IDS = ["claude", "cursor"];

// ---- /api/setup/status shapes ---------------------------------------------
interface BackendCheck {
  ok?: boolean;
  status?: string;
  message?: string;
}

interface Backend {
  id?: string;
  label?: string;
  installed?: boolean;
  authenticated?: boolean;
  check?: BackendCheck | null;
}

interface ProviderConfig {
  id: string;
  enabled?: boolean;
  model?: string;
  extra_args?: string[];
}

interface LlmConfig {
  providers?: ProviderConfig[];
  allow_web?: boolean;
  timeout_sec?: number;
}

interface LlmStatus {
  backends?: Backend[];
  config: LlmConfig;
}

interface SetupData {
  ready?: boolean;
  holdings?: { positions?: number };
  target_model?: { exists?: boolean };
}

interface AutomationTask {
  name: string;
  label: string;
  enabled: boolean;
  last_run?: string | null;
  last_result?: string | null;
  next_eligible?: string | null;
}
interface AutomationStatus { enabled?: boolean; tasks?: AutomationTask[] }

interface SetupState {
  llm: LlmStatus;
  perplexity?: { logged_in?: boolean };
  ibkr?: Record<string, any>;
  data?: SetupData;
  automation?: AutomationStatus;
  environment?: Record<string, any>;
}

// One model suggestion in a provider's combobox.
interface ModelOption {
  value: string;
  label?: string;
}

// One Setup wizard step (LLM / Perplexity / IBKR / environment).
interface SetupStep {
  id: string;
  title: string;
  required: boolean;
  done: boolean;
  partial: boolean;
  state: string;
  render: () => HTMLElement;
}

// One entry in the operational error log (/api/error-log).
interface ErrLogEntry {
  level?: string;
  category?: string;
  message?: string;
  ts?: string | number;
  context?: Record<string, unknown>;
}

// Model suggestions per provider, fetched once from /api/analysis-models and
// reused across re-renders. Empty until loaded; the inputs stay free-text so an
// unlisted model still works even if the list never arrives.
let _models: Record<string, ModelOption[]> = {};

function badge(ok: unknown, text: string) {
  return `<span class="setup-badge ${ok ? "ok" : "bad"}">${esc(text)}</span>`;
}

function toggle(id: string, label: string, checked: unknown) {
  return (
    `<label class="setup-toggle">` +
      `<input type="checkbox" id="${esc(id)}" ${checked ? "checked" : ""}>` +
      `<span class="setup-toggle-track"></span>` +
      `<span class="setup-toggle-label">${esc(label)}</span>` +
    `</label>`
  );
}

async function ensureModels() {
  if (Object.keys(_models).length) return;
  try {
    const r = await api("/api/analysis-models");
    _models = r.models || {};
  } catch {
    /* leave inputs as free-text */
  }
}

// Build the dropdown body. An empty filter (manual expand) shows everything;
// a non-empty filter (the user typing) narrows to substring matches.
function comboMenuHtml(id: string, filter: string) {
  const f = (filter || "").trim().toLowerCase();
  const items = (_models[id] || []).filter(
    (m) => !f || `${m.value} ${m.label || ""}`.toLowerCase().includes(f),
  );
  if (!items.length) {
    return `<div class="setup-combo-empty">${f ? "no matches" : "no suggestions"}</div>`;
  }
  return items
    .map(
      (m) =>
        `<div class="setup-combo-item" role="option" data-val="${esc(m.value)}">` +
          `<span class="setup-combo-val">${esc(m.value)}</span>` +
          (m.label && m.label !== m.value ? `<span class="setup-combo-label">${esc(m.label)}</span>` : "") +
        `</div>`,
    )
    .join("");
}

// Lightweight combobox: native <datalist> filters by the current value the
// moment the field is non-empty, which kills "open to see all". So we drive our
// own menu — focus/caret shows the full list, typing filters it. Options are
// read from _models lazily, so a slow Cursor list still populates once it lands.
function wireModelCombo(id: string) {
  const input = document.getElementById(`setup-${id}-model`) as HTMLInputElement | null;
  const menu = document.getElementById(`setup-combo-${id}`);
  if (!input || !menu || input.dataset.comboWired) return;
  input.dataset.comboWired = "1";

  const open = (showAll: boolean) => {
    menu.innerHTML = comboMenuHtml(id, showAll ? "" : input.value);
    menu.hidden = false;
    input.setAttribute("aria-expanded", "true");
  };
  const close = () => {
    menu.hidden = true;
    input.setAttribute("aria-expanded", "false");
  };
  const setActive = (items: HTMLElement[], idx: number) => {
    items.forEach((it) => it.classList.remove("active"));
    if (idx >= 0 && items[idx]) {
      items[idx].classList.add("active");
      items[idx].scrollIntoView({ block: "nearest" });
    }
  };

  input.addEventListener("focus", () => open(true));
  input.addEventListener("click", () => open(true));
  input.addEventListener("input", () => open(false));
  input.addEventListener("blur", () => setTimeout(close, 120));
  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape") return close();
    const items = [...menu.querySelectorAll<HTMLElement>(".setup-combo-item")];
    if (menu.hidden || !items.length) return;
    const idx = items.findIndex((it) => it.classList.contains("active"));
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive(items, Math.min(items.length - 1, idx + 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive(items, Math.max(0, idx - 1));
    } else if (e.key === "Enter" && idx >= 0) {
      e.preventDefault();
      input.value = items[idx].dataset.val ?? "";
      close();
    }
  });

  // mousedown (not click) so selecting fires before the input's blur closes it.
  menu.addEventListener("mousedown", (e) => {
    const it = (e.target as HTMLElement).closest<HTMLElement>(".setup-combo-item");
    if (!it) return;
    e.preventDefault();
    input.value = it.dataset.val ?? "";
    close();
  });
  const caret = input.parentElement?.querySelector(".setup-combo-caret");
  if (caret) {
    caret.addEventListener("mousedown", (e) => {
      e.preventDefault();
      if (menu.hidden) {
        input.focus();
        open(true);
      } else {
        close();
      }
    });
  }
}

// Wire any model comboboxes present after a render. Idempotent per element, and
// safe to call again once models finish loading since the menu reads _models live.
function fillModelLists() {
  LLM_IDS.forEach(wireModelCombo);
}

function backendById(st: SetupState, id: string): Backend {
  return (st?.llm?.backends || []).find((b) => b.id === id) || {};
}

function providerConfig(st: SetupState, id: string): ProviderConfig {
  return (st?.llm?.config?.providers || []).find((p) => p.id === id) || { id, enabled: true, model: "", extra_args: [] };
}

function commandBlock(lines: string[]) {
  return `<pre class="setup-command">${esc(lines.join("\n"))}</pre>`;
}

// Collapse the backend record (installed / authenticated / smoke-check) into a
// single phase the UI can drive status text, colours, and fix steps from.
function backendPhase(b: Backend) {
  const check = b.check;
  if (check) {
    if (check.ok) return "ok";
    if (check.status === "auth") return "logged_out";
    if (check.status === "quota") return "quota";
    return "error"; // includes "error" and "timeout"
  }
  if (!b.installed) return "missing";
  if (b.authenticated === false) return "logged_out";
  if (b.authenticated === true) return "ready";
  return "installed";
}

function backendStateText(b: Backend) {
  const check = b.check;
  switch (backendPhase(b)) {
    case "ok": return `Ready — smoke check passed${check?.message ? " (" + check.message + ")" : ""}`;
    case "logged_out": return check ? `Not authenticated: ${check.message || "login required"}` : "Installed, but not logged in";
    case "quota": return `Authenticated, but rate-limited / out of quota${check?.message ? ": " + check.message : ""}`;
    case "error": return `${check?.status || "error"}: ${check?.message || "unknown failure"}`;
    case "ready": return "Logged in — run a smoke check to confirm";
    case "missing": return "Not installed";
    default: return "Installed, not smoke-tested";
  }
}

function backendPhaseClass(phase: string) {
  if (phase === "ok" || phase === "ready") return "ok";
  if (phase === "installed") return "muted";
  return "bad"; // missing, logged_out, quota, error
}

// What to actually do, tailored to the phase (and which CLI).
function backendFixSteps(id: string, phase: string) {
  const loginCmd = id === "claude" ? "claude auth login" : "cursor-agent login";
  const installSteps = id === "claude"
    ? ["Install Claude Code, e.g. `npm i -g @anthropic-ai/claude-code` (or the official installer).",
       "Ensure `claude` is on your PATH, then reload this page."]
    : ["Install/update the Cursor CLI so `cursor-agent` is on your PATH.",
       "Reload this page once it resolves."];
  switch (phase) {
    case "missing": return installSteps;
    case "logged_out": return [
      `Installed but no credentials. Run \`${loginCmd}\` in a terminal and finish the browser sign-in.`,
      "Then re-run the smoke check here.",
    ];
    case "quota": return [
      "Credentials are valid — the plan is rate-limited or out of quota.",
      "Wait for the limit to reset, pick a cheaper model, or upgrade the plan. The other backend is used meanwhile.",
    ];
    case "error": return [
      "Unexpected failure — see the message above.",
      `If it mentions auth/credentials, run \`${loginCmd}\`; otherwise check the model name / extra args below.`,
    ];
    case "ready": return ["Looks authenticated. Run the smoke check to confirm it can actually answer."];
    case "ok": return [];
    default: return ["Run the smoke check to verify credentials and the selected model."];
  }
}

// Second header badge: credential state, once we know it.
function authBadge(b: Backend) {
  const phase = backendPhase(b);
  if (phase === "ok" || phase === "ready") return badge(true, "logged in");
  if (phase === "logged_out") return badge(false, "not logged in");
  return ""; // installed/unknown/quota/error/missing -> no auth claim
}

// Ordered setup steps. `done` drives which step auto-expands: the first
// not-done step opens, everything else stays collapsed so the user only ever
// deals with one thing at a time.
function setupSteps(st: SetupState): SetupStep[] {
  const backends = st?.llm?.backends || [];
  const anyCliInstalled = backends.some((b) => b.installed);
  const anyCliOk = backends.some((b) => b.check?.ok);
  const anyCliReady = backends.some((b) => b.check?.ok || b.authenticated === true);
  const anyLoggedOut = backends.some((b) => b.installed && (b.authenticated === false || b.check?.status === "auth"));
  const pplxOk = !!st?.perplexity?.logged_in;
  const secOk = !!(st.environment || {}).sec_user_agent;
  const ibkr = st.ibkr || {};
  const data = st.data || {};
  const dataReady = !!data.ready;
  const portfolioStep = {
    id: "ibkr",
    title: "Portfolio data (IBKR Flex)",
    required: !dataReady,
    done: dataReady,
    partial: !dataReady && !!ibkr.configured,
    state: dataReady
      ? `Ready — ${data.holdings?.positions || 0} positions`
      : (data.holdings?.positions || 0) > 0
        ? "Holdings synced — set a target model"
        : ibkr.configured
          ? "Credentials saved — sync to pull holdings"
          : (ibkr.token_set || ibkr.query_id) ? "Incomplete credentials" : "No portfolio data",
    render: () => renderIbkr(st),
  };
  const steps = [
    {
      id: "llm",
      title: "Analysis CLI",
      required: true,
      done: anyCliReady,
      partial: anyCliInstalled && !anyCliReady,
      state: anyCliOk ? "Ready"
        : anyCliReady ? "Logged in — run a smoke check"
        : anyLoggedOut ? "Installed — not logged in"
        : anyCliInstalled ? "Installed — checking credentials"
        : "Not installed yet",
      render: () => renderLlmCli(st),
    },
    {
      id: "pplx",
      title: "Perplexity Deep Research login",
      required: true,
      done: pplxOk,
      partial: false,
      state: pplxOk ? "Logged in" : "Not logged in",
      render: () => renderPerplexity(st),
    },
    {
      id: "automation",
      title: "Background auto-refresh",
      required: false,
      done: !!st.automation?.enabled,
      partial: false,
      state: st.automation?.enabled ? "On — keeping data fresh" : "Optional — refresh data by hand",
      render: () => renderAutomation(st),
    },
    {
      id: "env",
      title: "Environment",
      required: false,
      done: secOk,
      partial: false,
      state: secOk ? "SEC user-agent set" : "Optional — SEC user-agent recommended",
      render: () => renderEnvironment(st),
    },
  ];
  return dataReady ? [...steps.slice(0, 2), portfolioStep, steps[2]] : [portfolioStep, ...steps];
}

function stepStateBadge(step: SetupStep) {
  if (step.done) return badge(true, "OK");
  if (step.partial) return `<span class="setup-badge warn">IN PROGRESS</span>`;
  return badge(false, step.required ? "TODO" : "OPTIONAL");
}

function renderSetup(st: SetupState) {
  const out = $$("#setup-result");
  out.innerHTML = "";

  const steps = setupSteps(st);
  const next = steps.find((s) => s.required && !s.done) || steps.find((s) => !s.done);
  const doneCount = steps.filter((s) => s.done).length;

  const progress = el("div", "setup-progress" + (next ? "" : " ok"));
  progress.innerHTML = next
    ? `<div class="setup-progress-count">${doneCount}/${steps.length} done</div>` +
      `<div class="setup-next">Next: <strong>${esc(next.title)}</strong> — ${esc(next.state)}</div>`
    : `<div class="setup-progress-count ok">All set</div>` +
      `<div class="setup-next">Everything required is configured. Expand any step to revisit it.</div>`;
  out.appendChild(progress);

  steps.forEach((step) => {
    const d = el("details", "setup-step" + (step.done ? " done" : step.partial ? " partial" : ""));
    if (next && step.id === next.id) d.open = true;
    const summary = el("summary", "setup-step-head");
    summary.innerHTML =
      `<span class="setup-step-title">${esc(step.title)}</span>` +
      `<span class="setup-step-state"><span class="setup-step-note">${esc(step.state)}</span>${stepStateBadge(step)}</span>`;
    d.appendChild(summary);
    const body = el("div", "setup-step-body");
    body.appendChild(step.render());
    d.appendChild(body);
    out.appendChild(d);
  });

  const errlog = el("details", "setup-step errlog");
  errlog.id = "setup-errlog";
  out.appendChild(errlog);
  renderErrorLog();

  fillModelLists();
}

function fmtTs(ts: string | number | undefined) {
  try {
    return new Date(ts ?? "").toLocaleString();
  } catch {
    return ts || "";
  }
}

function errLogEntryHtml(e: ErrLogEntry) {
  const lvl = e.level === "warning" ? "warn" : "err";
  const ctx = e.context && Object.keys(e.context).length
    ? `<div class="errlog-ctx">${Object.entries(e.context)
        .map(([k, v]) => `${esc(k)}=${esc(String(v))}`)
        .join(" \u00b7 ")}</div>`
    : "";
  return (
    `<li class="errlog-entry ${lvl}">` +
      `<div class="errlog-entry-head">` +
        `<span class="errlog-badge ${lvl}">${esc(e.level || "error")}</span>` +
        `<span class="errlog-cat">${esc(e.category || "")}</span>` +
        `<span class="errlog-ts">${esc(fmtTs(e.ts))}</span>` +
      `</div>` +
      `<div class="errlog-msg">${esc(e.message || "")}</div>` +
      ctx +
    `</li>`
  );
}

// Operational error log: real incidents only (backend fallbacks, unhandled
// server errors). Lives at the foot of Setup, collapsed unless it has content.
async function renderErrorLog(open = false) {
  const card = document.getElementById("setup-errlog") as HTMLDetailsElement | null;
  if (!card) return;
  let entries: ErrLogEntry[];
  try {
    const r = await api<{ entries?: ErrLogEntry[] }>("/api/error-log?limit=100");
    entries = r.entries || [];
  } catch {
    card.innerHTML =
      `<summary class="setup-step-head"><span class="setup-step-title">Error log</span></summary>` +
      `<div class="setup-step-body"><p class="hint">Could not load the error log.</p></div>`;
    return;
  }
  const count = entries.length;
  const hasError = entries.some((e) => e.level !== "warning");
  card.open = open && count > 0;
  const stateBadge = count
    ? `<span class="setup-badge ${hasError ? "bad" : "warn"}">${count}</span>`
    : badge(true, "CLEAN");
  card.innerHTML =
    `<summary class="setup-step-head">` +
      `<span class="setup-step-title">Error log</span>` +
      `<span class="setup-step-state"><span class="setup-step-note">${count ? `${count} recent` : "nothing logged"}</span>${stateBadge}</span>` +
    `</summary>` +
    `<div class="setup-step-body">` +
      `<p class="hint">Real failures worth a record — analysis backend (Cursor/Claude) fallbacks, unhandled server errors. ` +
        `Expected misses like an unknown ticker or "not logged into Perplexity yet" are <em>not</em> logged here.</p>` +
      (count
        ? `<ul class="errlog-list">${entries.map(errLogEntryHtml).join("")}</ul>`
        : `<p class="setup-small ok">Quiet is good — nothing has failed since the last clear.</p>`) +
      `<div class="setup-actions">` +
        `<button class="ghost" id="setup-errlog-refresh" type="button">Refresh</button>` +
        (count ? `<button class="ghost" id="setup-errlog-clear" type="button">Clear log</button>` : "") +
        `<span class="status" id="setup-errlog-status"></span>` +
      `</div>` +
    `</div>`;
}

async function clearErrorLog() {
  const status = $$("#setup-errlog-status");
  if (status) {
    status.classList.remove("err");
    status.textContent = "clearing…";
  }
  try {
    await api("/api/error-log", "POST", { clear: true });
    await renderErrorLog(true);
  } catch (e) {
    if (status) {
      status.classList.add("err");
      status.textContent = "clear failed: " + (e as Error).message;
    }
  }
}

function renderIbkr(st: SetupState) {
  const k = st.ibkr || {};
  const data = st.data || {};
  const positions = data.holdings?.positions || 0;
  const hasSnapshot = positions > 0;
  const canSync = !!(k.configured || k.from_env);
  const needsModel = hasSnapshot && data.target_model && !data.target_model.exists;
  const wrap = el("div", "setup-body-inner");
  const tokenPlaceholder = k.token_set
    ? "\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022 saved \u2014 leave blank to keep"
    : "paste Flex Web Service token";
  wrap.innerHTML =
    `<p class="hint">Two steps: <strong>1.</strong> save your read-only Flex credentials, then <strong>2.</strong> sync to pull the holdings snapshot. ` +
      `Saved to <code>${esc(k.secrets_path || "tools/secrets.env")}</code> (gitignored); the token is stored locally and never shown back here.</p>` +
    `<div class="setup-row"><strong>1. Flex credentials</strong>${badge(k.configured, k.configured ? "saved" : "not set")}</div>` +
    `<ul class="setup-list">` +
      `<li>IBKR Client Portal &rarr; Settings &rarr; Account Settings &rarr; <strong>Flex Web Service</strong>: enable it and copy the <strong>token</strong>.</li>` +
      `<li>Create a <strong>Flex Query</strong> (positions, cash, open tax lots) and copy its <strong>Query ID</strong>.</li>` +
    `</ul>` +
    (k.from_env ? `<p class="setup-small">A value is currently set via environment variable; that takes precedence over what you save here.</p>` : "") +
    `<label class="setup-field">Flex Query ID` +
      `<input id="setup-ibkr-query" placeholder="e.g. 1234567" value="${esc(k.query_id || "")}" autocomplete="off" inputmode="numeric">` +
    `</label>` +
    `<label class="setup-field">History Flex Query ID <span class="setup-optional">(for the History tab)</span>` +
      `<input id="setup-ibkr-history-query" placeholder="optional — separate Activity query" value="${esc(k.history_query_id || "")}" autocomplete="off" inputmode="numeric">` +
    `</label>` +
    `<p class="setup-small">The full trade &amp; NAV history needs its own <strong>Activity</strong> Flex query that includes the <strong>Trades</strong> and <strong>Net Asset Value (NAV) in Base</strong> sections. The positions query above lacks those, so leave this set to a dedicated query if you want the History tab.${k.history_from_env ? " A value is currently set via environment variable; that wins over what you save here." : ""}</p>` +
    `<label class="setup-field">Flex token` +
      `<input id="setup-ibkr-token" type="password" placeholder="${esc(tokenPlaceholder)}" autocomplete="off">` +
    `</label>` +
    `<div class="setup-actions">` +
      `<button class="${k.configured ? "ghost" : "primary"}" id="setup-save-ibkr" type="button">Save credentials</button>` +
      `<span class="status" id="setup-ibkr-status"></span>` +
    `</div>` +
    `<div class="setup-row" style="margin-top:10px"><strong>History query</strong>${badge(k.history_configured, k.history_configured ? "ready" : "not set")}</div>` +
    `<div class="setup-row" style="margin-top:14px"><strong>2. Holdings snapshot</strong>${badge(hasSnapshot, hasSnapshot ? `${positions} positions` : "not pulled yet")}</div>` +
    `<p class="hint">Pulls the latest positions, cash, and tax lots directly from IBKR (read-only — the Flex query cannot trade). Same action as <strong>Resync from IBKR</strong> on the Holdings tab.</p>` +
    `<div class="setup-actions">` +
      `<button class="primary" id="setup-sync-ibkr" type="button"${canSync ? "" : " disabled"} title="${canSync ? "Re-pull holdings from IBKR (read-only)" : "Save your Flex credentials first"}">${hasSnapshot ? "Re-sync holdings" : "Sync holdings now"}</button>` +
      `<span class="status" id="setup-ibkr-sync-status">${canSync ? "" : "Save credentials first to enable syncing."}</span>` +
    `</div>` +
    (needsModel ? `<p class="setup-small">Snapshot present, but no target model yet — set one to finish portfolio setup.</p>` : "");
  return wrap;
}

function renderEnvironment(st: SetupState) {
  const env = st.environment || {};
  const wrap = el("div", "setup-body-inner");
  wrap.innerHTML =
    `<div class="setup-row"><strong>SEC user-agent</strong>${badge(env.sec_user_agent, env.sec_user_agent ? "set" : "missing")}</div>` +
    `<p class="hint">Set <code>SEC_USER_AGENT</code> before launching the server so SEC EDGAR requests identify you politely.</p>` +
    commandBlock([`$env:SEC_USER_AGENT = "assay research (you@example.com)"`, "py -3 tools/serve.py"]) +
    `<details class="setup-advanced">` +
      `<summary>Optional: FMP API key</summary>` +
      `<div class="setup-row"><strong>FMP API key</strong>${badge(env.fmp_api_key, env.fmp_api_key ? "set" : "optional")}</div>` +
      `<p class="hint">FMP is optional. If set, it gives a third opinion for some market-cap and profile fields.</p>` +
    `</details>`;
  return wrap;
}

function renderAutomation(st: SetupState) {
  const a = st.automation || {};
  const on = !!a.enabled;
  const wrap = el("div", "setup-body-inner");
  const rows = (a.tasks || []).map((t) => {
    const when = t.last_run ? esc(t.last_run.slice(0, 16).replace("T", " ")) : "never";
    const result = t.last_result ? ` — ${esc(t.last_result)}` : "";
    const off = t.enabled ? "" : ` <span class="setup-optional">(off)</span>`;
    return `<li><strong>${esc(t.label)}</strong>${off}: last ran ${when}${result}</li>`;
  }).join("");
  wrap.innerHTML =
    `<p class="hint">While the server is running, keep the app's own data current — holdings snapshot, ` +
      `portfolio history, segment caches, and prices for gated names — by running the same read-only jobs ` +
      `the buttons already trigger, on a schedule. <strong>Strictly read-only:</strong> it never stages, sizes, ` +
      `or places a trade, and never spends LLM/Perplexity quota.</p>` +
    toggle("setup-auto-refresh", "Enable background auto-refresh (ASSAY_AUTO_REFRESH)", on) +
    ` <span class="status" id="setup-auto-status"></span>` +
    (rows ? `<div class="setup-row" style="margin-top:12px"><strong>Recent activity</strong></div><ul class="setup-list">${rows}</ul>`
          : `<p class="setup-small">No background runs recorded yet.</p>`);
  return wrap;
}

function renderLlmCli(st: SetupState) {
  const claude = backendById(st, "claude");
  const cursor = backendById(st, "cursor");
  const wrap = el("div", "setup-body-inner");
  wrap.innerHTML =
    `<p class="hint">At least one local CLI must be installed <em>and logged in</em>. Credentials are probed on load; the smoke check sends a tiny prompt to confirm it can answer.</p>` +
    `<div class="setup-grid">` +
      renderBackendStatus(st, "claude", claude) +
      renderBackendStatus(st, "cursor", cursor) +
    `</div>` +
    `<div class="setup-actions">` +
      `<button class="primary" id="setup-check-llm" type="button">Run smoke checks</button>` +
      `<button class="ghost" id="setup-save-llm" type="button">Save config</button>` +
      `<span class="status" id="setup-llm-status"></span>` +
    `</div>` +
    `<div class="setup-globals">` +
      toggle("setup-web", "Allow web research (Claude preferred; Cursor fallback)", st.llm.config.allow_web) +
      `<label class="setup-field-inline">Timeout` +
        `<input id="setup-timeout" type="number" min="30" step="30" value="${esc(st.llm.config.timeout_sec || 300)}">` +
        `<span>s</span>` +
      `</label>` +
    `</div>`;
  return wrap;
}

function renderBackendStatus(st: SetupState, id: string, backend: Backend) {
  const cfg = providerConfig(st, id);
  const phase = backendPhase(backend);
  const stateCls = backendPhaseClass(phase);
  const steps = backendFixSteps(id, phase);
  return (
    `<div class="setup-provider${cfg.enabled ? "" : " disabled"}">` +
      `<div class="setup-provider-head">` +
        `<strong>${esc(backend.label || id)}</strong>` +
        `<span class="setup-provider-head-right">` +
          toggle(`setup-${id}-enabled`, "Enabled", cfg.enabled) +
          badge(backend.installed, backend.installed ? "installed" : "missing") +
          authBadge(backend) +
        `</span>` +
      `</div>` +
      `<div class="setup-small ${stateCls}">${esc(backendStateText(backend))}</div>` +
      (steps.length ? `<ul class="setup-list">${steps.map((line) => `<li>${esc(line)}</li>`).join("")}</ul>` : "") +
      `<label class="setup-field">Model` +
        `<div class="setup-combo">` +
          `<input class="setup-model" id="setup-${id}-model" placeholder="default (recommended)" value="${esc(cfg.model || "")}" autocomplete="off" role="combobox" aria-autocomplete="list" aria-expanded="false">` +
          `<span class="setup-combo-caret" aria-hidden="true">\u25be</span>` +
          `<div class="setup-combo-menu" id="setup-combo-${id}" role="listbox" hidden></div>` +
        `</div>` +
      `</label>` +
      `<details class="setup-advanced">` +
        `<summary>Advanced</summary>` +
        `<div class="setup-advanced-body">` +
          `<label class="setup-field">Extra args` +
            `<input id="setup-${id}-extra" placeholder="--flag value" value="${esc((cfg.extra_args || []).join(" "))}">` +
          `</label>` +
        `</div>` +
      `</details>` +
    `</div>`
  );
}

function renderPerplexity(st: SetupState) {
  const pplx = st.perplexity || {};
  const env = st.environment || {};
  const wrap = el("div", "setup-body-inner");
  wrap.innerHTML =
    `<div class="setup-row"><strong>Browser session</strong>${badge(pplx.logged_in, pplx.logged_in ? "logged in" : "not logged in")}</div>` +
    `<p class="hint">Deep Research uses the persistent browser profile below. The login window is visible so you can complete Google/Perplexity auth and CAPTCHA if those bastards show up.</p>` +
    commandBlock([env.pplx_profile_dir || "~/.cursor/pplx-automation-profile"]) +
    `<div class="thesis-actions">` +
      `<button class="primary" id="setup-pplx-login" type="button">Set up Perplexity login</button>` +
      `<button class="ghost" id="setup-pplx-check" type="button">Verify login</button>` +
      `<span class="status" id="setup-pplx-status"></span>` +
    `</div>`;
  return wrap;
}

function readLlmConfig() {
  return {
    providers: LLM_IDS.map((id) => ({
      id,
      enabled: $$<HTMLInputElement>(`#setup-${id}-enabled`).checked,
      model: $$<HTMLInputElement>(`#setup-${id}-model`).value.trim(),
      extra_args: $$<HTMLInputElement>(`#setup-${id}-extra`).value.trim().split(/\s+/).filter(Boolean),
    })),
    timeout_sec: Number($$<HTMLInputElement>("#setup-timeout").value || 300),
    allow_web: $$<HTMLInputElement>("#setup-web").checked,
  };
}

async function saveLlmConfig() {
  const status = $$("#setup-llm-status");
  status.classList.remove("err");
  status.textContent = "saving...";
  try {
    await api("/api/analysis-config", "POST", { config: readLlmConfig() });
    status.textContent = "saved";
    await loadSetup();
  } catch (e) {
    status.classList.add("err");
    status.textContent = "save failed: " + (e as Error).message;
  }
}

async function pollJob(job: Job, status: HTMLElement): Promise<Job> {
  for (;;) {
    const rec = await api<Job>("/api/deep-job?id=" + encodeURIComponent(job.id));
    if (rec.state === "done") return rec;
    if (rec.state === "error" || rec.state === "needs_login") throw new Error(rec.error || rec.message || rec.state);
    status.innerHTML = `<span class="spinner"></span> ${esc(rec.message || rec.state || "running")}`;
    await new Promise((resolve) => setTimeout(resolve, 1200));
  }
}

async function startPerplexityLogin() {
  const status = $$("#setup-pplx-status");
  status.classList.remove("err");
  status.innerHTML = `<span class="spinner"></span> opening login window...`;
  try {
    const job = await api("/api/deep-research/login", "POST");
    await pollJob(job, status);
    status.textContent = "Perplexity login confirmed.";
    await loadSetup();
  } catch (e) {
    status.classList.add("err");
    status.textContent = "login failed: " + (e as Error).message;
  }
}

async function verifyPerplexity() {
  const status = $$("#setup-pplx-status");
  status.classList.remove("err");
  status.innerHTML = `<span class="spinner"></span> checking browser session...`;
  try {
    const rec = await api("/api/deep-research/verify-login", "POST");
    status.textContent = rec.logged_in ? "Logged in." : "Not logged in.";
    await loadSetup();
  } catch (e) {
    status.classList.add("err");
    status.textContent = "check failed: " + (e as Error).message;
  }
}

async function runSmokeChecks() {
  const status = $$("#setup-llm-status");
  status.classList.remove("err");
  status.innerHTML = `<span class="spinner"></span> running smoke checks...`;
  try {
    const rec = await api("/api/setup/check", "POST", {});
    status.textContent = "checks finished";
    renderSetup(rec);
    ensureModels().then(fillModelLists);
  } catch (e) {
    status.classList.add("err");
    status.textContent = "checks failed: " + (e as Error).message;
  }
}

async function saveIbkr() {
  const status = $$("#setup-ibkr-status");
  const token = $$<HTMLInputElement>("#setup-ibkr-token").value.trim();
  const query_id = $$<HTMLInputElement>("#setup-ibkr-query").value.trim();
  const history_query_id = $$<HTMLInputElement>("#setup-ibkr-history-query").value.trim();
  if (!token && !query_id && !history_query_id) {
    status.classList.add("err");
    status.textContent = "Enter a Query ID and/or token.";
    return;
  }
  status.classList.remove("err");
  status.textContent = "saving…";
  try {
    await api("/api/setup/ibkr", "POST", { token, query_id, history_query_id });
    status.textContent = "saved";
    await loadSetup();
  } catch (e) {
    status.classList.add("err");
    status.textContent = "save failed: " + (e as Error).message;
  }
}

async function syncIbkr() {
  const btn = $$<HTMLButtonElement>("#setup-sync-ibkr");
  const status = $$("#setup-ibkr-sync-status");
  if (!btn || btn.disabled) return;
  status.classList.remove("err");
  const prev = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Syncing…";
  status.innerHTML = `<span class="spinner"></span> Re-pulling portfolio from IBKR (read-only, can take a minute)…`;
  try {
    // Registered background job: start it and poll the shared loop (also surfaces
    // in the global task pill) instead of blocking the request for a minute.
    const job = await api("/api/holdings/sync", "POST", {});
    await pollDeepJob(job.id, status, async () => {
      await loadSetup(); // re-renders with the fresh snapshot badge + position count
    }, "IBKR sync");
  } catch (e) {
    status.classList.add("err");
    status.textContent = "Sync failed: " + (e as Error).message;
  } finally {
    btn.disabled = false;
    btn.textContent = prev;
  }
}

const SETUP_ACTIONS: Record<string, () => unknown> = {
  "setup-save-llm": saveLlmConfig,
  "setup-check-llm": runSmokeChecks,
  "setup-pplx-login": startPerplexityLogin,
  "setup-pplx-check": verifyPerplexity,
  "setup-save-ibkr": saveIbkr,
  "setup-sync-ibkr": syncIbkr,
  "setup-errlog-clear": clearErrorLog,
  "setup-errlog-refresh": () => renderErrorLog(true),
};

function wireSetup() {
  if (_wired) return;
  _wired = true;
  $$("#setup-refresh").addEventListener("click", () => loadSetup());
  $$("#setup-result").addEventListener("click", (e) => {
    SETUP_ACTIONS[(e.target as HTMLElement)?.id]?.();
  });
  // Dim a provider card live when its Enabled toggle flips.
  $$("#setup-result").addEventListener("change", (e) => {
    const t = e.target as HTMLInputElement;
    if (t && /^setup-(claude|cursor)-enabled$/.test(t.id || "")) {
      t.closest(".setup-provider")?.classList.toggle("disabled", !t.checked);
    }
    if (t && t.id === "setup-auto-refresh") void saveAutomation(t.checked);
  });
}

async function saveAutomation(enabled: boolean) {
  const status = $$("#setup-auto-status");
  if (status) { status.classList.remove("err"); status.textContent = "Saving…"; }
  try {
    await api("/api/setup/automation", "POST", { enabled });
    if (status) status.textContent = enabled ? "On — the scheduler is armed." : "Off.";
    await loadSetup();
  } catch (e) {
    if (status) { status.classList.add("err"); status.textContent = "Could not save: " + (e as Error).message; }
  }
}

async function loadSetup() {
  wireSetup();
  const status = $$("#setup-status");
  status.classList.remove("err");
  status.innerHTML = `<span class="spinner"></span> checking setup...`;
  try {
    const st = await api("/api/setup/status");
    renderSetup(st);
    status.textContent = "Setup status loaded.";
    ensureModels().then(fillModelLists);
  } catch (e) {
    status.classList.add("err");
    status.textContent = "setup check failed: " + (e as Error).message;
  }
}

export { loadSetup };
