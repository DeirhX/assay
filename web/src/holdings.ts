// @ts-nocheck
import { $, api, el, esc, fmtCZK, fmtStamp, sensitive, state } from "./core";
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
    (h.positions || []).forEach((p) => { state.holdings[p.symbol] = p.percent_of_nav; });
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
      const w = p.percent_of_nav || 0;
      // Tier by absolute concentration (flags the AMD/ARM problem on sight);
      // bar length is relative to the largest holding for visual ranking.
      const tier = isOpt ? "opt" : w >= 10 ? "core" : w >= 5 ? "large" : w >= 1 ? "mid" : "small";
      const barW = isOpt ? 0 : (w / maxW) * 100;
      const right = isOpt ? sensitive(`${fmtCZK(p.base_market_value)} CZK`, "absolute position value") : `${w.toFixed(2)}%`;
      const label = isOpt ? (p.description || p.symbol) : p.symbol;
      const tag = isOpt ? ` <span class="opt-tag">OPT</span>` : "";
      const row = el("div", "pos-row tier-" + tier);
      row.innerHTML =
        `<span class="pos-sym">${esc(label)}${tag}</span>` +
        `<span class="pos-bar-track"><span class="pos-bar" style="width:${barW.toFixed(2)}%"></span></span>` +
        `<span class="pos-w">${right}</span>`;
      row.title =
        (p.description || p.symbol) +
        (isOpt && p.broker_percent_of_nav != null
          ? ` · broker tagged ${p.broker_percent_of_nav}% of NAV (margin/notional artifact, ignored)`
          : ` · ${w.toFixed(2)}% of invested`);
      if (!isOpt) row.addEventListener("click", () => analyzeFromAnywhere(p.symbol));
      list.appendChild(row);
    });
    out.appendChild(list);
    out.appendChild(el("div", "hint",
      "Bar length \u221d weight. Colour = concentration: red >10% (single-name risk), amber 5\u201310%, blue 1\u20135%, grey <1%. Click a row to deep-dive."));
  } catch (e) {
    status.textContent = "Could not load holdings: " + e.message;
    status.classList.add("err");
  }
}

export {
  loadHoldings,
};
