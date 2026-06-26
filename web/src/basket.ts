// The cross-surface basket: a persistent shortlist of tickers starred from
// anywhere in the app (a deep-dive, a rebalance row, a strategy proposal). It
// sits UPSTREAM of the working draft — bare interest, no sizing. This module
// owns the basket view plus the ★ affordance: a single delegated click handler
// toggles membership, and a client-side mirror of the symbol set keeps every ★
// in the DOM in sync without a per-surface re-fetch.
//
// Dependency-light (core only) and all DOM wiring is deferred to initBasket(),
// called once from initShell — same import-cycle discipline as strategy/staging.
import { $, api, esc, fmtWeight } from "./core";
import { pushNav, setActiveView } from "./shell";

interface BasketBand { low?: number | null; high?: number | null; rule?: string }
interface BasketItem {
  symbol: string;
  source: string;
  note?: string;
  tier?: string;          // "want" | "curious"
  segment?: string | null;
  run?: string | null;
  conviction?: string | null;
  added_at?: string;
  held_pct?: number | null;
  targeted?: boolean;
  target_band?: BasketBand | null;
  in_sleeve?: string | null;
}
interface BasketView { items: BasketItem[]; count: number; symbols: string[] }

// Client mirror of the server's basket symbols, so ★ buttons across every
// surface can render their on/off state without each one fetching.
let _symbols = new Set<string>();
const norm = (s: string | null | undefined) => (s || "").toUpperCase();
const inBasket = (sym: string) => _symbols.has(norm(sym));

// Human label for where a pick came from.
const SOURCE_LABEL: Record<string, string> = {
  deepdive: "from analysis", rebalance: "from rebalance", strategy: "from strategy",
  analyses: "from a report", suggestion: "suggested", manual: "added by hand",
};

// ---- the ★ affordance (usable from any surface) ---------------------------
// The star's inner content. Dense table rows want the bare glyph; a roomy
// surface (the deep-dive header) wants a labeled button that reads as a button.
function starInner(on: boolean, labeled: boolean): string {
  const glyph = on ? "\u2605" : "\u2606";
  if (!labeled) return glyph;
  return `${glyph} ${on ? "In basket" : "Add to basket"}`;
}

// Returns a button's HTML. Surfaces drop this next to a ticker; the delegated
// handler below does the rest. `source` records where it was starred from.
// `labeled` renders the bigger, text-labeled button variant for roomy surfaces.
// `tier`/`segment`/`run` flow through to the pool: a star on a segment-discovered
// candidate carries that provenance and (typically) a "curious" tier.
function starHtml(symbol: string, source = "manual",
  opts: { labeled?: boolean; tier?: string; segment?: string; run?: string } = {}): string {
  const sym = norm(symbol);
  if (!sym) return "";
  const on = inBasket(sym);
  const labeled = !!opts.labeled;
  const cls = "basket-star" + (on ? " on" : "") + (labeled ? " basket-star-btn" : "");
  return `<button class="${cls}" type="button" ` +
    `data-basket-sym="${esc(sym)}" data-basket-src="${esc(source)}" ` +
    (labeled ? `data-basket-labeled="1" ` : "") +
    (opts.tier ? `data-basket-tier="${esc(opts.tier)}" ` : "") +
    (opts.segment ? `data-basket-seg="${esc(opts.segment)}" ` : "") +
    (opts.run ? `data-basket-run="${esc(opts.run)}" ` : "") +
    `aria-pressed="${on ? "true" : "false"}" ` +
    `title="${on ? "In your basket — click to remove" : "Add to basket"}">` +
    `${starInner(on, labeled)}</button>`;
}

// Reflect the current symbol set onto every ★ already in the DOM.
function syncStars(): void {
  document.querySelectorAll<HTMLButtonElement>(".basket-star").forEach((b) => {
    const on = inBasket(b.dataset.basketSym || "");
    const labeled = b.dataset.basketLabeled === "1";
    b.classList.toggle("on", on);
    b.setAttribute("aria-pressed", on ? "true" : "false");
    b.title = on ? "In your basket — click to remove" : "Add to basket";
    b.textContent = starInner(on, labeled);
  });
}

function updateBadge(count: number): void {
  const b = $("#basket-count");
  if (!b) return;
  b.textContent = count ? String(count) : "";
  b.hidden = !count;
}

function applyView(v: BasketView): void {
  _symbols = new Set((v.symbols || []).map(norm));
  updateBadge(v.count || 0);
  syncStars();
}

// ---- basket view ----------------------------------------------------------
const symLink = (sym: string) => {
  const s = esc(sym);
  return `<a class="tlink" data-ticker="${s}" href="?view=deepdive&ticker=${encodeURIComponent(sym)}" title="Open ${s} deep-dive"><strong>${s}</strong></a>`;
};

function planCell(it: BasketItem): string {
  if (it.in_sleeve) return `<span class="basket-plan ok" title="Governed via a sleeve">in plan · ${esc(it.in_sleeve)}</span>`;
  const b = it.target_band;
  if (b && (b.low != null || b.high != null)) {
    return `<span class="basket-plan ok">${b.low ?? "?"}–${b.high ?? "?"}% ${esc(b.rule || "")}</span>`;
  }
  return `<span class="basket-plan muted">not in plan</span>`;
}

// A two-state interest toggle. "Want" = size it into the plan; "Curious" =
// parked, sized only when the optimizer is told to include curious picks.
function tierCell(it: BasketItem): string {
  const t = (it.tier || "want").toLowerCase();
  const opt = (val: string, label: string, title: string) =>
    `<button class="tier-opt tier-${val}${t === val ? " on" : ""}" type="button" ` +
    `data-tier-sym="${esc(it.symbol)}" data-tier-set="${val}" ` +
    `aria-pressed="${t === val ? "true" : "false"}" title="${esc(title)}">${label}</button>`;
  return `<span class="tier-toggle">` +
    opt("want", "Want", "Actively size this into the optimized plan") +
    opt("curious", "Curious", "Park it — included only if you opt in to curious picks") +
    `</span>`;
}

function srcCell(it: BasketItem): string {
  const base = SOURCE_LABEL[it.source] || it.source;
  const seg = it.segment ? `<span class="basket-src-seg" title="Discovered in this segment analysis">· ${esc(it.segment)}</span>` : "";
  return `<span class="basket-src">${esc(base)}</span>${seg}`;
}

function rowHtml(it: BasketItem): string {
  const held = typeof it.held_pct === "number"
    ? `<span class="basket-held">${fmtWeight(it.held_pct)}</span>`
    : `<span class="muted">—</span>`;
  return `<tr>
    <td>${symLink(it.symbol)}</td>
    <td>${tierCell(it)}</td>
    <td>${srcCell(it)}</td>
    <td>${held}</td>
    <td>${planCell(it)}</td>
    <td class="basket-note">${esc(it.note || "")}</td>
    <td class="basket-row-actions">${starHtml(it.symbol, it.source)}</td>
  </tr>`;
}

function render(v: BasketView): void {
  const body = $("#basket-body");
  if (!body) return;
  const clearBtn = $<HTMLButtonElement>("#basket-clear");
  if (clearBtn) clearBtn.disabled = !v.count;
  if (!v.count) {
    body.innerHTML = `<div class="empty"><strong>Your basket is empty.</strong><br>` +
      `Star (\u2606) any ticker on a deep-dive, a rebalance row, or a strategy proposal to collect it here ` +
      `\u2014 a shortlist that follows you across the app. Drafting a rebalance plan from your picks is the next step.</div>`;
    return;
  }
  const heldCount = v.items.filter((i) => typeof i.held_pct === "number").length;
  const planned = v.items.filter((i) => i.targeted).length;
  const wantCount = v.items.filter((i) => (i.tier || "want") === "want").length;
  const curiousCount = v.count - wantCount;
  body.innerHTML =
    `<p class="basket-summary">${v.count} pick${v.count === 1 ? "" : "s"}` +
    ` \u00b7 <span class="tier-want">${wantCount} want</span> \u00b7 <span class="tier-curious">${curiousCount} curious</span>` +
    ` \u00b7 ${heldCount} already held \u00b7 ${planned} already in your plan.</p>` +
    `<table class="basket-table">` +
    `<thead><tr><th>Ticker</th><th>Interest</th><th>Source</th><th>Held</th><th>In plan</th><th>Note</th><th></th></tr></thead>` +
    `<tbody>${v.items.map(rowHtml).join("")}</tbody></table>` +
    `<div class="basket-actions">` +
    `<button class="primary basket-draft-btn" type="button" title="Run a guided strategy run over your picks">Draft a plan from these picks \u2192</button>` +
    `<button class="ghost basket-optimize-btn" type="button" title="Open the portfolio optimizer with these picks in the pool">Optimize portfolio \u2192</button>` +
    `</div>` +
    `<p class="hint basket-next">"Draft a plan" runs a guided strategy run over your picks (Deep Research + sized bands). ` +
    `"Optimize portfolio" pulls these picks together with your current holdings into one pool and sizes the whole book at once.</p>`;
}

// Turn the basket into a guided strategy run and hand off to the strategy view.
async function draftPlan(btn: HTMLButtonElement): Promise<void> {
  if (btn.disabled) return;
  const status = $("#basket-status");
  const prev = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Starting\u2026";
  if (status) {
    status.classList.remove("err");
    status.innerHTML = `<span class="spinner"></span> drafting a plan from your basket\u2026`;
  }
  try {
    const m = await api<{ run_id: string }>("/api/basket/draft-plan", "POST", {});
    if (status) status.textContent = "";
    pushNav({ view: "strategy", run: m.run_id });
    setActiveView("strategy");
  } catch (e) {
    if (status) { status.textContent = "Could not draft a plan: " + (e as Error).message; status.classList.add("err"); }
    btn.disabled = false;
    btn.textContent = prev || "Draft a plan from these picks \u2192";
  }
}

async function loadBasket(): Promise<void> {
  const status = $("#basket-status");
  if (status) { status.textContent = ""; status.classList.remove("err"); }
  try {
    const v = await api<BasketView>("/api/basket");
    applyView(v);
    render(v);
  } catch (e) {
    if (status) { status.textContent = "Could not load the basket: " + (e as Error).message; status.classList.add("err"); }
  }
}

// Refresh just the symbol set + badge (no view render), e.g. on app boot so the
// nav badge is correct before the basket view is ever opened.
async function refreshBasket(): Promise<void> {
  try { applyView(await api<BasketView>("/api/basket")); } catch (_e) { /* badge stays 0 */ }
}

let _wired = false;
function initBasket(): void {
  if (_wired) return;
  _wired = true;

  // One delegated handler for every ★ anywhere in the app. stopPropagation so a
  // star sitting inside a clickable row/link doesn't also trigger that.
  document.addEventListener("click", async (e) => {
    const btn = (e.target as HTMLElement).closest?.<HTMLButtonElement>(".basket-star");
    if (!btn) return;
    e.preventDefault();
    e.stopPropagation();
    const sym = btn.dataset.basketSym;
    if (!sym || btn.disabled) return;
    const src = btn.dataset.basketSrc || "manual";
    const removing = inBasket(sym);
    btn.disabled = true;
    try {
      const v = removing
        ? await api<BasketView>("/api/basket/remove", "POST", { symbol: sym })
        : await api<BasketView>("/api/basket/add", "POST", {
            symbol: sym, source: src,
            tier: btn.dataset.basketTier || "want",
            segment: btn.dataset.basketSeg || undefined,
            run: btn.dataset.basketRun || undefined,
          });
      applyView(v);
      if ($("#view-basket")?.classList.contains("active")) render(v);
    } catch (_err) {
      // Leave the ★ as-is; a failed toggle just doesn't change state.
    } finally {
      btn.disabled = false;
    }
  });

  // Per-row interest tier toggle (want <-> curious) in the basket/pool view.
  document.addEventListener("click", async (e) => {
    const t = (e.target as HTMLElement).closest?.<HTMLButtonElement>("[data-tier-set]");
    if (!t) return;
    e.preventDefault();
    e.stopPropagation();
    const sym = t.dataset.tierSym;
    const tier = t.dataset.tierSet;
    if (!sym || !tier || t.classList.contains("on")) return;
    try {
      const v = await api<BasketView>("/api/basket/tier", "POST", { symbol: sym, tier });
      applyView(v);
      if ($("#view-basket")?.classList.contains("active")) render(v);
    } catch (_err) { /* leave the toggle as-is on failure */ }
  });

  // "Optimize portfolio" hands off to the optimizer view (pool = picks + holdings).
  document.addEventListener("click", (e) => {
    const b = (e.target as HTMLElement).closest?.<HTMLButtonElement>(".basket-optimize-btn");
    if (!b) return;
    e.preventDefault();
    pushNav({ view: "optimizer" });
    setActiveView("optimizer");
  });

  // "Draft a plan" is re-rendered with the view, so delegate rather than bind.
  document.addEventListener("click", (e) => {
    const b = (e.target as HTMLElement).closest?.<HTMLButtonElement>(".basket-draft-btn");
    if (!b) return;
    e.preventDefault();
    draftPlan(b);
  });

  const clear = $<HTMLButtonElement>("#basket-clear");
  if (clear) clear.addEventListener("click", async () => {
    if (clear.disabled) return;
    if (!window.confirm("Clear the whole basket? Your picks are removed (your portfolio and plan are untouched).")) return;
    try {
      const v = await api<BasketView>("/api/basket/clear", "POST", {});
      applyView(v);
      render(v);
    } catch (e) {
      const status = $("#basket-status");
      if (status) { status.textContent = "Could not clear: " + (e as Error).message; status.classList.add("err"); }
    }
  });

  refreshBasket();
}

export { initBasket, loadBasket, starHtml, inBasket };
