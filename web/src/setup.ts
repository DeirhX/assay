// @ts-nocheck
import { $, api, el, esc } from "./core";

let _wired = false;

const LLM_IDS = ["claude", "cursor"];

function badge(ok, text) {
  return `<span class="setup-badge ${ok ? "ok" : "bad"}">${esc(text)}</span>`;
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

function backendStateText(backend) {
  const check = backend.check;
  if (check) return `${check.status}: ${check.message || ""}`;
  if (backend.installed) return "installed, not smoke-tested";
  return "missing";
}

function setupSummary(st) {
  const backends = st?.llm?.backends || [];
  const anyCliInstalled = backends.some((b) => b.installed);
  const anyCliOk = backends.some((b) => b.check?.ok);
  const pplxOk = !!st?.perplexity?.logged_in;
  return [
    { label: "LLM CLI installed", ok: anyCliInstalled },
    { label: "LLM CLI smoke-tested", ok: anyCliOk },
    { label: "Perplexity login", ok: pplxOk },
  ];
}

function renderSetup(st) {
  const out = $("#setup-result");
  out.innerHTML = "";

  const summary = el("div", "setup-summary");
  setupSummary(st).forEach((item) => {
    summary.innerHTML += badge(item.ok, `${item.ok ? "OK" : "TODO"} · ${item.label}`);
  });
  out.appendChild(summary);

  out.appendChild(renderEnvironment(st));
  out.appendChild(renderLlmCli(st));
  out.appendChild(renderPerplexity(st));
}

function renderEnvironment(st) {
  const env = st.environment || {};
  const card = el("div", "card setup-card");
  card.innerHTML =
    `<h2 class="section">Environment</h2>` +
    `<div class="setup-row"><strong>SEC user-agent</strong>${badge(env.sec_user_agent, env.sec_user_agent ? "set" : "missing")}</div>` +
    `<p class="hint">Set <code>SEC_USER_AGENT</code> before launching the server so SEC EDGAR requests identify you politely.</p>` +
    commandBlock([`$env:SEC_USER_AGENT = "assay research (you@example.com)"`, "py -3 tools/serve.py"]) +
    `<div class="setup-row"><strong>FMP API key</strong>${badge(env.fmp_api_key, env.fmp_api_key ? "set" : "optional")}</div>` +
    `<p class="hint">FMP is optional. If set, it gives a third opinion for some market-cap and profile fields.</p>`;
  return card;
}

function renderLlmCli(st) {
  const card = el("div", "card setup-card");
  const claude = backendById(st, "claude");
  const cursor = backendById(st, "cursor");
  card.innerHTML =
    `<h2 class="section">LLM CLIs for ticker analysis</h2>` +
    `<p class="hint">At least one local CLI must be installed and authorized. The smoke check sends a tiny prompt and should return OK.</p>` +
    `<div class="setup-grid">` +
      renderBackendConfig(st, "claude", claude, [
        "Install Claude Code if needed.",
        "Run `claude` once and complete the interactive login/subscription flow.",
        "Return here and run the smoke check.",
      ]) +
      renderBackendConfig(st, "cursor", cursor, [
        "Install/update Cursor CLI so `cursor-agent` is on PATH.",
        "Run `cursor-agent login` if the smoke check reports an auth failure.",
        "Return here and run the smoke check.",
      ]) +
    `</div>` +
    `<div class="setup-form-row">` +
      `<label>Timeout seconds <input id="setup-timeout" type="number" min="30" step="30" value="${esc(st.llm.config.timeout_sec || 300)}"></label>` +
      `<label class="setup-check"><input id="setup-web" type="checkbox" ${st.llm.config.allow_web ? "checked" : ""}> Allow Claude web research</label>` +
    `</div>` +
    `<div class="thesis-actions">` +
      `<button class="primary" id="setup-save-llm" type="button">Save LLM config</button>` +
      `<button class="ghost" id="setup-check-llm" type="button">Run smoke checks</button>` +
      `<span class="status" id="setup-llm-status"></span>` +
    `</div>`;
  return card;
}

function renderBackendConfig(st, id, backend, instructions) {
  const cfg = providerConfig(st, id);
  const check = backend.check;
  return (
    `<div class="setup-provider">` +
      `<div class="setup-row"><strong>${esc(backend.label || id)}</strong>${badge(backend.installed, backend.installed ? "installed" : "missing")}</div>` +
      `<div class="setup-small ${check?.ok ? "ok" : check ? "bad" : ""}">${esc(backendStateText(backend))}</div>` +
      `<label class="setup-check"><input type="checkbox" id="setup-${id}-enabled" ${cfg.enabled ? "checked" : ""}> Enabled</label>` +
      `<label>Model <input id="setup-${id}-model" placeholder="default" value="${esc(cfg.model || "")}"></label>` +
      `<label>Extra args <input id="setup-${id}-extra" placeholder="--flag value" value="${esc((cfg.extra_args || []).join(" "))}"></label>` +
      `<ul class="setup-list">${instructions.map((line) => `<li>${esc(line)}</li>`).join("")}</ul>` +
    `</div>`
  );
}

function renderPerplexity(st) {
  const pplx = st.perplexity || {};
  const env = st.environment || {};
  const card = el("div", "card setup-card");
  card.innerHTML =
    `<h2 class="section">Perplexity Deep Research login</h2>` +
    `<div class="setup-row"><strong>Browser session</strong>${badge(pplx.logged_in, pplx.logged_in ? "logged in" : "not logged in")}</div>` +
    `<p class="hint">Deep Research uses the persistent browser profile below. The login window is visible so you can complete Google/Perplexity auth and CAPTCHA if those bastards show up.</p>` +
    commandBlock([env.pplx_profile_dir || "~/.cursor/pplx-chrome-profile"]) +
    `<div class="thesis-actions">` +
      `<button class="primary" id="setup-pplx-login" type="button">Set up Perplexity login</button>` +
      `<button class="ghost" id="setup-pplx-check" type="button">Verify login</button>` +
      `<span class="status" id="setup-pplx-status"></span>` +
    `</div>`;
  return card;
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
  } catch (e) {
    status.classList.add("err");
    status.textContent = "checks failed: " + e.message;
  }
}

const SETUP_ACTIONS = {
  "setup-save-llm": saveLlmConfig,
  "setup-check-llm": runSmokeChecks,
  "setup-pplx-login": startPerplexityLogin,
  "setup-pplx-check": verifyPerplexity,
};

function wireSetup() {
  if (_wired) return;
  _wired = true;
  $("#setup-refresh").addEventListener("click", () => loadSetup());
  $("#setup-result").addEventListener("click", (e) => {
    SETUP_ACTIONS[e.target?.id]?.();
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
  } catch (e) {
    status.classList.add("err");
    status.textContent = "setup check failed: " + e.message;
  }
}

export { loadSetup };
