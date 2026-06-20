// Locked, valuation-anchored ladder editor: a fair-value anchor plus buy/trim
// tranches (each margin% <-> price <-> size%). Locking sends the full ladder;
// the backend grades the rebalance by how many tranches the live price unlocks
// and uses the outermost tranche as the order's limit. You confirm every order.
// Extracted from deepdive.ts; pure rendering over /api/price-levels/*.
import { api, el, esc } from "../core";
import {
  activeFraction, fairValueStale, laddersMatch, marginFromPrice,
  priceFromMargin, sizeSum, sortLadder, type Side,
} from "../ladder";
import { relTime } from "../viewed";

// One editor row: a tranche under edit, plus references to its live DOM inputs
// so light syncs can repaint values without a full rebuild (which would steal
// focus mid-type).
interface Row {
  price: number | null;
  size: number | null;
  margin: number | null;
  _priceIn?: HTMLInputElement;
  _distEl?: HTMLElement;
}

// A tranche as the backend stores/returns it.
interface RawTranche {
  price?: number | null;
  size_pct?: number | null;
  discount_pct?: number | null;
  premium_pct?: number | null;
}

// A locked or suggested price-level record (also covers the legacy single-level
// shape via buy_below/trim_above).
interface LevelRecord {
  fair_value?: number | null;
  buy_ladder?: RawTranche[];
  trim_ladder?: RawTranche[];
  buy_below?: number | null;
  trim_above?: number | null;
  currency?: string;
  locked_at?: string | number | null;
}

interface PriceLevelsRec {
  symbol?: string;
  currency?: string;
  price?: { value?: number | null } | null;
}

interface AnalysisLike {
  meta?: { price_levels_suggested?: LevelRecord; currency?: string } | null;
  stem?: string;
}

interface LockPayload {
  symbol: string;
  fair_value: number | null;
  buy_ladder: RawTranche[];
  trim_ladder: RawTranche[];
  currency: string;
  source: {
    kind: string;
    stem?: string | null;
    suggested: { fair_value: number | null; buy_ladder: RawTranche[]; trim_ladder: RawTranche[] };
  };
}

function num(v: unknown): number | null {
  return typeof v === "number" && isFinite(v) ? v : null;
}

// Read a side's ladder off a locked/suggested record, upgrading a legacy
// {buy_below}/{trim_above} single level to a 1-tranche ladder so the editor
// always works in ladder terms.
function ladderOf(rec: LevelRecord | null | undefined, side: Side): Row[] {
  if (!rec) return [];
  const arr = side === "buy" ? rec.buy_ladder : rec.trim_ladder;
  if (Array.isArray(arr) && arr.length) {
    return arr.map((t) => ({
      price: num(t.price),
      size: num(t.size_pct),
      margin: num(side === "buy" ? t.discount_pct : t.premium_pct),
    }));
  }
  const legacy = num(side === "buy" ? rec.buy_below : rec.trim_above);
  return legacy != null ? [{ price: legacy, size: 1, margin: null }] : [];
}

export function priceLevelsBlock(
  rec: PriceLevelsRec,
  analysis: AnalysisLike | null | undefined,
  initialLocked: LevelRecord | null | undefined,
): HTMLElement {
  const sym = rec.symbol || "";
  const meta = ((analysis && analysis.meta) || {}) as { price_levels_suggested?: LevelRecord; currency?: string };
  const suggested: LevelRecord = meta.price_levels_suggested || {};
  const currency = (suggested.currency || meta.currency || rec.currency || "").toUpperCase();
  const spot = num(rec.price && rec.price.value);
  let locked: LevelRecord | null = initialLocked || null;

  const ccyPrefix = currency ? currency + " " : "";
  const fmtLvl = (v: number | null) => (v == null ? "\u2014" : ccyPrefix + (Math.round(v * 100) / 100));
  const r2 = (v: number | null) => (v == null ? null : Math.round(v * 100) / 100);

  // Editor state (rebuilt from the locked level if present, else the suggestion).
  let fairValue: number | null = null;
  let buyRows: Row[] = [];
  let trimRows: Row[] = [];
  function seedFrom(src: LevelRecord | null | undefined) {
    fairValue = num(src && src.fair_value);
    buyRows = ladderOf(src, "buy");
    trimRows = ladderOf(src, "trim");
  }
  seedFrom(locked && (locked.buy_ladder || locked.trim_ladder || locked.buy_below != null || locked.trim_above != null) ? locked : suggested);

  const sugBuy: Row[] = sortLadder((suggested.buy_ladder || []).map((t) => ({ price: num(t.price), size: num(t.size_pct), margin: num(t.discount_pct) })), "buy");
  const sugTrim: Row[] = sortLadder((suggested.trim_ladder || []).map((t) => ({ price: num(t.price), size: num(t.size_pct), margin: num(t.premium_pct) })), "trim");
  const sugFair = num(suggested.fair_value);

  const block = el("div", "price-levels");

  // Recompute a row's price from its margin when the fair value is known, and
  // update its bound input in place (avoids a full rebuild while typing).
  function repriceFromFair() {
    for (const [sd, rows] of [["buy", buyRows], ["trim", trimRows]] as [Side, Row[]][]) {
      for (const row of rows) {
        if (row.margin != null && fairValue != null) {
          row.price = priceFromMargin(fairValue, row.margin, sd);
          if (row._priceIn) row._priceIn.value = row.price != null ? String(row.price) : "";
        }
      }
    }
  }

  function trancheRow(side: Side, row: Row, rows: Row[]): HTMLElement {
    const wrap = el("div", "pl-tranche pl-tranche--" + side);
    const marginIn = numInput("pl-input pl-tr-num", row.margin != null ? r2(row.margin * 100) : null, side === "buy" ? "disc %" : "prem %");
    const priceIn = numInput("pl-input pl-tr-num", row.price != null ? r2(row.price) : null, "price");
    const sizeIn = numInput("pl-input pl-tr-num", row.size != null ? r2(row.size * 100) : null, "size %");
    row._priceIn = priceIn;

    marginIn.addEventListener("input", () => {
      row.margin = marginIn.value.trim() === "" ? null : Number(marginIn.value) / 100;
      if (fairValue != null && row.margin != null) {
        row.price = priceFromMargin(fairValue, row.margin, side);
        priceIn.value = row.price != null ? String(row.price) : "";
      }
      sync();
    });
    priceIn.addEventListener("input", () => {
      row.price = priceIn.value.trim() === "" ? null : Number(priceIn.value);
      if (fairValue != null && row.price != null) {
        row.margin = marginFromPrice(fairValue, row.price, side);
        marginIn.value = row.margin != null ? String(r2(row.margin * 100)) : "";
      }
      sync();
    });
    sizeIn.addEventListener("input", () => {
      row.size = sizeIn.value.trim() === "" ? null : Number(sizeIn.value) / 100;
      sync();
    });

    const dist = el("span", "pl-tr-dist", "");
    row._distEl = dist;
    const del = el("button", "pl-tr-del", "\u00d7");
    del.type = "button";
    del.title = "Remove this tranche";
    del.addEventListener("click", () => {
      const i = rows.indexOf(row);
      if (i >= 0) rows.splice(i, 1);
      render();
    });

    wrap.appendChild(numField(side === "buy" ? "discount" : "premium", marginIn, "%"));
    wrap.appendChild(numField("price", priceIn, currency));
    wrap.appendChild(numField("size", sizeIn, "%"));
    wrap.appendChild(dist);
    wrap.appendChild(del);
    return wrap;
  }

  function numField(label: string, input: HTMLInputElement, suffix: string): HTMLElement {
    const f = el("label", "pl-tr-field");
    f.appendChild(el("span", "pl-tr-label", esc(label) + (suffix ? " (" + esc(suffix) + ")" : "")));
    f.appendChild(input);
    return f;
  }

  // Per-tranche distance-to-spot + a live/pending dot, recomputed on every edit.
  function updateDistances() {
    for (const [side, rows] of [["buy", buyRows], ["trim", trimRows]] as [Side, Row[]][]) {
      for (const row of rows) {
        if (!row._distEl) continue;
        if (spot == null || row.price == null) { row._distEl.textContent = ""; row._distEl.className = "pl-tr-dist"; continue; }
        const live = side === "buy" ? spot <= row.price : spot >= row.price;
        if (live) {
          row._distEl.className = "pl-tr-dist live";
          row._distEl.textContent = "\u25cf live";
        } else {
          const away = Math.abs(spot - row.price) / spot;
          row._distEl.className = "pl-tr-dist pending";
          row._distEl.textContent = "\u25cb " + (Math.round(away * 1000) / 10) + "% away";
        }
      }
    }
  }

  function sideSummary(side: Side, rows: Row[], host: HTMLElement) {
    host.innerHTML = "";
    const sizes = rows.map((r) => r.size);
    const sum = sizeSum(sizes);
    const tranches = rows.filter((r) => r.price != null);
    if (!tranches.length) {
      host.appendChild(el("span", "muted", "no " + side + " tranches"));
      return;
    }
    const frac = activeFraction(tranches.map((r) => ({ price: r.price, size_pct: r.size })), spot, side);
    const live = tranches.filter((r) => (side === "buy" ? spot != null && spot <= (r.price as number) : spot != null && spot >= (r.price as number))).length;
    if (spot != null) {
      host.appendChild(el("span", "pl-sum-live" + (frac > 0 ? " on" : ""), `${live}/${tranches.length} live`));
      host.appendChild(el("span", "pl-sum-frac", `${Math.round(frac * 100)}% sized`));
    }
    const ok = Math.abs(sum - 1) <= 0.02;
    host.appendChild(el("span", "pl-sum-size" + (ok ? " ok" : " warn"),
      `sizes ${Math.round(sum * 100)}%${ok ? "" : " \u2014 will normalize to 100%"}`));
  }

  // Compare the current editor ladder to the analysis suggestion, per side.
  function matchLine(side: Side, rows: Row[], sug: Row[], host: HTMLElement) {
    host.innerHTML = "";
    if (!sug.length) { host.appendChild(el("span", "muted", "no suggestion")); return; }
    const mine = sortLadder(rows.filter((r) => r.price != null), side);
    if (laddersMatch(mine, sug)) {
      const ok = el("span", "pl-cmp pl-cmp-ok");
      ok.appendChild(el("span", "pl-cmp-ico", "\u2713"));
      ok.appendChild(el("span", "pl-cmp-lead", "matches analysis"));
      host.appendChild(ok);
      return;
    }
    const diff = el("span", "pl-cmp pl-cmp-diff");
    diff.appendChild(el("span", "pl-cmp-ico", "\u2260"));
    diff.appendChild(el("span", "pl-cmp-lead", "suggested:"));
    sug.forEach((t, i) => {
      if (i) diff.appendChild(el("span", "pl-cmp-sep", "\u00b7"));
      diff.appendChild(el("span", "pl-cmp-field", `${fmtLvl(t.price)} @ ${Math.round((t.size || 0) * 100)}%`));
    });
    const apply = el("button", "pl-cmp-apply", "Use suggested");
    apply.type = "button";
    apply.addEventListener("click", () => {
      if (side === "buy") buyRows = sug.map((t) => ({ ...t }));
      else trimRows = sug.map((t) => ({ ...t }));
      render();
    });
    diff.appendChild(apply);
    host.appendChild(diff);
  }

  let summaryHosts: { buySum: HTMLElement; trimSum: HTMLElement; buyMatch: HTMLElement; trimMatch: HTMLElement } | null = null;

  // Light refresh: distances + summaries + match lines (no rebuild, keeps focus).
  function sync() {
    updateDistances();
    if (summaryHosts) {
      sideSummary("buy", buyRows, summaryHosts.buySum);
      sideSummary("trim", trimRows, summaryHosts.trimSum);
      matchLine("buy", buyRows, sugBuy, summaryHosts.buyMatch);
      matchLine("trim", trimRows, sugTrim, summaryHosts.trimMatch);
    }
  }

  function sideColumn(side: Side, rows: Row[]): { col: HTMLElement; sum: HTMLElement; matchHost: HTMLElement } {
    const col = el("div", "pl-side pl-side--" + side);
    const head = el("div", "pl-side-head");
    head.appendChild(el("span", "pl-side-title", side === "buy" ? "Buy ladder" : "Trim ladder"));
    const sum = el("span", "pl-side-sum");
    head.appendChild(sum);
    col.appendChild(head);
    const list = el("div", "pl-tranches");
    rows.forEach((row) => list.appendChild(trancheRow(side, row, rows)));
    col.appendChild(list);
    const matchHost = el("div", "pl-side-match");
    col.appendChild(matchHost);
    const add = el("button", "ghost pl-add", "+ Add tranche");
    add.type = "button";
    add.addEventListener("click", () => { rows.push({ price: null, size: null, margin: null }); render(); });
    col.appendChild(add);
    return { col, sum, matchHost };
  }

  // Disclosure: the common case is one "buy below" / "trim above" price, so that
  // is the default. The fair-value anchor, per-tranche sizes, margin%, the
  // live/sized stats and multi-tranche laddering all live behind "Advanced".
  // Reveal it up front only when the seeded levels are actually non-trivial, so
  // we never silently hide real configuration.
  function ladderNonTrivial(): boolean {
    if (buyRows.length > 1 || trimRows.length > 1) return true;
    return [...buyRows, ...trimRows].some((r) => r.size != null && Math.abs(r.size - 1) > 0.001);
  }
  let advanced = ladderNonTrivial();

  // A lone "buy below" / "trim above" price bound to rows[0], created or dropped
  // as the field fills or clears. Size stays implicit (the backend splits a lone
  // tranche to 100%) and margin is irrelevant for a one-tranche side, so neither
  // is asked for here. The live/away dot reuses updateDistances().
  function simpleSide(side: Side, rows: Row[]): HTMLElement {
    const wrap = el("div", "pl-simple-side pl-side--" + side);
    const field = el("label", "pl-tr-field");
    field.appendChild(el("span", "pl-tr-label",
      (side === "buy" ? "Buy below" : "Trim above") + (currency ? " (" + currency + ")" : "")));
    const input = numInput("pl-input pl-simple-input",
      rows.length && rows[0].price != null ? r2(rows[0].price) : null, "price");
    const dist = el("span", "pl-tr-dist", "");
    input.addEventListener("input", () => {
      const v = input.value.trim() === "" ? null : Number(input.value);
      if (v == null || !isFinite(v)) rows.length = 0;
      else if (!rows.length) rows.push({ price: v, size: null, margin: null });
      else { rows[0].price = v; rows[0].margin = null; }
      if (rows.length) rows[0]._distEl = dist;
      updateDistances();
    });
    if (rows.length) rows[0]._distEl = dist;
    field.appendChild(input);
    wrap.appendChild(field);
    wrap.appendChild(dist);
    return wrap;
  }

  function renderSimpleBody() {
    summaryHosts = null;
    block.appendChild(el("p", "hint pl-intro",
      "Buy unlocks at or below your price, trim at or above. Locking gates the rebalance on the " +
      "live price; you confirm every order before it places."));
    const cols = el("div", "pl-simple");
    cols.appendChild(simpleSide("buy", buyRows));
    cols.appendChild(simpleSide("trim", trimRows));
    block.appendChild(cols);
    const notes: string[] = [];
    if (fairValue != null) notes.push(`fair value ${fmtLvl(fairValue)}`);
    if (spot != null) notes.push(`spot ${fmtLvl(spot)}`);
    if (notes.length) block.appendChild(el("p", "muted pl-simple-note", notes.join("  \u00b7  ")));
    updateDistances();
  }

  function renderAdvancedBody() {
    block.appendChild(el("p", "hint pl-intro",
      "A valuation-anchored ladder in the instrument's trading currency. Set a fair value, then " +
      "buy/trim tranches \u2014 each a price (or a margin vs fair value) and a size. Once locked, the " +
      "rebalance scales each trade by how many tranches the live price unlocks, and the outermost " +
      "tranche becomes the order's limit. You confirm every order before it places."));

    // Staleness banner: a newer analysis fair value differs from the locked one.
    if (locked && fairValueStale(num(locked.fair_value), sugFair)) {
      const banner = el("div", "pl-stale");
      banner.appendChild(el("span", "pl-stale-ico", "\u26a0"));
      banner.appendChild(el("span", "pl-stale-text",
        `Locked on fair value ${fmtLvl(num(locked.fair_value))}; latest analysis says ${fmtLvl(sugFair)}.`));
      const re = el("button", "pl-cmp-apply", "Re-anchor");
      re.type = "button";
      re.title = "Set the fair value to the latest analysis and re-derive tranche prices from their margins";
      re.addEventListener("click", () => { fairValue = sugFair; repriceFromFair(); render(); });
      banner.appendChild(re);
      block.appendChild(banner);
    }

    // Fair value anchor.
    const fvRow = el("div", "pl-fair");
    const fvField = el("label", "pl-tr-field");
    fvField.appendChild(el("span", "pl-tr-label", "Fair value" + (currency ? " (" + currency + ")" : "")));
    const fvIn = numInput("pl-input pl-fv-input", fairValue != null ? r2(fairValue) : null, "anchor");
    fvIn.addEventListener("input", () => {
      fairValue = fvIn.value.trim() === "" ? null : Number(fvIn.value);
      repriceFromFair();
      sync();
    });
    fvField.appendChild(fvIn);
    fvRow.appendChild(fvField);
    if (sugFair != null) {
      const hint = el("span", "pl-fair-hint muted", `analysis: ${fmtLvl(sugFair)}`);
      fvRow.appendChild(hint);
    }
    if (spot != null) fvRow.appendChild(el("span", "pl-fair-hint muted", `spot: ${fmtLvl(spot)}`));
    block.appendChild(fvRow);

    // Two ladders side by side.
    const cols = el("div", "pl-cols");
    const buyCol = sideColumn("buy", buyRows);
    const trimCol = sideColumn("trim", trimRows);
    cols.appendChild(buyCol.col);
    cols.appendChild(trimCol.col);
    block.appendChild(cols);

    summaryHosts = { buySum: buyCol.sum, trimSum: trimCol.sum, buyMatch: buyCol.matchHost, trimMatch: trimCol.matchHost };
    sync();
  }

  function renderFooter() {
    if (locked && locked.locked_at) {
      block.appendChild(el("p", "muted pl-when", `Locked ${esc(relTime(locked.locked_at))}`));
    }

    const msg = el("p", "hint pl-msg", "");
    const actions = el("div", "analysis-actions pl-actions");
    const lockBtn = el("button", "primary", locked ? "Update lock" : "Lock in");
    lockBtn.type = "button";
    lockBtn.addEventListener("click", async () => {
      const payload = buildLockPayload();
      const err = validate(payload);
      if (err) { msg.className = "hint pl-msg err"; msg.textContent = err; return; }
      lockBtn.disabled = true;
      msg.className = "hint pl-msg";
      msg.textContent = "Locking\u2026";
      try {
        const res = await api("/api/price-levels/lock", "POST", payload);
        locked = res.level;
        seedFrom(locked);
        render();
      } catch (e) {
        lockBtn.disabled = false;
        msg.className = "hint pl-msg err";
        msg.textContent = "Lock failed: " + (e as Error).message;
      }
    });
    actions.appendChild(lockBtn);
    if (locked) {
      const clearBtn = el("button", "ghost", "Clear");
      clearBtn.type = "button";
      clearBtn.addEventListener("click", async () => {
        clearBtn.disabled = true;
        msg.className = "hint pl-msg";
        msg.textContent = "Clearing\u2026";
        try {
          await api("/api/price-levels/clear", "POST", { symbol: sym });
          locked = null;
          seedFrom(suggested);
          advanced = ladderNonTrivial();
          render();
        } catch (e) {
          clearBtn.disabled = false;
          msg.className = "hint pl-msg err";
          msg.textContent = "Clear failed: " + (e as Error).message;
        }
      });
      actions.appendChild(clearBtn);
    }
    block.appendChild(actions);
    block.appendChild(msg);
  }

  function render() {
    block.innerHTML = "";
    block.classList.toggle("pl-is-locked", !!locked);
    block.classList.toggle("pl-advanced", advanced);
    const head = el("div", "pl-head");
    head.appendChild(el("h3", "pl-title", "Price levels"));
    if (locked) head.appendChild(el("span", "abadge ok pl-locked", "Locked"));
    const toggle = el("button", "ghost pl-adv-toggle", advanced ? "Simpler" : "Advanced\u2026");
    toggle.type = "button";
    toggle.title = advanced
      ? "Hide the fair-value anchor, sizes and multi-tranche ladder"
      : "Show the fair-value anchor, per-tranche sizes and multi-tranche laddering";
    toggle.addEventListener("click", () => { advanced = !advanced; render(); });
    head.appendChild(toggle);
    block.appendChild(head);

    if (advanced) renderAdvancedBody();
    else renderSimpleBody();
    renderFooter();
  }

  function buildLockPayload(): LockPayload {
    const mapSide = (rows: Row[], side: Side): RawTranche[] => rows
      .filter((r) => num(r.price) != null)
      .map((r) => {
        const t: RawTranche = { price: num(r.price), size_pct: num(r.size) };
        const m = num(r.margin);
        if (m != null) t[side === "buy" ? "discount_pct" : "premium_pct"] = m;
        return t;
      });
    return {
      symbol: sym,
      fair_value: num(fairValue),
      buy_ladder: mapSide(buyRows, "buy"),
      trim_ladder: mapSide(trimRows, "trim"),
      currency,
      source: {
        kind: "ticker_analysis",
        stem: analysis && analysis.stem,
        suggested: {
          fair_value: sugFair,
          buy_ladder: suggested.buy_ladder || [],
          trim_ladder: suggested.trim_ladder || [],
        },
      },
    };
  }

  // Mirror the backend validation so we fail fast with a friendly message.
  function validate(p: LockPayload): string | null {
    if (!p.buy_ladder.length && !p.trim_ladder.length) {
      return "Add at least one buy or trim tranche (or Clear to remove).";
    }
    const buyPrices = p.buy_ladder.map((t) => t.price as number);
    const trimPrices = p.trim_ladder.map((t) => t.price as number);
    if (buyPrices.length && trimPrices.length && Math.max(...buyPrices) >= Math.min(...trimPrices)) {
      return "Every buy price must be below every trim price.";
    }
    if (p.fair_value != null) {
      if (buyPrices.length && Math.max(...buyPrices) > p.fair_value) return "Buy prices must be at or below fair value.";
      if (trimPrices.length && Math.min(...trimPrices) < p.fair_value) return "Trim prices must be at or above fair value.";
    }
    return null;
  }

  render();
  return block;
}

// A bare numeric input for the ladder editor (currency/percent decorated by the
// surrounding field label).
function numInput(cls: string, value: number | null, placeholder?: string): HTMLInputElement {
  const inp = document.createElement("input");
  inp.type = "number";
  inp.step = "any";
  inp.min = "0";
  inp.className = cls;
  if (placeholder) inp.placeholder = placeholder;
  if (value != null) inp.value = String(value);
  return inp;
}
