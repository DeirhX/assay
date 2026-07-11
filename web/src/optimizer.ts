// The Portfolio Optimizer view: one candidate pool (everything held + basket
// picks + segment-discovered names + pinned names), sized as a whole book by
// conviction under user constraints, previewed with the shared band-shift bars,
// then staged into the working draft via the existing endpoints. The sizing math
// lives server-side in tools/optimizer.py; this module is the cockpit.
import { bandBar, scaleMaxFor, type BandRow } from "./band-viz";
import { tickerAnchorHtml } from "./analyses/linkify";
import { $, api, esc, fmtWeight } from "./core";
import { pushNav, setActiveView } from "./shell";

interface PoolEntry {
  symbol: string;
  sleeve?: string;
  sleeve_managed?: boolean;
  held_pct?: number | null;
  current_target?: { low?: number; high?: number; rule?: string } | null;
  pinned?: boolean;
  tier?: string | null;
  sources?: string[];
  conviction?: string;
  conviction_source?: string;
  rationale?: string;
  segment?: string | null;
  run?: string | null;
}
interface Constraints {
  cash_target_pct: number;
  per_name_cap: number;
  concentration_pct: number;
  min_position_pct?: number;
  max_names?: number | null;
  conviction_curve?: string;
  include_curious: boolean;
}
interface OptimizerView { pool: PoolEntry[]; constraints: Constraints }
interface Change {
  symbol: string;
  action: string;
  conviction?: string;
  conviction_source?: string;
  current_target?: any;
  proposed_target?: { low?: number; high?: number; rule?: string };
  rationale?: string;
}
interface Proposal { changes: Change[]; optimizer_meta: any; findings: any[] }

let _pool: PoolEntry[] = [];
let _constraints: Constraints | null = null;
let _excluded = new Set<string>();
let _lastProposal: Proposal | null = null;

const pct = (v: any) => (typeof v === "number" ? fmtWeight(v) : "—");
const CONV_TONE: Record<string, string> = { high: "ok", medium: "", low: "warn", avoid: "bad" };

// ---- pool table -----------------------------------------------------------
function sourceChips(e: PoolEntry): string {
  const seen = new Set<string>();
  return (e.sources || []).map((s) => {
    const label = s === "held" ? "held" : s === "model" ? "in plan" : s === "pin" ? "pinned"
      : s === "analyses" ? "from report" : s === "manual" ? "starred" : s;
    if (seen.has(label)) return "";
    seen.add(label);
    const tone = s === "pin" ? "strat-tag-warn" : s === "held" ? "strat-tag-ok" : "";
    return `<span class="strat-tag ${tone}">${esc(label)}</span>`;
  }).join(" ");
}

function convCell(e: PoolEntry): string {
  const c = (e.conviction || "").toLowerCase();
  const tone = CONV_TONE[c] ?? "";
  const src = e.conviction_source ? `<span class="opt-conv-src">${esc(e.conviction_source)}</span>` : "";
  return `<span class="opt-conv ${tone ? "strat-tag-" + tone : ""}" title="${esc(e.rationale || "")}">${esc(c || "—")}</span>${src}`;
}

function bandTextShort(b: any): string {
  if (!b || (b.low == null && b.high == null)) return `<span class="muted">—</span>`;
  return `${b.low ?? "?"}–${b.high ?? "?"}%`;
}

function poolRow(e: PoolEntry): string {
  const excluded = _excluded.has(e.symbol);
  const sym = esc(e.symbol);
  // Sleeve members are sized collectively by their allocation sleeve, not as
  // standalone names — flag that so the missing individual band isn't a mystery.
  const managed = !!e.sleeve_managed;
  const bandCell = managed
    ? `<span class="opt-sleeve-managed" title="Governed by allocation sleeve '${esc(e.sleeve || "")}'; sized as part of that sleeve, not individually.">→ sleeve ${esc(e.sleeve || "")}</span>`
    : bandTextShort(e.current_target);
  const exCell = managed
    ? `<span class="muted" title="Excluding a sleeve member here has no effect — its sleeve governs it.">—</span>`
    : `<label class="opt-ex" title="Exclude ${sym} from sizing"><input type="checkbox" aria-label="Exclude ${sym} from sizing" data-opt-exclude="${sym}" ${excluded ? "checked" : ""}></label>`;
  return `<tr class="${excluded ? "opt-row-excluded" : ""} ${managed ? "opt-row-sleeve" : ""}">
    <td>${tickerAnchorHtml(e.symbol, { bold: true })}</td>
    <td>${sourceChips(e)}</td>
    <td>${convCell(e)}</td>
    <td class="num">${pct(e.held_pct)}</td>
    <td class="num">${bandCell}</td>
    <td class="opt-ex-cell">${exCell}</td>
  </tr>`;
}

// Chips summarising the pool, shown on the drawer's summary bar so the counts
// read without expanding it.
function poolChips(): string {
  const held = _pool.filter((e) => typeof e.held_pct === "number").length;
  const want = _pool.filter((e) => e.tier === "want").length;
  const curious = _pool.filter((e) => e.tier === "curious").length;
  const chip = (n: number, label: string, cls = "") =>
    `<span class="opt-chip ${cls}"><strong>${n}</strong> ${label}</span>`;
  return chip(_pool.length, _pool.length === 1 ? "candidate" : "candidates", "opt-chip-total") +
    chip(held, "held", "opt-chip-held") +
    chip(want, "want", "opt-chip-want") +
    chip(curious, "curious", "opt-chip-curious");
}

function poolTable(): string {
  return `<div class="opt-pool-scroll"><table class="opt-pool-table">` +
    `<colgroup><col class="col-sym"><col class="col-src"><col class="col-conv"><col class="col-held"><col class="col-band"><col class="col-excl"></colgroup>` +
    `<thead><tr>` +
    `<th>Symbol</th><th>Source</th><th>Conviction</th><th class="num">Held</th><th class="num">Band</th><th class="opt-ex-th" title="Exclude from sizing">Excl</th>` +
    `</tr></thead><tbody>${_pool.map(poolRow).join("")}</tbody></table></div>`;
}

// The pool as a collapsible drawer: open before the first run (it's the input
// you review and exclude from), auto-collapsed after a proposal exists so it
// stops competing with the result for screen space.
function poolDrawer(): string {
  if (!_pool.length) {
    return `<div class="opt-card empty"><strong>Your pool is empty.</strong><br>Hold positions, or star tickers into your watchlist, then come back to size the whole book.</div>`;
  }
  return `<details class="opt-pool-details" id="opt-pool-details">` +
    `<summary class="opt-pool-bar">` +
      `<span class="opt-pool-caret" aria-hidden="true"></span>` +
      `<span class="opt-pool-title">Candidate pool</span>` +
      `<span class="opt-pool-chips">${poolChips()}</span>` +
      `<span class="opt-pool-bar-hint">exclude names, then re-optimize</span>` +
    `</summary>` +
    `<div class="opt-pool-body opt-card">${poolTable()}</div>` +
    `</details>`;
}

// ---- constraints panel ----------------------------------------------------
function constraintsPanel(c: Constraints): string {
  return `<div class="opt-constraints">
    <div class="opt-constraints-head">
      <span class="opt-constraints-title">Constraints</span>
      <span class="opt-constraints-sub">tune the whole-book sizer, then hit Optimize</span>
    </div>
    <div class="opt-constraints-grid">
      <div class="opt-field"><label for="opt-cash">Cash target</label>
        <div class="opt-inwrap"><input id="opt-cash" type="number" min="0" max="95" step="0.5" value="${c.cash_target_pct}"><span>%</span></div></div>
      <div class="opt-field"><label for="opt-cap">Per-name cap</label>
        <div class="opt-inwrap"><input id="opt-cap" type="number" min="1" max="100" step="0.5" value="${c.per_name_cap}"><span>%</span></div></div>
      <div class="opt-field"><label for="opt-conc">Max concentration</label>
        <div class="opt-inwrap"><input id="opt-conc" type="number" min="1" max="100" step="0.5" value="${c.concentration_pct}"><span>%</span></div></div>
      <div class="opt-field" title="Auto-drop dust: any name sized below this midpoint weight is pruned and its budget concentrates into the keepers (0 = off).">
        <label for="opt-minpos">Min position</label>
        <div class="opt-inwrap"><input id="opt-minpos" type="number" min="0" max="10" step="0.5" value="${c.min_position_pct ?? 1.5}"><span>%</span></div></div>
      <div class="opt-field" title="Fund at most this many names (pins always kept). Blank = no limit.">
        <label for="opt-maxnames">Max names</label>
        <div class="opt-inwrap"><input id="opt-maxnames" type="number" min="1" max="100" step="1" value="${c.max_names ?? ""}" placeholder="∞"></div></div>
      <div class="opt-field" title="How sharply conviction maps to size. Aggressive lets high-conviction names dominate; balanced spreads more evenly.">
        <label for="opt-curve">Conviction curve</label>
        <select id="opt-curve">
          <option value="aggressive" ${(c.conviction_curve ?? "aggressive") === "aggressive" ? "selected" : ""}>Aggressive</option>
          <option value="balanced" ${c.conviction_curve === "balanced" ? "selected" : ""}>Balanced</option>
        </select></div>
    </div>
    <div class="opt-constraints-opts">
      <label class="opt-check"><input id="opt-curious" type="checkbox" ${c.include_curious ? "checked" : ""}> Include <span class="tier-curious">curious</span> picks</label>
      <label class="opt-check"><input id="opt-drop" type="checkbox"> Drop avoid-rated held names (instead of trimming)</label>
      <label class="opt-check" title="Use the configured AI backend to read conviction from each name's latest research; falls back to the deterministic read if unavailable. Slower."><input id="opt-llm" type="checkbox"> AI conviction synthesis</label>
    </div>
  </div>`;
}

function readConstraints() {
  const num = (id: string, dflt: number) => {
    const v = parseFloat(($<HTMLInputElement>(id)?.value ?? "").trim());
    return Number.isFinite(v) ? v : dflt;
  };
  const c = _constraints;
  const maxRaw = ($<HTMLInputElement>("#opt-maxnames")?.value ?? "").trim();
  const maxNames = maxRaw === "" ? null : Math.max(1, Math.round(parseFloat(maxRaw)));
  return {
    cash_target_pct: num("#opt-cash", c?.cash_target_pct ?? 5),
    per_name_cap: num("#opt-cap", c?.per_name_cap ?? 12),
    concentration_pct: num("#opt-conc", c?.concentration_pct ?? 20),
    min_position_pct: num("#opt-minpos", c?.min_position_pct ?? 1.5),
    max_names: Number.isFinite(maxNames as number) ? maxNames : null,
    conviction_curve: $<HTMLSelectElement>("#opt-curve")?.value || "aggressive",
    include_curious: !!$<HTMLInputElement>("#opt-curious")?.checked,
    drop_avoid: !!$<HTMLInputElement>("#opt-drop")?.checked,
    use_llm: !!$<HTMLInputElement>("#opt-llm")?.checked,
    exclude: [..._excluded],
  };
}

// ---- preview --------------------------------------------------------------
// Map an optimizer change onto the shared band-shift row shape.
function changeToRow(ch: Change): BandRow & { symbol: string; conviction?: string; rule?: string } {
  const removed = ch.action === "remove_target";
  return {
    symbol: ch.symbol,
    change: removed ? "removed" : (ch.current_target ? "modified" : "added"),
    before: ch.current_target || null,
    after: removed ? null : (ch.proposed_target || null),
    conviction: ch.conviction,
    rule: ch.proposed_target?.rule,
  };
}

function reconTiles(meta: any): string {
  const book = meta.book_reconciliation || {};
  const over = !!book.over_allocated;
  return `<div class="stage-recon ${over ? "stage-recon-bad" : "stage-recon-ok"}">
    <div class="stage-recon-tiles">
      <div class="stat-tile stat-tile--accent"><div class="stat-label">In named positions</div><div class="stat-value">${pct(book.targeted_mid_pct)}</div></div>
      <div class="stat-tile"><div class="stat-label">Cash target</div><div class="stat-value">${pct(book.cash_target_pct)}</div></div>
      <div class="stat-tile stat-tile--${over ? "bad" : "ok"}"><div class="stat-label">${over ? "Over budget by" : "Free to allocate"}</div><div class="stat-value">${pct(Math.abs(book.available_pct ?? 0))}</div></div>
      <div class="stat-tile stat-tile--warn"><div class="stat-label">Invested budget</div><div class="stat-value">${pct(meta.invested_budget_pct)}</div></div>
    </div>
    <p class="hint opt-recon-note">${meta.funded_count ?? meta.buy_count} funded · ${meta.trim_count} trimmed · ${meta.drop_count} dropped` +
    `${meta.prune_count ? " · " + meta.prune_count + " pruned (concentration)" : ""}` +
    `${meta.sleeve_budget_pct ? " · " + meta.sleeve_count + " sleeve" + (meta.sleeve_count === 1 ? "" : "s") + " reserve " + pct(meta.sleeve_budget_pct) : ""}` +
    `${meta.sleeve_dedup_count ? " · " + meta.sleeve_dedup_count + " sleeve dup" + (meta.sleeve_dedup_count === 1 ? "" : "s") + " removed" : ""}` +
    `${meta.pinned_count ? " · " + meta.pinned_count + " pinned" : ""}${meta.included_curious ? "" : " · curious excluded"}` +
    `${meta.conviction_curve ? " · " + meta.conviction_curve + " curve" : ""}` +
    `${meta.synthesis === "llm" ? " · AI-synthesized convictions" : ""}.</p>
  </div>`;
}

function findingsBlock(findings: any[]): string {
  if (!findings || !findings.length) return "";
  const worst = findings.some((f) => f.level === "BLOCK" || f.level === "WARN") ? "warn" : "ok";
  const items = findings.map((f) => {
    const tone = f.level === "BLOCK" ? "bad" : f.level === "WARN" ? "warn" : "ok";
    return `<li class="stage-finding sf-${tone}"><span class="dot ${esc(f.level || "FYI")}"></span>` +
      `<span class="sf-sev">${esc(f.level || "FYI")}</span><span class="sf-msg">${esc(f.message || "")}</span></li>`;
  }).join("");
  const heading = `${findings.length} thing${findings.length === 1 ? "" : "s"} worth a look`;
  const list = `<ul class="stage-findings">${items}</ul>`;
  // A handful inline; a wall of advisories collapses so it doesn't bury the
  // proposal. Anything more severe than FYI auto-expands so it isn't missed.
  const severe = findings.some((f) => f.level === "BLOCK" || f.level === "WARN");
  const body = (findings.length > 4 && !severe)
    ? `<details class="opt-findings-det"><summary><span class="opt-findings-caret" aria-hidden="true"></span><span class="subhead" style="display:inline">${heading}</span></summary>${list}</details>`
    : `<div class="subhead">${heading}</div>${list}`;
  return `<div class="stage-section"><div class="stage-checks stage-checks-${worst}" style="padding:12px 14px;border:1px solid var(--border);border-left-width:4px;border-radius:var(--radius-soft)">` +
    `${body}</div></div>`;
}

// A compact change card: symbol + conviction + target band on one line, then a
// thin axis-less band bar. Designed to tile in a multi-column grid so 30+
// changes read as a dense board instead of one endless column.
function previewRow(row: ReturnType<typeof changeToRow>, scaleMax: number): string {
  const conv = row.conviction ? `<span class="opt-conv ${CONV_TONE[row.conviction] ? "strat-tag-" + CONV_TONE[row.conviction] : ""}">${esc(row.conviction)}</span>` : "";
  const after = row.after ? `${row.after.low ?? "?"}–${row.after.high ?? "?"}%` : (row.change === "removed" ? "drop" : "—");
  return `<div class="opt-prev-row">
    <div class="opt-prev-top">
      <span class="opt-prev-sym"><strong>${esc(row.symbol)}</strong>${conv}</span>
      <span class="opt-prev-after">${esc(after)}</span>
    </div>
    ${bandBar(row, scaleMax, { axis: false })}
  </div>`;
}

function renderPreview(proposal: Proposal): void {
  const host = $("#opt-preview");
  if (!host) return;
  const rows = proposal.changes.map(changeToRow);
  if (!rows.length) {
    host.innerHTML = `<div class="empty">The optimizer proposed no changes — your plan already matches the pool under these constraints.</div>`;
    return;
  }
  const scaleMax = scaleMaxFor(rows);
  const buys = rows.filter((r) => r.change !== "removed" && r.rule !== "trim_only");
  const trims = rows.filter((r) => r.rule === "trim_only" || r.change === "removed");
  // One shared axis legend per bucket header rather than under every row.
  const legend = `<span class="opt-bucket-axis">0–${scaleMax}% of book</span>`;
  const group = (title: string, tone: string, list: typeof rows) => list.length
    ? `<div class="stage-bucket stage-bucket-${tone}"><div class="subhead stage-bucket-head"><span class="stage-bucket-dot"></span>${title}<span class="stage-bucket-count">${list.length}</span>${legend}</div>` +
      `<div class="opt-prev-rows">${list.map((r) => previewRow(r, scaleMax)).join("")}</div></div>`
    : "";
  host.innerHTML =
    reconTiles(proposal.optimizer_meta) +
    findingsBlock(proposal.findings) +
    group("Sized to buy / hold", "ok", buys) +
    group("Trimmed / dropped", "bad", trims);
}

// ---- orchestration --------------------------------------------------------
function renderShell(v: OptimizerView): void {
  const body = $("#opt-body");
  if (!body) return;
  // Single-column flow on one (page) scrollbar: constraints, then the proposal
  // as the primary surface, then the candidate pool as a collapsible drawer.
  body.innerHTML =
    constraintsPanel(v.constraints) +
    `<div class="opt-card opt-preview-card">` +
    `<div class="subhead">Proposed allocation</div>` +
    `<div id="opt-preview">${previewPlaceholder()}</div></div>` +
    poolDrawer();
}

// Calm, centered empty state for the preview column before the first run.
function previewPlaceholder(): string {
  return `<div class="opt-empty">` +
    `<svg viewBox="0 0 24 24" width="30" height="30" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 3v18h18"/><path d="M7 14l4-4 3 3 5-6"/></svg>` +
    `<p class="opt-empty-title">No proposal yet</p>` +
    `<p class="opt-empty-sub">Set your constraints and hit <strong>Optimize</strong> to size the whole pool into a reviewable allocation.</p>` +
    `</div>`;
}

async function loadOptimizer(): Promise<void> {
  const body = $("#opt-body");
  const status = $("#opt-status");
  if (!body) return;
  if (status) { status.textContent = ""; status.classList.remove("err"); }
  body.innerHTML = `<div class="hint">Loading the candidate pool…</div>`;
  const stageBtn = $<HTMLButtonElement>("#opt-stage");
  if (stageBtn) stageBtn.disabled = true;
  _lastProposal = null;
  try {
    const v = await api<OptimizerView>("/api/optimizer");
    _pool = v.pool || [];
    _constraints = v.constraints;
    // Drop excludes for names no longer in the pool.
    const live = new Set(_pool.map((e) => e.symbol));
    _excluded = new Set([..._excluded].filter((s) => live.has(s)));
    renderShell(v);
  } catch (e) {
    if (status) { status.textContent = "Could not load the pool: " + (e as Error).message; status.classList.add("err"); }
    body.innerHTML = "";
  }
}

async function runOptimize(): Promise<void> {
  const status = $("#opt-status");
  const btn = $<HTMLButtonElement>("#opt-run");
  const stageBtn = $<HTMLButtonElement>("#opt-stage");
  if (btn) { btn.disabled = true; }
  if (status) { status.classList.remove("err"); status.innerHTML = `<span class="spinner"></span> sizing the pool…`; }
  try {
    const res = await api<{ proposal: Proposal }>("/api/optimizer/run", "POST", readConstraints());
    _lastProposal = res.proposal;
    renderPreview(res.proposal);
    if (status) status.textContent = "";
    if (stageBtn) stageBtn.disabled = !(res.proposal.changes && res.proposal.changes.length);
    // Collapse the (now-reviewed) pool so the proposal owns the screen, and bring
    // the result into view.
    const drawer = $<HTMLDetailsElement>("#opt-pool-details");
    if (drawer) drawer.open = false;
    document.querySelector(".opt-preview-card")?.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (e) {
    if (status) { status.textContent = "Optimize failed: " + (e as Error).message; status.classList.add("err"); }
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function stageProposal(): Promise<void> {
  if (!_lastProposal || !_lastProposal.changes.length) return;
  const status = $("#opt-status");
  const btn = $<HTMLButtonElement>("#opt-stage");
  if (btn) btn.disabled = true;
  if (status) { status.classList.remove("err"); status.innerHTML = `<span class="spinner"></span> staging to your working draft…`; }
  try {
    await api("/api/optimizer/stage", "POST", { changes: _lastProposal.changes });
    if (status) status.textContent = "";
    pushNav({ view: "working-draft" });
    setActiveView("working-draft");
  } catch (e) {
    if (status) { status.textContent = "Could not stage: " + (e as Error).message; status.classList.add("err"); }
    if (btn) btn.disabled = false;
  }
}

async function reviewHoldings(): Promise<void> {
  const status = $("#opt-status");
  const btn = $<HTMLButtonElement>("#opt-review");
  if (btn) btn.disabled = true;
  if (status) { status.classList.remove("err"); status.innerHTML = `<span class="spinner"></span> starting a portfolio review…`; }
  try {
    await api("/api/portfolio-review", "POST", {});
    if (status) status.innerHTML = `Portfolio review started — watch the Task Center. When it finishes, hit <strong>Optimize</strong> to use the fresh convictions.`;
  } catch (e) {
    if (status) { status.textContent = "Could not start the review: " + (e as Error).message; status.classList.add("err"); }
  } finally {
    if (btn) btn.disabled = false;
  }
}

let _wired = false;
function initOptimizer(): void {
  if (_wired) return;
  _wired = true;
  $("#opt-run")?.addEventListener("click", () => runOptimize());
  $("#opt-stage")?.addEventListener("click", () => stageProposal());
  $("#opt-review")?.addEventListener("click", () => reviewHoldings());
  // Per-row exclude toggle (delegated; the table is re-rendered on load).
  document.addEventListener("change", (e) => {
    const cb = (e.target as HTMLElement).closest?.<HTMLInputElement>("[data-opt-exclude]");
    if (!cb) return;
    const sym = cb.dataset.optExclude || "";
    if (cb.checked) _excluded.add(sym); else _excluded.delete(sym);
    cb.closest("tr")?.classList.toggle("opt-row-excluded", cb.checked);
  });
}

export { initOptimizer, loadOptimizer };
