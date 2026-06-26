// Guided "Direction -> Rebalance" flow. One screen drives a server-side
// orchestrated run (tools/orchestrate.py): you type a direction, the server
// drafts a segment, runs Deep Research, synthesizes target-model bands, and
// pauses at two human gates before showing the rebalance recommendation. The
// durable state lives in the run manifest; this view is a thin renderer over
// GET /api/strategy/{run_id}, polled while a leg is running.
import { starHtml } from "./basket";
import { $, api, el, esc, fmtCZK, fmtSignedWeight, relAge, sensitive } from "./core";
import { openDeepRunInPipeline, pushNav, setActiveView } from "./shell";

// ---- manifest shapes (GET /api/strategy/{run_id}) -------------------------
// A target band, with the rule token that produced it.
interface Band {
  low?: number | null;
  high?: number | null;
  rule?: string;
}

// One drafted segment member (Gate 1) / Deep Research candidate.
interface SegMember {
  symbol?: string;
  confidence?: string;
  sleeve?: string;
  rationale?: string;
}

interface SegmentDef {
  members?: SegMember[];
  comment?: string;
  [key: string]: unknown;
}

// One synthesized target-model change (Gate 2).
interface Change {
  symbol?: string;
  conviction?: string;
  action?: string;
  current_target?: Band;
  proposed_target?: Band;
  rationale?: string;
}

interface ConstructMeta {
  segment_budget_pct?: number | string;
  sized_midpoint_total_pct?: number | string;
}

interface Proposal {
  changes?: Change[];
  blocked_symbols?: string[];
  construct_meta?: ConstructMeta;
}

interface PlanMember {
  symbol?: string;
}

// One row of the compact rebalance preview (a target name or a sleeve basket).
interface PlanRow {
  kind?: string;
  name?: string;
  key?: string;
  action?: string;
  status?: string;
  drift_pct?: number | null;
  suggest_delta_czk?: number | null;
  members?: PlanMember[];
}

interface Plan {
  rows?: PlanRow[];
  invested?: number | null;
  currency?: string;
  cash_target_pct?: number | string | null;
}

interface Preview {
  available?: boolean;
  reason?: string;
  plan?: Plan;
  counts?: { total?: number | string };
}

interface SkippedName {
  symbol?: string;
  reason?: string;
}

interface Applied {
  applied?: string[];
  skipped?: SkippedName[];
  backup?: string;
}

// Result of staging a proposal into the shared working draft (the "staged" state).
interface Staged {
  applied?: string[];
  skipped?: SkippedName[];
}

interface Review {
  blocked_symbols?: string[];
  source_summary?: unknown;
  findings?: unknown;
}

interface Draft {
  definition?: SegmentDef;
  warnings?: string[];
}

// The run manifest — the single object every renderer reads from.
interface Manifest {
  run_id?: string;
  state: string;
  message?: string;
  error?: string;
  segment?: string;
  date?: string;
  draft?: Draft;
  review?: Review | null;
  proposal?: Proposal;
  preview?: Preview;
  applied?: Applied;
  staged?: Staged;
}

// A summary row in the recent-runs list (GET /api/strategy/runs).
interface RunSummary {
  run_id: string;
  state?: string;
  direction?: string;
  segment?: string;
  created_at?: string;
  updated_at?: string;
}

// States in which a background leg is working and we should keep polling.
const RUNNING = new Set(["draft_running", "synthesis_running", "applying"]);
// Progress-tracker stages, in order, mapped from the manifest state.
const STAGE_ORDER = ["draft", "segment", "research", "synthesize", "review", "done"];
const STATE_STAGE: Record<string, string> = {
  draft_running: "draft",
  awaiting_segment_approval: "segment",
  synthesis_running: "synthesize",
  needs_login: "research",
  awaiting_proposal_approval: "review",
  applying: "review",
  staged: "done",
  done: "done",
  error: "draft",
};

let _activeRunId: string | null = null;
let _pollTimer: ReturnType<typeof setTimeout> | null = null;
// The user can revisit any step the run has already reached. `_viewStage` pins a
// past step for read-only viewing; null means "follow the live step" (the one the
// run is actually working on or parked at). `_lastM` caches the last manifest so
// a stepper click can re-render instantly without re-fetching.
let _viewStage: string | null = null;
let _lastM: Manifest | null = null;

const STAGE_TITLE: Record<string, string> = {
  draft: "Draft", segment: "Segment", research: "Research",
  synthesize: "Synthesize", review: "Review", done: "Recommendation",
};

// The stage the run is currently on, derived from its state.
const liveStage = (m: Manifest) => STATE_STAGE[m.state] || "draft";
// Stages the user may click into: everything up to and including the live one.
// (Nothing is revisitable while errored — the run never produced those steps.)
function reachedStages(m: Manifest) {
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

function schedulePoll(runId: string) {
  stopPolling();
  _pollTimer = setTimeout(() => refreshOnce(runId), 3000);
}

async function refreshOnce(runId: string) {
  if (_activeRunId !== runId) return;  // navigated away
  let m: Manifest;
  try {
    m = await api<Manifest>("/api/strategy/" + encodeURIComponent(runId));
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
  const dir = $<HTMLInputElement>("#strat-direction").value.trim();
  const status = $("#strat-status");
  status.classList.remove("err");
  if (!dir) { status.classList.add("err"); status.textContent = "describe a direction first"; return; }
  const btn = $<HTMLButtonElement>("#strat-go");
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
    const { runs } = await api<{ runs?: RunSummary[] }>("/api/strategy/runs");
    if (!runs || !runs.length) { box.innerHTML = ""; return; }
    box.innerHTML = `<div class="subhead">Recent runs</div>` + runs.slice(0, 6).map((r) => {
      const age = relAge(r.updated_at || r.created_at);
      const metaBits: string[] = [];
      if (age) metaBits.push(esc(age));
      if (r.segment) metaBits.push(esc(r.segment));
      const meta = metaBits.length ? `<span class="strat-recent-meta">${metaBits.join(" · ")}</span>` : "";
      return `<button type="button" class="strat-recent-item" data-run="${esc(r.run_id)}">` +
        `<span class="strat-recent-main">` +
          `<span class="strat-recent-dir">${esc(r.direction || "(no direction)")}</span>${meta}` +
        `</span>` +
        recentStateBadge(r.state) +
      `</button>`;
    }).join("");
    box.querySelectorAll<HTMLElement>(".strat-recent-item").forEach((b) => {
      b.addEventListener("click", () => { pushNav({ view: "strategy", run: b.dataset.run }); loadStrategy(); });
    });
  } catch (_e) {
    box.innerHTML = "";
  }
}

// ---- render dispatch ------------------------------------------------------
function render(m: Manifest) {
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
function renderLiveStage(m: Manifest, panel: HTMLElement) {
  if (m.state === "awaiting_segment_approval") return renderSegmentGate(m, panel);
  if (m.state === "awaiting_proposal_approval") return renderProposalGate(m, panel);
  if (m.state === "needs_login") return renderNeedsLogin(m, panel);
  if (m.state === "staged") return renderStaged(m, panel);
  if (m.state === "done") return renderDone(m, panel);
  // Any running state: a spinner with the live message.
  renderLoading(m.message || m.state);
}

function renderStages(m: Manifest) {
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
function renderPastStage(m: Manifest, stage: string, panel: HTMLElement) {
  const renderer = (({
    draft: renderDraftStep, segment: renderSegmentStep, research: renderResearchStep,
    synthesize: renderSynthesizeStep, review: renderReviewStep, done: renderDone,
  }) as Record<string, (m: Manifest, panel: HTMLElement) => void>)[stage]
    || ((mm: Manifest) => renderLoading(mm.message || mm.state));
  renderer(m, panel);
  panel.insertBefore(viewingBar(m, stage), panel.firstChild);
}

function viewingBar(m: Manifest, stage: string) {
  const bar = el("div", "strat-viewing-bar");
  bar.innerHTML = `<span>Viewing the <strong>${esc(STAGE_TITLE[stage] || "completed")}</strong> step (read-only).</span>`;
  const btn = el("button", "ghost", `Back to current step: ${esc(STAGE_TITLE[liveStage(m)] || "")} →`);
  btn.type = "button";
  btn.addEventListener("click", () => { _viewStage = null; if (_lastM) render(_lastM); });
  bar.appendChild(btn);
  return bar;
}

function memberTable(definition: SegmentDef) {
  const members = (definition && definition.members) || [];
  const tbl = el("table", "strat-changes");
  tbl.innerHTML = `<thead><tr><th>Symbol</th><th>Conviction</th><th>Sleeve</th><th>Rationale</th></tr></thead>`;
  const body = el("tbody");
  if (!members.length) body.innerHTML = `<tr><td colspan="4" class="muted">No members recorded.</td></tr>`;
  members.forEach((mem: SegMember) => {
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

function renderDraftStep(m: Manifest, panel: HTMLElement) {
  const def: SegmentDef = (m.draft && m.draft.definition) || {};
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

function renderSegmentStep(m: Manifest, panel: HTMLElement) {
  const def: SegmentDef = (m.draft && m.draft.definition) || {};
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

function renderResearchStep(m: Manifest, panel: HTMLElement) {
  const review: Review = m.review || {};
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
      // Land on the run's review gate, not the Step 1 segment chooser.
      void openDeepRunInPipeline(`${m.segment}-${m.date}`);
    });
    actions.appendChild(open);
    card.appendChild(actions);
  }
  panel.appendChild(card);
}

function renderSynthesizeStep(m: Manifest, panel: HTMLElement) {
  const proposal: Proposal = m.proposal || {};
  const meta: ConstructMeta = proposal.construct_meta || {};
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

function renderReviewStep(m: Manifest, panel: HTMLElement) {
  const proposal: Proposal = m.proposal || {};
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

function renderLoading(msg: string) {
  $("#strat-panel").innerHTML =
    `<div class="card strat-running"><span class="spinner"></span> ${esc(msg || "working…")}</div>`;
}

function renderError(msg: string) {
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
function renderSegmentGate(m: Manifest, panel: HTMLElement) {
  const draft: Draft = m.draft || {};
  const definition: SegmentDef = draft.definition || {};
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
  $<HTMLTextAreaElement>("#strat-seg-json").value = JSON.stringify(definition, null, 2);
  $("#strat-approve-seg").addEventListener("click", () => approveSegment(m.run_id));
}

async function approveSegment(runId: string) {
  const status = $("#strat-seg-status");
  const btn = $<HTMLButtonElement>("#strat-approve-seg");
  status.classList.remove("err");
  let definition;
  try {
    definition = JSON.parse($<HTMLTextAreaElement>("#strat-seg-json").value);
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
const RULE_LABEL: Record<string, string> = {
  trim_only: "trim only", do_not_add: "don't add", reduce: "reduce",
  avoid: "avoid", accumulate: "accumulate", hold: "hold", wait: "wait",
};
// Semantic tone for an action/rule token so buy- / hold- / sell-leaning cells
// read at a glance (green / grey / amber / red) instead of as identical text.
const TONE: Record<string, string> = {
  accumulate: "pos", add: "pos", buy: "pos",
  hold: "neutral", wait: "neutral",
  reduce: "caution", trim: "caution", trim_only: "caution", do_not_add: "caution",
  avoid: "neg", sell: "neg", exit: "neg",
};
const toneOf = (token: string | null | undefined) => TONE[token || ""] || TONE[(token || "").replace("_target", "")] || "neutral";
// Above its band => overweight (trim side, amber); below => underweight (buy side, green).
const statusTone = (s: string | null | undefined) => {
  const t = (s || "").toLowerCase();
  if (t.includes("above")) return "caution";
  if (t.includes("below")) return "pos";
  return "neutral";
};
// Positive drift = heavy (trim side); negative = light (buy side). Same colour story.
const driftTone = (d: number | null | undefined) => (typeof d === "number" && d > 0 ? "caution" : typeof d === "number" && d < 0 ? "pos" : "neutral");
const bandStr = (t: Band | null | undefined) => (t && t.low != null ? `${t.low}–${t.high}%` : "—");
// Render a symbol as a deep-dive link. The global a.tlink click handler in shell
// intercepts it and calls openTicker (which live-pulls on a miss); the href is a
// fallback for middle-click / open-in-new-tab.
const symLink = (sym: string | null | undefined) => {
  if (!sym) return "—";
  const s = esc(sym);
  return `<a class="tlink" data-ticker="${s}" href="?view=deepdive&ticker=${encodeURIComponent(sym)}" title="Open ${s} deep-dive"><strong>${s}</strong></a>`;
};

function renderProposalGate(m: Manifest, panel: HTMLElement) {
  const proposal: Proposal = m.proposal || {};
  const changes = proposal.changes || [];
  const blocked = proposal.blocked_symbols || [];
  panel.innerHTML = "";
  const card = el("div", "card strat-gate");
  const meta: ConstructMeta = proposal.construct_meta || {};
  card.innerHTML =
    `<h3>Gate 2 · Approve target-model changes</h3>` +
    `<p class="hint">Synthesized bands for ${changes.length} name(s). Budget ${esc(meta.segment_budget_pct ?? "?")}% of book, ` +
    `sized total ${esc(meta.sized_midpoint_total_pct ?? "?")}%. Review each band before approving — this STAGES the changes into the working draft (it does not write your live portfolio; you commit the draft later).</p>` +
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
    `<button class="primary" id="strat-approve-prop" type="button">Add to working draft →</button>` +
    allowBlockedHtml +
    `<span class="status" id="strat-prop-status"></span>`;
  card.appendChild(actions);

  panel.appendChild(card);
  $<HTMLTextAreaElement>("#strat-changes-json").value = JSON.stringify(changes, null, 2);
  $("#strat-approve-prop").addEventListener("click", () => approveProposal(m.run_id));
}

function changesTable(changes: Change[]) {
  const tbl = el("table", "strat-changes");
  tbl.innerHTML =
    `<thead><tr><th></th><th>Symbol</th><th>Conviction</th><th>Action</th>` +
    `<th>Current</th><th>Proposed</th><th>Rule</th><th>Rationale</th></tr></thead>`;
  const body = el("tbody");
  if (!changes.length) {
    body.innerHTML = `<tr><td colspan="8" class="muted">No target changes proposed.</td></tr>`;
  }
  changes.forEach((c: Change) => {
    const tr = el("tr");
    const conv = c.conviction || "";
    const actRaw = c.action || "";
    const act = actRaw.replace("_target", "");
    const rule = (c.proposed_target && c.proposed_target.rule) || "";
    tr.innerHTML =
      `<td class="strat-star-cell">${c.symbol ? starHtml(c.symbol, "strategy") : ""}</td>` +
      `<td>${symLink(c.symbol)}</td>` +
      `<td><span class="strat-conv strat-conv-${esc(conv)}">${esc(conv || "—")}</span></td>` +
      `<td>${act ? `<span class="strat-tag strat-tag-${toneOf(actRaw)}">${esc(act)}</span>` : "—"}</td>` +
      `<td class="strat-cur">${esc(bandStr(c.current_target))}</td>` +
      `<td><span class="strat-band">${esc(bandStr(c.proposed_target))}</span></td>` +
      `<td>${rule ? `<span class="strat-tag strat-tag-${toneOf(rule)}">${esc(RULE_LABEL[rule] || rule)}</span>` : "—"}</td>` +
      `<td class="strat-rationale">${esc(c.rationale || "")}</td>`;
    body.appendChild(tr);
  });
  tbl.appendChild(body);
  return tbl;
}

async function approveProposal(runId: string) {
  const status = $("#strat-prop-status");
  const btn = $<HTMLButtonElement>("#strat-approve-prop");
  status.classList.remove("err");
  let changes;
  try {
    changes = JSON.parse($<HTMLTextAreaElement>("#strat-changes-json").value);
  } catch (e) {
    status.classList.add("err");
    status.textContent = "invalid changes JSON: " + e.message;
    return;
  }
  const allowBlocked = !!($<HTMLInputElement>("#strat-allow-blocked") && $<HTMLInputElement>("#strat-allow-blocked").checked);
  btn.disabled = true;
  status.innerHTML = `<span class="spinner"></span> staging…`;
  try {
    const m = await api("/api/strategy/" + encodeURIComponent(runId) + "/approve-proposal", "POST",
      { changes, allow_blocked: allowBlocked });
    render(m);
  } catch (e) {
    status.classList.add("err");
    status.textContent = "could not stage: " + e.message;
    btn.disabled = false;
  }
}

// ---- staged ---------------------------------------------------------------
// Approving a proposal now lands here: the run's changes are in the shared
// working draft (composed with any other runs/edits), awaiting a single commit.
function renderStaged(m: Manifest, panel: HTMLElement) {
  const staged: Staged = m.staged || {};
  const diff: Preview = m.preview || {};
  const counts: { total?: number | string } = (diff && diff.counts) || {};
  const appliedN = (staged.applied || []).length;
  const skipped = staged.skipped || [];
  panel.innerHTML = "";
  const card = el("div", "card strat-done");
  card.innerHTML =
    `<h3>✓ Staged into the working draft</h3>` +
    `<p class="hint">${esc(m.message || "")}</p>` +
    `<p>Added ${appliedN} change(s) from this run. The working draft now holds ` +
    `<strong>${esc(counts.total ?? "?")}</strong> pending change(s) across all runs and edits.</p>` +
    (skipped.length
      ? `<p class="muted">Skipped: ${skipped.map((s) => esc(s.symbol) + " (" + esc(s.reason) + ")").join("; ")}.</p>` : "");
  const actions = el("div", "thesis-actions");
  const goDraft = el("button", "primary", "Review working draft →");
  goDraft.type = "button";
  goDraft.addEventListener("click", () => { pushNav({ view: "working-draft" }); setActiveView("working-draft"); });
  const restart = el("button", "ghost", "New run ↺");
  restart.type = "button";
  restart.addEventListener("click", () => { pushNav({ view: "strategy" }); loadStrategy(); });
  actions.appendChild(goDraft);
  actions.appendChild(restart);
  card.appendChild(actions);
  panel.appendChild(card);
}

// ---- needs login ----------------------------------------------------------
function renderNeedsLogin(m: Manifest, panel: HTMLElement) {
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

async function approveSegmentResume(runId: string) {
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
function renderDone(m: Manifest, panel: HTMLElement) {
  const applied: Applied = m.applied || {};
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
  const restart = el("button", "ghost", "New run ↺");
  restart.type = "button";
  restart.addEventListener("click", () => { pushNav({ view: "strategy" }); loadStrategy(); });
  actions.appendChild(goReb);
  actions.appendChild(restart);
  card.appendChild(actions);
  panel.appendChild(card);
}

// ---- shared: compact rebalance preview ------------------------------------
function previewBlock(preview: Preview | null | undefined, { final = false }: { final?: boolean } = {}) {
  const wrap = el("div", "strat-preview");
  if (!preview || !preview.available) {
    wrap.innerHTML = `<div class="hint">${esc((preview && preview.reason) || "No rebalance preview available (need a target model and a holdings snapshot).")}</div>`;
    return wrap;
  }
  const plan: Plan = preview.plan || {};
  const rows = (plan.rows || []).filter((r: PlanRow) => r.action && r.action !== "none" && r.action !== "hold");
  rows.sort((a: PlanRow, b: PlanRow) => Math.abs(b.drift_pct || 0) - Math.abs(a.drift_pct || 0));
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
  // Names that appear as their own target row anywhere in the plan (not just the
  // actionable subset). Used to flag a sleeve member that is ALSO targeted on its
  // own line — the overlap that otherwise reads as a double-counted ticker.
  const targetNames = new Set(
    (plan.rows || []).filter((r: PlanRow) => r.kind === "target").map((r: PlanRow) => r.name));
  const hasSleeve = rows.some((r: PlanRow) => r.kind === "sleeve");

  const nameCellHtml = (r: PlanRow) => {
    if (r.kind !== "sleeve") {
      // A standalone ticker that's also bundled inside a sleeve gets a quiet tag
      // so the same name showing up twice in the table is explained, not magic.
      const dup = (plan.rows || []).some(
        (o: PlanRow) => o.kind === "sleeve" && (o.members || []).some((m: PlanMember) => m.symbol === r.name));
      return symLink(r.name || r.key) +
        (dup ? ` <span class="strat-tag in-sleeve" title="Also part of a sleeve below — the sleeve's amount is separate from this one">in sleeve</span>` : "");
    }
    const members = r.members || [];
    const mem = members.map((m: PlanMember) => {
      const dup = targetNames.has(m.symbol);
      const s = esc(m.symbol);
      return `<a class="tlink strat-mem${dup ? " dup" : ""}" data-ticker="${s}" href="?view=deepdive&ticker=${encodeURIComponent(m.symbol)}"` +
        ` title="${dup ? s + " is also targeted on its own row — counted there too. Click to open." : "Open " + s + " deep-dive"}"` +
        `>${s}</a>`;
    }).join("");
    return `<div class="strat-sleeve-name"><strong>${esc(r.name || r.key)}</strong>` +
      `<span class="strat-tag sleeve" title="A basket of names governed by one combined band, not a tradable ticker">sleeve</span></div>` +
      (members.length ? `<div class="strat-mems">${mem}</div>` : "");
  };

  const tbl = el("table", "strat-plan");
  tbl.innerHTML = `<thead><tr><th>Symbol</th><th>Status</th><th>Drift</th><th>Action</th><th>Suggested</th></tr></thead>`;
  const body = el("tbody");
  rows.slice(0, 20).forEach((r: PlanRow) => {
    const isSleeve = r.kind === "sleeve";
    const tr = el("tr", isSleeve ? "strat-sleeve-row" : null);
    const suggested = sensitive(`${fmtCZK(r.suggest_delta_czk)} ${esc(plan.currency || "")}`, "suggested trade") +
      (isSleeve ? `<small class="strat-spread">spread across members</small>` : "");
    const statusCell = r.status
      ? `<span class="strat-tag strat-tag-${statusTone(r.status)}">${esc(r.status)}</span>` : "—";
    const actionCell = r.action
      ? `<span class="strat-tag strat-tag-${toneOf(r.action)}">${esc(r.action)}</span>` : "—";
    tr.innerHTML =
      `<td>${nameCellHtml(r)}</td>` +
      `<td>${statusCell}</td>` +
      `<td><span class="strat-drift ${driftTone(r.drift_pct)}">${esc(fmtSignedWeight(r.drift_pct))}</span></td>` +
      `<td>${actionCell}</td>` +
      `<td>${suggested}</td>`;
    body.appendChild(tr);
  });
  tbl.appendChild(body);
  wrap.appendChild(tbl);
  if (hasSleeve) {
    wrap.appendChild(el("p", "hint strat-sleeve-note",
      "A sleeve is a basket of names sharing one combined target band — its suggested amount " +
      "is for the whole group, spread across the listed members by hand, not a single trade. " +
      "A member shown in amber is also targeted on its own row, so it's counted in both places."));
  }
  return wrap;
}

function stateLabel(s: string | null | undefined) {
  return (({
    draft_running: "drafting",
    awaiting_segment_approval: "needs segment approval",
    synthesis_running: "synthesizing",
    needs_login: "needs login",
    awaiting_proposal_approval: "needs approval",
    applying: "applying",
    staged: "staged",
    done: "done",
    error: "failed",
  }) as Record<string, string>)[s || ""] || s || "";
}

// Colour-coded lifecycle pill for the recent-runs list: green = done, red =
// failed, amber = waiting on you, accent (with a pulsing dot) = a leg is working.
const STATE_TONE: Record<string, string> = {
  done: "ok",
  staged: "ok",
  error: "bad",
  awaiting_segment_approval: "warn",
  awaiting_proposal_approval: "warn",
  needs_login: "warn",
  draft_running: "run",
  synthesis_running: "run",
  applying: "run",
};
function recentStateBadge(state: string | null | undefined) {
  const tone = STATE_TONE[state || ""] || "muted";
  const running = tone === "run";
  const cls = running ? "accent" : tone;
  const dot = running ? '<span class="strat-recent-dot"></span>' : "";
  return `<span class="abadge ${cls} strat-recent-pill">${dot}${esc(stateLabel(state))}</span>`;
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
    const tgt = e.target as HTMLElement;
    const li = tgt.closest ? tgt.closest<HTMLElement>("li") : null;
    if (!li || !li.classList.contains("clickable") || !_lastM) return;
    _viewStage = li.dataset.stage === liveStage(_lastM) ? null : li.dataset.stage;
    render(_lastM);
  });
}

export { initStrategy, loadStrategy };
