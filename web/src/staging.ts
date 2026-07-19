import { bandBar, bandText, directionTag, ruleWord, scaleMaxFor, type Band } from "./band-viz";
import { loadComposition } from "./composition";
import { $, api, apiLoad, esc, fmtWeight, loadError } from "./core";

// The working-draft (staging) view. Renders the whole-book diff of the staged
// target model vs the live one: reconciliation totals, overlap warnings, pins,
// and a per-change row with provenance and a Revert action. A single Commit
// promotes the draft to the live model; Discard throws it away.

type DiffRow = {
  key: string; kind: "target" | "sleeve"; change: "added" | "modified" | "removed";
  before: Band; after: Band; provenance?: any; locked?: boolean;
};
type Staging = {
  has_draft: boolean;
  targets: DiffRow[]; sleeves: DiffRow[];
  reconciliation: any; overlaps: any[]; runs: any[]; pins: Record<string, any>;
  counts: { targets: number; sleeves: number; total: number };
};

const pct = (v: any) => (typeof v === "number" ? fmtWeight(v) : "n/a");

// Set by a successful commit so the very next render can show a persistent
// "committed" confirmation instead of the bare "No working draft yet" empty
// state -- otherwise loadStaging() wipes the status line and the user is left
// staring at an empty draft with no sign their commit worked. Consumed (cleared)
// on first render so revisits show the normal empty state.
let _committed: { as_of?: string; backup?: string } | null = null;

// Band rendering, the shared axis, and direction tagging now live in band-viz.ts
// (used by both this view and the optimizer preview). Provenance labels below
// stay here — they're specific to the working draft's change records.

// Full provenance string (kept verbose for the row's tooltip and for tests).
function provLabel(p: any): string {
  if (!p || typeof p !== "object") return "unknown origin";
  const conv = p.conviction ? ` · ${esc(p.conviction)}` : "";
  switch (p.source) {
    case "user-pin": return `pinned${p.stance ? " " + esc(p.stance) : ""}`;
    case "legacy-plan": return `legacy plan${p.set_at ? " · " + esc(p.set_at) : ""}`;
    case "strategy": return `run ${esc(p.run_id || "?")}${p.segment ? " · " + esc(p.segment) : ""}${conv}`;
    case "pipeline": return `pipeline${p.segment ? " · " + esc(p.segment) : ""}${conv}`;
    case "manual": return "manual edit";
    default: return esc(p.source || "unknown");
  }
}

// Short, friendly origin for the row headline (the opaque run hash moves to the
// tooltip via provLabel — it means nothing to a person reading this).
function provHeadline(p: any): string {
  if (!p || typeof p !== "object") return "origin unknown";
  const conv = p.conviction ? ` · ${esc(p.conviction)} conviction` : "";
  switch (p.source) {
    case "user-pin": return `your pin${p.stance ? " · " + esc(p.stance) : ""}`;
    case "legacy-plan": return "from your earlier plan";
    case "strategy":
    case "pipeline": return `from research${p.segment ? " · " + esc(p.segment) : ""}${conv}`;
    case "manual": return "your manual edit";
    default: return esc(p.source || "origin unknown");
  }
}

function rowHtml(r: DiffRow, scaleMax: number): string {
  const dir = directionTag(r);
  const lockBadge = r.locked ? `<span class="strat-tag strat-tag-warn" title="Pinned: your standing intent — a run can challenge it but never drops it silently">pinned</span>` : "";
  const challenged = r.provenance && r.provenance.challenges_pin
    ? `<span class="strat-tag strat-tag-bad" title="This change contradicts a pin you set">challenges your pin</span>` : "";
  const priorPin = r.provenance && r.provenance.prior_pin
    ? `<div class="stage-prior">was pinned: ${esc(ruleWord(r.provenance.prior_pin.stance) || r.provenance.prior_pin.stance || "")}${typeof r.provenance.prior_pin.floor_pct === "number" ? " · floor " + r.provenance.prior_pin.floor_pct + "%" : ""}</div>` : "";
  const kindTag = r.kind === "sleeve" ? `<span class="stage-kind">sleeve</span>` : "";
  return `<div class="stage-row stage-${r.change}">
    <div class="stage-row-main">
      <div class="stage-key">
        <span class="strat-tag strat-tag-${dir.tone}">${esc(dir.label)}</span>
        <strong>${esc(r.key)}</strong>
        ${kindTag}${lockBadge}${challenged}
      </div>
      <div class="stage-bands">
        <span class="stage-before">${bandText(r.before)}</span>
        <span class="stage-arrow" aria-label="changes to">→</span>
        <span class="stage-after">${bandText(r.after)}</span>
      </div>
      ${bandBar(r, scaleMax)}
      <div class="stage-prov" title="${provLabel(r.provenance)}">${provHeadline(r.provenance)}</div>
      ${priorPin}
    </div>
    <div class="stage-row-actions">
      <button class="ghost stage-revert" type="button" data-key="${esc(r.key)}" title="Reject this change and keep your current plan for ${esc(r.key)}">Keep current</button>
    </div>
  </div>`;
}

function reconHtml(rec: any): string {
  if (!rec) return "";
  const over = rec.over_allocated;
  const tone = over ? "bad" : "ok";
  const head = over
    ? `This plan would commit <strong>${pct(rec.targeted_mid_pct)}</strong> to named positions plus <strong>${pct(rec.cash_target_pct)}</strong> cash — that's <strong>${pct(Math.abs(rec.available_pct))}</strong> more than your book holds.`
    : `This plan puts <strong>${pct(rec.targeted_mid_pct)}</strong> into named positions and keeps <strong>${pct(rec.cash_target_pct)}</strong> in cash, leaving <strong>${pct(Math.abs(rec.available_pct))}</strong> free to allocate.`;
  const untargeted = (rec.untargeted || []).slice(0, 6)
    .map((u: any) => `<span class="chip">${esc(u.symbol)} ${pct(u.current_pct)}</span>`).join(" ");
  const funding = (rec.funding_order || []).length
    ? `<div class="stage-funding">If you need cash, trim in this order: ${(rec.funding_order || []).map((s: string) => esc(s)).join(", ")}.</div>` : "";
  return `<div class="stage-recon stage-recon-${tone}">
    <p class="stage-recon-lead">${head}</p>
    <div class="stage-recon-tiles">
      <div class="stat-tile stat-tile--accent"><div class="stat-label">In named positions</div><div class="stat-value">${pct(rec.targeted_mid_pct)}</div></div>
      <div class="stat-tile"><div class="stat-label">Cash</div><div class="stat-value">${pct(rec.cash_target_pct)}</div></div>
      <div class="stat-tile stat-tile--${over ? "bad" : "ok"}"><div class="stat-label">${over ? "Over budget by" : "Free to allocate"}</div><div class="stat-value">${pct(Math.abs(rec.available_pct))}</div></div>
      ${typeof rec.untargeted_pct === "number" ? `<div class="stat-tile stat-tile--warn"><div class="stat-label">Held but unplanned</div><div class="stat-value">${pct(rec.untargeted_pct)}</div></div>` : ""}
    </div>
    ${over ? `<div class="stage-warn">Named positions + cash exceed 100% of your book — trim a funding source before committing.</div>` : ""}
    ${untargeted ? `<div class="stage-untargeted"><span class="muted">Held with no plan yet:</span> ${untargeted}</div>` : ""}
    ${funding}
  </div>`;
}

// Coarse, person-friendly bucket for an advisory check, derived from its
// structured `area` field (not by parsing the message).
function checkCategory(o: any): string {
  const a = String(o.area || "");
  if (a.startsWith("coverage:")) return "Held with no plan";
  if (a.startsWith("sleeve:")) return "Counted in two places";
  if (a.startsWith("home:")) return "Allocation home";
  return "Plan vs. your holdings";
}

// The model checks were a wall of ~25 yellow lines — alarming and unreadable.
// Group them by category, show a one-line summary, and tuck the detail behind a
// native <details> (keyboard- and screen-reader-friendly), collapsed by default.
// Worst severity in a list, and its tone class, so a category chip / header and
// the whole callout speak the same red/amber/green language as the change list.
function worstSev(list: any[]): "ERROR" | "WARN" | "INFO" {
  if (list.some((o) => o.severity === "ERROR")) return "ERROR";
  if (list.some((o) => o.severity === "WARN")) return "WARN";
  return "INFO";
}
const sevTone = (s: string): "bad" | "warn" | "ok" => (s === "ERROR" ? "bad" : s === "WARN" ? "warn" : "ok");

function overlapsHtml(overlaps: any[]): string {
  if (!overlaps || !overlaps.length) return "";
  const hasError = overlaps.some((o) => o.severity === "ERROR");
  const groups = new Map<string, any[]>();
  for (const o of overlaps) {
    const c = checkCategory(o);
    if (!groups.has(c)) groups.set(c, []);
    groups.get(c)!.push(o);
  }
  const topTone = hasError ? "bad" : "warn";
  const chips = [...groups.entries()]
    .map(([cat, list]) => `<span class="chip ${sevTone(worstSev(list))}">${esc(cat)} · ${list.length}</span>`).join(" ");
  const sections = [...groups.entries()].map(([cat, list]) => {
    const gTone = sevTone(worstSev(list));
    const items = list.map((o) => {
      const sev = String(o.severity || "INFO");
      return `<li class="stage-finding stage-finding-${esc(sev.toLowerCase())} sf-${sevTone(sev)}">` +
        `<span class="dot ${esc(sev)}"></span>` +
        `<span class="sf-sev">${esc(sev)}</span>` +
        `<span class="sf-msg">${esc(o.message || o.area)}</span></li>`;
    }).join("");
    return `<div class="stage-check-group">` +
      `<div class="subhead stage-bucket-head stage-bucket-${gTone}">` +
      `<span class="stage-bucket-dot"></span>${esc(cat)}<span class="stage-bucket-count">${list.length}</span></div>` +
      `<ul class="stage-findings">${items}</ul></div>`;
  }).join("");
  const note = hasError
    ? `<span class="stage-check-note err">Some checks are errors — resolve them before committing.</span>`
    : `<span class="stage-check-note">These are advisories — they won't stop you committing.</span>`;
  return `<div class="stage-section">
    <details class="stage-checks stage-checks-${topTone}"${hasError ? " open" : ""}>
      <summary><span class="stage-checks-tally">${overlaps.length}</span><strong>thing${overlaps.length === 1 ? "" : "s"} worth a look</strong> ${chips} ${note}</summary>
      <div class="stage-checks-body">${sections}</div>
    </details>
  </div>`;
}

function pinsHtml(pins: Record<string, any>): string {
  const keys = Object.keys(pins || {});
  if (!keys.length) return "";
  const chips = keys.map((k) => {
    const p = pins[k];
    const floor = typeof p.floor_pct === "number" ? ` ≥${p.floor_pct}%` : "";
    return `<span class="chip" title="${esc(p.rationale || "")}">${esc(k)} · ${esc(ruleWord(p.stance) || p.stance || "")}${floor}</span>`;
  }).join(" ");
  return `<div class="stage-section"><div class="subhead">Pinned convictions <span class="stage-sub">— anchored; a run can challenge but never drop them</span></div><div class="stage-pins">${chips}</div></div>`;
}

// Pending changes grouped into New / Adjusted / Removed, each with a count, so
// the list reads as three short buckets instead of one interleaved stream.
function pendingHtml(rows: DiffRow[]): string {
  if (!rows.length) return `<div class="empty">This draft matches your live plan — nothing to commit.</div>`;
  const scaleMax = scaleMaxFor(rows);
  const buckets: { title: string; tone: string; test: (r: DiffRow) => boolean }[] = [
    { title: "New targets", tone: "ok", test: (r) => r.change === "added" },
    { title: "Adjusted targets", tone: "warn", test: (r) => r.change === "modified" },
    { title: "Removed targets", tone: "bad", test: (r) => r.change === "removed" },
  ];
  return buckets.map(({ title, tone, test }) => {
    const group = rows.filter(test);
    if (!group.length) return "";
    return `<div class="stage-bucket stage-bucket-${tone}">` +
      `<div class="subhead stage-bucket-head"><span class="stage-bucket-dot"></span>${title}` +
      `<span class="stage-bucket-count">${group.length}</span></div>` +
      `<div class="stage-rows">${group.map((r) => rowHtml(r, scaleMax)).join("")}</div></div>`;
  }).join("");
}

// ---- revert-to-pre-commit (the visible undo for a commit) -----------------
// The restore-preview diff shares the working-draft diff shape minus provenance,
// framed as live(before) -> backup(after), so it renders on the same band tracks.
interface RestoreDiffRow { key: string; kind: string; change: "added" | "modified" | "removed"; before: Band; after: Band; }
interface RestoreDiff { targets?: RestoreDiffRow[]; sleeves?: RestoreDiffRow[]; }

const DIR_CHIP: Record<string, string> = { ok: "good", warn: "warn", bad: "bad" };

// The confirmation panel: every band that reverting would change, as a track, so
// the undo is reviewed with the same care as the commit that preceded it.
function revertPanelHtml(diff: RestoreDiff): string {
  const rows = [...(diff.targets || []), ...(diff.sleeves || [])];
  if (!rows.length) return `<div class="hint">The live model already matches this backup — nothing to revert.</div>`;
  const bandRows = rows.map((r) => ({ change: r.change, before: r.before, after: r.after }));
  const scaleMax = scaleMaxFor(bandRows);
  const list = rows.map((r, i) => {
    const dir = directionTag(bandRows[i]);
    const name = r.kind === "sleeve" ? `[${r.key}]` : r.key;
    return `<div class="stage-revert-row">`
      + `<span class="stage-revert-name">${esc(name)}</span>`
      + `<span class="chip ${DIR_CHIP[dir.tone] || "muted"}">${esc(dir.label)}</span>`
      + bandBar(bandRows[i], scaleMax, { axis: false })
      + `<span class="stage-revert-nums">${esc(bandText(r.before))} \u2192 ${esc(bandText(r.after))}</span>`
      + `</div>`;
  }).join("");
  return `<div class="stage-revert-head">Reverting restores the model saved before this commit — `
    + `${rows.length} band(s) change back:</div>`
    + `<div class="stage-revert-list">${list}</div>`
    + `<div class="stage-revert-actions">`
    + `<button type="button" class="danger" data-revert-go>Confirm revert</button>`
    + `<button type="button" class="linklike" data-revert-cancel>Cancel</button>`
    + `<span class="stage-revert-status status"></span></div>`;
}

function render(s: Staging): void {
  const body = $("#stage-body");
  const commit = $<HTMLButtonElement>("#stage-commit");
  const discard = $<HTMLButtonElement>("#stage-discard");
  if (!body) return;
  if (!s.has_draft) {
    if (commit) commit.disabled = true;
    if (discard) discard.disabled = true;
    const done = _committed;
    _committed = null; // consume: only show right after a commit, not on revisits
    const head = done
      ? `<div class="stage-committed"><strong>&#10003; Target model applied.</strong> `
        + `Your model is now <code>as_of ${esc(done.as_of || "today")}</code>`
        + (done.backup ? ` and a reversible backup was saved` : "")
        + `.<br>Current holdings have not changed. `
        + `<button type="button" class="linklike" data-shell-view="rebalance">Build orders from the updated model &rarr;</button>`
        + (done.backup
            ? `<div class="stage-revert-affordance">`
              + `<button type="button" class="linklike" id="stage-revert-commit" data-backup="${esc(done.backup)}">&#8617; Revert this commit</button>`
              + `<div id="stage-revert-panel"></div></div>`
            : "")
        + `</div>`
      : `<div class="stage-empty-hero">`
        + `<strong>No pending draft.</strong>`
        + `<p>Edit allocation segments above, or use Advanced modes (guided plan / optimizer) to stage proposals here.</p>`
        + `</div>`;
    // Even with no draft, the reconciliation snapshot ("where your book stands,
    // what's unallocated, funding order") is a genuinely useful destination —
    // lead with it so the page isn't a dead end when the draft is empty.
    body.innerHTML = head
      + (s.reconciliation ? `<div class="stage-section"><div class="subhead">Where your live portfolio stands</div>${reconHtml(s.reconciliation)}</div>` : "");
    return;
  }
  if (commit) commit.disabled = false;
  if (discard) discard.disabled = false;
  const rows = [...(s.targets || []), ...(s.sleeves || [])];
  const nNew = rows.filter((r) => r.change === "added").length;
  const nMod = rows.filter((r) => r.change === "modified").length;
  const nDrop = rows.filter((r) => r.change === "removed").length;
  const parts = [nNew ? `${nNew} new` : "", nMod ? `${nMod} adjusted` : "", nDrop ? `${nDrop} removed` : ""].filter(Boolean);
  const summary = rows.length
    ? `These proposals contain <strong>${rows.length} target-model change${rows.length === 1 ? "" : "s"}</strong>${parts.length ? ` (${parts.join(", ")})` : ""}. Applying them changes target bands, not current holdings or orders.`
    : `The pending changes currently match your live target model.`;
  body.innerHTML = `
    <p class="stage-summary">${summary}</p>
    <div class="stage-section">
      <div class="subhead">How the whole book looks with this draft applied</div>
      ${reconHtml(s.reconciliation)}
    </div>
    ${overlapsHtml(s.overlaps)}
    ${pinsHtml(s.pins)}
    <div class="stage-section">
      <div class="subhead">Changes in this draft (${s.counts.total})</div>
      ${pendingHtml(rows)}
    </div>`;
}

async function loadStaging(): Promise<void> {
  // Composition editor sits above the draft; refresh it whenever this view loads.
  void loadComposition();
  await apiLoad<Staging>({
    path: "/api/staging",
    status: $("#stage-status"),
    errorLabel: "Could not load pending model changes",
    render,
  });
}

let _wired = false;
function initStaging(): void {
  if (_wired) return;
  _wired = true;
  const commit = $<HTMLButtonElement>("#stage-commit");
  const discard = $<HTMLButtonElement>("#stage-discard");
  const status = $("#stage-status");

  if (commit) commit.addEventListener("click", async () => {
    if (commit.disabled) return;
    if (!window.confirm("Apply these changes to the live target model? This does not trade. A reversible backup is kept.")) return;
    commit.disabled = true;
    try {
      const res = await api<{ as_of?: string; backup?: string }>("/api/staging/commit", "POST", { confirm: true });
      // Stash the result so the next render shows a persistent confirmation (with
      // the Trade hand-off) instead of the bare empty state -- loadStaging()
      // clears the status line, so a transient message there wouldn't survive.
      _committed = { as_of: res.as_of, backup: res.backup };
      await loadStaging();
    } catch (e: any) {
      loadError(status, "Commit failed", e);
      commit.disabled = false;
    }
  });

  if (discard) discard.addEventListener("click", async () => {
    if (discard.disabled) return;
    if (!window.confirm("Discard the pending model changes? The live target model is unchanged.")) return;
    try {
      await api("/api/staging/discard", "POST", {});
      if (status) { status.classList.remove("err"); status.textContent = "Pending model changes discarded."; }
      await loadStaging();
    } catch (e: any) {
      loadError(status, "Discard failed", e);
    }
  });

  // Per-row Revert is delegated so it survives re-renders. Workflow hand-offs
  // use data-shell-view and are handled centrally by the shell.
  const bodyHost = $("#view-working-draft");
  if (bodyHost) bodyHost.addEventListener("click", async (e) => {
    // Post-commit revert: open the diff preview, then confirm/cancel. The backup
    // path rides on the button/panel dataset so it survives re-renders.
    const revertOpen = (e.target as HTMLElement).closest<HTMLElement>("#stage-revert-commit");
    if (revertOpen) {
      const backup = revertOpen.dataset.backup;
      const panel = document.querySelector<HTMLElement>("#stage-revert-panel");
      if (!backup || !panel) return;
      revertOpen.setAttribute("disabled", "true");
      panel.innerHTML = `<div class="hint"><span class="spinner"></span> computing what reverting changes…</div>`;
      try {
        const diff = await api<RestoreDiff>("/api/target-model/restore-preview?backup=" + encodeURIComponent(backup));
        panel.innerHTML = revertPanelHtml(diff);
        panel.dataset.backup = backup;
      } catch (err: any) {
        panel.innerHTML = `<div class="stage-warn">Could not preview revert: ${esc((err && err.message) || String(err))}</div>`;
      } finally {
        revertOpen.removeAttribute("disabled");
      }
      return;
    }
    if ((e.target as HTMLElement).closest<HTMLElement>("[data-revert-cancel]")) {
      const panel = document.querySelector<HTMLElement>("#stage-revert-panel");
      if (panel) { panel.innerHTML = ""; delete panel.dataset.backup; }
      return;
    }
    const revertGo = (e.target as HTMLElement).closest<HTMLElement>("[data-revert-go]");
    if (revertGo) {
      const panel = document.querySelector<HTMLElement>("#stage-revert-panel");
      const backup = panel && panel.dataset.backup;
      if (!panel || !backup) return;
      const st = panel.querySelector<HTMLElement>(".stage-revert-status");
      revertGo.setAttribute("disabled", "true");
      if (st) { st.classList.remove("err"); st.innerHTML = `<span class="spinner"></span> reverting…`; }
      try {
        await api("/api/target-model/restore", "POST", { backup, confirm: true });
        if (status) { status.classList.remove("err"); status.textContent = "Reverted to the pre-commit target model."; }
        await loadStaging();
      } catch (err: any) {
        if (st) { st.textContent = "Revert failed: " + ((err && err.message) || String(err)); st.classList.add("err"); }
        revertGo.removeAttribute("disabled");
      }
      return;
    }
    const btn = (e.target as HTMLElement).closest<HTMLElement>(".stage-revert");
    if (!btn) return;
    const key = btn.dataset.key;
    if (!key) return;
    btn.setAttribute("disabled", "true");
    try {
      await api("/api/staging/edit", "POST", { op: "revert", key });
      await loadStaging();
    } catch (err: any) {
      loadError(status, "Revert failed", err);
      btn.removeAttribute("disabled");
    }
  });
}

export { initStaging, loadStaging, bandText, provLabel, reconHtml };
