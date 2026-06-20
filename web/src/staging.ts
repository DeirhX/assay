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

function bandText(b: Band): string {
  if (!b) return "—";
  const lo = typeof b.low === "number" ? b.low : "?";
  const hi = typeof b.high === "number" ? b.high : "?";
  const sleeve = b.sleeve ? ` · ${esc(b.sleeve)}` : "";
  return `${lo}–${hi}% ${esc(b.rule || "")}${sleeve}`;
}

// One-line, human description of where a band came from.
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

const CHANGE_TONE = { added: "ok", modified: "warn", removed: "bad" } as const;

function rowHtml(r: DiffRow): string {
  const tone = CHANGE_TONE[r.change] || "warn";
  const lockBadge = r.locked ? `<span class="strat-tag strat-tag-warn" title="Pinned: standing human intent">pinned</span>` : "";
  const challenged = r.provenance && r.provenance.challenges_pin
    ? `<span class="strat-tag strat-tag-bad" title="This change contradicts a pin">challenges pin</span>` : "";
  const priorPin = r.provenance && r.provenance.prior_pin
    ? `<div class="stage-prior">was pinned: ${esc(r.provenance.prior_pin.stance || "")}${typeof r.provenance.prior_pin.floor_pct === "number" ? " · floor " + r.provenance.prior_pin.floor_pct + "%" : ""}</div>` : "";
  return `<div class="stage-row stage-${r.change}">
    <div class="stage-row-main">
      <div class="stage-key">
        <span class="strat-tag strat-tag-${tone}">${esc(r.change)}</span>
        <strong>${esc(r.key)}</strong>
        <span class="stage-kind">${esc(r.kind)}</span>
        ${lockBadge}${challenged}
      </div>
      <div class="stage-bands">
        <span class="stage-before">${bandText(r.before)}</span>
        <span class="stage-arrow">→</span>
        <span class="stage-after">${bandText(r.after)}</span>
      </div>
      <div class="stage-prov">${provLabel(r.provenance)}</div>
      ${priorPin}
    </div>
    <div class="stage-row-actions">
      <button class="ghost stage-revert" type="button" data-key="${esc(r.key)}" title="Restore this key to the live model (reject the staged change)">Revert</button>
    </div>
  </div>`;
}

function reconHtml(rec: any): string {
  if (!rec) return "";
  const over = rec.over_allocated;
  const tone = over ? "bad" : "ok";
  const untargeted = (rec.untargeted || []).slice(0, 6)
    .map((u: any) => `<span class="chip">${esc(u.symbol)} ${pct(u.current_pct)}</span>`).join(" ");
  const funding = (rec.funding_order || []).length
    ? `<div class="stage-funding">Funding order: ${(rec.funding_order || []).map((s: string) => esc(s)).join(", ")}</div>` : "";
  return `<div class="stage-recon stage-recon-${tone}">
    <div class="stage-recon-tiles">
      <div class="stat-tile"><div class="stat-label">Targeted midpoints</div><div class="stat-value">${pct(rec.targeted_mid_pct)}</div></div>
      <div class="stat-tile"><div class="stat-label">Cash target</div><div class="stat-value">${pct(rec.cash_target_pct)}</div></div>
      <div class="stat-tile"><div class="stat-label">${over ? "Over-allocated by" : "Available"}</div><div class="stat-value">${pct(Math.abs(rec.available_pct))}</div></div>
      ${typeof rec.untargeted_pct === "number" ? `<div class="stat-tile"><div class="stat-label">Untargeted book</div><div class="stat-value">${pct(rec.untargeted_pct)}</div></div>` : ""}
    </div>
    ${over ? `<div class="stage-warn">Midpoints + cash exceed 100% of the book — trim funding sources before committing.</div>` : ""}
    ${untargeted ? `<div class="stage-untargeted"><span class="muted">Untargeted:</span> ${untargeted}</div>` : ""}
    ${funding}
  </div>`;
}

function overlapsHtml(overlaps: any[]): string {
  if (!overlaps || !overlaps.length) return "";
  const items = overlaps.map((o) =>
    `<li class="stage-finding stage-finding-${esc((o.severity || "").toLowerCase())}"><span class="strat-tag strat-tag-${o.severity === "ERROR" ? "bad" : "warn"}">${esc(o.severity)}</span> ${esc(o.area)}: ${esc(o.message)}</li>`).join("");
  return `<div class="stage-section"><div class="subhead">Model checks against the draft</div><ul class="stage-findings">${items}</ul></div>`;
}

function pinsHtml(pins: Record<string, any>): string {
  const keys = Object.keys(pins || {});
  if (!keys.length) return "";
  const chips = keys.map((k) => {
    const p = pins[k];
    const floor = typeof p.floor_pct === "number" ? ` ≥${p.floor_pct}%` : "";
    return `<span class="chip" title="${esc(p.rationale || "")}">${esc(k)} · ${esc(p.stance || "")}${floor}</span>`;
  }).join(" ");
  return `<div class="stage-section"><div class="subhead">Pinned convictions</div><div class="stage-pins">${chips}</div></div>`;
}

function render(s: Staging): void {
  const body = $("#stage-body");
  const commit = $<HTMLButtonElement>("#stage-commit");
  const discard = $<HTMLButtonElement>("#stage-discard");
  if (!body) return;
  if (!s.has_draft) {
    if (commit) commit.disabled = true;
    if (discard) discard.disabled = true;
    body.innerHTML = `<div class="empty">No working draft. Stage changes from a strategy run ("Add to working draft") or the rebalance planner, then review and commit them here.`
      + (s.reconciliation ? `<div class="stage-section"><div class="subhead">Live portfolio reconciliation</div>${reconHtml(s.reconciliation)}</div>` : "")
      + `</div>`;
    return;
  }
  if (commit) commit.disabled = false;
  if (discard) discard.disabled = false;
  const rows = [...(s.targets || []), ...(s.sleeves || [])];
  const rowsHtml = rows.length ? rows.map(rowHtml).join("") : `<div class="empty">Draft matches the live model (no net changes).</div>`;
  body.innerHTML = `
    <div class="stage-section">
      <div class="subhead">Whole-book reconciliation (with the draft applied)</div>
      ${reconHtml(s.reconciliation)}
    </div>
    ${overlapsHtml(s.overlaps)}
    ${pinsHtml(s.pins)}
    <div class="stage-section">
      <div class="subhead">Pending changes (${s.counts.total})</div>
      <div class="stage-rows">${rowsHtml}</div>
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
