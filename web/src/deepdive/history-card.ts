// Recent-pulls change log: a collapsible table of the last few cached snapshots
// for a ticker, with per-row delete. Extracted from deepdive.ts; reads/writes
// via /api/history/* and repaints the dossier's history slot in place.
//
// Each row pairs the market's *perceived value* at that pull (price + forward
// P/E, with the change vs the previous pull) against *our guidance* at the same
// moment (held weight, position vs the model band, and the research role). That
// turns a list of prices into a story of how the read drifted over time.
import { $, api, decisionClass, el, esc, fmtPrice, fmtSignedWeight, fmtWeight, fmtX, simpleTable } from "../core";
import { collapsibleCard } from "./cards";

interface HistoryRow {
  as_of?: string | null;
  price?: number | null;
  pe_fwd?: number | null;
  ps?: number | null;
  revenue_ttm_usd_b?: number | null;
  data_quality?: string;
  stamp?: string | number | null;
  // guidance-at-pull-time (widened server reader)
  decision?: string | null;
  weight_pct?: number | null;
  status?: string | null;
  band_low?: number | null;
  band_high?: number | null;
  gap_to_band_pct?: number | null;
}

// A signed % change vs the prior pull, arrow + colour. Tiny moves read as flat
// so noise doesn't masquerade as signal.
function deltaPct(cur?: number | null, prev?: number | null): string {
  if (cur == null || prev == null || prev === 0) return "";
  const d = ((cur - prev) / Math.abs(prev)) * 100;
  if (Math.abs(d) < 0.05) return `<span class="hd-delta flat">\u00b10%</span>`;
  return `<span class="hd-delta ${d > 0 ? "up" : "down"}">${d > 0 ? "\u25b2" : "\u25bc"} ${Math.abs(d).toFixed(1)}%</span>`;
}

// An absolute change vs the prior pull (for multiples). Neutral tone: P/E
// expansion isn't inherently "good", it's just richer — we show direction, not
// judgement.
function deltaAbs(cur?: number | null, prev?: number | null, unit = "x"): string {
  if (cur == null || prev == null) return "";
  const d = cur - prev;
  if (Math.abs(d) < 0.05) return `<span class="hd-delta flat">\u00b10</span>`;
  return `<span class="hd-delta ${d > 0 ? "up" : "down"} neutral">${d > 0 ? "+" : "\u2212"}${Math.abs(d).toFixed(1)}${unit}</span>`;
}

// Our position vs the model band at pull time: status word + signed gap, with
// the band range itself in the tooltip.
function bandCell(h: HistoryRow): string {
  if (h.band_low == null || h.band_high == null) return `<span class="muted">not modeled</span>`;
  const range = `${fmtWeight(h.band_low)}\u2013${fmtWeight(h.band_high)}`;
  const status = (h.status || "").replace(/_/g, " ") || "\u2014";
  const gap = h.gap_to_band_pct;
  const cls = gap == null ? "" : gap > 0 ? "good" : gap < 0 ? "bad" : "";
  return `<span class="hd-band" title="target band ${esc(range)}">${esc(status)}</span>` +
    (gap == null ? "" : ` <span class="hd-gap ${cls}">${esc(fmtSignedWeight(gap))}</span>`);
}

// The research role pill, flagged when it differs from the previous pull so a
// stance change jumps out of the column.
function roleCell(h: HistoryRow, prior?: HistoryRow): string {
  const d = h.decision || "research";
  const changed = !!(prior && prior.decision && prior.decision !== d);
  return `<span class="decision-pill ${decisionClass(d)}">${esc(d.replace(/_/g, " "))}</span>` +
    (changed ? `<span class="hd-changed" title="was ${esc((prior!.decision || "").replace(/_/g, " "))}">changed</span>` : "");
}

interface HistoryRec {
  symbol?: string;
  history?: HistoryRow[];
}

export function renderHistory(rec: HistoryRec): HTMLElement {
  // undefined == not fetched yet (streaming in). Show the section shell with a
  // progress bar overlaid so the rest of the dossier isn't held hostage to it.
  if (rec.history === undefined) {
    const card = el("div", "card section-loading");
    card.appendChild(el("h2", "section", "Recent pulls"));
    const body = el("div", "section-body");
    body.appendChild(el("div", "hint", "Fetching the change log\u2026"));
    body.appendChild(el("div", "section-overlay", `<div class="progress-bar"><span></span></div>`));
    card.appendChild(body);
    return card;
  }
  const rows = rec.history || [];
  const meta = rows.length ? `${rows.length} snapshot${rows.length === 1 ? "" : "s"}` : "none yet";
  const { details, body } = collapsibleCard("Recent pulls", { meta });
  if (!rows.length) {
    body.appendChild(el("div", "hint", "No history yet. Pull this ticker again later and this becomes a change log instead of a memory test."));
    return details;
  }
  body.appendChild(el("div", "hint", "Perceived value (price, forward P/E \u2014 with the move since the prior pull) next to our guidance (held weight, position vs the model band, research role) at each pull."));
  if (rows.length === 1) {
    body.appendChild(el("div", "hint hd-single", "Only one pull so far \u2014 the change columns light up once you pull again."));
  }
  const table = simpleTable<HistoryRow>({
    className: "history-table hd-table",
    head: `<tr><th>As of</th><th class="num">Price</th><th class="num">Fwd P/E</th><th class="num">Held</th><th>vs band</th><th>Role</th><th>Trust</th><th></th></tr>`,
    rows: rows.slice(0, 8),
    cells: (h, i) => {
      const prior = rows[i + 1];  // the next-older pull (rows are newest-first)
      const date = h.as_of ? new Date(h.as_of) : null;
      const when = date ? `${date.toLocaleDateString(undefined, { month: "short", day: "numeric" })} <span class="hd-time">${date.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}</span>` : "n/a";
      return `<td>${when}</td>` +
        `<td class="num">${esc(fmtPrice(h.price))}<div class="hd-sub">${deltaPct(h.price, prior?.price)}</div></td>` +
        `<td class="num">${esc(fmtX(h.pe_fwd))}<div class="hd-sub">${deltaAbs(h.pe_fwd, prior?.pe_fwd)}</div></td>` +
        `<td class="num">${esc(fmtWeight(h.weight_pct))}</td>` +
        `<td>${bandCell(h)}</td>` +
        `<td>${roleCell(h, prior)}</td>` +
        `<td><span class="dot ${esc(h.data_quality || "INFO")}"></span>${esc(h.data_quality || "INFO")}</td>`;
    },
    onRow: (tr, h) => {
      // The delete column carries a per-row listener, so it's appended as real
      // DOM after the string cells rather than baked into the cells() HTML.
      const delCell = el("td", "history-del-cell");
      if (h.stamp) {
        const del = el("button", "history-del", "\u2715");
        del.type = "button";
        del.title = "Delete this snapshot";
        del.setAttribute("aria-label", "Delete this snapshot");
        del.addEventListener("click", () => {
          const when = h.as_of ? new Date(h.as_of).toLocaleString() : "this";
          if (!confirm(`Delete the ${when} snapshot for ${rec.symbol}? This cannot be undone.`)) return;
          del.disabled = true;
          deleteHistorySnapshot(rec, h.stamp).catch((e) => {
            del.disabled = false;
            alert("Delete failed: " + (e as Error).message);
          });
        });
        delCell.appendChild(del);
      }
      tr.appendChild(delCell);
    },
  });
  body.appendChild(table);
  return details;
}

async function deleteHistorySnapshot(rec: HistoryRec, stamp: string | number | null | undefined): Promise<void> {
  const res = await api("/api/history/delete", "POST", { symbol: rec.symbol, stamp });
  rec.history = res.history || [];
  const slot = $("#dd-result [data-slot='history']");
  if (slot) {
    slot.innerHTML = "";
    slot.appendChild(renderHistory(rec));
  }
}
