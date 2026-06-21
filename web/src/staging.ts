import { $, api, esc, fmtWeight } from "./core";

// The working-draft (staging) view. Renders the whole-book diff of the staged
// target model vs the live one: reconciliation totals, overlap warnings, pins,
// and a per-change row with provenance and a Revert action. A single Commit
// promotes the draft to the live model; Discard throws it away.

type Band = { low?: number; high?: number; rule?: string; sleeve?: string } | null;
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

// Turn the model's terse rule verbs into plain words. Unknown verbs just get
// their underscores spaced out. Folded into bandText so the whole UI speaks the
// same language.
const RULE_WORDS: Record<string, string> = {
  accumulate: "accumulate",
  hold: "hold",
  wait: "wait",
  buy: "buy",
  avoid: "avoid",
  trim_only: "trim only",
  do_not_add: "hold, don't add",
};
const ruleWord = (r?: string): string => (r ? RULE_WORDS[r] || r.replace(/_/g, " ") : "");

function bandText(b: Band): string {
  if (!b) return "—";
  const lo = typeof b.low === "number" ? b.low : "?";
  const hi = typeof b.high === "number" ? b.high : "?";
  const sleeve = b.sleeve ? ` · ${esc(b.sleeve)}` : "";
  return `${lo}–${hi}% ${esc(ruleWord(b.rule))}${sleeve}`;
}

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

const midOf = (b: Band): number | null => {
  if (!b) return null;
  const lo = typeof b.low === "number" ? b.low : null;
  const hi = typeof b.high === "number" ? b.high : null;
  if (lo != null && hi != null) return (lo + hi) / 2;
  return lo != null ? lo : hi;
};

// Plain-language direction of a change, so a user reads "trimmed" instead of
// decoding "10–12% → 0–7.7%".
function directionTag(r: DiffRow): { label: string; tone: "ok" | "warn" | "bad" } {
  if (r.change === "added") return { label: "new", tone: "ok" };
  if (r.change === "removed") return { label: "dropped", tone: "bad" };
  const a = midOf(r.before);
  const b = midOf(r.after);
  if (a != null && b != null) {
    if (b > a + 0.05) return { label: "raised", tone: "ok" };
    if (b < a - 0.05) return { label: "trimmed", tone: "warn" };
  }
  if ((r.before && r.before.rule) !== (r.after && r.after.rule)) return { label: "rule change", tone: "warn" };
  return { label: "tweaked", tone: "warn" };
}

function rowHtml(r: DiffRow): string {
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
      <div class="stat-tile"><div class="stat-label">In named positions</div><div class="stat-value">${pct(rec.targeted_mid_pct)}</div></div>
      <div class="stat-tile"><div class="stat-label">Cash</div><div class="stat-value">${pct(rec.cash_target_pct)}</div></div>
      <div class="stat-tile"><div class="stat-label">${over ? "Over budget by" : "Free to allocate"}</div><div class="stat-value">${pct(Math.abs(rec.available_pct))}</div></div>
      ${typeof rec.untargeted_pct === "number" ? `<div class="stat-tile"><div class="stat-label">Held but unplanned</div><div class="stat-value">${pct(rec.untargeted_pct)}</div></div>` : ""}
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
  return "Plan vs. your holdings";
}

// The model checks were a wall of ~25 yellow lines — alarming and unreadable.
// Group them by category, show a one-line summary, and tuck the detail behind a
// native <details> (keyboard- and screen-reader-friendly), collapsed by default.
function overlapsHtml(overlaps: any[]): string {
  if (!overlaps || !overlaps.length) return "";
  const hasError = overlaps.some((o) => o.severity === "ERROR");
  const groups = new Map<string, any[]>();
  for (const o of overlaps) {
    const c = checkCategory(o);
    if (!groups.has(c)) groups.set(c, []);
    groups.get(c)!.push(o);
  }
  const chips = [...groups.entries()]
    .map(([cat, list]) => `<span class="chip">${esc(cat)} · ${list.length}</span>`).join(" ");
  const sections = [...groups.entries()].map(([cat, list]) => {
    const items = list.map((o) =>
      `<li class="stage-finding stage-finding-${esc((o.severity || "").toLowerCase())}">` +
      `<span class="strat-tag strat-tag-${o.severity === "ERROR" ? "bad" : "warn"}">${esc(o.severity)}</span> ` +
      `${esc(o.message || o.area)}</li>`).join("");
    return `<div class="stage-check-group"><div class="subhead">${esc(cat)}</div><ul class="stage-findings">${items}</ul></div>`;
  }).join("");
  const note = hasError
    ? `<span class="stage-check-note err">Some checks are errors — resolve them before committing.</span>`
    : `<span class="stage-check-note">These are advisories — they won't stop you committing.</span>`;
  return `<div class="stage-section">
    <details class="stage-checks"${hasError ? " open" : ""}>
      <summary><strong>${overlaps.length} thing${overlaps.length === 1 ? "" : "s"} worth a look</strong> ${chips} ${note}</summary>
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
  const buckets: { title: string; test: (r: DiffRow) => boolean }[] = [
    { title: "New targets", test: (r) => r.change === "added" },
    { title: "Adjusted targets", test: (r) => r.change === "modified" },
    { title: "Removed targets", test: (r) => r.change === "removed" },
  ];
  return buckets.map(({ title, test }) => {
    const group = rows.filter(test);
    if (!group.length) return "";
    return `<div class="stage-bucket"><div class="subhead">${title} (${group.length})</div>` +
      `<div class="stage-rows">${group.map(rowHtml).join("")}</div></div>`;
  }).join("");
}

function render(s: Staging): void {
  const body = $("#stage-body");
  const commit = $<HTMLButtonElement>("#stage-commit");
  const discard = $<HTMLButtonElement>("#stage-discard");
  if (!body) return;
  if (!s.has_draft) {
    if (commit) commit.disabled = true;
    if (discard) discard.disabled = true;
    body.innerHTML = `<div class="empty"><strong>No working draft yet.</strong><br>`
      + `Run a strategy and choose <em>"Add to working draft"</em>, or stage changes from the Rebalance planner. They'll collect here so you can review the whole book and commit once.</div>`
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
    ? `This draft proposes <strong>${rows.length} change${rows.length === 1 ? "" : "s"}</strong> to your plan${parts.length ? ` (${parts.join(", ")})` : ""}. Nothing affects your portfolio until you press <em>Commit</em>.`
    : `This draft currently matches your live plan.`;
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
  const status = $("#stage-status");
  if (status) status.textContent = "";
  try {
    render(await api<Staging>("/api/staging"));
  } catch (e: any) {
    if (status) { status.textContent = "Could not load the working draft: " + (e && e.message); status.classList.add("err"); }
  }
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
    if (!window.confirm("Commit the working draft to your live portfolio? A reversible backup is kept.")) return;
    commit.disabled = true;
    try {
      const res = await api("/api/staging/commit", "POST", { confirm: true });
      if (status) { status.classList.remove("err"); status.textContent = `Committed. Live model is now as_of ${res.as_of}. Backup: ${res.backup || "none"}.`; }
      await loadStaging();
    } catch (e: any) {
      if (status) { status.textContent = "Commit failed: " + (e && e.message); status.classList.add("err"); }
      commit.disabled = false;
    }
  });

  if (discard) discard.addEventListener("click", async () => {
    if (discard.disabled) return;
    if (!window.confirm("Discard the working draft? Your live portfolio is unchanged.")) return;
    try {
      await api("/api/staging/discard", "POST", {});
      if (status) { status.classList.remove("err"); status.textContent = "Working draft discarded."; }
      await loadStaging();
    } catch (e: any) {
      if (status) { status.textContent = "Discard failed: " + (e && e.message); status.classList.add("err"); }
    }
  });

  // Per-row Revert (delegated).
  const bodyHost = $("#view-working-draft");
  if (bodyHost) bodyHost.addEventListener("click", async (e) => {
    const btn = (e.target as HTMLElement).closest<HTMLElement>(".stage-revert");
    if (!btn) return;
    const key = btn.dataset.key;
    if (!key) return;
    btn.setAttribute("disabled", "true");
    try {
      await api("/api/staging/edit", "POST", { op: "revert", key });
      await loadStaging();
    } catch (err: any) {
      if (status) { status.textContent = "Revert failed: " + (err && err.message); status.classList.add("err"); }
      btn.removeAttribute("disabled");
    }
  });
}

export { initStaging, loadStaging, bandText, provLabel, reconHtml };
