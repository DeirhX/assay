// App-wide "go look at this ticker" navigation. Extracted from rebalance.ts so
// the many surfaces that just want to open a dossier (holdings, segment, exit,
// overview, viewed-history, the global .tlink click handler) no longer import
// the 1200-line rebalance planner as a side effect — and so rebalance stops
// depending on deepdive purely for navigation. This is a thin orchestration
// leaf: shell owns the nav primitives, deepdive owns the rendering; here we just
// wire "switch to the deep-dive view and show/pull the symbol".
import { $$, api } from "./core";
import { hydrateHistory, pullTicker, renderDeepDive } from "./deepdive";
import { cleanSymbol, pushNav, setActiveView } from "./shell";

// Force a live pull: used by surfaces whose intent is "analyze this now"
// (a holdings row, a segment peer) rather than "browse what we already have".
export function analyzeFromAnywhere(sym: string | null | undefined) {
  const ticker = cleanSymbol(sym);
  if (!ticker) return;
  pushNav({ view: "deepdive", ticker, sort: "", sec: "" });
  setActiveView("deepdive");
  $$<HTMLInputElement>("#ticker-input").value = ticker;
  pullTicker(ticker, { push: false });
}

// Cache-first open for in-report ticker links: show what we already have
// instantly, and only hit the network (live pull) when there's no cached
// dossier. Browsing a report shouldn't trigger a slow pull per click.
export async function openTicker(sym: string | null | undefined) {
  const ticker = cleanSymbol(sym);
  if (!ticker) return;
  pushNav({ view: "deepdive", ticker, sort: "", sec: "" });
  setActiveView("deepdive");
  $$<HTMLInputElement>("#ticker-input").value = ticker;
  const status = $$("#dd-status");
  status.classList.remove("err");
  status.textContent = `Loading ${ticker}…`;
  try {
    const rec = await api("/api/research/" + encodeURIComponent(ticker));
    status.textContent = `Cached ${rec.symbol} from ${new Date(rec.as_of).toLocaleString()} — press Analyze to refresh`;
    // Paint everything that's already on file now; the recent-pulls change log is
    // a separate fetch that streams in under its own progress bar.
    // Opening a ticker anchors on its price history (nav already pushed above).
    renderDeepDive(rec, { anchorChart: true });
    hydrateHistory(rec);
  } catch (_e) {
    await pullTicker(ticker, { push: false, anchor: true });  // nothing cached -> pull live
  }
}
