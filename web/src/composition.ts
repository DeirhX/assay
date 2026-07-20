// Allocation-segment composition editor: LLM (or heuristic) propose → hand-tune
// midpoints → stage into the working draft. Sleeves are the partition; this is
// the whole-book "how big is each segment?" control surface.
import { $, api, esc, fmtWeight, loadError, spinner } from "./core";

interface CompSegment {
  name: string;
  members: string[];
  member_count: number;
  current_pct: number;
  low?: number | null;
  high?: number | null;
  target_pct?: number | null;
  rule?: string;
  note?: string;
}
interface CompMigration {
  counts?: { standalone?: number; sleeves_touched?: number };
  sleeves?: { name: string; adding: string[] }[];
}
interface CompSnapshot {
  segments: CompSegment[];
  unassigned: { symbol: string; current_pct: number; home_segment?: string | null }[];
  cash_target_pct?: number | null;
  cash?: { pct_of_nav?: number; status?: string } | null;
  migration?: CompMigration;
}
interface CompProposal {
  source: string;
  targets: Record<string, number>;
  rationales?: Record<string, string>;
  cash_target_pct?: number | null;
  note?: string;
  snapshot?: CompSnapshot;
}

let _snap: CompSnapshot | null = null;
let _targets: Record<string, number> = {};
let _cash = 5;
let _note = "";
let _wired = false;

function sumTargets(): number {
  return Object.values(_targets).reduce((a, b) => a + (Number(b) || 0), 0);
}

function panelHtml(): string {
  if (!_snap) return "";
  const segs = _snap.segments || [];
  if (!segs.length) {
    return `<div class="comp-panel">
      <div class="subhead">Allocation segments</div>
      <p class="hint">No allocation sleeves in the target model yet. Add sleeves (or migrate standalone targets into them) before editing composition.</p>
      <details class="comp-advanced" open>
        <summary>Advanced ways to update targets</summary>
        <p class="hint">Composition is the default. These modes also stage into this draft — they do not trade.</p>
        <div class="comp-advanced-actions">
          <button class="ghost" type="button" data-shell-view="strategy">Guided plan →</button>
          <button class="ghost" type="button" data-shell-view="optimizer">Optimizer →</button>
        </div>
      </details>
    </div>`;
  }
  const total = sumTargets() + (Number(_cash) || 0);
  const over = total > 100.05;
  const rows = segs.map((s) => {
    const mid = _targets[s.name] ?? s.target_pct ?? s.current_pct ?? 0;
    const members = (s.members || []).slice(0, 6).join(", ")
      + ((s.members || []).length > 6 ? "…" : "");
    return `<tr>
      <td><button type="button" class="linklike comp-sleeve-link" data-open-alloc="${esc(s.name)}"
        title="Open ${esc(s.name)} members, held vs band, OC rank">${esc(s.name)}</button>
        <div class="muted comp-members">${esc(members) || "no members"}</div></td>
      <td class="num">${fmtWeight(s.current_pct)}</td>
      <td class="num">${s.target_pct == null ? "–" : fmtWeight(s.target_pct)}</td>
      <td class="num"><input class="comp-pct" type="number" min="0" max="60" step="0.5"
        data-seg="${esc(s.name)}" value="${Number(mid).toFixed(1)}" aria-label="${esc(s.name)} target %"></td>
    </tr>`;
  }).join("");
  const unassigned = (_snap.unassigned || []).slice(0, 8)
    .map((u) => `<span class="chip" title="home: ${esc(u.home_segment || "none")}">${esc(u.symbol)} ${fmtWeight(u.current_pct)}</span>`)
    .join(" ");
  const migN = _snap.migration?.counts?.standalone || 0;
  const migrateBtn = migN
    ? `<button class="ghost" type="button" id="comp-migrate" title="Fold ${migN} standalone target(s) into allocation sleeves (staged for review)">Fold ${migN} standalones into sleeves →</button>`
    : "";
  return `<div class="comp-panel">
    <div class="comp-head">
      <div>
        <div class="subhead">Allocation segments <span class="stage-sub">— ratios suggested by research, fine-tuned by hand</span></div>
        <p class="hint">Each name has one home segment (a sleeve). Edit midpoints, then stage into the working draft. Click a segment name for members &amp; OC rank. Band discipline still owns trades.</p>
      </div>
      <div class="comp-actions">
        ${migrateBtn}
        <button class="ghost" type="button" id="comp-propose" title="Ask the LLM (or heuristic fallback) for a segment mix">Propose from research</button>
        <button class="primary" type="button" id="comp-stage" title="Stage these midpoints into the working draft">Stage composition →</button>
      </div>
    </div>
    <div class="comp-direction">
      <label for="comp-direction">Direction (optional)</label>
      <input id="comp-direction" type="text" placeholder="e.g. lean into equipment, trim ETF sleeve"
        autocomplete="off">
    </div>
    ${_note ? `<p class="comp-note hint">${esc(_note)}</p>` : ""}
    <div class="seg-table-scroll">
      <table class="segment-table comp-table">
        <thead><tr>
          <th>Segment</th><th class="num">Held %</th><th class="num">Live mid</th><th class="num">Proposed mid</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
    <div class="comp-footer">
      <label>Cash target % of NAV
        <input class="comp-pct" id="comp-cash" type="number" min="0" max="40" step="0.5" value="${Number(_cash).toFixed(1)}">
      </label>
      <span class="comp-total ${over ? "err" : ""}">Σ segments + cash = <strong>${total.toFixed(1)}%</strong>${over ? " (over 100%)" : ""}</span>
    </div>
    ${unassigned ? `<div class="comp-unassigned"><span class="muted">Standalone targets (migrate into a segment):</span> ${unassigned}</div>` : ""}
    <details class="comp-advanced">
      <summary>Advanced ways to update targets</summary>
      <p class="hint">Composition is the default. These modes also stage into this draft — they do not trade.</p>
      <div class="comp-advanced-actions">
        <button class="ghost" type="button" data-shell-view="strategy">Guided plan →</button>
        <button class="ghost" type="button" data-shell-view="optimizer">Optimizer →</button>
      </div>
    </details>
    <div class="status" id="comp-status"></div>
  </div>`;
}

function readInputs(): void {
  document.querySelectorAll<HTMLInputElement>(".comp-pct[data-seg]").forEach((inp) => {
    const name = inp.dataset.seg;
    if (!name) return;
    const v = Number(inp.value);
    if (Number.isFinite(v)) _targets[name] = v;
  });
  const cashInp = $<HTMLInputElement>("#comp-cash");
  if (cashInp) {
    const v = Number(cashInp.value);
    if (Number.isFinite(v)) _cash = v;
  }
}

function mount(): void {
  const host = $("#composition-panel");
  if (!host) return;
  host.innerHTML = panelHtml();
}

async function loadComposition(): Promise<void> {
  const host = $("#composition-panel");
  if (!host) return;
  try {
    _snap = await api<CompSnapshot>("/api/composition");
    _targets = {};
    for (const s of _snap.segments || []) {
      _targets[s.name] = Number(s.target_pct ?? s.current_pct ?? 0);
    }
    _cash = Number(_snap.cash_target_pct ?? 5);
    _note = "";
    mount();
  } catch (e) {
    host.innerHTML = `<div class="comp-panel"><div class="status err">Could not load composition: ${esc((e as Error).message)}</div></div>`;
  }
}

function initComposition(): void {
  if (_wired) return;
  _wired = true;
  const host = $("#view-working-draft");
  if (!host) return;
  host.addEventListener("input", (e) => {
    if (!(e.target as HTMLElement).classList?.contains("comp-pct")
        && (e.target as HTMLElement).id !== "comp-cash") return;
    readInputs();
    const totalEl = host.querySelector(".comp-total");
    if (!totalEl) return;
    const total = sumTargets() + (Number(_cash) || 0);
    const over = total > 100.05;
    totalEl.classList.toggle("err", over);
    totalEl.innerHTML = `Σ segments + cash = <strong>${total.toFixed(1)}%</strong>${over ? " (over 100%)" : ""}`;
  });
  host.addEventListener("click", async (e) => {
    const propose = (e.target as HTMLElement).closest<HTMLElement>("#comp-propose");
    const stage = (e.target as HTMLElement).closest<HTMLElement>("#comp-stage");
    const migrate = (e.target as HTMLElement).closest<HTMLElement>("#comp-migrate");
    const status = () => $("#comp-status");
    if (migrate) {
      const n = _snap?.migration?.counts?.standalone || 0;
      if (!n) return;
      if (!window.confirm(
        `Fold ${n} standalone target(s) into allocation sleeves?\n\n`
        + `Tagged names go to their sleeve (aliases applied); ETFs → semis-etf; `
        + `untagged names need a destination sleeve. Review the working draft, `
        + `then Apply target model. This does not trade.`,
      )) return;
      migrate.setAttribute("disabled", "true");
      const st = status();
      if (st) { st.classList.remove("err"); st.innerHTML = `${spinner()} Migrating…`; }
      try {
        const res = await api<{ staged?: boolean; reason?: string; plan?: CompMigration }>(
          "/api/composition/migrate", "POST", {});
        if (st) {
          st.classList.remove("err");
          st.textContent = res.staged
            ? "Standalones folded into sleeves — review the draft below, then Apply."
            : (res.reason || "Nothing to migrate.");
        }
        const { loadStaging } = await import("./staging");
        await loadStaging();
      } catch (err) {
        loadError(status(), "Migrate failed", err);
      } finally {
        migrate.removeAttribute("disabled");
      }
      return;
    }
    if (propose) {
      propose.setAttribute("disabled", "true");
      const st = status();
      if (st) { st.classList.remove("err"); st.innerHTML = `${spinner()} Proposing composition…`; }
      try {
        const direction = $<HTMLInputElement>("#comp-direction")?.value || "";
        const prop = await api<CompProposal>("/api/composition/propose", "POST", {
          direction, use_llm: true,
        });
        _targets = { ...(prop.targets || {}) };
        if (prop.cash_target_pct != null) _cash = Number(prop.cash_target_pct);
        if (prop.snapshot) _snap = prop.snapshot;
        _note = prop.note || `Source: ${prop.source}`;
        mount();
        const st2 = status();
        if (st2) { st2.classList.remove("err"); st2.textContent = _note; }
      } catch (err) {
        loadError(status(), "Propose failed", err);
      } finally {
        propose.removeAttribute("disabled");
      }
      return;
    }
    if (stage) {
      readInputs();
      stage.setAttribute("disabled", "true");
      const st = status();
      if (st) { st.classList.remove("err"); st.innerHTML = `${spinner()} Staging…`; }
      try {
        await api("/api/composition/stage", "POST", {
          targets: _targets, cash_target_pct: _cash,
        });
        if (st) { st.classList.remove("err"); st.textContent = "Composition staged — review below, then Apply target model."; }
        // Refresh the draft list (dynamic import avoids a cycle with staging.ts).
        const { loadStaging } = await import("./staging");
        await loadStaging();
      } catch (err) {
        loadError(status(), "Stage failed", err);
      } finally {
        stage.removeAttribute("disabled");
      }
    }
  });
}

export { initComposition, loadComposition };
