// Recent-pulls change log: a collapsible table of the last few cached snapshots
// for a ticker, with per-row delete. Extracted from deepdive.ts; reads/writes
// via /api/history/* and repaints the dossier's history slot in place.
import { $, api, el, esc, fmtB, fmtPrice, fmtX, simpleTable } from "../core";
import { collapsibleCard } from "./cards";

interface HistoryRow {
  as_of?: string | null;
  price?: number | null;
  pe_fwd?: number | null;
  ps?: number | null;
  revenue_ttm_usd_b?: number | null;
  data_quality?: string;
  stamp?: string | number | null;
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
  const table = simpleTable<HistoryRow>({
    className: "history-table",
    head: `<tr><th>As of</th><th class="num">Price</th><th class="num">Fwd P/E</th><th class="num">P/S</th><th class="num">Revenue</th><th>Trust</th><th></th></tr>`,
    rows: rows.slice(0, 8),
    cells: (h) =>
      `<td>${esc(h.as_of ? new Date(h.as_of).toLocaleString() : "n/a")}</td>` +
      `<td class="num">${esc(fmtPrice(h.price))}</td>` +
      `<td class="num">${esc(fmtX(h.pe_fwd))}</td>` +
      `<td class="num">${esc(fmtX(h.ps))}</td>` +
      `<td class="num">${esc(fmtB(h.revenue_ttm_usd_b))}</td>` +
      `<td><span class="dot ${esc(h.data_quality || "INFO")}"></span>${esc(h.data_quality || "INFO")}</td>`,
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
