import type { Job } from "./api-types";
import { $, api, esc, state } from "./core";
import { loadDeepRun, refreshDeepRuns, refreshLoginStatus, pollDeepJob, renderPipeReport } from "./errors";
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
  const date = ($<HTMLInputElement>("#pipe-date").value || "").trim();
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

function pipeLockReason(n: number) {
  if (n >= 4) return "Save or import a report for this segment + date first — the review gate has nothing to read otherwise.";
  if (n >= 2) return "Choose or approve a segment on Step 1 first.";
  return "";
}

let _pipeLockTimer: ReturnType<typeof setTimeout> | null = null;
function showPipeLock(n: number) {
  const note = $("#pipe-lock-note");
  if (!note) return;
  note.textContent = pipeLockReason(n);
  note.hidden = false;
  clearTimeout(_pipeLockTimer);
  _pipeLockTimer = setTimeout(() => { note.hidden = true; }, 4500);
}

function setPipeStep(n: number, { silent = false }: { silent?: boolean } = {}) {
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
  document.querySelectorAll<HTMLElement>("#pipe-wizard .wizard-step").forEach((s) => {
    s.classList.toggle("active", Number(s.dataset.step) === n);
  });
  document.querySelectorAll<HTMLElement>("#pipe-stepper .step-pill").forEach((p) => {
    const s = Number(p.dataset.step);
    p.classList.toggle("active", s === n);
    p.classList.toggle("done", s < n);
    p.classList.toggle("locked", s > max);
  });
  if (n === 2) { updateStep2LoginGate(); refreshLoginStatus(); updateExistingReportNotice(); }
  if (n === 3) updateRepSubstate();
  if (n === 4) renderPipeReport();
  const w = $("#pipe-wizard");
  if (w && !silent) w.scrollIntoView({ behavior: "smooth", block: "start" });
}

// Re-evaluate the locked frontier after data changes (segment picked, report
// saved/loaded, pipeline reset) without forcing a navigation.
function refreshPipeLocks() {
  setPipeStep(state.pipeStep, { silent: true });
}

document.querySelectorAll<HTMLElement>("#pipe-stepper .step-pill").forEach((p) => {
  p.addEventListener("click", () => {
    const s = Number(p.dataset.step);
    if (s > pipeUnlockedMax()) { showPipeLock(s); return; }
    setPipeStep(s);
  });
});
document.querySelectorAll<HTMLElement>("#pipe-wizard .step-next, #pipe-wizard .step-back").forEach((b) => {
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
    const elx = $<HTMLInputElement>(sel);
    if (elx) elx.value = "";
  });
  updateStep2Actions();
  setRepMode("current");
  pushNav({ view: "pipeline", segment: pipeSegment() }, { replace: true });
  setPipeStep(1);
});

$<HTMLSelectElement>("#pipe-segment-select").addEventListener("change", () => {
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
  if (!$<HTMLInputElement>("#pipe-date").value) $<HTMLInputElement>("#pipe-date").value = new Date().toISOString().slice(0, 10);
}

// Step 1 shows exactly one path at a time: an approved-segment dropdown, or a
// new-segment drafter that only reveals its editor + approve action after a draft.
function setSegMode(mode: string) {
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
    // only after a draft exists (LLM "Draft it" or the manual template) -- so
    // the footer Continue is out of the way. Key the editor on real JSON draft
    // content, not a leftover slug, so a stale slug can't surface an empty
    // editor with a live Approve button (the bug this fixes).
    cont.hidden = true;
    note.textContent = "Draft a theme or enter one manually, review it, then approve to continue.";
    $("#seg-draft-editor").hidden = !$<HTMLTextAreaElement>("#pipe-segment-json").value.trim();
    updateSegDraftState();
  }
}

$("#seg-mode-existing").addEventListener("click", () => setSegMode("existing"));
$("#seg-mode-new").addEventListener("click", () => setSegMode("new"));

// Step 3 is one-lane too: review the report this run produced (or paste one you
// ran yourself), OR import an existing run. Never both at once.
function setRepMode(mode: string) {
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
  return !!stem && state.currentDeepRun === stem && !!($<HTMLTextAreaElement>("#pipe-report").value || "").trim();
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
  const next = $<HTMLButtonElement>("#pipe-step3-next");
  if (next) {
    const ok = pipeHasSavedReport();
    next.disabled = !ok;
    next.title = ok ? "" : pipeLockReason(4);
  }
}

$("#rep-mode-current").addEventListener("click", () => setRepMode("current"));
$("#rep-mode-import").addEventListener("click", () => setRepMode("import"));

function pipeSegment() {
  if (state.segMode === "new") return $<HTMLInputElement>("#pipe-slug").value.trim() || $<HTMLSelectElement>("#pipe-segment-select").value;
  return $<HTMLSelectElement>("#pipe-segment-select").value || $<HTMLInputElement>("#pipe-slug").value.trim();
}

function parseJsonField(sel: string, fallback: unknown) {
  const raw = $<HTMLTextAreaElement>(sel).value.trim();
  if (!raw) return fallback;
  return JSON.parse(raw);
}

// The example member's symbol in the manual template. segDraftValid() rejects
// it, so the user must replace it with a real ticker before continuing.
const SEG_PLACEHOLDER_SYM = "TICKER";

function segSlugify(s: string) {
  return String(s || "").toLowerCase().trim()
    .replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 60);
}

// A minimal but structurally valid segment definition for the manual path: it
// shows the real shape with one example member. The placeholder symbol is
// intentionally rejected by segDraftValid() so Approve & continue stays blocked
// until it is replaced.
function blankSegmentDef(theme: string) {
  const title = theme
    ? theme.replace(/\s+/g, " ").trim().replace(/\b\w/g, (c: string) => c.toUpperCase())
    : "New segment";
  return {
    title,
    kind: "research",
    status: "approved",
    comment: "Manual draft — replace the example member with real tickers and refine the rationales.",
    members: [
      { symbol: SEG_PLACEHOLDER_SYM, rationale: "Why this company belongs in the segment." },
    ],
  };
}

// A draft is good enough to continue only when the slug is set and the JSON
// parses into an object with at least one member carrying a real ticker (not
// the manual-template placeholder). This gates the Approve & continue action so
// you can never advance to Deep Research on an empty or skeleton segment.
function segDraftValid() {
  if (!$<HTMLInputElement>("#pipe-slug").value.trim()) return false;
  const raw = $<HTMLTextAreaElement>("#pipe-segment-json").value.trim();
  if (!raw) return false;
  let def;
  try { def = JSON.parse(raw); } catch (_e) { return false; }
  if (!def || typeof def !== "object" || Array.isArray(def)) return false;
  const members = Array.isArray(def.members) ? def.members : [];
  if (!members.length) return false;
  return members.every((m: { symbol?: unknown }) => {
    const sym = m && typeof m.symbol === "string" ? m.symbol.trim() : "";
    return !!sym && sym.toUpperCase() !== SEG_PLACEHOLDER_SYM;
  });
}

function updateSegDraftState() {
  const btn = $<HTMLButtonElement>("#pipe-save-segment");
  if (!btn) return;
  const ok = segDraftValid();
  btn.disabled = !ok;
  btn.title = ok ? "" : "Add a valid definition with at least one real ticker before continuing.";
}

// Live-validate as the user edits either field, so the gate reflects the
// current draft without needing a save attempt.
$<HTMLTextAreaElement>("#pipe-segment-json").addEventListener("input", updateSegDraftState);
$<HTMLInputElement>("#pipe-slug").addEventListener("input", updateSegDraftState);

// The manual path: reveal the editor prefilled with a minimal valid template
// (deriving slug + title from the theme box when present) so the user can fill
// in real tickers instead of waiting on the LLM drafter.
$("#seg-enter-manual").addEventListener("click", () => {
  const status = $("#pipe-segment-status");
  status.classList.remove("err");
  const theme = $<HTMLInputElement>("#pipe-query").value.trim();
  if (!$<HTMLInputElement>("#pipe-slug").value.trim()) $<HTMLInputElement>("#pipe-slug").value = segSlugify(theme) || "new-segment";
  if (!$<HTMLTextAreaElement>("#pipe-segment-json").value.trim()) {
    $<HTMLTextAreaElement>("#pipe-segment-json").value = JSON.stringify(blankSegmentDef(theme), null, 2);
  }
  $("#pipe-draft-prompt-wrap").hidden = true;
  $("#seg-draft-editor").hidden = false;
  status.textContent = "Manual draft — replace the example ticker(s), then approve to continue.";
  updateSegDraftState();
  $<HTMLTextAreaElement>("#pipe-segment-json").focus();
});

// Drafting is now LLM-backed and async: for a subject you don't hold (e.g.
// "space exploration") the keyword baseline finds nothing, so the server asks
// the analysis CLI to propose real, currently-listed tickers and we poll for
// the result. The keyword universe is still merged in as an instant baseline,
// and the copy-the-prompt fallback survives for when no CLI is configured.
$("#pipe-draft").addEventListener("click", async () => {
  const status = $("#pipe-segment-status");
  const btn = $("#pipe-draft") as HTMLButtonElement;
  const query = $<HTMLInputElement>("#pipe-query").value.trim();
  status.classList.remove("err");
  if (!query) {
    status.classList.add("err");
    status.textContent = "describe a theme first";
    return;
  }
  btn.disabled = true;
  status.innerHTML = `<span class="spinner"></span> researching candidate tickers...`;
  try {
    const job = await api("/api/segment-draft", "POST", { query });
    await pollDeepJob(job.id, status, async (done: Job) => {
      const rec = (done.result || {}) as Record<string, any>;
      $<HTMLInputElement>("#pipe-slug").value = rec.slug || "";
      $<HTMLTextAreaElement>("#pipe-segment-json").value = JSON.stringify(rec.definition || {}, null, 2);
      // The draft prompt asks an LLM for structured JSON members; it is NOT the
      // Deep Research prompt (Step 2 builds that from the saved segment). Keep it
      // here in Step 1 as a copy-paste fallback so it can't leak into #pipe-prompt.
      const draftPrompt = rec.llm_prompt || "";
      $<HTMLTextAreaElement>("#pipe-draft-prompt").value = draftPrompt;
      $("#pipe-draft-prompt-wrap").hidden = !draftPrompt;
      $("#seg-draft-editor").hidden = false;
      updateSegDraftState();
      const warn = (rec.warnings || []).join(" ");
      status.textContent = warn || `drafted ${rec.member_count || 0} names — review, then approve to continue`;
    }, `Drafting ${query}`);
  } catch (e) {
    status.textContent = "draft failed: " + e.message;
    status.classList.add("err");
  } finally {
    btn.disabled = false;
  }
});

$("#pipe-save-segment").addEventListener("click", async () => {
  const status = $("#pipe-segment-status");
  status.classList.remove("err");
  // Defense in depth: the button is disabled while the draft is invalid, but
  // re-check here so a stale enable or a programmatic click can't save an empty
  // or placeholder-only segment.
  if (!segDraftValid()) {
    status.classList.add("err");
    status.textContent = "Add a valid definition with at least one real ticker first.";
    updateSegDraftState();
    return;
  }
  status.textContent = "saving segment...";
  try {
    const slug = $<HTMLInputElement>("#pipe-slug").value.trim();
    const definition = parseJsonField("#pipe-segment-json", {});
    definition.status = "approved";
    const rec = await api("/api/segment-def/" + encodeURIComponent(slug), "POST", { definition });
    status.textContent = `saved ${rec.name} — continuing to Deep Research`;
    await loadSegmentList();
    $<HTMLSelectElement>("#pipe-segment-select").value = rec.name;
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
  const hasPrompt = !!$<HTMLTextAreaElement>("#pipe-prompt").value.trim();
  $("#pipe-build-prompt").hidden = hasPrompt;
  $("#pipe-run-deep").hidden = !hasPrompt;
  $("#pipe-rebuild-prompt").hidden = !hasPrompt;
}

// Most recent saved run for `seg` that actually has a report on disk. Stems are
// `${seg}-YYYY-MM-DD`; the date check stops a segment like "ai" from matching
// "ai-software-...". Lexical desc sort on the stem orders by date newest-first.
function latestReportForSegment(seg: string | null | undefined) {
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
  if ($<HTMLTextAreaElement>("#pipe-prompt").value.trim() && !stale) return;
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
    $<HTMLInputElement>("#pipe-date").value = rec.date;
    $<HTMLTextAreaElement>("#pipe-prompt").value = rec.prompt;
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
$<HTMLTextAreaElement>("#pipe-prompt").addEventListener("input", updateStep2Actions);

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
