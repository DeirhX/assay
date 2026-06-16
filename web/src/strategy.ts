// @ts-nocheck
// Guided "Direction -> Rebalance" flow. One screen drives a server-side
// orchestrated run (tools/orchestrate.py): you type a direction, the server
// drafts a segment, runs Deep Research, synthesizes target-model bands, and
// pauses at two human gates before showing the rebalance recommendation. The
// durable state lives in the run manifest; this view is a thin renderer over
// GET /api/strategy/{run_id}, polled while a leg is running.
import { $, api, el, esc, fmtCZK, fmtSignedWeight, sensitive } from "./core";
import { pushNav, setActiveView } from "./shell";

// States in which a background leg is working and we should keep polling.
const RUNNING = new Set(["draft_running", "synthesis_running", "applying"]);
// Progress-tracker stages, in order, mapped from the manifest state.
const STAGE_ORDER = ["draft", "segment", "research", "synthesize", "review", "done"];
const STATE_STAGE = {
  draft_running: "draft",
  awaiting_segment_approval: "segment",
  synthesis_running: "synthesize",
  needs_login: "research",
  awaiting_proposal_approval: "review",
  applying: "review",
  done: "done",
  error: "draft",
};

let _activeRunId = null;
let _pollTimer = null;
// The user can revisit any step the run has already reached. `_viewStage` pins a
// past step for read-only viewing; null means "follow the live step" (the one the
// run is actually working on or parked at). `_lastM` caches the last manifest so
// a stepper click can re-render instantly without re-fetching.
let _viewStage = null;
let _lastM = null;

const STAGE_TITLE = {
  draft: "Draft", segment: "Segment", research: "Research",
  synthesize: "Synthesize", review: "Review", done: "Recommendation",
};

// The stage the run is currently on, derived from its state.
const liveStage = (m) => STATE_STAGE[m.state] || "draft";
// Stages the user may click into: everything up to and including the live one.
// (Nothing is revisitable while errored — the run never produced those steps.)
function reachedStages(m) {
  if (m.state === "error") return [];
  const liveIdx = STAGE_ORDER.indexOf(liveStage(m));
  return STAGE_ORDER.slice(0, Math.max(0, liveIdx) + 1);
}

function currentRunParam() {
  return new URLSearchParams(window.location.search).get("run") || "";
}

async function loadStrategy() {
  const runId = currentRunParam();
  _viewStage = null;  // a fresh load always lands on the live/actionable step
  _lastM = null;
  if (runId) {
    _activeRunId = runId;
    $("#strat-start").hidden = true;
    renderLoading("Loading run…");
    await refreshOnce(runId);
  } else {
    stopPolling();
    _activeRunId = null;
    $("#strat-start").hidden = false;
    $("#strat-stages").hidden = true;
    $("#strat-panel").innerHTML = "";
    $("#strat-status").textContent = "";
    $("#strat-status").classList.remove("err");
    loadRecentRuns();
  }
}

function stopPolling() {
  if (_pollTimer) { clearTimeout(_pollTimer); _pollTimer = null; }
}

function schedulePoll(runId) {
  stopPolling();
  _pollTimer = setTimeout(() => refreshOnce(runId), 3000);
}

async function refreshOnce(runId) {
  if (_activeRunId !== runId) return;  // navigated away
  let m;
  try {
    m = await api("/api/strategy/" + encodeURIComponent(runId));
  } catch (e) {
    renderError("Lost the run: " + e.message);
    return;
  }
  if (_activeRunId !== runId) return;
  render(m);
  if (RUNNING.has(m.state)) schedulePoll(runId);
  else stopPolling();
}

// ---- start + recents ------------------------------------------------------
async function startRun() {
  const dir = $("#strat-direction").value.trim();
  const status = $("#strat-status");
  status.classList.remove("err");
  if (!dir) { status.classList.add("err"); status.textContent = "describe a direction first"; return; }
  const btn = $("#strat-go");
  btn.disabled = true;
  status.innerHTML = `<span class="spinner"></span> starting run…`;
  try {
    const m = await api("/api/strategy/start", "POST", { direction: dir });
    _activeRunId = m.run_id;
    _viewStage = null;
    $("#strat-start").hidden = true;
    status.textContent = "";
    pushNav({ view: "strategy", run: m.run_id });
    render(m);
    schedulePoll(m.run_id);
  } catch (e) {
    status.classList.add("err");
    status.textContent = "could not start: " + e.message;
  } finally {
    btn.disabled = false;
  }
}

async function loadRecentRuns() {
  const box = $("#strat-recent");
  if (!box) return;
  try {
    const { runs } = await api("/api/strategy/runs");
    if (!runs || !runs.length) { box.innerHTML = ""; return; }
    box.innerHTML = `<div class="subhead">Recent runs</div>` + runs.slice(0, 6).map((r) =>
      `<button type="button" class="strat-recent-item" data-run="${esc(r.run_id)}">` +
        `<span class="strat-recent-dir">${esc(r.direction || "(no direction)")}</span>` +
        `<span class="strat-recent-state">${esc(stateLabel(r.state))}</span>` +
      `</button>`).join("");
    box.querySelectorAll(".strat-recent-item").forEach((b) => {
      b.addEventListener("click", () => { pushNav({ view: "strategy", run: b.dataset.run }); loadStrategy(); });
    });
  } catch (_e) {
    box.innerHTML = "";
  }
}

// ---- render dispatch ------------------------------------------------------
function render(m) {
  _lastM = m;
  renderStages(m);
  const panel = $("#strat-panel");
  if (m.state === "error") { _viewStage = null; return renderError(m.error || "the run failed"); }
  // Drop a stale pin (e.g. a run reloaded at an earlier state) and decide whether
  // we're showing the live step (interactive) or a revisited one (read-only).
  const reached = reachedStages(m);
  if (_viewStage && !reached.includes(_viewStage)) _viewStage = null;
  const live = liveStage(m);
  const showing = _viewStage || live;
  if (showing === live) return renderLiveStage(m, panel);
  renderPastStage(m, showing, panel);
}

// The live step keeps its full interactive treatment (gate / spinner / done).
function renderLiveStage(m, panel) {
  if (m.state === "awaiting_segment_approval") return renderSegmentGate(m, panel);
  if (m.state === "awaiting_proposal_approval") return renderProposalGate(m, panel);
  if (m.state === "needs_login") return renderNeedsLogin(m, panel);
  if (m.state === "done") return renderDone(m, panel);
  // Any running state: a spinner with the live message.
  renderLoading(m.message || m.state);
}

function renderStages(m) {
  const wrap = $("#strat-stages");
  if (!wrap) return;
  wrap.hidden = false;
  const errored = m.state === "error";
  const live = liveStage(m);
  const liveIdx = STAGE_ORDER.indexOf(live);
  const reached = reachedStages(m);
  const showing = (!errored && _viewStage && reached.includes(_viewStage)) ? _viewStage : live;
  wrap.querySelectorAll("li").forEach((li) => {
    const stage = li.dataset.stage;
    const idx = STAGE_ORDER.indexOf(stage);
    li.classList.toggle("active", !errored && stage === showing);
    li.classList.toggle("done", !errored && (idx < liveIdx || m.state === "done"));
    li.classList.toggle("live", !errored && stage === live && m.state !== "done");
    li.classList.toggle("clickable", reached.includes(stage));
    li.setAttribute("aria-current", !errored && stage === showing ? "step" : "false");
  });
}

// ---- revisiting completed steps -------------------------------------------
// A reached-but-not-live step renders read-only from the persisted manifest, with
// a banner that jumps back to wherever the run actually is.
function renderPastStage(m, stage, panel) {
  const renderer = {
    draft: renderDraftStep, segment: renderSegmentStep, research: renderResearchStep,
    synthesize: renderSynthesizeStep, review: renderReviewStep, done: renderDone,
  }[stage] || renderLoading;
  renderer(m, panel);
  panel.insertBefore(viewingBar(m, stage), panel.firstChild);
}

function viewingBar(m, stage) {
  const bar = el("div", "strat-viewing-bar");
  bar.innerHTML = `<span>Viewing the <strong>${esc(STAGE_TITLE[stage] || "completed")}</strong> step (read-only).</span>`;
  const btn = el("button", "ghost", `Back to current step: ${esc(STAGE_TITLE[liveStage(m)] || "")} →`);
  btn.type = "button";
  btn.addEventListener("click", () => { _viewStage = null; if (_lastM) render(_lastM); });
  bar.appendChild(btn);
  return bar;
}

function memberTable(definition) {
  const members = (definition && definition.members) || [];
  const tbl = el("table", "strat-changes");
  tbl.innerHTML = `<thead><tr><th>Symbol</th><th>Conviction</th><th>Sleeve</th><th>Rationale</th></tr></thead>`;
  const body = el("tbody");
  if (!members.length) body.innerHTML = `<tr><td colspan="4" class="muted">No members recorded.</td></tr>`;
  members.forEach((mem) => {
    const conf = mem.confidence || "";
    const tr = el("tr");
    tr.innerHTML =
      `<td><strong>${esc(mem.symbol || "")}</strong></td>` +
      `<td><span class="strat-conv strat-conv-${esc(conf)}">${esc(conf || "—")}</span></td>` +
      `<td>${esc(mem.sleeve || "")}</td>` +
      `<td class="strat-rationale">${esc(mem.rationale || "")}</td>`;
    body.appendChild(tr);
  });
  tbl.appendChild(body);
  return tbl;
}

function renderDraftStep(m, panel) {
  const def = (m.draft && m.draft.definition) || {};
  const warnings = ((m.draft && m.draft.warnings) || []).join(" ");
  const n = (def.members || []).length;
  panel.innerHTML = "";
  const card = el("div", "card strat-gate");
  card.innerHTML =
    `<h3>Draft · ${n} candidate name(s)</h3>` +
    `<p class="hint">${esc(def.comment || "The segment the run drafted from your direction.")}</p>` +
    (warnings ? `<div class="strat-warn">${esc(warnings)}</div>` : "");
  card.appendChild(memberTable(def));
  panel.appendChild(card);
}

function renderSegmentStep(m, panel) {
  const def = (m.draft && m.draft.definition) || {};
  panel.innerHTML = "";
  const card = el("div", "card strat-gate");
  card.innerHTML =
    `<h3>Segment · ${esc(m.segment || "approved")}</h3>` +
    `<p class="hint">The approved research segment. Deep Research and synthesis ran against these names.</p>` +
    `<label>Segment definition JSON</label>` +
    `<textarea rows="14" spellcheck="false" readonly></textarea>`;
  panel.appendChild(card);
  card.querySelector("textarea").value = JSON.stringify(def, null, 2);
}

function renderResearchStep(m, panel) {
  const review = m.review || {};
  const blocked = review.blocked_symbols || [];
  const ready = m.review != null;
  panel.innerHTML = "";
  const card = el("div", "card strat-gate");
  card.innerHTML =
    `<h3>Research · Deep Research ${esc(m.date || "")}</h3>` +
    `<p class="hint">${esc(!ready ? "Research is still in progress…"
      : (typeof review.source_summary === "string" ? review.source_summary
         : "Deep Research report reviewed before synthesis."))}</p>` +
    (blocked.length ? `<div class="strat-warn">Blocked (insufficient/ERROR data, skipped): ${blocked.map(esc).join(", ")}</div>` : "");
  if (review.findings != null) {
    const det = el("details", "strat-advanced");
    det.innerHTML = `<summary>Review findings (raw)</summary>` +
      `<pre class="strat-pre">${esc(JSON.stringify(review.findings, null, 2))}</pre>`;
    card.appendChild(det);
  }
  if (m.segment && m.date) {
    const actions = el("div", "thesis-actions");
    const open = el("button", "ghost", "Open the full Deep Research run ↗");
    open.type = "button";
    open.addEventListener("click", () => {
      pushNav({ view: "pipeline", segment: m.segment, run: `${m.segment}-${m.date}` });
      setActiveView("pipeline");
    });
    actions.appendChild(open);
    card.appendChild(actions);
  }
  panel.appendChild(card);
}

function renderSynthesizeStep(m, panel) {
  const proposal = m.proposal || {};
  const meta = proposal.construct_meta || {};
  const changes = proposal.changes || [];
  panel.innerHTML = "";
  const card = el("div", "card strat-gate");
  card.innerHTML =
    `<h3>Synthesize · ${changes.length} target band(s)</h3>` +
    `<p class="hint">${m.proposal
      ? `Budget ${esc(meta.segment_budget_pct ?? "?")}% of book, sized total ${esc(meta.sized_midpoint_total_pct ?? "?")}%.`
      : "Synthesis is still in progress…"}</p>`;
  if (m.proposal) card.appendChild(changesTable(changes));
  panel.appendChild(card);
}

function renderReviewStep(m, panel) {
  const proposal = m.proposal || {};
  const changes = proposal.changes || [];
  panel.innerHTML = "";
  const card = el("div", "card strat-gate");
  card.innerHTML =
    `<h3>Review · proposed target-model changes</h3>` +
    `<p class="hint">The bands ${m.state === "done" ? "you approved" : "proposed"} at Gate 2.</p>`;
  card.appendChild(changesTable(changes));
  card.appendChild(previewBlock(m.preview));
  panel.appendChild(card);
}

function renderLoading(msg) {
  $("#strat-panel").innerHTML =
    `<div class="card strat-running"><span class="spinner"></span> ${esc(msg || "working…")}</div>`;
}

function renderError(msg) {
  const panel = $("#strat-panel");
  panel.innerHTML =
    `<div class="card strat-error"><strong>Run failed.</strong> <span>${esc(msg)}</span></div>`;
  const actions = el("div", "thesis-actions");
  const restart = el("button", "ghost", "Start a new run ↺");
  restart.type = "button";
  restart.addEventListener("click", () => { pushNav({ view: "strategy" }); loadStrategy(); });
  actions.appendChild(restart);
  panel.querySelector(".card").appendChild(actions);
}

// ---- gate 1: approve the drafted segment ----------------------------------
function renderSegmentGate(m, panel) {
  const draft = m.draft || {};
  const definition = draft.definition || {};
  const warnings = (draft.warnings || []).join(" ");
  panel.innerHTML = "";
  const card = el("div", "card strat-gate");
  card.innerHTML =
    `<h3>Gate 1 · Approve the research segment</h3>` +
    `<p class="hint">Review the drafted tickers and sleeves. Edit the JSON if needed, then approve to run Deep Research and synthesis.</p>` +
    (warnings ? `<div class="strat-warn">${esc(warnings)}</div>` : "") +
    `<label>Segment definition JSON</label>` +
    `<textarea id="strat-seg-json" rows="14" spellcheck="false"></textarea>` +
    `<div class="thesis-actions">` +
      `<button class="primary" id="strat-approve-seg" type="button">Approve segment & synthesize →</button>` +
      `<span class="status" id="strat-seg-status"></span>` +
    `</div>`;
  panel.appendChild(card);
  $("#strat-seg-json").value = JSON.stringify(definition, null, 2);
  $("#strat-approve-seg").addEventListener("click", () => approveSegment(m.run_id));
}

async function approveSegment(runId) {
  const status = $("#strat-seg-status");
  const btn = $("#strat-approve-seg");
  status.classList.remove("err");
  let definition;
  try {
    definition = JSON.parse($("#strat-seg-json").value);
  } catch (e) {
    status.classList.add("err");
    status.textContent = "invalid JSON: " + e.message;
    return;
  }
  btn.disabled = true;
  status.innerHTML = `<span class="spinner"></span> approving…`;
  try {
    const m = await api("/api/strategy/" + encodeURIComponent(runId) + "/approve-segment", "POST", { definition });
    render(m);
    schedulePoll(runId);
  } catch (e) {
    status.classList.add("err");
    status.textContent = "could not approve: " + e.message;
    btn.disabled = false;
  }
}

// ---- gate 2: approve the synthesized target changes -----------------------
const RULE_LABEL = {
  trim_only: "trim only", do_not_add: "don't add", reduce: "reduce",
  avoid: "avoid", accumulate: "accumulate", hold: "hold", wait: "wait",
};
const bandStr = (t) => (t && t.low != null ? `${t.low}–${t.high}%` : "—");

function renderProposalGate(m, panel) {
  const proposal = m.proposal || {};
  const changes = proposal.changes || [];
  const blocked = proposal.blocked_symbols || [];
  panel.innerHTML = "";
  const card = el("div", "card strat-gate");
  const meta = proposal.construct_meta || {};
  card.innerHTML =
    `<h3>Gate 2 · Approve target-model changes</h3>` +
    `<p class="hint">Synthesized bands for ${changes.length} name(s). Budget ${esc(meta.segment_budget_pct ?? "?")}% of book, ` +
    `sized total ${esc(meta.sized_midpoint_total_pct ?? "?")}%. Review each band before approving — applying writes target-model.json (a backup is kept).</p>` +
    (blocked.length ? `<div class="strat-warn">Blocked (ERROR-level data, skipped): ${blocked.map(esc).join(", ")}</div>` : "");

  card.appendChild(changesTable(changes));

  // Advanced: edit the raw change list before applying.
  const adv = el("details", "strat-advanced");
  adv.innerHTML = `<summary>Edit changes JSON (advanced)</summary>` +
    `<textarea id="strat-changes-json" rows="12" spellcheck="false"></textarea>`;
  card.appendChild(adv);

  card.appendChild(previewBlock(m.preview));

  const actions = el("div", "thesis-actions");
  let allowBlockedHtml = "";
  if (blocked.length) {
    allowBlockedHtml = `<label class="strat-check"><input type="checkbox" id="strat-allow-blocked"> apply blocked names anyway</label>`;
  }
  actions.innerHTML =
    `<button class="primary" id="strat-approve-prop" type="button">Approve & apply →</button>` +
    allowBlockedHtml +
    `<span class="status" id="strat-prop-status"></span>`;
  card.appendChild(actions);

  panel.appendChild(card);
  $("#strat-changes-json").value = JSON.stringify(changes, null, 2);
  $("#strat-approve-prop").addEventListener("click", () => approveProposal(m.run_id));
}

function changesTable(changes) {
  const tbl = el("table", "strat-changes");
  tbl.innerHTML =
    `<thead><tr><th>Symbol</th><th>Conviction</th><th>Action</th>` +
    `<th>Current</th><th>Proposed</th><th>Rule</th><th>Rationale</th></tr></thead>`;
  const body = el("tbody");
  if (!changes.length) {
    body.innerHTML = `<tr><td colspan="7" class="muted">No target changes proposed.</td></tr>`;
  }
  changes.forEach((c) => {
    const tr = el("tr");
    const conv = c.conviction || "";
    tr.innerHTML =
      `<td><strong>${esc(c.symbol)}</strong></td>` +
      `<td><span class="strat-conv strat-conv-${esc(conv)}">${esc(conv || "—")}</span></td>` +
      `<td>${esc((c.action || "").replace("_target", ""))}</td>` +
      `<td class="muted">${esc(bandStr(c.current_target))}</td>` +
      `<td>${esc(bandStr(c.proposed_target))}</td>` +
      `<td>${esc(RULE_LABEL[c.proposed_target && c.proposed_target.rule] || (c.proposed_target && c.proposed_target.rule) || "")}</td>` +
      `<td class="strat-rationale">${esc(c.rationale || "")}</td>`;
    body.appendChild(tr);
  });
  tbl.appendChild(body);
  return tbl;
}

async function approveProposal(runId) {
  const status = $("#strat-prop-status");
  const btn = $("#strat-approve-prop");
  status.classList.remove("err");
  let changes;
  try {
    changes = JSON.parse($("#strat-changes-json").value);
  } catch (e) {
    status.classList.add("err");
    status.textContent = "invalid changes JSON: " + e.message;
    return;
  }
  const allowBlocked = !!($("#strat-allow-blocked") && $("#strat-allow-blocked").checked);
  btn.disabled = true;
  status.innerHTML = `<span class="spinner"></span> applying…`;
  try {
    const m = await api("/api/strategy/" + encodeURIComponent(runId) + "/approve-proposal", "POST",
      { changes, allow_blocked: allowBlocked });
    render(m);
  } catch (e) {
    status.classList.add("err");
    status.textContent = "could not apply: " + e.message;
    btn.disabled = false;
  }
}

// ---- needs login ----------------------------------------------------------
function renderNeedsLogin(m, panel) {
  panel.innerHTML = "";
  const card = el("div", "card strat-gate");
  card.innerHTML =
    `<h3>Perplexity login required</h3>` +
    `<p class="hint">${esc(m.message || "Set up the Perplexity login, then resume the run.")}</p>` +
    `<div class="thesis-actions">` +
      `<button class="ghost" id="strat-open-setup" type="button">Open settings to log in</button>` +
      `<button class="primary" id="strat-resume" type="button">Resume run</button>` +
      `<span class="status" id="strat-login-status"></span>` +
    `</div>`;
  panel.appendChild(card);
  $("#strat-open-setup").addEventListener("click", () => setActiveView("setup"));
  // Resuming re-approves the (already approved) segment, which the state machine
  // accepts from needs_login -> synthesis_running.
  $("#strat-resume").addEventListener("click", () => approveSegmentResume(m.run_id));
}

async function approveSegmentResume(runId) {
  const status = $("#strat-login-status");
  status.classList.remove("err");
  status.innerHTML = `<span class="spinner"></span> resuming…`;
  try {
    const m = await api("/api/strategy/" + encodeURIComponent(runId) + "/approve-segment", "POST", {});
    render(m);
    schedulePoll(runId);
  } catch (e) {
    status.classList.add("err");
    status.textContent = "could not resume: " + e.message;
  }
}

// ---- done -----------------------------------------------------------------
function renderDone(m, panel) {
  const applied = m.applied || {};
  const appliedSyms = applied.applied || [];
  panel.innerHTML = "";
  const card = el("div", "card strat-done");
  card.innerHTML =
    `<h3>✓ Applied — rebalance recommendation ready</h3>` +
    `<p class="hint">${esc(m.message || "")}` +
    (applied.backup ? ` Backup: <code>${esc(applied.backup)}</code>.` : "") + `</p>` +
    (appliedSyms.length ? `<p>Updated: ${appliedSyms.map(esc).join(", ")}.</p>` : "") +
    ((applied.skipped && applied.skipped.length)
      ? `<p class="muted">Skipped: ${applied.skipped.map((s) => esc(s.symbol) + " (" + esc(s.reason) + ")").join("; ")}.</p>` : "");
  card.appendChild(previewBlock(m.preview, { final: true }));

  const actions = el("div", "thesis-actions");
  const goReb = el("button", "primary", "Open the rebalance planner →");
  goReb.type = "button";
  goReb.addEventListener("click", () => { pushNav({ view: "rebalance" }); setActiveView("rebalance"); });
  const planLink = el("a", "ghost", "Open standing plan ↗");
  planLink.href = "/next-steps.html"; planLink.target = "_blank"; planLink.rel = "noopener"; planLink.setAttribute("role", "button");
  const restart = el("button", "ghost", "New run ↺");
  restart.type = "button";
  restart.addEventListener("click", () => { pushNav({ view: "strategy" }); loadStrategy(); });
  actions.appendChild(goReb);
  actions.appendChild(planLink);
  actions.appendChild(restart);
  card.appendChild(actions);
  panel.appendChild(card);
}

// ---- shared: compact rebalance preview ------------------------------------
function previewBlock(preview, { final = false } = {}) {
  const wrap = el("div", "strat-preview");
  if (!preview || !preview.available) {
    wrap.innerHTML = `<div class="hint">${esc((preview && preview.reason) || "No rebalance preview available (need a target model and a holdings snapshot).")}</div>`;
    return wrap;
  }
  const plan = preview.plan || {};
  const rows = (plan.rows || []).filter((r) => r.action && r.action !== "none" && r.action !== "hold");
  rows.sort((a, b) => Math.abs(b.drift_pct || 0) - Math.abs(a.drift_pct || 0));
  const head = el("div", "subhead", final ? "Resulting rebalance recommendation" : "Preview rebalance (if applied)");
  wrap.appendChild(head);
  wrap.innerHTML +=
    `<div class="reb-meta">` +
    `<span>invested ${sensitive(`${fmtCZK(plan.invested)} ${esc(plan.currency || "")}`, "invested book")}</span>` +
    `<span>cash target ${esc(plan.cash_target_pct)}%</span>` +
    `<span>${rows.length} actionable name(s)</span>` +
    `</div>`;
  if (!rows.length) {
    wrap.innerHTML += `<div class="hint">Every targeted name is inside its band — no trades suggested.</div>`;
    return wrap;
  }
  const tbl = el("table", "strat-plan");
  tbl.innerHTML = `<thead><tr><th>Symbol</th><th>Status</th><th>Drift</th><th>Action</th><th>Suggested</th></tr></thead>`;
  const body = el("tbody");
  rows.slice(0, 20).forEach((r) => {
    const tr = el("tr");
    tr.innerHTML =
      `<td><strong>${esc(r.name || r.key)}</strong></td>` +
      `<td>${esc(r.status)}</td>` +
      `<td>${esc(fmtSignedWeight(r.drift_pct))}</td>` +
      `<td>${esc(r.action)}</td>` +
      `<td>${sensitive(`${fmtCZK(r.suggest_delta_czk)} ${esc(plan.currency || "")}`, "suggested trade")}</td>`;
    body.appendChild(tr);
  });
  tbl.appendChild(body);
  wrap.appendChild(tbl);
  return wrap;
}

function stateLabel(s) {
  return ({
    draft_running: "drafting",
    awaiting_segment_approval: "needs segment approval",
    synthesis_running: "synthesizing",
    needs_login: "needs login",
    awaiting_proposal_approval: "needs approval",
    applying: "applying",
    done: "done",
    error: "failed",
  })[s] || s || "";
}

// All DOM wiring is deferred to initStrategy(), called once from main()'s boot,
// to avoid the shell<->strategy import-cycle TDZ trap (see shell.ts).
function initStrategy() {
  const go = $("#strat-go");
  if (go) go.addEventListener("click", startRun);
  const dir = $("#strat-direction");
  if (dir) dir.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); startRun(); } });

  // Stepper navigation: clicking a reached step pins it for read-only viewing;
  // clicking the live step (or "Back to current step") unpins and follows along.
  const stages = $("#strat-stages");
  if (stages) stages.addEventListener("click", (e) => {
    const li = e.target.closest ? e.target.closest("li") : null;
    if (!li || !li.classList.contains("clickable") || !_lastM) return;
    _viewStage = li.dataset.stage === liveStage(_lastM) ? null : li.dataset.stage;
    render(_lastM);
  });
}

export { initStrategy, loadStrategy };
