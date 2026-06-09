// @ts-nocheck
import { $, api, el, esc } from "./core";

let _wired = false;

const LLM_IDS = ["claude", "cursor"];

// Model suggestions per provider, fetched once from /api/analysis-models and
// reused across re-renders. Empty until loaded; the inputs stay free-text so an
// unlisted model still works even if the list never arrives.
let _models = {};

function badge(ok, text) {
  return `<span class="setup-badge ${ok ? "ok" : "bad"}">${esc(text)}</span>`;
}

function toggle(id, label, checked) {
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
  } catch (e) {
    /* leave inputs as free-text */
  }
}

// Build the dropdown body. An empty filter (manual expand) shows everything;
// a non-empty filter (the user typing) narrows to substring matches.
function comboMenuHtml(id, filter) {
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
function wireModelCombo(id) {
  const input = document.getElementById(`setup-${id}-model`);
  const menu = document.getElementById(`setup-combo-${id}`);
  if (!input || !menu || input.dataset.comboWired) return;
  input.dataset.comboWired = "1";

  const open = (showAll) => {
    menu.innerHTML = comboMenuHtml(id, showAll ? "" : input.value);
    menu.hidden = false;
    input.setAttribute("aria-expanded", "true");
  };
  const close = () => {
    menu.hidden = true;
    input.setAttribute("aria-expanded", "false");
  };
  const setActive = (items, idx) => {
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
    const items = [...menu.querySelectorAll(".setup-combo-item")];
    if (menu.hidden || !items.length) return;
    let idx = items.findIndex((it) => it.classList.contains("active"));
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive(items, Math.min(items.length - 1, idx + 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive(items, Math.max(0, idx - 1));
    } else if (e.key === "Enter" && idx >= 0) {
      e.preventDefault();
      input.value = items[idx].dataset.val;
      close();
    }
  });

  // mousedown (not click) so selecting fires before the input's blur closes it.
  menu.addEventListener("mousedown", (e) => {
    const it = e.target.closest(".setup-combo-item");
    if (!it) return;
    e.preventDefault();
    input.value = it.dataset.val;
    close();
  });
  const caret = input.parentElement.querySelector(".setup-combo-caret");
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

function backendById(st, id) {
  return (st?.llm?.backends || []).find((b) => b.id === id) || {};
}

function providerConfig(st, id) {
  return (st?.llm?.config?.providers || []).find((p) => p.id === id) || { id, enabled: true, model: "", extra_args: [] };
}

function commandBlock(lines) {
  return `<pre class="setup-command">${esc(lines.join("\n"))}</pre>`;
}

// Collapse the backend record (installed / authenticated / smoke-check) into a
// single phase the UI can drive status text, colours, and fix steps from.
function backendPhase(b) {
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

function backendStateText(b) {
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

function backendPhaseClass(phase) {
  if (phase === "ok" || phase === "ready") return "ok";
  if (phase === "installed") return "muted";
  return "bad"; // missing, logged_out, quota, error
}

// What to actually do, tailored to the phase (and which CLI).
function backendFixSteps(id, phase) {
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
function authBadge(b) {
  const phase = backendPhase(b);
  if (phase === "ok" || phase === "ready") return badge(true, "logged in");
  if (phase === "logged_out") return badge(false, "not logged in");
  return ""; // installed/unknown/quota/error/missing -> no auth claim
}

// Ordered setup steps. `done` drives which step auto-expands: the first
// not-done step opens, everything else stays collapsed so the user only ever
// deals with one thing at a time.
function setupSteps(st) {
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
      : ibkr.configured
        ? "Credentials configured — sync holdings"
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

function stepStateBadge(step) {
  if (step.done) return badge(true, "OK");
  if (step.partial) return `<span class="setup-badge warn">IN PROGRESS</span>`;
  return badge(false, step.required ? "TODO" : "OPTIONAL");
}

function renderSetup(st) {
  const out = $("#setup-result");
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

  fillModelLists();
}

function renderIbkr(st) {
  const k = st.ibkr || {};
  const data = st.data || {};
  const dataReady = !!data.ready;
  const positions = data.holdings?.positions || 0;
  const wrap = el("div", "setup-body-inner");
  const tokenPlaceholder = k.token_set
    ? "\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022 saved \u2014 leave blank to keep"
    : "paste Flex Web Service token";
  wrap.innerHTML =
    `<div class="setup-row"><strong>Portfolio snapshot</strong>${badge(dataReady, dataReady ? `${positions} positions` : "empty")}</div>` +
    `<div class="setup-row"><strong>IBKR Flex Web Service</strong>${badge(k.configured, k.configured ? "configured" : "not set")}</div>` +
    `<p class="hint">Read-only Flex query behind <strong>Resync from IBKR</strong> on the Holdings tab. ` +
      `Saved to <code>${esc(k.secrets_path || "tools/secrets.env")}</code> (gitignored) and never committed. ` +
      `The token is stored locally and never shown back here.</p>` +
    `<ul class="setup-list">` +
      `<li>IBKR Client Portal &rarr; Settings &rarr; Account Settings &rarr; <strong>Flex Web Service</strong>: enable it and copy the <strong>token</strong>.</li>` +
      `<li>Create a <strong>Flex Query</strong> (positions, cash, open tax lots) and copy its <strong>Query ID</strong>.</li>` +
      `<li>Save below, then <strong>Resync from IBKR</strong> on the Holdings tab.</li>` +
    `</ul>` +
    (k.from_env ? `<p class="setup-small">A value is currently set via environment variable; that takes precedence over what you save here.</p>` : "") +
    `<label class="setup-field">Flex Query ID` +
      `<input id="setup-ibkr-query" placeholder="e.g. 1234567" value="${esc(k.query_id || "")}" autocomplete="off" inputmode="numeric">` +
    `</label>` +
    `<label class="setup-field">Flex token` +
      `<input id="setup-ibkr-token" type="password" placeholder="${esc(tokenPlaceholder)}" autocomplete="off">` +
    `</label>` +
    `<div class="setup-actions">` +
      `<button class="primary" id="setup-save-ibkr" type="button">Save credentials</button>` +
      `<span class="status" id="setup-ibkr-status"></span>` +
    `</div>`;
  return wrap;
}

function renderEnvironment(st) {
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

function renderLlmCli(st) {
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

function renderBackendStatus(st, id, backend) {
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

function renderPerplexity(st) {
  const pplx = st.perplexity || {};
  const env = st.environment || {};
  const wrap = el("div", "setup-body-inner");
  wrap.innerHTML =
    `<div class="setup-row"><strong>Browser session</strong>${badge(pplx.logged_in, pplx.logged_in ? "logged in" : "not logged in")}</div>` +
    `<p class="hint">Deep Research uses the persistent browser profile below. The login window is visible so you can complete Google/Perplexity auth and CAPTCHA if those bastards show up.</p>` +
    commandBlock([env.pplx_profile_dir || "~/.cursor/pplx-chrome-profile"]) +
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
      enabled: $(`#setup-${id}-enabled`).checked,
      model: $(`#setup-${id}-model`).value.trim(),
      extra_args: $(`#setup-${id}-extra`).value.trim().split(/\s+/).filter(Boolean),
    })),
    timeout_sec: Number($("#setup-timeout").value || 300),
    allow_web: $("#setup-web").checked,
  };
}

async function saveLlmConfig() {
  const status = $("#setup-llm-status");
  status.classList.remove("err");
  status.textContent = "saving...";
  try {
    await api("/api/analysis-config", "POST", { config: readLlmConfig() });
    status.textContent = "saved";
    await loadSetup();
  } catch (e) {
    status.classList.add("err");
    status.textContent = "save failed: " + e.message;
  }
}

async function pollJob(job, status) {
  for (;;) {
    const rec = await api("/api/deep-job?id=" + encodeURIComponent(job.id));
    if (rec.state === "done") return rec;
    if (rec.state === "error" || rec.state === "needs_login") throw new Error(rec.error || rec.message || rec.state);
    status.innerHTML = `<span class="spinner"></span> ${esc(rec.message || rec.state || "running")}`;
    await new Promise((resolve) => setTimeout(resolve, 1200));
  }
}

async function startPerplexityLogin() {
  const status = $("#setup-pplx-status");
  status.classList.remove("err");
  status.innerHTML = `<span class="spinner"></span> opening login window...`;
  try {
    const job = await api("/api/deep-research/login", "POST");
    await pollJob(job, status);
    status.textContent = "Perplexity login confirmed.";
    await loadSetup();
  } catch (e) {
    status.classList.add("err");
    status.textContent = "login failed: " + e.message;
  }
}

async function verifyPerplexity() {
  const status = $("#setup-pplx-status");
  status.classList.remove("err");
  status.innerHTML = `<span class="spinner"></span> checking browser session...`;
  try {
    const rec = await api("/api/deep-research/verify-login", "POST");
    status.textContent = rec.logged_in ? "Logged in." : "Not logged in.";
    await loadSetup();
  } catch (e) {
    status.classList.add("err");
    status.textContent = "check failed: " + e.message;
  }
}

async function runSmokeChecks() {
  const status = $("#setup-llm-status");
  status.classList.remove("err");
  status.innerHTML = `<span class="spinner"></span> running smoke checks...`;
  try {
    const rec = await api("/api/setup/check", "POST", {});
    status.textContent = "checks finished";
    renderSetup(rec);
    ensureModels().then(fillModelLists);
  } catch (e) {
    status.classList.add("err");
    status.textContent = "checks failed: " + e.message;
  }
}

async function saveIbkr() {
  const status = $("#setup-ibkr-status");
  const token = $("#setup-ibkr-token").value.trim();
  const query_id = $("#setup-ibkr-query").value.trim();
  if (!token && !query_id) {
    status.classList.add("err");
    status.textContent = "Enter a Query ID and/or token.";
    return;
  }
  status.classList.remove("err");
  status.textContent = "saving…";
  try {
    await api("/api/setup/ibkr", "POST", { token, query_id });
    status.textContent = "saved";
    await loadSetup();
  } catch (e) {
    status.classList.add("err");
    status.textContent = "save failed: " + e.message;
  }
}

const SETUP_ACTIONS = {
  "setup-save-llm": saveLlmConfig,
  "setup-check-llm": runSmokeChecks,
  "setup-pplx-login": startPerplexityLogin,
  "setup-pplx-check": verifyPerplexity,
  "setup-save-ibkr": saveIbkr,
};

function wireSetup() {
  if (_wired) return;
  _wired = true;
  $("#setup-refresh").addEventListener("click", () => loadSetup());
  $("#setup-result").addEventListener("click", (e) => {
    SETUP_ACTIONS[e.target?.id]?.();
  });
  // Dim a provider card live when its Enabled toggle flips.
  $("#setup-result").addEventListener("change", (e) => {
    const t = e.target;
    if (t && /^setup-(claude|cursor)-enabled$/.test(t.id || "")) {
      t.closest(".setup-provider")?.classList.toggle("disabled", !t.checked);
    }
  });
}

async function loadSetup() {
  wireSetup();
  const status = $("#setup-status");
  status.classList.remove("err");
  status.innerHTML = `<span class="spinner"></span> checking setup...`;
  try {
    const st = await api("/api/setup/status");
    renderSetup(st);
    status.textContent = "Setup status loaded.";
    ensureModels().then(fillModelLists);
  } catch (e) {
    status.classList.add("err");
    status.textContent = "setup check failed: " + e.message;
  }
}

export { loadSetup };
