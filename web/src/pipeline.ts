import type { DeepRun, Job } from "./api-types";
import { $$, api, el, esc, sectionCard, state } from "./core";
import { pollDeepJob, setNeedsLoginHandler } from "./jobs";
import { mdToHtml, openRunInAnalyses } from "./analyses";
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
  const date = ($$<HTMLInputElement>("#pipe-date").value || "").trim();
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
  const note = $$("#pipe-lock-note");
  if (!note) return;
  note.textContent = pipeLockReason(n);
  note.hidden = false;
  if (_pipeLockTimer) clearTimeout(_pipeLockTimer);
  _pipeLockTimer = setTimeout(() => { note.hidden = true; }, 4500);
}

function setPipeStep(n: number, { silent = false }: { silent?: boolean } = {}) {
  n = Math.max(1, Math.min(4, Number(n) || 1));
  const max = pipeUnlockedMax();
  if (n > max) {
    if (!silent) showPipeLock(n);
    n = max;
  } else if (!silent) {
    const note = $$("#pipe-lock-note");
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
  const w = $$("#pipe-wizard");
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

$$("#pipe-restart").addEventListener("click", () => {
  state.currentDeepRun = null;
  state.repManual = false;
  ["#pipe-report", "#pipe-sources", "#pipe-source-url", "#pipe-prompt"].forEach((sel) => {
    const elx = $$<HTMLInputElement>(sel);
    if (elx) elx.value = "";
  });
  updateStep2Actions();
  setRepMode("current");
  pushNav({ view: "pipeline", segment: pipeSegment() }, { replace: true });
  setPipeStep(1);
});

$$<HTMLSelectElement>("#pipe-segment-select").addEventListener("change", () => {
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
  if (!$$<HTMLInputElement>("#pipe-date").value) $$<HTMLInputElement>("#pipe-date").value = new Date().toISOString().slice(0, 10);
}

// Step 1 shows exactly one path at a time: an approved-segment dropdown, or a
// new-segment drafter that only reveals its editor + approve action after a draft.
function setSegMode(mode: string) {
  mode = mode === "new" ? "new" : "existing";
  state.segMode = mode;
  $$("#seg-pane-existing").hidden = mode !== "existing";
  $$("#seg-pane-new").hidden = mode !== "new";
  $$("#seg-mode-existing").classList.toggle("active", mode === "existing");
  $$("#seg-mode-new").classList.toggle("active", mode === "new");
  const cont = $$("#pipe-step1-continue");
  const note = $$("#pipe-step1-note");
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
    $$("#seg-draft-editor").hidden = !$$<HTMLTextAreaElement>("#pipe-segment-json").value.trim();
    updateSegDraftState();
  }
}

$$("#seg-mode-existing").addEventListener("click", () => setSegMode("existing"));
$$("#seg-mode-new").addEventListener("click", () => setSegMode("new"));

// Step 3 is one-lane too: review the report this run produced (or paste one you
// ran yourself), OR import an existing run. Never both at once.
function setRepMode(mode: string) {
  mode = mode === "import" ? "import" : "current";
  state.repMode = mode;
  $$("#rep-pane-current").hidden = mode !== "current";
  $$("#rep-pane-import").hidden = mode !== "import";
  $$("#rep-mode-current").classList.toggle("active", mode === "current");
  $$("#rep-mode-import").classList.toggle("active", mode === "import");
  updateRepSubstate();
}

// A report "result" is known for the current segment + date once a run/import/
// load has populated the report body and tagged it as the current run.
function pipeHasRunResult() {
  const stem = pipeCurrentStem();
  return !!stem && state.currentDeepRun === stem && !!($$<HTMLTextAreaElement>("#pipe-report").value || "").trim();
}

// Step 3 "This run's report" is itself step-by-step: until a report actually
// exists, show only the run action and keep the finished-report fields hidden.
// The Perplexity URL is read-only for an automated run (it is filled by the
// run); only a manual "I ran it elsewhere" paste makes it editable. Continue to
// Review stays blocked until a report is saved on disk.
function updateRepSubstate() {
  const pending = $$("#rep-current-pending");
  const done = $$("#rep-current-done");
  if (!pending || !done) return;
  const hasResult = pipeHasRunResult() || state.repManual;
  pending.hidden = hasResult;
  done.hidden = !hasResult;
  const url = $$("#pipe-source-url");
  if (url) {
    const editable = state.repManual && !pipeHasRunResult();
    url.toggleAttribute("readonly", !editable);
  }
  const next = $$<HTMLButtonElement>("#pipe-step3-next");
  if (next) {
    const ok = pipeHasSavedReport();
    next.disabled = !ok;
    next.title = ok ? "" : pipeLockReason(4);
  }
}

$$("#rep-mode-current").addEventListener("click", () => setRepMode("current"));
$$("#rep-mode-import").addEventListener("click", () => setRepMode("import"));

function pipeSegment() {
  if (state.segMode === "new") return $$<HTMLInputElement>("#pipe-slug").value.trim() || $$<HTMLSelectElement>("#pipe-segment-select").value;
  return $$<HTMLSelectElement>("#pipe-segment-select").value || $$<HTMLInputElement>("#pipe-slug").value.trim();
}

function parseJsonField(sel: string, fallback: unknown) {
  const raw = $$<HTMLTextAreaElement>(sel).value.trim();
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
  if (!$$<HTMLInputElement>("#pipe-slug").value.trim()) return false;
  const raw = $$<HTMLTextAreaElement>("#pipe-segment-json").value.trim();
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
  const btn = $$<HTMLButtonElement>("#pipe-save-segment");
  if (!btn) return;
  const ok = segDraftValid();
  btn.disabled = !ok;
  btn.title = ok ? "" : "Add a valid definition with at least one real ticker before continuing.";
}

// Live-validate as the user edits either field, so the gate reflects the
// current draft without needing a save attempt.
$$<HTMLTextAreaElement>("#pipe-segment-json").addEventListener("input", updateSegDraftState);
$$<HTMLInputElement>("#pipe-slug").addEventListener("input", updateSegDraftState);

// The manual path: reveal the editor prefilled with a minimal valid template
// (deriving slug + title from the theme box when present) so the user can fill
// in real tickers instead of waiting on the LLM drafter.
$$("#seg-enter-manual").addEventListener("click", () => {
  const status = $$("#pipe-segment-status");
  status.classList.remove("err");
  const theme = $$<HTMLInputElement>("#pipe-query").value.trim();
  if (!$$<HTMLInputElement>("#pipe-slug").value.trim()) $$<HTMLInputElement>("#pipe-slug").value = segSlugify(theme) || "new-segment";
  if (!$$<HTMLTextAreaElement>("#pipe-segment-json").value.trim()) {
    $$<HTMLTextAreaElement>("#pipe-segment-json").value = JSON.stringify(blankSegmentDef(theme), null, 2);
  }
  $$("#pipe-draft-prompt-wrap").hidden = true;
  $$("#seg-draft-editor").hidden = false;
  status.textContent = "Manual draft — replace the example ticker(s), then approve to continue.";
  updateSegDraftState();
  $$<HTMLTextAreaElement>("#pipe-segment-json").focus();
});

// Drafting is now LLM-backed and async: for a subject you don't hold (e.g.
// "space exploration") the keyword baseline finds nothing, so the server asks
// the analysis CLI to propose real, currently-listed tickers and we poll for
// the result. The keyword universe is still merged in as an instant baseline,
// and the copy-the-prompt fallback survives for when no CLI is configured.
$$("#pipe-draft").addEventListener("click", async () => {
  const status = $$("#pipe-segment-status");
  const btn = $$("#pipe-draft") as HTMLButtonElement;
  const query = $$<HTMLInputElement>("#pipe-query").value.trim();
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
      $$<HTMLInputElement>("#pipe-slug").value = rec.slug || "";
      $$<HTMLTextAreaElement>("#pipe-segment-json").value = JSON.stringify(rec.definition || {}, null, 2);
      // The draft prompt asks an LLM for structured JSON members; it is NOT the
      // Deep Research prompt (Step 2 builds that from the saved segment). Keep it
      // here in Step 1 as a copy-paste fallback so it can't leak into #pipe-prompt.
      const draftPrompt = rec.llm_prompt || "";
      $$<HTMLTextAreaElement>("#pipe-draft-prompt").value = draftPrompt;
      $$("#pipe-draft-prompt-wrap").hidden = !draftPrompt;
      $$("#seg-draft-editor").hidden = false;
      updateSegDraftState();
      const warn = (rec.warnings || []).join(" ");
      status.textContent = warn || `drafted ${rec.member_count || 0} names — review, then approve to continue`;
    }, `Drafting ${query}`);
  } catch (e) {
    status.textContent = "draft failed: " + (e as Error).message;
    status.classList.add("err");
  } finally {
    btn.disabled = false;
  }
});

$$("#pipe-save-segment").addEventListener("click", async () => {
  const status = $$("#pipe-segment-status");
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
    const slug = $$<HTMLInputElement>("#pipe-slug").value.trim();
    const definition = parseJsonField("#pipe-segment-json", {});
    definition.status = "approved";
    const rec = await api("/api/segment-def/" + encodeURIComponent(slug), "POST", { definition });
    status.textContent = `saved ${rec.name} — continuing to Deep Research`;
    await loadSegmentList();
    $$<HTMLSelectElement>("#pipe-segment-select").value = rec.name;
    setSegMode("existing");
    pushNav({ view: "pipeline", segment: rec.name }, { replace: true });
    setPipeStep(2);
  } catch (e) {
    status.textContent = "save failed: " + (e as Error).message;
    status.classList.add("err");
  }
});

// Step 2 shows one primary action at a time: "Build prompt" until a prompt
// exists, then "Run Deep Research". Rebuild + deterministic pull are secondary.
function updateStep2Actions() {
  const hasPrompt = !!$$<HTMLTextAreaElement>("#pipe-prompt").value.trim();
  $$("#pipe-build-prompt").hidden = hasPrompt;
  $$("#pipe-run-deep").hidden = !hasPrompt;
  $$("#pipe-rebuild-prompt").hidden = !hasPrompt;
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
  const box = $$("#pipe-existing");
  if (!box) return;
  const run = latestReportForSegment(pipeSegment());
  if (!run) { box.hidden = true; box.dataset.stem = ""; return; }
  const date = (run.stem.match(/-(\d{4}-\d{2}-\d{2})$/) || [])[1] || "";
  box.dataset.stem = run.stem;
  $$("#pipe-existing-text").textContent =
    `This segment already has a saved Deep Research report${date ? ` from ${date}` : ""}. Reuse it instead of spending a new run?`;
  box.hidden = false;
}

$$("#pipe-existing-use").addEventListener("click", async () => {
  const stem = $$("#pipe-existing").dataset.stem;
  if (!stem) return;
  await loadDeepRun(stem);
  setPipeStep(3);
});

// Deep Research only works through a logged-in Perplexity session. When we are
// not logged in, block the prompt workflow behind the login gate and insist the
// user sets it up first. The deterministic pull and the Step 3 import path stay
// reachable, so this gates the prompt, not the whole step.
function updateStep2LoginGate() {
  const gate = $$("#pipe-login-gate");
  const area = $$("#pipe-prompt-area");
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
  if ($$<HTMLTextAreaElement>("#pipe-prompt").value.trim() && !stale) return;
  state._autoBuilding = true;
  try { await buildPrompt(); } finally { state._autoBuilding = false; }
}

async function buildPrompt() {
  const status = $$("#pipe-prompt-status");
  const seg = pipeSegment();
  status.classList.remove("err");
  if (!state.pplxLoggedIn) {
    updateStep2LoginGate();
    $$("#pipe-login-gate-status").textContent = "Set up the Perplexity login first.";
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
    $$<HTMLInputElement>("#pipe-date").value = rec.date;
    $$<HTMLTextAreaElement>("#pipe-prompt").value = rec.prompt;
    state.promptSegment = rec.segment || seg;
    pushNav({ view: "pipeline", segment: rec.segment || seg }, { replace: true });
    status.textContent = "prompt ready — review it, then run Deep Research";
    updateStep2Actions();
  } catch (e) {
    status.textContent = "prompt failed: " + (e as Error).message;
    status.classList.add("err");
  }
}

$$("#pipe-build-prompt").addEventListener("click", buildPrompt);
$$("#pipe-rebuild-prompt").addEventListener("click", buildPrompt);
$$<HTMLTextAreaElement>("#pipe-prompt").addEventListener("input", updateStep2Actions);

$$("#pipe-run-deterministic").addEventListener("click", async () => {
  const status = $$("#pipe-prompt-status");
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
    status.textContent = "pull failed: " + (e as Error).message;
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

// ---------------------------------------------------------------------------
// Deep-research runners (moved here from errors.ts, which now stays a pure
// error-center leaf). These drive Steps 2-4 — run/import/login, refresh the
// saved-run list, load a run, and render the review gate — so they belong with
// the wizard state machine above. Cross-calls (setPipeStep, pipeSegment, etc.)
// are now local instead of an errors↔pipeline import cycle.
// ---------------------------------------------------------------------------

// Recover a stale Perplexity session surfaced by a background job: mark the gate
// logged-out and offer an in-place login button. Registered with jobs.ts so the
// shared poller can trigger it without importing this module (which would
// recreate the cycle the errors/jobs/pipeline split removed).
//
// Called from main.ts's boot path, NOT at module init: jobs.ts sits in an import
// cycle with this module (jobs -> tasks -> shell -> pipeline), so registering
// during evaluation would touch jobs' handler binding while it's still in its
// temporal dead zone. Deferring to the composition root guarantees jobs is fully
// initialized first (the same reason core.setErrorSink is safe: core is a leaf).
function registerPipelineJobHandlers() {
  setNeedsLoginHandler((statusEl, message) => {
    state.pplxLoggedIn = false;
    updateStep2LoginGate();
    renderNeedsLogin(statusEl, message);
  });
}

// Render a "not logged in" run/import outcome as an actionable prompt: the
// message plus a real "Set up Perplexity login" button that opens the login
// window in place. After it succeeds, refreshLoginStatus reopens the prompt.
function renderNeedsLogin(statusEl: HTMLElement, message?: string | null) {
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
async function runDeepResearch(status: HTMLElement) {
  status.classList.remove("err");
  const segment = pipeSegment();
  const date = $$<HTMLInputElement>("#pipe-date").value.trim() || undefined;
  const prompt = $$<HTMLTextAreaElement>("#pipe-prompt").value.trim();
  if (!segment) { status.classList.add("err"); status.textContent = "pick or save a segment first"; return; }
  if (!state.pplxLoggedIn) {
    setPipeStep(2);
    updateStep2LoginGate();
    $$("#pipe-login-gate-status").textContent = "Set up the Perplexity login first.";
    return;
  }
  if (!prompt) {
    setPipeStep(2);
    const ps = $$("#pipe-prompt-status");
    ps.classList.add("err");
    ps.textContent = "Build a prompt on Step 2 first.";
    return;
  }
  status.innerHTML = `<span class="spinner"></span> starting deep research (off-screen browser)...`;
  try {
    const job = await api("/api/deep-research/run", "POST", { segment, date, prompt });
    await pollDeepJob(job.id, status, async (done) => {
      const stem = (done.artifact && done.artifact.stem) || `${segment}-${done.date || date}`;
      const r = (done.result || {}) as Record<string, any>;
      const n = (r.citations && r.citations.length) || 0;
      status.textContent = `done: ${stem} - ${r.report_chars || 0} chars, ${n} sources. Review the saved report below.`;
      await refreshDeepRuns();
      await loadDeepRun(stem);
      setPipeStep(3);
    }, `Deep research \u00b7 ${segment}`);
    await refreshLoginStatus();
  } catch (e) {
    status.classList.add("err");
    status.textContent = "run failed: " + (e as Error).message;
    await refreshLoginStatus();
  }
}

$$("#pipe-run-deep").addEventListener("click", () => runDeepResearch($$("#pipe-prompt-status")));
$$("#pipe-run-deep-report").addEventListener("click", () => runDeepResearch($$("#pipe-report-run-status")));
$$("#rep-paste-manual").addEventListener("click", () => {
  state.repManual = true;
  setRepMode("current");
  const r = $$<HTMLTextAreaElement>("#pipe-report");
  if (r) r.focus();
});

$$("#pipe-import").addEventListener("click", async () => {
  const status = $$("#pipe-import-status");
  status.classList.remove("err");
  const url = $$<HTMLInputElement>("#pipe-import-url").value.trim();
  const segment = pipeSegment();
  const date = $$<HTMLInputElement>("#pipe-date").value.trim() || undefined;
  if (!segment) { status.classList.add("err"); status.textContent = "pick or save a segment first"; return; }
  if (!url) { status.classList.add("err"); status.textContent = "paste a Perplexity run URL"; return; }
  status.innerHTML = `<span class="spinner"></span> pulling the finished run (off-screen browser)...`;
  try {
    const job = await api("/api/deep-research/import", "POST", { segment, date, url });
    await pollDeepJob(job.id, status, async (done) => {
      const stem = (done.artifact && done.artifact.stem) || `${segment}-${done.date || date}`;
      const r = (done.result || {}) as Record<string, any>;
      const n = (r.citations && r.citations.length) || 0;
      status.textContent = `imported: ${stem} - ${r.report_chars || 0} chars, ${n} sources.`;
      await refreshDeepRuns();
      await loadDeepRun(stem);
    }, `Importing \u00b7 ${segment}`);
    await refreshLoginStatus();
  } catch (e) {
    status.classList.add("err");
    status.textContent = "import failed: " + (e as Error).message;
  }
});

async function refreshLoginStatus() {
  let st;
  try {
    st = await api("/api/deep-research/login-status");
  } catch {
    st = { logged_in: false };
  }
  state.pplxLoggedIn = !!st.logged_in;
  updateStep2LoginGate();
  return state.pplxLoggedIn;
}

async function runPplxLogin(statusEl: HTMLElement) {
  statusEl.classList.remove("err");
  statusEl.innerHTML = `<span class="spinner"></span> opening a visible login window...`;
  try {
    const job = await api("/api/deep-research/login", "POST");
    await pollDeepJob(job.id, statusEl, async () => {
      statusEl.textContent = "Perplexity login confirmed. Off-screen runs will reuse it.";
    });
  } catch (e) {
    statusEl.classList.add("err");
    statusEl.textContent = "login failed: " + (e as Error).message;
  }
  await refreshLoginStatus();
}

$$("#pipe-pplx-login").addEventListener("click", () => runPplxLogin($$("#pipe-login-gate-status")));

$$("#pipe-login-recheck").addEventListener("click", async () => {
  const txt = $$("#pipe-login-gate-status");
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
    txt.textContent = "check failed: " + (e as Error).message;
  }
});

$$("#pipe-save-report").addEventListener("click", async () => {
  const status = $$("#pipe-artifact-status");
  status.classList.remove("err");
  status.textContent = "saving artifacts...";
  try {
    const rec = await api("/api/deep-research/save", "POST", {
      segment: pipeSegment(),
      date: $$<HTMLInputElement>("#pipe-date").value.trim(),
      source_url: $$<HTMLInputElement>("#pipe-source-url").value.trim(),
      report: $$<HTMLTextAreaElement>("#pipe-report").value,
      citations: parseJsonField("#pipe-sources", []),
    });
    status.textContent = `saved ${rec.stem} — continuing to Review`;
    state.currentDeepRun = rec.stem;
    state.repManual = false;
    pushNav({ view: "pipeline", segment: pipeSegment(), run: rec.stem });
    await refreshDeepRuns();
    setPipeStep(4);
  } catch (e) {
    status.textContent = "save failed: " + (e as Error).message;
    status.classList.add("err");
  }
});

$$("#pipe-run-review").addEventListener("click", async () => {
  const status = $$("#pipe-review-status");
  status.classList.remove("err");
  status.textContent = "running review gate...";
  try {
    const segment = pipeSegment();
    const date = $$<HTMLInputElement>("#pipe-date").value.trim();
    const rec = await api("/api/deep-research/review", "POST", { segment, date });
    state.currentDeepRun = `${segment}-${date}`;
    pushNav({ view: "pipeline", segment, run: state.currentDeepRun });
    status.textContent = `review generated: ${rec.warnings.length} warning(s), ${rec.proposal.changes.length} proposal change(s)`;
    renderReviewGate(rec);
    await refreshDeepRuns();
  } catch (e) {
    status.textContent = "review failed: " + (e as Error).message;
    status.classList.add("err");
  }
});

$$("#pipe-refresh-runs").addEventListener("click", refreshDeepRuns);

async function refreshDeepRuns() {
  const out = $$("#pipe-runs");
  if (!out) return;
  try {
    const { runs } = await api<{ runs: DeepRun[] }>("/api/deep-runs");
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
    out.innerHTML = `<div class="status err">could not load runs: ${esc((e as Error).message)}</div>`;
  }
}

async function loadDeepRun(stem: string, { push = true }: { push?: boolean } = {}) {
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
    $$<HTMLSelectElement>("#pipe-segment-select").value = m[1];
    $$<HTMLInputElement>("#pipe-date").value = m[2];
    if (push) pushNav({ view: "pipeline", segment: m[1], run: stem });
  } else if (push) {
    pushNav({ view: "pipeline", run: stem });
  }
  if (rec.report) $$<HTMLTextAreaElement>("#pipe-report").value = rec.report;
  if (rec.sources) $$<HTMLTextAreaElement>("#pipe-sources").value = JSON.stringify(rec.sources.citations || [], null, 2);
  if (rec.sources && rec.sources.source_url) $$<HTMLInputElement>("#pipe-source-url").value = rec.sources.source_url;
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

// Data-quality / source-strength -> tag color. Most rows are "INFO" (neutral);
// only escalate color when the gate flags something worth a second look.
function reviewTagClass(v: unknown) {
  const s = String(v).toLowerCase();
  if (s.includes("block") || s.includes("bad") || s.includes("conflict")) return "bad";
  if (s.includes("warn") || s.includes("weak")) return "warn";
  if (s.includes("ok") || s.includes("good") || s.includes("primary") || s.includes("strong")) return "good";
  return "";
}

// Step 4 no longer re-renders the report (the Analyses reader is the single
// canonical reader, with sources + review + follow-up Q&A in one place). Instead
// surface a one-click route into that reader for the loaded run, so the review
// gate's verdict sits next to the full document without duplicating it here.
function renderPipeReport() {
  const box = document.getElementById("pipe-report-view");
  if (!box) return;
  const stem = state.currentDeepRun;
  const raw = ($$<HTMLTextAreaElement>("#pipe-report")?.value || "").trim();
  if (!raw) { box.hidden = true; box.innerHTML = ""; return; }
  box.hidden = false;
  box.innerHTML = "";
  if (stem) {
    box.appendChild(el("span", "pipe-report-link-text",
      "Full report, sources & follow-up Q&A live in the reader."));
    const btn = el("button", "primary", "Open report & Q&A in Reports \u2197");
    btn.type = "button";
    btn.addEventListener("click", () => openRunInAnalyses(stem));
    box.appendChild(btn);
  } else {
    box.appendChild(el("span", "pipe-report-link-text",
      "Save the report on the Report step to read it (and ask follow-ups) in Reports."));
  }
}

// The review-gate payload (/api/deep-research/review or a loaded run). Loose by
// nature -- the gate evolves -- so most fields are optional and nested findings
// carry only what the table reads.
interface ReviewFinding {
  level?: string;
  message?: string;
}

interface ReviewRow {
  symbol?: string;
  report_action?: string;
  target_rule?: string;
  data_quality?: string;
  conflict?: string;
}

interface ReviewChange {
  symbol?: string;
  [key: string]: unknown;
}

interface ReviewProposal {
  changes?: ReviewChange[];
  blocked_symbols?: string[];
  findings?: ReviewFinding[];
  warnings?: string[];
}

interface ReviewGate {
  source_summary?: { buckets?: Record<string, number> } | null;
  findings?: ReviewFinding[] | null;
  proposal?: ReviewProposal | null;
  warnings?: string[];
  rows?: ReviewRow[];
  blocked_symbols?: string[];
  markdown?: string;
  report?: string;
  review?: string;
}

function renderReviewGate(rec: ReviewGate) {
  const out = $$("#pipe-review-output");
  out.innerHTML = "";
  const card = sectionCard("Review gate output");
  if (rec.source_summary) {
    const b: Record<string, number> = rec.source_summary.buckets || {};
    card.appendChild(el("div", "badges",
      Object.keys(b).map((k) => `<span class="badge ${k === "weak" && b[k] ? "off" : "on"}">${esc(k)}: ${b[k]}</span>`).join("")));
  }
  const findings = rec.findings || (rec.proposal && rec.proposal.findings) || null;
  if (findings && findings.length) {
    const cls: Record<string, string> = { BLOCK: "ERROR", WARN: "WARN", FYI: "INFO" };
    const checks = el("div", "checks");
    findings.forEach((f) => checks.appendChild(
      el("div", `check ${cls[f.level || ""] || "INFO"}`, `<span class="sev">${esc(f.level)}</span><span>${esc(f.message)}</span>`)));
    card.appendChild(checks);
  } else if (rec.warnings && rec.warnings.length) {
    const checks = el("div", "checks");
    rec.warnings.forEach((w) => checks.appendChild(el("div", "check WARN", `<span class="sev">WARN</span><span>${esc(w)}</span>`)));
    card.appendChild(checks);
  }
  if (rec.rows && rec.rows.length) {
    const table = el("table", "review-table");
    table.innerHTML =
      "<thead><tr><th>Symbol</th><th>Action</th><th>Target</th><th>Data</th><th>Conflict</th></tr></thead>" +
      "<tbody>" + rec.rows.map((r) => {
        const action = (r.report_action || "").trim();
        const target = (r.target_rule || "").trim();
        const dq = (r.data_quality || "").trim();
        const conflict = (r.conflict || "").trim();
        return "<tr>" +
          `<td class="rev-sym">${esc(r.symbol)}</td>` +
          `<td>${action ? `<span class="rev-tag">${esc(action)}</span>` : `<span class="rev-dash">—</span>`}</td>` +
          `<td>${target ? esc(target) : `<span class="rev-dash">—</span>`}</td>` +
          `<td>${dq ? `<span class="rev-tag ${reviewTagClass(dq)}">${esc(dq)}</span>` : `<span class="rev-dash">—</span>`}</td>` +
          `<td>${conflict ? `<span class="rev-tag bad">${esc(conflict)}</span>` : `<span class="rev-dash">—</span>`}</td>` +
          "</tr>";
      }).join("") + "</tbody>";
    card.appendChild(table);
  }
  const proposal: ReviewProposal = rec.proposal || {};
  const changes = proposal.changes || [];
  const blocked = rec.blocked_symbols || proposal.blocked_symbols || [];
  const applicable = changes.filter((c) => !blocked.includes(c.symbol || ""));
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
    const det = el("details", "review-notes");
    det.appendChild(el("summary", undefined, "Full review notes"));
    det.appendChild(el("div", "prose", mdToHtml(rec.markdown)));
    card.appendChild(det);
  }
  out.appendChild(card);
  // Surface the actual report on this step: running the review gate alone doesn't
  // fill the Step 3 field, so without this the analyst lands here with nothing to
  // read but the gate's verdict. The review payload carries the report text.
  if (rec.report && $$<HTMLTextAreaElement>("#pipe-report")) $$<HTMLTextAreaElement>("#pipe-report").value = rec.report;
  renderPipeReport();
  // Apply only becomes available once the review produced a change we're allowed
  // to apply -- i.e. at least one proposed symbol that isn't data-blocked.
  const applyBtn = $$<HTMLButtonElement>("#pipe-apply-proposal");
  if (applyBtn) applyBtn.disabled = !applicable.length;
}

$$("#pipe-apply-proposal").addEventListener("click", async () => {
  const status = $$("#pipe-apply-status");
  const segment = pipeSegment();
  const date = ($$<HTMLInputElement>("#pipe-date").value || "").trim();
  if (!segment || !date) {
    status.textContent = "run the review gate first";
    status.classList.add("err");
    return;
  }
  // Parity with Strategy/Optimizer: this stages into the working draft rather
  // than writing the live model, so nothing is irreversible until the user
  // commits the draft. The confirm + follow-up reflect that safety model.
  if (!window.confirm("Stage this proposal to your working draft? Nothing touches your live target model until you commit there.")) return;
  status.classList.remove("err");
  status.textContent = "staging proposal…";
  try {
    const rec = await api("/api/target-proposal/apply", "POST", { segment, date, confirm: true });
    const n = rec.staged_count ?? (rec.applied ? rec.applied.length : 0);
    const skipped = (rec.skipped && rec.skipped.length) ? ` (${rec.skipped.length} skipped)` : "";
    status.textContent = `Staged ${n} change${n === 1 ? "" : "s"} to the working draft${skipped}. `;
    const go = el("button", "linklike") as HTMLButtonElement;
    go.type = "button";
    go.textContent = "Review working draft →";
    go.addEventListener("click", () => { pushNav({ view: "working-draft" }); setActiveView("working-draft"); });
    status.appendChild(go);
  } catch (e) {
    status.textContent = "staging failed: " + (e as Error).message;
    status.classList.add("err");
  }
});

export {
  registerPipelineJobHandlers,
  runDeepResearch,
  refreshLoginStatus,
  runPplxLogin,
  refreshDeepRuns,
  loadDeepRun,
  renderReviewGate,
  renderPipeReport,
};
