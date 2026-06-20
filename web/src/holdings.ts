import type { HoldingPosition, HoldingsPayload } from "./api-types";
import { $, api, el, esc, fmtStamp, freshnessNote, loadError, sensitive, state } from "./core";
import { analyzeFromAnywhere } from "./rebalance";

// ---- holdings -------------------------------------------------------------
async function loadHoldings() {
  const status = $("#hold-status");
  const out = $("#hold-result");
  status.textContent = "Loading portfolio snapshot...";
  try {
    const h = await api<HoldingsPayload>("/api/holdings");
    state.nav = h.net_asset_value;
    state.holdings = {};
    (h.positions || []).forEach((p) => {
      state.holdings[p.symbol] = p.percent_of_nav;
      if (p.provider_symbol && p.provider_symbol !== p.symbol) {
        state.holdings[p.provider_symbol] = p.percent_of_nav;
      }
    });
    // NAV/concentration is the portfolio's headline, so give it a visual hero
    // rather than a muted text line -- and keep it in #hold-result, not the
    // transient #hold-status, so a "Synced…" message can't overwrite it.
    const synced = $("#hold-synced");
    if (synced) synced.innerHTML = h.generated_at ? `Last synced ${freshnessNote(h.generated_at) || esc(fmtStamp(h.generated_at))}` : "No snapshot yet";
    status.textContent = "";
    out.innerHTML = "";

    const rows = (h.positions || [])
      .slice()
      .sort((a, b) => (b.percent_of_nav || 0) - (a.percent_of_nav || 0));
    const weights = rows.map((p) => p.percent_of_nav || 0);
    const maxW = Math.max(1e-6, ...weights);
    const cum = (n: number) => weights.slice(0, n).reduce((s, w) => s + w, 0);

    out.appendChild(portfolioHero(h, rows.length, cum));
    // Legend goes above the list: with 40+ positions it was below the fold, so
    // the colour coding (its whole point) was invisible until you scrolled past
    // everything it explains.
    out.appendChild(el("div", "hint pos-legend",
      "Bar length \u221d weight. Colour = concentration: red >10% (single-name risk), amber 5\u201310%, blue 1\u20135%, grey <1%. " +
      "Striped bar = option notional if exercised (\u2193 downside/short, \u2191 upside/long), not capital at risk. Click a row to deep-dive."));

    // Opt-in column of each position's current market value (base currency).
    // Off by default because the bars are about concentration, not money, and the
    // values are sensitive; the preference sticks across reloads. Toggling just
    // flips a class so we never re-fetch or re-render the rows.
    const showValues = localStorage.getItem("holdings.showValues") === "1";
    const controls = el("div", "pos-controls");
    controls.innerHTML =
      `<label class="setup-toggle pos-val-toggle"><input type="checkbox" id="hold-show-values"` +
      `${showValues ? " checked" : ""}><span class="setup-toggle-track"></span> ` +
      `Show asset values</label>`;
    out.appendChild(controls);

    const list = el("div", "pos-list" + (showValues ? " show-values" : ""));
    rows.forEach((p) => {
      const isOpt = p.asset_class === "OPT";
      const researchable = isResearchableHolding(p);
      const providerSymbol = p.provider_symbol || p.symbol;
      const w = p.percent_of_nav || 0;
      // Tier by absolute concentration (flags the AMD/ARM problem on sight);
      // bar length is relative to the largest holding for visual ranking.
      const tier = isOpt ? "opt" : w >= 10 ? "core" : w >= 5 ? "large" : w >= 1 ? "mid" : "small";
      const o = isOpt ? p.option : null;
      const exPct = o ? o.exercise_pct : null;

      let right, barW, barClass;
      if (isOpt) {
        // Options carry ~0 capital but real notional exposure if exercised; show
        // that (signed: a put is downside protection) instead of the tiny premium
        // value, and draw a striped "notional, not capital" bar. Percentages are
        // privacy-safe, so unlike the old absolute CZK value this needs no blur.
        right = exPct != null
          ? `${exPct < 0 ? "\u2193" : "\u2191"}${Math.abs(exPct).toFixed(1)}% if exercised`
          : "n/a";
        barW = exPct != null ? Math.min(100, (Math.abs(exPct) / maxW) * 100) : 0;
        barClass = "pos-bar opt-bar";
      } else {
        right = `${w.toFixed(2)}%`;
        barW = (w / maxW) * 100;
        barClass = "pos-bar";
      }

      const displaySymbol = providerSymbol && providerSymbol !== p.symbol ? `${p.symbol} \u2192 ${providerSymbol}` : p.symbol;
      const label = isOpt ? (p.description || p.symbol) : displaySymbol;
      const tag = isOpt ? ` <span class="opt-tag">OPT</span>` : "";
      const valText = p.base_market_value == null
        ? "\u2014"
        : sensitive(`${Math.round(p.base_market_value).toLocaleString()} CZK`, "position value");
      const row = el("div", "pos-row tier-" + tier);
      row.innerHTML =
        `<span class="pos-sym">${esc(label)}${tag}</span>` +
        `<span class="pos-bar-track"><span class="${barClass}" style="width:${barW.toFixed(2)}%"></span></span>` +
        `<span class="pos-w">${right}</span>` +
        `<span class="pos-val">${valText}</span>`;
      row.title = isOpt && o
        ? `${p.description || p.symbol} \u00b7 ${Math.abs(o.contracts)} ${o.right === "P" ? "put" : "call"} @ ${o.strike} \u00b7 ` +
          `${exPct.toFixed(1)}% of invested if exercised (notional, not capital)`
        : (p.description || p.symbol) + ` \u00b7 ${w.toFixed(2)}% of invested` +
          (providerSymbol !== p.symbol ? ` \u00b7 opens ${providerSymbol}` : "");
      if (researchable) {
        row.addEventListener("click", () => analyzeFromAnywhere(providerSymbol));
      } else {
        row.classList.add("disabled");
        row.title += " \u00b7 not a researchable ticker";
      }
      list.appendChild(row);
    });
    out.appendChild(list);

    const valToggle = controls.querySelector<HTMLInputElement>("#hold-show-values");
    valToggle?.addEventListener("change", () => {
      list.classList.toggle("show-values", valToggle.checked);
      localStorage.setItem("holdings.showValues", valToggle.checked ? "1" : "0");
    });
  } catch (e) {
    loadError(status, "Could not load holdings", e);
  }
}

const fmtCzkNum = (v: number | null | undefined) => `${Math.round(Number(v) || 0).toLocaleString()}`;

// Concentration severity from a cumulative weight: the book gets risky fast, so
// the chips escalate grey -> blue -> amber -> red as the cumulative share climbs.
function concSeverity(pct: number) {
  return pct >= 60 ? "bad" : pct >= 40 ? "warn" : pct >= 20 ? "info" : "small";
}

// The portfolio headline: NAV / invested / uninvested as stat tiles plus the
// top-N concentration as escalating, severity-coloured bars. Money is masked by
// the privacy toggle; the bar widths tell the concentration story at a glance.
function portfolioHero(h: HoldingsPayload, count: number, cum: (n: number) => number) {
  const nav = h.net_asset_value || 0;
  const invested = h.invested_value || 0;
  const hero = el("div", "port-hero");

  const stats = el("div", "port-stats");
  const stat = (label: string, valueHtml: string, title?: string) =>
    `<div class="port-stat"${title ? ` title="${esc(title)}"` : ""}>` +
    `<span class="ps-label">${esc(label)}</span>` +
    `<span class="ps-value">${valueHtml} <span class="ps-unit">CZK</span></span></div>`;
  stats.innerHTML =
    stat("Net asset value", sensitive(fmtCzkNum(nav), "total NAV")) +
    stat("Invested", sensitive(fmtCzkNum(invested), "invested value")) +
    stat("Uninvested", sensitive(fmtCzkNum(nav - invested), "uninvested"),
      "NAV minus invested book value (cash and anything not in the invested book)");
  hero.appendChild(stats);

  const conc = el("div", "conc-card");
  const chips = ([["Top 2", cum(2)], ["Top 5", cum(5)], ["Top 10", cum(10)]] as [string, number][])
    .map(([label, pct]) =>
      `<div class="conc-chip tier-${concSeverity(pct)}">` +
      `<div class="cc-top"><span class="cc-label">${label}</span>` +
      `<span class="cc-pct">${pct.toFixed(1)}%</span></div>` +
      `<div class="cc-track"><span class="cc-fill" style="width:${Math.min(100, pct).toFixed(1)}%"></span></div>` +
      `</div>`).join("");
  conc.innerHTML =
    `<div class="conc-head"><span class="conc-title">Concentration</span>` +
    `<span class="muted">${count} positions \u00b7 weights = % of invested</span></div>` +
    `<div class="conc-bars">${chips}</div>`;
  hero.appendChild(conc);

  return hero;
}

function isResearchableHolding(position: HoldingPosition) {
  if (position.researchable === false) return false;
  if (position.asset_class === "OPT") return false;
  const symbol = String(position.symbol || "").toUpperCase();
  return !symbol.endsWith(".DRRT");
}

export {
  loadHoldings,
};
