// The portfolio-history tables: "By sector", "Activity by name", and the raw
// trade ledger, plus their shared pager and the row primitives (clickable
// ticker chip, caret cell, money cells). Extracted from history.ts; pure
// rendering over the already-shaped groups -- the composer decides which
// tables to show and supplies the data.
import { el, esc, sensitive } from "../core";
import { openTicker } from "../ticker-nav";
import {
  contractLabel, paginate,
  type ActivityGroup, type ActivityRow, type Page, type SectorGroup, type Trade,
} from "./data";
import { ccyTag, fmtMoney, fmtSigned } from "./format";

const ACTIVITY_PAGE = 25;
const LEDGER_PAGE = 50;

// ---- sortable "Activity by name" columns -----------------------------------
export type ActivitySortKey = "name" | "ccy" | "trades" | "bought" | "sold" | "flow" | "pnl";
export interface ActivitySort { key: ActivitySortKey; dir: "asc" | "desc"; }

// Per-column value pulled off a group for comparison. Name/Ccy compare as
// strings; the rest are base-currency numbers.
const ACTIVITY_SORT_VAL: Record<ActivitySortKey, (g: ActivityGroup) => number | string> = {
  name: (g) => (g.label || "").toUpperCase(),
  ccy: (g) => (g.currency || "").toUpperCase(),
  trades: (g) => Number(g.n) || 0,
  bought: (g) => Number(g.bought_base) || 0,
  sold: (g) => Number(g.sold_base) || 0,
  flow: (g) => Number(g.net_base_cash_flow) || 0,
  pnl: (g) => Number(g.base_realized_pnl) || 0,
};

// Numbers open descending (largest first is what you usually want for money /
// counts); text opens ascending (A→Z).
const activityDefaultDir = (key: ActivitySortKey): "asc" | "desc" =>
  key === "name" || key === "ccy" ? "asc" : "desc";

// Sort a COPY of the groups; the primary key breaks ties on name so the order is
// stable and direction-independent (a name tiebreak never flips with dir).
export function sortActivityGroups(groups: ActivityGroup[], sort: ActivitySort): ActivityGroup[] {
  const get = ACTIVITY_SORT_VAL[sort.key];
  const sign = sort.dir === "asc" ? 1 : -1;
  return [...groups].sort((a, b) => {
    const va = get(a), vb = get(b);
    const cmp = typeof va === "string" || typeof vb === "string"
      ? String(va).localeCompare(String(vb))
      : (va as number) - (vb as number);
    if (cmp !== 0) return cmp * sign;
    return (a.label || "").localeCompare(b.label || "");
  });
}

interface ActivityCol { key: ActivitySortKey; label: string; num: boolean; }

// A clickable/keyboard-operable header row. The active column carries an arrow
// and aria-sort; clicking re-sorts (toggling direction on the active column).
function activityHead(cols: ActivityCol[], sort: ActivitySort, onSort: (k: ActivitySortKey) => void): HTMLElement {
  const thead = el("thead");
  const tr = el("tr");
  cols.forEach((c) => {
    const active = sort.key === c.key;
    const th = el("th", (c.num ? "num " : "") + "hist-sortable" + (active ? " active" : ""));
    th.setAttribute("role", "button");
    th.tabIndex = 0;
    th.title = "Sort by " + c.label;
    th.setAttribute("aria-sort", active ? (sort.dir === "asc" ? "ascending" : "descending") : "none");
    const arrow = active ? (sort.dir === "asc" ? "\u2191" : "\u2193") : "";
    th.innerHTML = `<span class="hist-sort-lbl">${esc(c.label)}</span><span class="hist-sort-ind">${arrow}</span>`;
    const go = () => onSort(c.key);
    th.addEventListener("click", go);
    th.addEventListener("keydown", (ev) => {
      const k = (ev as KeyboardEvent).key;
      if (k === "Enter" || k === " ") { ev.preventDefault(); go(); }
    });
    tr.appendChild(th);
  });
  thead.appendChild(tr);
  return thead;
}

// Leading caret cell for a grouped row. Always rendered (empty when there's
// nothing to expand) so expandable and plain names share the same left edge.
const caretCell = (expandable: boolean): string =>
  `<span class="hist-caret">${expandable ? "\u25B8" : ""}</span>`;

// Clickable ticker that opens the dossier (cache-first, via the shared
// rebalance opener). data-ticker carries the symbol so one per-row wiring pass
// can find it. ``text`` lets the visible label differ from the symbol (e.g.
// "GEN shares" links to GEN); defaults to the symbol itself.
const tickerSpan = (sym: string | undefined, text: string | null = null): string =>
  `<span class="hist-tick" data-ticker="${esc(sym)}" title="Open ${esc(sym)} dossier">` +
  `${esc(text == null ? sym : text)}</span>`;

// Wire every .hist-tick inside a freshly built row to open its dossier. Stops
// propagation so clicking the ticker in an expandable row doesn't also toggle
// the row open/closed (the row's own click handler sits above it in bubbling).
function wireTickers(scope: HTMLElement): void {
  scope.querySelectorAll<HTMLElement>(".hist-tick[data-ticker]").forEach((node) => {
    node.addEventListener("click", (ev) => {
      ev.stopPropagation();
      const sym = node.dataset.ticker;
      if (sym) openTicker(sym);
    });
  });
}

// Prev / page-of / Next control. Renders nothing interactive for a single page.
function drawPager(pager: HTMLElement, pg: Page<unknown>, onGo: (page: number) => void): void {
  pager.innerHTML = "";
  if (pg.pages <= 1) {
    pager.appendChild(el("span", "hint", `${pg.total} row${pg.total === 1 ? "" : "s"}`));
    return;
  }
  const prev = el("button", "linklike", "\u2039 Prev");
  const next = el("button", "linklike", "Next \u203a");
  prev.disabled = pg.page <= 1;
  next.disabled = pg.page >= pg.pages;
  prev.addEventListener("click", () => onGo(pg.page - 1));
  next.addEventListener("click", () => onGo(pg.page + 1));
  pager.appendChild(prev);
  pager.appendChild(el("span", "hist-pager-label",
    `Page ${pg.page} of ${pg.pages} · ${pg.total} rows`));
  pager.appendChild(next);
}

// Money cells share one shape across the group row and its contract members.
// Both cash flow and P&L are BASE-currency (the only way cross-ticker sums are
// valid); the per-name native currency is shown in its own column instead.
function activityCells(r: ActivityGroup | SectorGroup | ActivityRow): string {
  const pnl = Number(r.base_realized_pnl) || 0;
  const flowCls = (r.net_base_cash_flow ?? 0) >= 0 ? "good" : "bad";
  const pnlCls = pnl > 0 ? "good" : pnl < 0 ? "bad" : "muted";
  // Gross cash out (bought) and in (sold), base currency. Unsigned magnitudes;
  // net cash flow keeps the signed good/bad treatment.
  const b = Number(r.buys) || 0, s = Number(r.sells) || 0;
  const split = `${b} buy${b === 1 ? "" : "s"} \u00b7 ${s} sell${s === 1 ? "" : "s"}`;
  return `<td class="num"><span class="ccy">${esc((r as ActivityGroup).currency || "")}</span></td>` +
    `<td class="num"><span class="hist-trades-n" title="${esc(split)}">${esc(r.n)}</span></td>` +
    `<td class="num muted">${sensitive(fmtMoney(r.bought_base), "amount bought")}</td>` +
    `<td class="num muted">${sensitive(fmtMoney(r.sold_base), "amount sold")}</td>` +
    `<td class="num ${flowCls}">${sensitive(fmtSigned(r.net_base_cash_flow), "cash flow")}</td>` +
    `<td class="num ${pnlCls}">${sensitive(fmtSigned(pnl), "realized pnl")}</td>`;
}

// "By sector" table: one row per sector, expandable to the folded names within.
// Columns mirror "Activity by name" so member rows can reuse activityCells; the
// sector header row leaves the Ccy cell blank (a sector spans many currencies).
export function sectorTable(secGroups: SectorGroup[], baseCcy: string): HTMLElement {
  const baseLbl = baseCcy ? ` (${esc(baseCcy)})` : "";
  const tbl = el("table", "risk-pos-table hist-activity");
  tbl.innerHTML =
    `<thead><tr><th>Sector</th><th class="num">Ccy</th><th class="num">Trades</th>` +
    `<th class="num">Bought${baseLbl}</th><th class="num">Sold${baseLbl}</th>` +
    `<th class="num">Net cash flow${baseLbl}</th><th class="num">Realized P&L${baseLbl}</th></tr></thead>`;
  const body = el("tbody");
  tbl.appendChild(body);
  secGroups.forEach((g) => {
    const members = g.groups || [];
    const expandable = members.length > 0;
    const tr = el("tr", "hist-grp" + (expandable ? " expandable" : ""));
    const badge = ` <span class="hist-optbadge">${g.names} name${g.names === 1 ? "" : "s"}</span>`;
    tr.innerHTML = `<td class="risk-pos-sym">${caretCell(expandable)}${esc(g.sector)}${badge}</td>` + activityCells(g);
    body.appendChild(tr);
    if (!expandable) return;
    const memberRows = members.map((m) => {
      const mtr = el("tr", "hist-member");
      mtr.hidden = true;
      mtr.innerHTML = `<td class="risk-pos-sym hist-member-sym">${tickerSpan(m.key, m.label)}</td>` + activityCells(m);
      body.appendChild(mtr);
      wireTickers(mtr);
      return mtr;
    });
    tr.addEventListener("click", () => {
      const open = tr.classList.toggle("open");
      const c = tr.querySelector(".hist-caret");
      if (c) c.textContent = open ? "\u25BE" : "\u25B8";
      memberRows.forEach((mr) => (mr.hidden = !open));
    });
  });
  return tbl;
}

export function activityTable(groups: ActivityGroup[], baseCcy: string): HTMLElement {
  const wrap = el("div");
  const baseLbl = baseCcy ? ` (${esc(baseCcy)})` : "";
  const cols: ActivityCol[] = [
    { key: "name", label: "Name", num: false },
    { key: "ccy", label: "Ccy", num: true },
    { key: "trades", label: "Trades", num: true },
    { key: "bought", label: `Bought${baseLbl}`, num: true },
    { key: "sold", label: `Sold${baseLbl}`, num: true },
    { key: "flow", label: `Net cash flow${baseLbl}`, num: true },
    { key: "pnl", label: `Realized P&L${baseLbl}`, num: true },
  ];
  const tbl = el("table", "risk-pos-table hist-activity");
  // Default order mirrors the data layer's own sort (most-traded first).
  let sort: ActivitySort = { key: "trades", dir: "desc" };
  let thead = activityHead(cols, sort, onSort);
  tbl.appendChild(thead);
  const body = el("tbody");
  tbl.appendChild(body);
  const pager = el("div", "hist-pager");
  let page = 1;

  function onSort(key: ActivitySortKey): void {
    sort = sort.key === key
      ? { key, dir: sort.dir === "desc" ? "asc" : "desc" }  // toggle the active column
      : { key, dir: activityDefaultDir(key) };
    page = 1;  // a re-sort invalidates the current page window
    const next = activityHead(cols, sort, onSort);
    tbl.replaceChild(next, thead);
    thead = next;
    draw();
  }

  const draw = () => {
    const pg = paginate(sortActivityGroups(groups, sort), page, ACTIVITY_PAGE);
    page = pg.page;
    body.innerHTML = "";
    pg.items.forEach((g) => {
      // Expand when there's more than one leg to reveal (shares + options, or
      // several contracts). A lone stock or single contract has nothing to open.
      const expandable = g.members.length > 1;
      const tr = el("tr", "hist-grp" + (expandable ? " expandable" : ""));
      const badge = g.opt_count
        ? ` <span class="hist-optbadge">${g.opt_count} opt${g.opt_count > 1 ? "s" : ""}</span>`
        : "";
      tr.innerHTML = `<td class="risk-pos-sym">${caretCell(expandable)}${tickerSpan(g.key, g.label)}${badge}</td>` + activityCells(g);
      body.appendChild(tr);
      wireTickers(tr);
      if (!expandable) return;
      const memberRows = g.members.map((m) => {
        const mtr = el("tr", "hist-member");
        mtr.hidden = true;
        // Distinguish the equity leg from contracts when both sit under one name.
        // The equity leg's ticker links to its dossier; option contracts have no
        // ticker text of their own (the parent group row already links it).
        const labelHtml = m.is_option ? esc(contractLabel(m)) : `${tickerSpan(m.symbol)} shares`;
        mtr.innerHTML =
          `<td class="risk-pos-sym hist-member-sym">${labelHtml}</td>` + activityCells(m);
        body.appendChild(mtr);
        wireTickers(mtr);
        return mtr;
      });
      tr.addEventListener("click", () => {
        const open = tr.classList.toggle("open");
        const c = tr.querySelector(".hist-caret");
        if (c) c.textContent = open ? "\u25BE" : "\u25B8";
        memberRows.forEach((m) => (m.hidden = !open));
      });
    });
    drawPager(pager, pg, (p) => { page = p; draw(); });
  };
  draw();

  wrap.appendChild(tbl);
  wrap.appendChild(pager);
  return wrap;
}

export function tradeTable(trades: Trade[], baseCcy: string): HTMLElement {
  const all = [...trades].reverse(); // newest first; full set, now paginated
  const baseLbl = baseCcy ? ` (${esc(baseCcy)})` : "";
  const wrap = el("div");
  const tbl = el("table", "risk-pos-table hist-trades");
  tbl.innerHTML =
    `<thead><tr><th>Date</th><th>Side</th><th>Name</th><th class="num">Qty</th>` +
    `<th class="num">Price</th><th class="num">Cash flow${baseLbl}</th>` +
    `<th class="num">Realized P&L</th></tr></thead>`;
  const body = el("tbody");
  tbl.appendChild(body);
  const pager = el("div", "hist-pager");
  let page = 1;

  const draw = () => {
    const pg = paginate(all, page, LEDGER_PAGE);
    page = pg.page;
    body.innerHTML = "";
    pg.items.forEach((t) => {
      const provisional = !!t.provisional || t.source === "live";
      const tr = el("tr", provisional ? "hist-trade-live" : "");
      const buy = t.side === "BUY";
      const pnlCls = t.realized_pnl > 0 ? "good" : t.realized_pnl < 0 ? "bad" : "muted";
      // Options: show the readable contract ("AMD 19APR24 7.5 P") not the cryptic
      // symbol, and leave it un-linked (it's a contract, not a ticker). Equities
      // link their ticker to the dossier.
      const nameHtml = t.is_option ? esc(t.description || t.symbol) : tickerSpan(t.symbol);
      const liveBadge = provisional
        ? ` <span class="hist-livebadge" title="Live execution; cash flow and realized P&L arrive with the next finalized Flex statement">live</span>`
        : "";
      const pendingMoney = `<span class="muted" title="Pending finalized Flex statement">\u2014</span>`;
      // Price + realized P&L are NATIVE currency (per ticker); cash flow is base.
      tr.innerHTML =
        `<td>${esc(t.date)}</td>` +
        `<td class="${buy ? "good" : "bad"}">${esc(t.side)}</td>` +
        `<td class="risk-pos-sym">${nameHtml}${liveBadge}</td>` +
        `<td class="num">${esc(Math.abs(Number(t.quantity)))}</td>` +
        `<td class="num">${esc(t.price)}${ccyTag(t.currency)}</td>` +
        `<td class="num">${provisional ? pendingMoney : sensitive(fmtSigned(t.base_cash_flow), "cash flow")}</td>` +
        `<td class="num ${pnlCls}">${provisional ? pendingMoney : t.realized_pnl ? sensitive(fmtSigned(t.realized_pnl), "realized pnl") + ccyTag(t.currency) : "\u2014"}</td>`;
      body.appendChild(tr);
      wireTickers(tr);
    });
    drawPager(pager, pg, (p) => { page = p; draw(); });
  };
  draw();

  wrap.appendChild(tbl);
  wrap.appendChild(pager);
  return wrap;
}
