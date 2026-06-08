// @ts-nocheck
import { $, api, el, esc, fmtStamp, sensitive, state } from "./core";
import { analyzeFromAnywhere } from "./rebalance";

// ---- holdings -------------------------------------------------------------
async function loadHoldings() {
  const status = $("#hold-status");
  const out = $("#hold-result");
  status.textContent = "Loading portfolio snapshot...";
  try {
    const h = await api("/api/holdings");
    state.nav = h.net_asset_value;
    state.holdings = {};
    (h.positions || []).forEach((p) => {
      state.holdings[p.symbol] = p.percent_of_nav;
      if (p.provider_symbol && p.provider_symbol !== p.symbol) {
        state.holdings[p.provider_symbol] = p.percent_of_nav;
      }
    });
    status.innerHTML =
      `NAV ${sensitive(`${Math.round(h.net_asset_value || 0).toLocaleString()} CZK`, "total NAV")} · ` +
      `invested ${sensitive(`${Math.round(h.invested_value || 0).toLocaleString()} CZK`, "invested value")}`;
    const synced = $("#hold-synced");
    if (synced) synced.textContent = h.generated_at ? `Last synced ${fmtStamp(h.generated_at)}` : "No snapshot yet";
    out.innerHTML = "";

    const rows = (h.positions || [])
      .slice()
      .sort((a, b) => (b.percent_of_nav || 0) - (a.percent_of_nav || 0));
    const weights = rows.map((p) => p.percent_of_nav || 0);
    const maxW = Math.max(1e-6, ...weights);
    const cum = (n) => weights.slice(0, n).reduce((s, w) => s + w, 0);

    // Concentration is the single most important fact about this book; state it.
    const banner = el("div", "conc-summary");
    banner.innerHTML =
      `<span>Top 2 <strong>${cum(2).toFixed(1)}%</strong></span>` +
      `<span>Top 5 <strong>${cum(5).toFixed(1)}%</strong></span>` +
      `<span>Top 10 <strong>${cum(10).toFixed(1)}%</strong></span>` +
      `<span class="muted">${rows.length} positions · weights = % of invested</span>`;
    out.appendChild(banner);

    const list = el("div", "pos-list");
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
      const row = el("div", "pos-row tier-" + tier);
      row.innerHTML =
        `<span class="pos-sym">${esc(label)}${tag}</span>` +
        `<span class="pos-bar-track"><span class="${barClass}" style="width:${barW.toFixed(2)}%"></span></span>` +
        `<span class="pos-w">${right}</span>`;
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
    out.appendChild(el("div", "hint",
      "Bar length \u221d weight. Colour = concentration: red >10% (single-name risk), amber 5\u201310%, blue 1\u20135%, grey <1%. " +
      "Striped bar = option notional if exercised (\u2193 downside/short, \u2191 upside/long), not capital at risk. Click a row to deep-dive."));
  } catch (e) {
    status.textContent = "Could not load holdings: " + e.message;
    status.classList.add("err");
  }
}

function isResearchableHolding(position) {
  if (position.researchable === false) return false;
  if (position.asset_class === "OPT") return false;
  const symbol = String(position.symbol || "").toUpperCase();
  return !symbol.endsWith(".DRRT");
}

export {
  loadHoldings,
};
