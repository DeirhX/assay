// @ts-nocheck
import { $, api, esc, state } from "./core";
import { loadDeepRun, refreshDeepRuns, refreshLoginStatus } from "./errors";
import { loadSegmentList, renderSegment } from "./segment";
import { pushNav, setActiveView, setSegmentControls } from "./shell";

// ---- pipeline -------------------------------------------------------------
// The pipeline is a strict sequence, not four free-floating panels. You may
// always step BACK to revisit earlier work, but you can only advance to a step
// once its prerequisite exists. The reachable frontier is derived from real
// data, so it stays honest no matter how you got here (URL, reload, back/fwd).
//   1 Segment      -> always available
//   2 Deep Research, 3 Report  -> need a chosen/approved segment
//   4 Review & apply           -> need a saved or loaded report artifact
function pipeCurrentStem() {
  const seg = pipeSegment();
  const date = ($("#pipe-date").value || "").trim();
  return seg && date ? `${seg}-${date}` : "";
}

// Step 4 needs a report saved on disk for THIS exact segment + date — that is
// the only thing the review gate can actually read. Anything weaker (a sticky
// "a run was loaded once" flag) lets you switch to an empty segment and hit a
// dead gate, which is exactly the bug being fixed.
function pipeHasSavedReport() {
  const stem = pipeCurrentStem();
  return !!stem && state.savedRuns.has(stem);
}

function pipeUnlockedMax() {
  if (pipeHasSavedReport()) return 4;
  if (pipeSegment()) return 3;
  return 1;
}

function pipeLockReason(n) {
  if (n >= 4) return "Save or import a report for this segment + date first — the review gate has nothing to read otherwise.";
  if (n >= 2) return "Choose or approve a segment on Step 1 first.";
  return "";
}

let _pipeLockTimer = null;
function showPipeLock(n) {
  const note = $("#pipe-lock-note");
  if (!note) return;
  note.textContent = pipeLockReason(n);
  note.hidden = false;
  clearTimeout(_pipeLockTimer);
  _pipeLockTimer = setTimeout(() => { note.hidden = true; }, 4500);
}

function setPipeStep(n, { silent = false } = {}) {
  n = Math.max(1, Math.min(4, Number(n) || 1));
  const max = pipeUnlockedMax();
  if (n > max) {
    if (!silent) showPipeLock(n);
    n = max;
  } else if (!silent) {
    const note = $("#pipe-lock-note");
    if (note) note.hidden = true;
  }
  state.pipeStep = n;
  document.querySelectorAll("#pipe-wizard .wizard-step").forEach((s) => {
    s.classList.toggle("active", Number(s.dataset.step) === n);
  });
  document.querySelectorAll("#pipe-stepper .step-pill").forEach((p) => {
    const s = Number(p.dataset.step);
    p.classList.toggle("active", s === n);
    p.classList.toggle("done", s < n);
    p.classList.toggle("locked", s > max);
  });
  if (n === 2) { updateStep2LoginGate(); refreshLoginStatus(); updateExistingReportNotice(); }
  if (n === 3) updateRepSubstate();
  const w = $("#pipe-wizard");
  if (w && !silent) w.scrollIntoView({ behavior: "smooth", block: "start" });
}

// Re-evaluate the locked frontier after data changes (segment picked, report
// saved/loaded, pipeline reset) without forcing a navigation.
function refreshPipeLocks() {
  setPipeStep(state.pipeStep, { silent: true });
}

document.querySelectorAll("#pipe-stepper .step-pill").forEach((p) => {
  p.addEventListener("click", () => {
    const s = Number(p.dataset.step);
    if (s > pipeUnlockedMax()) { showPipeLock(s); return; }
    setPipeStep(s);
  });
});
document.querySelectorAll("#pipe-wizard .step-next, #pipe-wizard .step-back").forEach((b) => {
  b.addEventListener("click", () => {
    const goto = Number(b.dataset.goto);
    if (b.classList.contains("step-next") && goto > pipeUnlockedMax()) { showPipeLock(goto); return; }
    setPipeStep(goto);
  });
});

$("#pipe-restart").addEventListener("click", () => {
  state.currentDeepRun = null;
  state.repManual = false;
  ["#pipe-report", "#pipe-sources", "#pipe-source-url", "#pipe-prompt"].forEach((sel) => {
    const elx = $(sel);
    if (elx) elx.value = "";
  });
  updateStep2Actions();
  setRepMode("current");
  pushNav({ view: "pipeline", segment: pipeSegment() }, { replace: true });
  setPipeStep(1);
});

$("#pipe-segment-select").addEventListener("change", () => {
  pushNav({ view: "pipeline", segment: pipeSegment() }, { replace: true });
  refreshPipeLocks();
  updateExistingReportNotice();
});


async function loadPipeline() {
  await loadSegmentList();
  // A launch from the Analyses pane stashes the segment to preselect here, since
  // the dropdown only exists after loadSegmentList has populated it.
  if (state.pipePreselect) {
    setSegmentControls(state.pipePreselect);
    state.pipePreselect = null;
  }
  await refreshDeepRuns();
  refreshLoginStatus();
  setSegMode(state.segMode);
  setRepMode(state.repMode);
  updateStep2Actions();
  setPipeStep(state.pipeStep);
  if (!$("#pipe-date").value) $("#pipe-date").value = new Date().toISOString().slice(0, 10);
}

// Step 1 shows exactly one path at a time: an approved-segment dropdown, or a
// new-segment drafter that only reveals its editor + approve action after a draft.
function setSegMode(mode) {
  mode = mode === "new" ? "new" : "existing";
  state.segMode = mode;
  $("#seg-pane-existing").hidden = mode !== "existing";
  $("#seg-pane-new").hidden = mode !== "new";
  $("#seg-mode-existing").classList.toggle("active", mode === "existing");
  $("#seg-mode-new").classList.toggle("active", mode === "new");
  const cont = $("#pipe-step1-continue");
  const note = $("#pipe-step1-note");
  if (mode === "existing") {
    cont.hidden = false;
    note.textContent = "Pick a segment, then continue.";
  } else {
    // In "new" mode the single forward action is Approve & continue, revealed
    // only after a draft exists -- so the footer Continue is out of the way.
    cont.hidden = true;
    note.textContent = "Draft a theme, review it, then approve to continue.";
    $("#seg-draft-editor").hidden = !$("#pipe-slug").value.trim();
  }
}

$("#seg-mode-existing").addEventListener("click", () => setSegMode("existing"));
$("#seg-mode-new").addEventListener("click", () => setSegMode("new"));

// Step 3 is one-lane too: review the report this run produced (or paste one you
// ran yourself), OR import an existing run. Never both at once.
function setRepMode(mode) {
  mode = mode === "import" ? "import" : "current";
  state.repMode = mode;
  $("#rep-pane-current").hidden = mode !== "current";
  $("#rep-pane-import").hidden = mode !== "import";
  $("#rep-mode-current").classList.toggle("active", mode === "current");
  $("#rep-mode-import").classList.toggle("active", mode === "import");
  updateRepSubstate();
}

// A report "result" is known for the current segment + date once a run/import/
// load has populated the report body and tagged it as the current run.
function pipeHasRunResult() {
  const stem = pipeCurrentStem();
  return !!stem && state.currentDeepRun === stem && !!($("#pipe-report").value || "").trim();
}

// Step 3 "This run's report" is itself step-by-step: until a report actually
// exists, show only the run action and keep the finished-report fields hidden.
// The Perplexity URL is read-only for an automated run (it is filled by the
// run); only a manual "I ran it elsewhere" paste makes it editable. Continue to
// Review stays blocked until a report is saved on disk.
function updateRepSubstate() {
  const pending = $("#rep-current-pending");
  const done = $("#rep-current-done");
  if (!pending || !done) return;
  const hasResult = pipeHasRunResult() || state.repManual;
  pending.hidden = hasResult;
  done.hidden = !hasResult;
  const url = $("#pipe-source-url");
  if (url) {
    const editable = state.repManual && !pipeHasRunResult();
    url.toggleAttribute("readonly", !editable);
  }
  const next = $("#pipe-step3-next");
  if (next) {
    const ok = pipeHasSavedReport();
    next.disabled = !ok;
    next.title = ok ? "" : pipeLockReason(4);
  }
}

$("#rep-mode-current").addEventListener("click", () => setRepMode("current"));
$("#rep-mode-import").addEventListener("click", () => setRepMode("import"));

function pipeSegment() {
  if (state.segMode === "new") return $("#pipe-slug").value.trim() || $("#pipe-segment-select").value;
  return $("#pipe-segment-select").value || $("#pipe-slug").value.trim();
}

function parseJsonField(sel, fallback) {
  const raw = $(sel).value.trim();
  if (!raw) return fallback;
  return JSON.parse(raw);
}

$("#pipe-draft").addEventListener("click", async () => {
  const status = $("#pipe-segment-status");
  status.classList.remove("err");
  status.textContent = "drafting...";
  try {
    const rec = await api("/api/segment-draft", "POST", { query: $("#pipe-query").value });
    $("#pipe-slug").value = rec.slug;
    $("#pipe-segment-json").value = JSON.stringify(rec.definition, null, 2);
    $("#pipe-prompt").value = rec.llm_prompt || "";
    $("#seg-draft-editor").hidden = false;
    status.textContent = rec.warnings && rec.warnings.length ? rec.warnings.join(" ") : "draft ready; review it, then approve to continue";
  } catch (e) {
    status.textContent = "draft failed: " + e.message;
    status.classList.add("err");
  }
});

$("#pipe-save-segment").addEventListener("click", async () => {
  const status = $("#pipe-segment-status");
  status.classList.remove("err");
  status.textContent = "saving segment...";
  try {
    const slug = $("#pipe-slug").value.trim();
    const definition = parseJsonField("#pipe-segment-json", {});
    definition.status = "approved";
    const rec = await api("/api/segment-def/" + encodeURIComponent(slug), "POST", { definition });
    status.textContent = `saved ${rec.name} — continuing to Deep Research`;
    await loadSegmentList();
    $("#pipe-segment-select").value = rec.name;
    setSegMode("existing");
    pushNav({ view: "pipeline", segment: rec.name }, { replace: true });
    setPipeStep(2);
  } catch (e) {
    status.textContent = "save failed: " + e.message;
    status.classList.add("err");
  }
});

// Step 2 shows one primary action at a time: "Build prompt" until a prompt
// exists, then "Run Deep Research". Rebuild + deterministic pull are secondary.
function updateStep2Actions() {
  const hasPrompt = !!$("#pipe-prompt").value.trim();
  $("#pipe-build-prompt").hidden = hasPrompt;
  $("#pipe-run-deep").hidden = !hasPrompt;
  $("#pipe-rebuild-prompt").hidden = !hasPrompt;
}

// Most recent saved run for `seg` that actually has a report on disk. Stems are
// `${seg}-YYYY-MM-DD`; the date check stops a segment like "ai" from matching
// "ai-software-...". Lexical desc sort on the stem orders by date newest-first.
function latestReportForSegment(seg) {
  if (!seg) return null;
  const prefix = seg + "-";
  const matches = (state.deepRuns || [])
    .filter((r) => r.files && r.files.report && r.stem.startsWith(prefix)
      && /^\d{4}-\d{2}-\d{2}$/.test(r.stem.slice(prefix.length)))
    .sort((a, b) => (a.stem < b.stem ? 1 : -1));
  return matches[0] || null;
}

// Deep Research spends quota, so if we already have a report for this segment,
// surface it on Step 2 and let the user reuse it instead of running a new one.
// This needs no login (reuse is read-only), so it sits above the login gate.
function updateExistingReportNotice() {
  const box = $("#pipe-existing");
  if (!box) return;
  const run = latestReportForSegment(pipeSegment());
  if (!run) { box.hidden = true; box.dataset.stem = ""; return; }
  const date = (run.stem.match(/-(\d{4}-\d{2}-\d{2})$/) || [])[1] || "";
  box.dataset.stem = run.stem;
  $("#pipe-existing-text").textContent =
    `This segment already has a saved Deep Research report${date ? ` from ${date}` : ""}. Reuse it instead of spending a new run?`;
  box.hidden = false;
}

$("#pipe-existing-use").addEventListener("click", async () => {
  const stem = $("#pipe-existing").dataset.stem;
  if (!stem) return;
  await loadDeepRun(stem);
  setPipeStep(3);
});

// Deep Research only works through a logged-in Perplexity session. When we are
// not logged in, block the prompt workflow behind the login gate and insist the
// user sets it up first. The deterministic pull and the Step 3 import path stay
// reachable, so this gates the prompt, not the whole step.
function updateStep2LoginGate() {
  const gate = $("#pipe-login-gate");
  const area = $("#pipe-prompt-area");
  const blocked = !state.pplxLoggedIn;
  if (gate) gate.hidden = !blocked;
  if (area) area.hidden = blocked;
  if (!blocked) { updateStep2Actions(); maybeAutoBuildPrompt(); }
}

// Step 2 builds the prompt for you the moment you land on it (and rebuilds it if
// you arrived with a different segment than the one the current prompt is for).
// The textarea is just there to tweak the result before running. "Build prompt"
// stays as a manual fallback for when auto-build fails. A manual edit for the
// same segment is preserved (not clobbered) because the prompt is non-empty and
// not stale.
async function maybeAutoBuildPrompt() {
  if (state.pipeStep !== 2 || !state.pplxLoggedIn || state._autoBuilding) return;
  const seg = pipeSegment();
  if (!seg) return;
  const stale = !!state.promptSegment && state.promptSegment !== seg;
  if ($("#pipe-prompt").value.trim() && !stale) return;
  state._autoBuilding = true;
  try { await buildPrompt(); } finally { state._autoBuilding = false; }
}

async function buildPrompt() {
  const status = $("#pipe-prompt-status");
  const seg = pipeSegment();
  status.classList.remove("err");
  if (!state.pplxLoggedIn) {
    updateStep2LoginGate();
    $("#pipe-login-gate-status").textContent = "Set up the Perplexity login first.";
    return;
  }
  if (!seg) {
    status.classList.add("err");
    status.textContent = "pick or approve a segment on Step 1 first";
    return;
  }
  status.textContent = "building prompt...";
  try {
    const rec = await api("/api/deep-prompt?segment=" + encodeURIComponent(seg));
    $("#pipe-date").value = rec.date;
    $("#pipe-prompt").value = rec.prompt;
    state.promptSegment = rec.segment || seg;
    pushNav({ view: "pipeline", segment: rec.segment || seg }, { replace: true });
    status.textContent = "prompt ready — review it, then run Deep Research";
    updateStep2Actions();
  } catch (e) {
    status.textContent = "prompt failed: " + e.message;
    status.classList.add("err");
  }
}

$("#pipe-build-prompt").addEventListener("click", buildPrompt);
$("#pipe-rebuild-prompt").addEventListener("click", buildPrompt);
$("#pipe-prompt").addEventListener("input", updateStep2Actions);

$("#pipe-run-deterministic").addEventListener("click", async () => {
  const status = $("#pipe-prompt-status");
  const name = pipeSegment();
  status.classList.remove("err");
  status.innerHTML = `<span class="spinner"></span> Pulling deterministic data for ${esc(name)}...`;
  try {
    const rec = await api("/api/pull-segment/" + encodeURIComponent(name), "POST");
    status.textContent = `pulled ${rec.members.length} names`;
    pushNav({ view: "segment", segment: name });
    setActiveView("segment");
    renderSegment(rec);
  } catch (e) {
    status.textContent = "pull failed: " + e.message;
    status.classList.add("err");
  }
});

export {
  pipeCurrentStem,
  pipeHasSavedReport,
  pipeUnlockedMax,
  pipeLockReason,
  _pipeLockTimer,
  showPipeLock,
  setPipeStep,
  refreshPipeLocks,
  loadPipeline,
  setSegMode,
  setRepMode,
  pipeHasRunResult,
  updateRepSubstate,
  pipeSegment,
  parseJsonField,
  updateStep2Actions,
  latestReportForSegment,
  updateExistingReportNotice,
  updateStep2LoginGate,
  maybeAutoBuildPrompt,
  buildPrompt,
};
