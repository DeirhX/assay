// Build a compact, paste-ready portfolio snapshot for an LLM prompt, from data
// the app already has: the /api/holdings payload (weights, uPnL, option legs) and
// the /api/rebalance plan (standing stance, band, sleeve membership, conviction,
// locked price levels). Weight-based and privacy-safe -- no absolute cash; uPnL is
// a percentage of cost, which leaks nothing about NAV size. Grouping is by SLEEVE
// only; standalone names stay ungrouped (segment tags aren't a sector taxonomy).
import type { HoldingPosition, HoldingsPayload, RebalancePlan } from "./api-types";

const num = (n: number) => String(Math.round(n * 100) / 100);
const pct = (v: number | null | undefined, d = 2) =>
  typeof v === "number" && isFinite(v) ? `${v.toFixed(d)}%` : "\u2014";
const signedPct = (v: number | null | undefined, d = 1) =>
  typeof v === "number" && isFinite(v) ? (v >= 0 ? `+${v.toFixed(d)}%` : `${v.toFixed(d)}%`) : "\u2014";

// Unrealized P&L as a % of cost basis (a ratio -> privacy-safe, unlike the CZK
// amount). cost = market value - unrealized P&L, both in base currency.
function upnlPct(p: HoldingPosition): number | null {
  const mv = p.base_market_value;
  const pnl = p.unrealized_pnl;
  if (typeof mv !== "number" || typeof pnl !== "number") return null;
  const cost = mv - pnl;
  return cost ? (pnl / cost) * 100 : null;
}

// Underlying ticker of an OCC option symbol ("KLAC  260717P00238000" -> "KLAC").
function optionRoot(sym: string): string {
  const raw = (sym || "").trim();
  const head = raw.split(/\s+/)[0] || "";
  if (head && head.length <= 6) return head.toUpperCase();
  const compact = raw.replace(/\s+/g, "");
  return (compact.length >= 15 ? compact.slice(0, -15) : head).toUpperCase();
}

interface OptAgg { long: number; short: number; net: number; label: string; }

// Aggregate option legs to per-underlying exposure if assigned/exercised. Mirrors
// portfolio.pending_option_exposure: exercise_pct > 0 is bullish (short put / long
// call), < 0 is a hedge; net is the signed sum.
function aggregateOptions(h: HoldingsPayload): Map<string, OptAgg> {
  const out = new Map<string, OptAgg>();
  (h.positions || []).forEach((p) => {
    if (p.asset_class !== "OPT" || !p.option) return;
    const o = p.option;
    const root = optionRoot(p.symbol);
    if (!root) return;
    const e = out.get(root) || { long: 0, short: 0, net: 0, label: "" };
    const x = o.exercise_pct;
    if (typeof x === "number") {
      if (x >= 0) e.long += x; else e.short += -x;
      e.net += x;
    }
    const side = o.contracts < 0 ? "short" : "long";
    const leg = `${side} ${Math.abs(o.contracts)}\u00d7 ${o.strike}${o.right}`;
    e.label = e.label ? `${e.label}, ${leg}` : leg;
    out.set(root, e);
  });
  return out;
}

interface Stance {
  rule?: string;
  band?: string;
  sleeve?: string;
  conviction?: string;
  note?: string | null;
  buyBelow?: number | null;
  trimAbove?: number | null;
}

// Standing stance per symbol from the rebalance plan: sleeve members inherit the
// sleeve's rule/band and carry the sleeve name; standalone targets carry their own
// rule/band, note, and any locked buy/trim trigger. Untargeted names get nothing.
function stanceMap(plan: RebalancePlan | null): Map<string, Stance> {
  const m = new Map<string, Stance>();
  if (!plan) return m;
  const prov = plan.provenance || {};
  const band = (lo: number, hi: number) =>
    typeof lo === "number" && typeof hi === "number" ? `${num(lo)}\u2013${num(hi)}` : undefined;
  (plan.rows || []).forEach((r) => {
    if (r.kind === "sleeve") {
      (r.members || []).forEach((mem) => {
        m.set(mem.symbol.toUpperCase(), {
          rule: r.rule, band: band(r.low, r.high), sleeve: r.name,
          conviction: (prov[mem.symbol] || undefined)?.conviction,
        });
      });
    } else {
      const g = r.price_gate;
      m.set(r.name.toUpperCase(), {
        rule: r.rule, band: band(r.low, r.high), note: r.note,
        conviction: (prov[r.name] || undefined)?.conviction,
        buyBelow: g?.buy_below ?? null, trimAbove: g?.trim_above ?? null,
      });
    }
  });
  return m;
}

// Main entry: the whole-book context block, optionally followed by a focus block
// for one ticker (its stance, options, sleeve neighbours) and a question scaffold.
export function buildPortfolioPrompt(h: HoldingsPayload, plan: RebalancePlan | null, focus?: string | null): string {
  const stances = stanceMap(plan);
  const opts = aggregateOptions(h);
  const equities = (h.positions || [])
    .filter((p) => p.asset_class !== "OPT" && typeof p.percent_of_nav === "number")
    .slice()
    .sort((a, b) => (b.percent_of_nav || 0) - (a.percent_of_nav || 0));

  const asOf = plan?.as_of || (h.generated_at || "").slice(0, 10) || "today";
  const cashT = plan?.cash_target_pct;
  const L: string[] = [];
  L.push(`# My portfolio (as of ${asOf}; weights = % of invested book, excl. cash)`, "");
  L.push(`${equities.length} equity/ETF positions${typeof cashT === "number" ? `, cash target ${cashT}% of NAV` : ""}.`, "");
  L.push("| Ticker | Weight | uPnL% | Stance | Band | Sleeve |");
  L.push("|---|--:|--:|---|---|---|");
  equities.forEach((p) => {
    const s = stances.get(p.symbol.toUpperCase()) || {};
    const stance = (s.rule || "\u2014") + (s.conviction ? ` (${s.conviction})` : "");
    L.push(`| ${p.symbol.toUpperCase()} | ${pct(p.percent_of_nav)} | ${signedPct(upnlPct(p))} | ${stance} | ${s.band || "\u2014"} | ${s.sleeve || "\u2014"} |`);
  });
  L.push("");

  if (opts.size) {
    L.push("## Options overlay (exposure if assigned/exercised \u2014 not owned shares)");
    [...opts.entries()].sort((a, b) => b[1].net - a[1].net).forEach(([root, e]) => {
      L.push(`- **${root}**: ${e.label} \u2192 net ${signedPct(e.net)} underlying (long ${pct(e.long, 1)}, hedge ${pct(e.short, 1)})`);
    });
    L.push("");
  }

  if (focus) L.push(focusBlock(focus, stances, opts, equities));
  return L.join("\n").replace(/\n+$/, "\n");
}

function focusBlock(
  focus: string, stances: Map<string, Stance>, opts: Map<string, OptAgg>, equities: HoldingPosition[],
): string {
  const sym = focus.toUpperCase();
  const pos = equities.find((p) => p.symbol.toUpperCase() === sym);
  const s = stances.get(sym) || {};
  const w = pos?.percent_of_nav;
  const L: string[] = [`## Focus: ${sym}`, ""];
  L.push(`- ${typeof w === "number" ? `Weight ${pct(w)}` : "Not currently held as stock"}`);
  if (s.rule) L.push(`- Stance: **${s.rule}**${s.band ? `, band ${s.band}` : ""}${s.conviction ? `, conviction ${s.conviction}` : ""}`);
  if (s.note) L.push(`- Standing note: ${s.note}`);
  const gate: string[] = [];
  if (typeof s.buyBelow === "number") gate.push(`buy \u2264 ${s.buyBelow}`);
  if (typeof s.trimAbove === "number") gate.push(`trim \u2265 ${s.trimAbove}`);
  if (gate.length) L.push(`- Locked price levels: ${gate.join(", ")}`);
  const oe = opts.get(sym);
  if (oe) L.push(`- Options on ${sym}: ${oe.label} \u2192 net ${signedPct(oe.net)} underlying if assigned`);
  if (s.sleeve) {
    const peers = equities.filter((p) => stances.get(p.symbol.toUpperCase())?.sleeve === s.sleeve && p.symbol.toUpperCase() !== sym);
    if (peers.length) {
      const total = peers.reduce((t, p) => t + (p.percent_of_nav || 0), 0) + (typeof w === "number" ? w : 0);
      const names = peers.slice(0, 8).map((p) => `${p.symbol.toUpperCase()} ${pct(p.percent_of_nav, 1)}`).join(", ");
      L.push(`- Sleeve \`${s.sleeve}\` totals ${pct(total)} across ${peers.length + 1} names: ${names}`);
    }
  }
  L.push("", "## Question", `> Given my portfolio above, <your question about ${sym} here>.`);
  return L.join("\n");
}
