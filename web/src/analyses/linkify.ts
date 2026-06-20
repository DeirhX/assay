// Ticker auto-linking: turns ticker-shaped tokens in rendered report/analysis
// prose into deep-dive links. A token links if it's $-prefixed, parenthetical,
// or present in the curated/report-local ticker set -- and never if stoplisted.
// Extracted from analyses.ts; pure DOM post-processing over already-rendered HTML.
import { api, esc, state } from "../core";

// All-caps tokens that are common finance/English shorthand, not tickers. Bare
// matches are additionally gated by the curated ticker set; this stoplist guards
// the structural ($X, parenthetical) paths and trims obvious noise.
export const TICKER_STOP = new Set<string>([
  "US", "EU", "UK", "USA", "EV", "AI", "AR", "VR", "ML", "LLM", "GPU", "CPU", "API", "SDK",
  "UI", "UX", "CEO", "CFO", "CTO", "COO", "IPO", "ETF", "ETFS", "NAV", "EPS", "PE", "PEG",
  "ROE", "ROI", "ROIC", "FCF", "GAAP", "YOY", "QOQ", "CAGR", "ARR", "MRR", "TAM", "SAM", "SOM",
  "FY", "H1", "H2", "Q1", "Q2", "Q3", "Q4", "USD", "EUR", "GBP", "JPY", "KPI", "OEM", "ESG",
  "IRR", "WACC", "DCF", "EBITDA", "IT", "OK", "NO", "AND", "THE", "FOR", "WITH", "FROM",
  "THAT", "THIS", "ARE", "NOT", "ALL", "ANY", "OS", "PC", "TV", "IOT", "SAAS", "B2B", "B2C",
  "RD", "IP", "ID", "VS", "ETC", "CES", "FDA", "SEC", "GDP", "API",
]);

export let _tickerSetLoaded = false;
export async function ensureTickerSet(): Promise<Set<string>> {
  if (_tickerSetLoaded) return state.tickerSet;
  try {
    const d = await api("/api/tickers");
    state.tickerSet = new Set(d.tickers || []);
  } catch (_e) { state.tickerSet = new Set(); }
  _tickerSetLoaded = true;
  return state.tickerSet;
}

export function tickerAnchorHtml(raw: string): string {
  const s = String(raw).toUpperCase();
  return `<a class="tlink" data-ticker="${esc(s)}" href="?view=deepdive&ticker=${encodeURIComponent(s)}" title="Open ${esc(s)} deep-dive">${esc(raw)}</a>`;
}

// Walk text nodes and turn ticker-shaped tokens into deep-dive links. Skips text
// already inside <a>/<code>/<pre>. A token links if it's $-prefixed, wrapped in
// (parens), or present in the curated set -- and never if in the stoplist.
// Two shapes: a 2-5 letter US-style base (optionally exchange-qualified), or a
// foreign exchange-qualified symbol whose base may be numeric (e.g. 000660.KS,
// 0700.HK) -- the suffix is REQUIRED there so plain numbers/dollar amounts ($5,
// $1000) are never mistaken for tickers.
export const _TICKER_TOKEN = /\b[A-Z]{2,5}(?:\.[A-Z]{1,3})?\b|\b[A-Z0-9]{1,6}\.[A-Z]{1,3}\b/g;
export function linkifyTextNode(node: Text, set: Set<string>): void {
  const text = node.nodeValue || "";
  let m: RegExpExecArray | null, last = 0, frag: DocumentFragment | null = null;
  _TICKER_TOKEN.lastIndex = 0;
  while ((m = _TICKER_TOKEN.exec(text))) {
    const tok = m[0];
    const base = tok.split(".")[0];
    const i = m.index;
    const prev = text[i - 1] || "";
    const after = text[i + tok.length] || "";
    const dollar = prev === "$";  // explicit author intent -- overrides the stoplist
    // A "$NOW" must link even though NOW is a stoplisted English word; bare and
    // parenthetical tokens still respect the stoplist.
    if (!dollar && (TICKER_STOP.has(tok) || TICKER_STOP.has(base))) continue;
    const linkable = dollar || (prev === "(" && after === ")") || set.has(tok) || set.has(base);
    if (!linkable) continue;
    frag = frag || document.createDocumentFragment();
    if (i > last) frag.appendChild(document.createTextNode(text.slice(last, i)));
    const a = document.createElement("a");
    a.className = "tlink";
    a.dataset.ticker = tok;
    a.href = `?view=deepdive&ticker=${encodeURIComponent(tok)}`;
    a.title = `Open ${tok} deep-dive`;
    a.textContent = tok;
    frag.appendChild(a);
    last = i + tok.length;
  }
  if (frag) {
    if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
    node.parentNode!.replaceChild(frag, node);
  }
}

// High-confidence ticker signals the report itself carries. The Deep Research
// prompt instructs the model to $-tag the first mention of every company and to
// include a Ticker column, and Perplexity habitually writes exchange-qualified
// mentions like "(NYSE: CCJ)". Harvesting these gives a report-local universe of
// symbols we are confident are tickers -- so every *subsequent* bare mention can
// be linked too, without pulling in the full US/EU list that collides with
// English words (NOW, ON, ALL, IT...).
// A numeric base only counts as a ticker when exchange-qualified (the `\.[A-Z]`
// suffix), so "$5" / "$1000" stay plain dollar amounts while "$000660.KS" links.
const _DOLLAR_TICKER = /\$([A-Z]{1,5}(?:\.[A-Z]{1,3})?|[A-Z0-9]{1,6}\.[A-Z]{1,3})\b/g;
const _PAREN_TICKER = /\(\s*([A-Z]{2,5}(?:\.[A-Z]{1,3})?|[A-Z0-9]{1,6}\.[A-Z]{1,3})\s*\)/g;
const _EXCH_TICKER =
  /\(\s*(?:NYSE(?:\s+American)?|NASDAQ|AMEX|CBOE|OTCMKTS|OTC|TSXV?|LSE|ASX|HKEX|HKG|EURONEXT|KRX|KOSPI|KOSDAQ|SEHK|TSE|SSE|SZSE)[:\s]+([A-Z0-9]{1,6}(?:\.[A-Z]{1,3})?)\s*\)/gi;

export function collectReportTickers(root: HTMLElement | null): Set<string> {
  const found = new Set<string>();
  if (!root) return found;
  const text = root.textContent || "";
  const harvest = (re: RegExp, gate: boolean) => {
    re.lastIndex = 0;
    let m: RegExpExecArray | null;
    while ((m = re.exec(text))) {
      const t = m[1].toUpperCase();
      if (gate && (TICKER_STOP.has(t) || TICKER_STOP.has(t.split(".")[0]))) continue;
      found.add(t);
    }
  };
  // $-prefixed and exchange-qualified are explicit author intent -- trusted even
  // if the symbol collides with a stoplisted word. Bare parentheticals are
  // weaker, so they still respect the stoplist.
  harvest(_DOLLAR_TICKER, false);
  harvest(_EXCH_TICKER, false);
  harvest(_PAREN_TICKER, true);
  // Ticker/Symbol table cells were already turned into .tlink anchors by mdToHtml.
  root.querySelectorAll<HTMLElement>("a.tlink[data-ticker]").forEach((a) => {
    const t = (a.dataset.ticker || "").toUpperCase();
    if (t) found.add(t);
  });
  return found;
}

export function linkifyTickers(root: HTMLElement | null): void {
  if (!root) return;
  // Union the curated server set with symbols this report self-identifies, so a
  // peer the report discusses but you don't hold still links on every mention.
  const set = new Set<string>(state.tickerSet || []);
  collectReportTickers(root).forEach((t) => set.add(t));
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(n) {
      if (!n.nodeValue || !/[A-Z]{2}/.test(n.nodeValue)) return NodeFilter.FILTER_REJECT;
      for (let p = (n as Text).parentElement; p && p !== root.parentElement; p = p.parentElement) {
        const tag = p.tagName;
        if (tag === "A" || tag === "CODE" || tag === "PRE") return NodeFilter.FILTER_REJECT;
      }
      return NodeFilter.FILTER_ACCEPT;
    },
  });
  const nodes: Text[] = [];
  while (walker.nextNode()) nodes.push(walker.currentNode as Text);
  nodes.forEach((n) => linkifyTextNode(n, set));
}
