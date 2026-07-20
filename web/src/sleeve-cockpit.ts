// Allocation-segment cockpit: one sleeve's members, held vs band, advisory OC.
// Read-only v1 — edits stay on Composition / Guided / Rebalance.
import { $, api, esc, fmtWeight, loadError, spinner } from "./core";
import { navFromUrl, pushNav, setActiveView } from "./shell";

interface AllocMember {
  symbol: string;
  current_pct?: number | null;
  target_pct?: number | null;
  cap?: number | null;
  suggest_delta_pct?: number | null;
  member_action?: string | null;
  conviction?: string | null;
  prospect?: number | null;
  oc_score?: number | null;
  oc_rank?: number | null;
  data_quality?: string | null;
  decision?: string | null;
  home_segment?: string | null;
}

interface AllocDetail {
  name: string;
  sleeve: {
    name: string;
    low?: number | null;
    high?: number | null;
    mid?: number | null;
    current_pct?: number | null;
    status?: string | null;
    rule?: string;
    note?: string;
    action?: string | null;
    suggest_delta_pct?: number | null;
  };
  members: AllocMember[];
}

function statusClass(status: string | null | undefined): string {
  const s = (status || "").toUpperCase();
  if (s === "IN") return "alloc-ok";
  if (s === "ABOVE" || s === "BELOW") return "alloc-drift";
  return "";
}

function fmtDelta(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v) || Math.abs(v) < 0.005) return "–";
  const sign = v > 0 ? "+" : "";
  return `${sign}${v.toFixed(2)}%`;
}

function panelHtml(d: AllocDetail): string {
  const sl = d.sleeve;
  const band = (sl.low != null && sl.high != null)
    ? `${fmtWeight(sl.low)}–${fmtWeight(sl.high)}`
    : "no band";
  const note = sl.note ? `<p class="hint alloc-note">${esc(sl.note)}</p>` : "";
  const rows = (d.members || []).map((m) => {
    const deltaCls = (m.suggest_delta_pct || 0) > 0.05 ? "pos"
      : (m.suggest_delta_pct || 0) < -0.05 ? "neg" : "";
    return `<tr>
      <td><button type="button" class="linklike" data-open-ticker="${esc(m.symbol)}">${esc(m.symbol)}</button></td>
      <td class="num">${m.oc_rank == null ? "–" : m.oc_rank}</td>
      <td class="num">${m.prospect == null ? "–" : Number(m.prospect).toFixed(0)}</td>
      <td>${esc(m.conviction || "–")}</td>
      <td class="num">${fmtWeight(m.current_pct)}</td>
      <td class="num">${m.target_pct == null ? "–" : fmtWeight(m.target_pct)}</td>
      <td class="num">${m.cap == null ? "–" : fmtWeight(m.cap)}</td>
      <td class="num ${deltaCls}">${fmtDelta(m.suggest_delta_pct)}</td>
      <td class="muted">${esc(m.member_action || "–")}</td>
    </tr>`;
  }).join("");
  return `
    <div class="alloc-summary">
      <div class="alloc-stat">
        <span class="muted">Held</span>
        <strong class="${statusClass(sl.status)}">${fmtWeight(sl.current_pct)}</strong>
      </div>
      <div class="alloc-stat">
        <span class="muted">Band</span>
        <strong>${esc(band)}</strong>
        <span class="muted">mid ${sl.mid == null ? "–" : fmtWeight(sl.mid)}</span>
      </div>
      <div class="alloc-stat">
        <span class="muted">Status</span>
        <strong class="${statusClass(sl.status)}">${esc(sl.status || "–")}</strong>
      </div>
      <div class="alloc-stat">
        <span class="muted">Rule</span>
        <strong>${esc(sl.rule || "–")}</strong>
      </div>
      <div class="alloc-stat">
        <span class="muted">Plan Δ</span>
        <strong>${fmtDelta(sl.suggest_delta_pct)}</strong>
        <span class="muted">${esc(sl.action || "")}</span>
      </div>
    </div>
    ${note}
    <p class="hint">OC rank is advisory (opportunity cost among sleeve peers). Band discipline still owns trades.</p>
    <div class="seg-table-scroll">
      <table class="segment-table alloc-table">
        <thead><tr>
          <th>Symbol</th><th class="num">OC #</th><th class="num">Prospect</th>
          <th>Conviction</th><th class="num">Held %</th><th class="num">Share tgt</th>
          <th class="num">Cap</th><th class="num">Δ %</th><th>Action</th>
        </tr></thead>
        <tbody>${rows || `<tr><td colspan="9" class="muted">No members in this sleeve.</td></tr>`}</tbody>
      </table>
    </div>`;
}

async function loadAlloc(): Promise<void> {
  const status = $("#alloc-status");
  const body = $("#alloc-body");
  const title = $("#alloc-title");
  if (!body || !status) return;
  const name = (navFromUrl().sleeve || "").trim();
  if (!name) {
    status.classList.add("err");
    status.textContent = "No allocation sleeve selected — open one from Composition.";
    body.innerHTML = "";
    if (title) title.textContent = "Allocation segment";
    return;
  }
  if (title) title.textContent = name;
  status.classList.remove("err");
  status.innerHTML = `${spinner()} Loading ${esc(name)}…`;
  body.innerHTML = "";
  try {
    const d = await api<AllocDetail>(`/api/sleeve/${encodeURIComponent(name)}`);
    status.classList.remove("err");
    status.textContent = `${(d.members || []).length} members · advisory OC + band plan`;
    body.innerHTML = panelHtml(d);
  } catch (e) {
    loadError(status, "Allocation sleeve failed", e);
  }
}

function openAlloc(name: string): void {
  const sleeve = (name || "").trim();
  if (!sleeve) return;
  pushNav({ view: "alloc", sleeve });
  setActiveView("alloc");
  void loadAlloc();
  window.scrollTo(0, 0);
}

let _wired = false;
function initAlloc(): void {
  if (_wired) return;
  _wired = true;
  const host = $("#view-alloc");
  if (!host) return;
  host.addEventListener("click", async (e) => {
    const t = (e.target as HTMLElement).closest<HTMLElement>("[data-open-ticker]");
    if (!t?.dataset.openTicker) return;
    const { openTicker } = await import("./ticker-nav");
    openTicker(t.dataset.openTicker);
  });
  // Composition (and elsewhere) deep-links via data-open-alloc.
  document.addEventListener("click", (e) => {
    const el = (e.target as HTMLElement).closest<HTMLElement>("[data-open-alloc]");
    if (!el?.dataset.openAlloc) return;
    e.preventDefault();
    openAlloc(el.dataset.openAlloc);
  });
}

export { initAlloc, loadAlloc, openAlloc };
