// @ts-nocheck
import { $, api, el, esc, relAge, setErrorSink, state } from "./core";
import { parseJsonField, pipeSegment, refreshPipeLocks, setPipeStep, setRepMode, updateExistingReportNotice, updateRepSubstate, updateStep2LoginGate } from "./pipeline";
import { pushNav } from "./shell";
import { taskEnd, taskStart, taskUpdate } from "./tasks";

// ---- Centralized error center ----------------------------------------------
// Counterpart to the task pill: failures collect here instead of dying in a
// scrolled-off card. A topbar badge shows the count; clicking opens a panel
// listing each error with source, time, and a dismiss action. Sources funnel
// in from api() (network/5xx), failed background jobs, and the global
// unhandledrejection/error handlers.
const errorLog = []; // newest last: { id, source, message, detail, time, count }
const ERROR_LOG_MAX = 50;
let _errorSeq = 0;

function recordError(source, message, opts = {}) {
  const msg = String(message || "unknown error").trim();
  if (!msg) return null;
  const now = Date.now();
  // Collapse rapid duplicates (same source+message within 5s) into one entry
  // with a bumped count, so a flapping poll loop can't bury the panel.
  const dup = errorLog.find((e) => e.source === source && e.message === msg && now - e.time < 5000);
  if (dup) {
    dup.count += 1;
    dup.time = now;
    if (opts.detail) dup.detail = String(opts.detail);
    renderErrorCenter();
    return dup.id;
  }
  const id = "err-" + (++_errorSeq);
  errorLog.push({ id, source, message: msg, detail: opts.detail ? String(opts.detail) : "", time: now, count: 1 });
  while (errorLog.length > ERROR_LOG_MAX) errorLog.shift();
  renderErrorCenter();
  return id;
}

// Wire core.api()'s failures into the error center without core importing us
// (keeps core a dependency-free leaf -- see core.ts).
setErrorSink(recordError);

function dismissError(id) {
  const i = errorLog.findIndex((e) => e.id === id);
  if (i >= 0) errorLog.splice(i, 1);
  renderErrorCenter();
}

function clearErrors() {
  errorLog.length = 0;
  renderErrorCenter();
}

function toggleErrorPanel(force) {
  const panel = $("#error-panel");
  if (!panel) return;
  const show = force != null ? force : panel.hidden;
  panel.hidden = !show;
  const btn = $("#error-indicator");
  if (btn) btn.setAttribute("aria-expanded", show ? "true" : "false");
  if (show) renderErrorCenter();
}

const ERROR_SOURCE_LABEL = { api: "Server", network: "Network", task: "Task", js: "App" };

function renderErrorCenter() {
  const btn = $("#error-indicator");
  if (btn) {
    const n = errorLog.length;
    btn.hidden = n === 0;
    btn.textContent = n === 0 ? "" : `Errors ${n}`;
    if (n === 0) toggleErrorPanel(false);
  }
  const list = $("#error-list");
  if (!list) return;
  if (!errorLog.length) {
    list.innerHTML = `<div class="error-empty">No errors. Carry on.</div>`;
    return;
  }
  list.innerHTML = "";
  // Newest first.
  [...errorLog].reverse().forEach((e) => {
    const row = el("div", "error-item");
    const src = ERROR_SOURCE_LABEL[e.source] || e.source;
    const times = e.count > 1 ? ` <span class="error-x">x${e.count}</span>` : "";
    row.innerHTML =
      `<div class="error-meta"><span class="error-src">${esc(src)}</span>` +
      `<span class="error-time">${esc(relAge(new Date(e.time).toISOString())) || "just now"}</span>${times}</div>` +
      `<div class="error-msg">${esc(e.message)}</div>` +
      (e.detail ? `<div class="error-detail">${esc(e.detail)}</div>` : "");
    const x = el("button", "error-dismiss", "&times;");
    x.title = "Dismiss";
    x.addEventListener("click", () => dismissError(e.id));
    row.appendChild(x);
    list.appendChild(row);
  });
}

// `label` is optional: pass it for LLM jobs that should surface in the global
// pill (analysis, Q&A, deep research); omit it for non-LLM jobs (e.g. login).
async function pollDeepJob(jobId, statusEl, onDone, label) {
  if (label) taskStart(jobId, label);
  try {
    for (;;) {
      await new Promise((r) => setTimeout(r, 4000));
      let job;
      try {
        job = await api("/api/deep-job?id=" + encodeURIComponent(jobId));
      } catch (e) {
        statusEl.classList.add("err");
        statusEl.textContent = "lost the job: " + e.message;
        return;
      }
      if (job.state === "queued" || job.state === "running") {
        statusEl.classList.remove("err");
        statusEl.innerHTML = `<span class="spinner"></span> ${esc(job.message || job.state)}`;
        if (label) taskUpdate(jobId, job.message || job.state);
        continue;
      }
      if (job.state === "done") {
        statusEl.classList.remove("err");
        await onDone(job);
        return;
      }
      if (job.state === "cancelled") {
        // Clean stop on user request -- not an error.
        statusEl.classList.remove("err");
        statusEl.textContent = "";
        return;
      }
      if (job.state === "needs_login") {
        // The run proved the cached login flag was stale, so resync the gate and
        // hand the user an actual login button instead of an instruction to read.
        state.pplxLoggedIn = false;
        updateStep2LoginGate();
        renderNeedsLogin(statusEl, job.message || job.error);
        return;
      }
      statusEl.classList.add("err");
      const jobErr = job.error || job.message || job.state;
      statusEl.textContent = jobErr;
      recordError("task", `${label || "Background task"} failed: ${jobErr}`);
      return;
    }
  } finally {
    if (label) taskEnd(jobId);
  }
}

// Render a "not logged in" run/import outcome as an actionable prompt: the
// message plus a real "Set up Perplexity login" button that opens the login
// window in place. After it succeeds, refreshLoginStatus reopens the prompt.
function renderNeedsLogin(statusEl, message) {
  statusEl.classList.remove("err");
  statusEl.innerHTML = "";
  statusEl.appendChild(document.createTextNode((message || "Not logged in.") + " "));
  const btn = el("button", "ghost", "Set up Perplexity login");
  btn.type = "button";
  btn.addEventListener("click", () => runPplxLogin(statusEl));
  statusEl.appendChild(btn);
}

// Shared by the Step 2 run button and the Step 3 "Run Deep Research" action.
// Login and prompt are prerequisites that live on Step 2, so if either is
// missing we bounce the user back there instead of failing in place.
async function runDeepResearch(status) {
  status.classList.remove("err");
  const segment = pipeSegment();
  const date = $("#pipe-date").value.trim() || undefined;
  const prompt = $("#pipe-prompt").value.trim();
  if (!segment) { status.classList.add("err"); status.textContent = "pick or save a segment first"; return; }
  if (!state.pplxLoggedIn) {
    setPipeStep(2);
    updateStep2LoginGate();
    $("#pipe-login-gate-status").textContent = "Set up the Perplexity login first.";
    return;
  }
  if (!prompt) {
    setPipeStep(2);
    const ps = $("#pipe-prompt-status");
    ps.classList.add("err");
    ps.textContent = "Build a prompt on Step 2 first.";
    return;
  }
  status.innerHTML = `<span class="spinner"></span> starting deep research (off-screen browser)...`;
  try {
    const job = await api("/api/deep-research/run", "POST", { segment, date, prompt });
    await pollDeepJob(job.id, status, async (done) => {
      const stem = (done.artifact && done.artifact.stem) || `${segment}-${done.date || date}`;
      const r = done.result || {};
      const n = (r.citations && r.citations.length) || 0;
      status.textContent = `done: ${stem} - ${r.report_chars || 0} chars, ${n} sources. Review the saved report below.`;
      await refreshDeepRuns();
      await loadDeepRun(stem);
      setPipeStep(3);
    }, `Deep research \u00b7 ${segment}`);
    await refreshLoginStatus();
  } catch (e) {
    status.classList.add("err");
    status.textContent = "run failed: " + e.message;
    await refreshLoginStatus();
  }
}

$("#pipe-run-deep").addEventListener("click", () => runDeepResearch($("#pipe-prompt-status")));
$("#pipe-run-deep-report").addEventListener("click", () => runDeepResearch($("#pipe-report-run-status")));
$("#rep-paste-manual").addEventListener("click", () => {
  state.repManual = true;
  setRepMode("current");
  const r = $("#pipe-report");
  if (r) r.focus();
});

$("#pipe-import").addEventListener("click", async () => {
  const status = $("#pipe-import-status");
  status.classList.remove("err");
  const url = $("#pipe-import-url").value.trim();
  const segment = pipeSegment();
  const date = $("#pipe-date").value.trim() || undefined;
  if (!segment) { status.classList.add("err"); status.textContent = "pick or save a segment first"; return; }
  if (!url) { status.classList.add("err"); status.textContent = "paste a Perplexity run URL"; return; }
  status.innerHTML = `<span class="spinner"></span> pulling the finished run (off-screen browser)...`;
  try {
    const job = await api("/api/deep-research/import", "POST", { segment, date, url });
    await pollDeepJob(job.id, status, async (done) => {
      const stem = (done.artifact && done.artifact.stem) || `${segment}-${done.date || date}`;
      const r = done.result || {};
      const n = (r.citations && r.citations.length) || 0;
      status.textContent = `imported: ${stem} - ${r.report_chars || 0} chars, ${n} sources.`;
      await refreshDeepRuns();
      await loadDeepRun(stem);
    }, `Importing \u00b7 ${segment}`);
    await refreshLoginStatus();
  } catch (e) {
    status.classList.add("err");
    status.textContent = "import failed: " + e.message;
  }
});

async function refreshLoginStatus() {
  let st;
  try {
    st = await api("/api/deep-research/login-status");
  } catch (e) {
    st = { logged_in: false };
  }
  state.pplxLoggedIn = !!st.logged_in;
  updateStep2LoginGate();
  const txt = $("#settings-login-state");
  if (txt) {
    const when = st.updated_at ? " (confirmed " + st.updated_at.slice(0, 10) + ")" : "";
    txt.classList.remove("err");
    txt.textContent = state.pplxLoggedIn
      ? `Logged in${when}. Re-login only if runs start hitting the login wall.`
      : "Not logged in. Use Re-login to set up the Perplexity session.";
  }
  return state.pplxLoggedIn;
}

async function runPplxLogin(statusEl) {
  statusEl.classList.remove("err");
  statusEl.innerHTML = `<span class="spinner"></span> opening a visible login window...`;
  try {
    const job = await api("/api/deep-research/login", "POST");
    await pollDeepJob(job.id, statusEl, async () => {
      statusEl.textContent = "Perplexity login confirmed. Off-screen runs will reuse it.";
    });
  } catch (e) {
    statusEl.classList.add("err");
    statusEl.textContent = "login failed: " + e.message;
  }
  await refreshLoginStatus();
}

$("#pipe-pplx-login").addEventListener("click", () => runPplxLogin($("#pipe-login-gate-status")));

$("#pipe-login-recheck").addEventListener("click", async () => {
  const txt = $("#pipe-login-gate-status");
  txt.classList.remove("err");
  txt.innerHTML = `<span class="spinner"></span> checking (off-screen browser, ~10s)...`;
  try {
    await api("/api/deep-research/verify-login", "POST");
    await refreshLoginStatus();
    txt.textContent = state.pplxLoggedIn
      ? "Logged in — prompt unlocked."
      : "Still not logged in. Use Set up Perplexity login.";
  } catch (e) {
    txt.classList.add("err");
    txt.textContent = "check failed: " + e.message;
  }
});

$("#settings-toggle").addEventListener("click", () => {
  const panel = $("#settings-panel");
  const opening = panel.hasAttribute("hidden");
  if (opening) {
    panel.removeAttribute("hidden");
    refreshLoginStatus();
  } else {
    panel.setAttribute("hidden", "");
  }
  $("#settings-toggle").setAttribute("aria-expanded", opening ? "true" : "false");
});

$("#settings-close").addEventListener("click", () => {
  $("#settings-panel").setAttribute("hidden", "");
  $("#settings-toggle").setAttribute("aria-expanded", "false");
});

$("#settings-relogin").addEventListener("click", () => runPplxLogin($("#settings-login-state")));

$("#settings-check").addEventListener("click", async () => {
  const txt = $("#settings-login-state");
  txt.classList.remove("err");
  txt.innerHTML = `<span class="spinner"></span> checking (off-screen browser, ~10s)...`;
  try {
    await api("/api/deep-research/verify-login", "POST");
    await refreshLoginStatus();
  } catch (e) {
    txt.classList.add("err");
    txt.textContent = "check failed: " + e.message;
  }
});

$("#pipe-save-report").addEventListener("click", async () => {
  const status = $("#pipe-artifact-status");
  status.classList.remove("err");
  status.textContent = "saving artifacts...";
  try {
    const rec = await api("/api/deep-research/save", "POST", {
      segment: pipeSegment(),
      date: $("#pipe-date").value.trim(),
      source_url: $("#pipe-source-url").value.trim(),
      report: $("#pipe-report").value,
      citations: parseJsonField("#pipe-sources", []),
    });
    status.textContent = `saved ${rec.stem} — continuing to Review`;
    state.currentDeepRun = rec.stem;
    state.repManual = false;
    pushNav({ view: "pipeline", segment: pipeSegment(), run: rec.stem });
    await refreshDeepRuns();
    setPipeStep(4);
  } catch (e) {
    status.textContent = "save failed: " + e.message;
    status.classList.add("err");
  }
});

$("#pipe-run-review").addEventListener("click", async () => {
  const status = $("#pipe-review-status");
  status.classList.remove("err");
  status.textContent = "running review gate...";
  try {
    const segment = pipeSegment();
    const date = $("#pipe-date").value.trim();
    const rec = await api("/api/deep-research/review", "POST", { segment, date });
    state.currentDeepRun = `${segment}-${date}`;
    pushNav({ view: "pipeline", segment, run: state.currentDeepRun });
    status.textContent = `review generated: ${rec.warnings.length} warning(s), ${rec.proposal.changes.length} proposal change(s)`;
    renderReviewGate(rec);
    await refreshDeepRuns();
  } catch (e) {
    status.textContent = "review failed: " + e.message;
    status.classList.add("err");
  }
});

$("#pipe-refresh-runs").addEventListener("click", refreshDeepRuns);

async function refreshDeepRuns() {
  const out = $("#pipe-runs");
  if (!out) return;
  try {
    const { runs } = await api("/api/deep-runs");
    state.deepRuns = runs || [];
    state.savedRuns = new Set(state.deepRuns.map((r) => r.stem));
    refreshPipeLocks();
    updateRepSubstate();
    updateExistingReportNotice();
    out.innerHTML = "";
    const list = el("div", "run-list");
    (runs || []).forEach((run) => {
      const row = el("button", "run-row", "");
      const files = Object.keys(run.files || {}).sort().join(", ");
      row.innerHTML = `<strong>${esc(run.stem)}</strong><span>${esc(files)}</span>`;
      row.addEventListener("click", async () => { await loadDeepRun(run.stem); setPipeStep(3); });
      list.appendChild(row);
    });
    out.appendChild(list);
  } catch (e) {
    out.innerHTML = `<div class="status err">could not load runs: ${esc(e.message)}</div>`;
  }
}

async function loadDeepRun(stem, { push = true } = {}) {
  const rec = await api("/api/deep-run/" + encodeURIComponent(stem));
  state.currentDeepRun = stem;
  state.repManual = false;
  // We just loaded this run off disk, so its report exists -- register it now so
  // the Step 4 lock opens immediately, without waiting on the async deep-runs
  // refresh (otherwise deep-linking to a run can bounce off a still-locked gate).
  state.savedRuns = state.savedRuns || new Set();
  state.savedRuns.add(stem);
  const m = stem.match(/^(.*)-(\d{4}-\d{2}-\d{2})$/);
  if (m) {
    $("#pipe-segment-select").value = m[1];
    $("#pipe-date").value = m[2];
    if (push) pushNav({ view: "pipeline", segment: m[1], run: stem });
  } else if (push) {
    pushNav({ view: "pipeline", run: stem });
  }
  if (rec.report) $("#pipe-report").value = rec.report;
  if (rec.sources) $("#pipe-sources").value = JSON.stringify(rec.sources.citations || [], null, 2);
  if (rec.sources && rec.sources.source_url) $("#pipe-source-url").value = rec.sources.source_url;
  if (rec.markdown || rec.review || rec.proposal) renderReviewGate({
    markdown: rec.review || "",
    proposal: rec.proposal || { changes: [], warnings: [] },
    warnings: (rec.proposal && rec.proposal.warnings) || [],
    rows: [],
    source_summary: rec.proposal ? null : undefined,
  });
  setRepMode("current");
  refreshPipeLocks();
}

function renderReviewGate(rec) {
  const out = $("#pipe-review-output");
  out.innerHTML = "";
  const card = el("div", "card");
  card.appendChild(el("h2", "section", "Review gate output"));
  if (rec.source_summary) {
    const b = rec.source_summary.buckets || {};
    card.appendChild(el("div", "badges",
      Object.keys(b).map((k) => `<span class="badge ${k === "weak" && b[k] ? "off" : "on"}">${esc(k)}: ${b[k]}</span>`).join("")));
  }
  const findings = rec.findings || (rec.proposal && rec.proposal.findings) || null;
  if (findings && findings.length) {
    const cls = { BLOCK: "ERROR", WARN: "WARN", FYI: "INFO" };
    const checks = el("div", "checks");
    findings.forEach((f) => checks.appendChild(
      el("div", `check ${cls[f.level] || "INFO"}`, `<span class="sev">${esc(f.level)}</span><span>${esc(f.message)}</span>`)));
    card.appendChild(checks);
  } else if (rec.warnings && rec.warnings.length) {
    const checks = el("div", "checks");
    rec.warnings.forEach((w) => checks.appendChild(el("div", "check WARN", `<span class="sev">WARN</span><span>${esc(w)}</span>`)));
    card.appendChild(checks);
  }
  if (rec.rows && rec.rows.length) {
    const table = el("table");
    table.innerHTML =
      "<thead><tr><th>Symbol</th><th>Action</th><th>Target</th><th>Data</th><th>Conflict</th></tr></thead>" +
      "<tbody>" + rec.rows.map((r) =>
        `<tr><td><strong>${esc(r.symbol)}</strong></td><td>${esc(r.report_action)}</td><td>${esc(r.target_rule || "")}</td><td>${esc(r.data_quality)}</td><td>${esc(r.conflict || "")}</td></tr>`
      ).join("") + "</tbody>";
    card.appendChild(table);
  }
  const proposal = rec.proposal || {};
  const changes = proposal.changes || [];
  const blocked = rec.blocked_symbols || proposal.blocked_symbols || [];
  const applicable = changes.filter((c) => !blocked.includes(c.symbol));
  const noMembers = !(rec.rows && rec.rows.length);
  card.appendChild(el("h2", "section", "Target-model proposal"));
  if (changes.length) {
    const pre = el("pre", "json-preview", esc(JSON.stringify(changes, null, 2)));
    card.appendChild(pre);
    if (blocked.length) {
      card.appendChild(el("div", "hint",
        `Apply is blocked for ${blocked.map(esc).join(", ")} (ERROR-level data). Re-pull and fix the data first.`));
    }
  } else if (noMembers) {
    card.appendChild(el("div", "hint err",
      "This segment has no members, so there's nothing to review or apply. Add tickers to the segment definition, then re-pull and re-review."));
  } else {
    card.appendChild(el("div", "hint", "No target-model changes proposed."));
  }
  if (rec.markdown) {
    card.appendChild(el("h2", "section", "Review markdown"));
    card.appendChild(el("pre", "markdown-preview", esc(rec.markdown.slice(0, 8000))));
  }
  out.appendChild(card);
  // Apply only becomes available once the review produced a change we're allowed
  // to apply -- i.e. at least one proposed symbol that isn't data-blocked.
  const applyBtn = $("#pipe-apply-proposal");
  if (applyBtn) applyBtn.disabled = !applicable.length;
}

$("#pipe-apply-proposal").addEventListener("click", async () => {
  const status = $("#pipe-apply-status");
  const segment = pipeSegment();
  const date = ($("#pipe-date").value || "").trim();
  if (!segment || !date) {
    status.textContent = "run the review gate first";
    status.classList.add("err");
    return;
  }
  if (!window.confirm("Apply this target-model proposal? This changes target-model.json, not trades.")) return;
  status.classList.remove("err");
  status.textContent = "applying proposal...";
  try {
    const rec = await api("/api/target-proposal/apply", "POST", { segment, date, confirm: true });
    status.textContent = `applied: ${rec.applied.join(", ") || "none"}; skipped: ${rec.skipped.length}`;
  } catch (e) {
    status.textContent = "apply failed: " + e.message;
    status.classList.add("err");
  }
});

export {
  errorLog,
  ERROR_LOG_MAX,
  _errorSeq,
  recordError,
  dismissError,
  clearErrors,
  toggleErrorPanel,
  ERROR_SOURCE_LABEL,
  renderErrorCenter,
  pollDeepJob,
  renderNeedsLogin,
  runDeepResearch,
  refreshLoginStatus,
  runPplxLogin,
  refreshDeepRuns,
  loadDeepRun,
  renderReviewGate,
};
