// Analysis prose decoration: colour-codes a rendered CLI analysis so the call,
// the upside/downside cases, and any inline stance words are scannable. Pure DOM
// post-processing over already-rendered markdown -- no fetching, no app state.
// Extracted from deepdive.ts; shared with the viewed-tickers list (detectStance).
import { el, esc, freshnessNote } from "../core";

export interface Stance {
  cls: string;
  label: string;
  re: RegExp;
  index: number;
}

interface SourcesRec {
  sources?: Record<string, unknown> | null;
  as_of?: string | null;
}

// Stances the analysis prompt asks for (Accumulate / Hold / Trim / Avoid) plus
// common synonyms. The earliest occurrence in the verdict block wins, so a
// stance word buried in the justification (e.g. "Hold ... better to avoid
// adding") never overrides the leading call.
const VERDICT_STANCES: { re: RegExp; cls: string; label: string }[] = [
  { re: /\b(accumulate|accumulating|overweight|add(?:ing)?|buy|buying)\b/i, cls: "v-good", label: "Accumulate" },
  { re: /\b(trim|trimming|reduce|reducing|underweight|lighten)\b/i, cls: "v-warn", label: "Trim" },
  { re: /\b(avoid|sell|selling|exit|exiting)\b/i, cls: "v-bad", label: "Avoid" },
  { re: /\b(hold|holding|neutral|maintain)\b/i, cls: "v-hold", label: "Hold" },
];

// Earliest-match stance detection over arbitrary verdict text. Shared by the
// analysis card and the recents list. Returns {cls,label,re,index} or null.
export function detectStance(text: string | null | undefined): Stance | null {
  if (!text) return null;
  let best: Stance | null = null;
  VERDICT_STANCES.forEach((s) => {
    const m = text.match(s.re);
    const idx = m?.index ?? 0;
    if (m && (best === null || idx < best.index)) best = { cls: s.cls, label: s.label, re: s.re, index: idx };
  });
  return best;
}

// Colour-codes the verdict: a pill on the heading + the inline stance word, so
// the recommendation is unmissable when scanning the analysis.
function decorateVerdict(root: HTMLElement): void {
  const heads = [...root.querySelectorAll("h1,h2,h3,h4,h5,h6")];
  const vh = heads.find((h) => /^\s*verdict\b/i.test(h.textContent || ""));
  if (!vh || vh.querySelector(".verdict-pill")) return; // idempotent
  const block: Element[] = [];
  for (let n = vh.nextElementSibling; n && !/^H[1-6]$/.test(n.tagName); n = n.nextElementSibling) block.push(n);
  const text = block.map((n) => n.textContent).join(" ");
  const best = detectStance(text);
  if (!best) return;
  const pill = el("span", "verdict-pill " + best.cls, esc(best.label));
  vh.appendChild(document.createTextNode(" "));
  vh.appendChild(pill);
  for (const elBlock of block) {
    if (highlightFirstMatch(elBlock, best.re, "verdict-stance " + best.cls)) break;
  }
}

// The analysis template emits fixed sections (Bull case / Bear case / What would
// change the thesis). Tint each one so a reader scanning the report can land on
// the upside, the downside, and the trip-wires without reading every heading.
// Matched on the heading text only, so unrelated prose never gets painted.
const CASE_RULES: { re: RegExp; cls: string }[] = [
  { re: /\b(bull case|bull thesis|bull|upside|reasons to (buy|own|accumulate))\b/i, cls: "case-bull" },
  { re: /\b(bear case|bear thesis|bear|downside|risk|red flag)\b/i, cls: "case-bear" },
  { re: /\b(what would change|thesis (buster|breaker|risk)|catalyst|what to watch|triggers?)\b/i, cls: "case-flip" },
];

// Wraps each recognised section (heading + its body, up to the next heading) in a
// coloured rail. Idempotent: re-running on already-decorated DOM is a no-op.
function decorateCases(root: HTMLElement): void {
  const heads = [...root.querySelectorAll("h1,h2,h3,h4,h5,h6")];
  heads.forEach((h) => {
    if (h.classList.contains("case-head") || h.closest(".case-section")) return;
    const text = (h.textContent || "").trim();
    if (/^\s*verdict\b/i.test(text)) return; // owned by decorateVerdict
    const rule = CASE_RULES.find((r) => r.re.test(text));
    if (!rule) return;
    const block: Element[] = [];
    for (let n = h.nextElementSibling; n && !/^H[1-6]$/.test(n.tagName); n = n.nextElementSibling) block.push(n);
    const wrap = el("div", "case-section " + rule.cls);
    h.parentNode!.insertBefore(wrap, h);
    h.classList.add("case-head", rule.cls);
    wrap.appendChild(h);
    block.forEach((n) => wrap.appendChild(n));
  });
}

// The four canonical stances, mapped to the verdict palette. Matched
// CASE-SENSITIVELY (capitalised) so we colour the deliberate recommendation
// label ("Downgrade to Trim", "Upgrade to Accumulate") but NOT the lowercase
// verb that shares the word -- e.g. "margins hold or improve", "avoid chasing".
const DECISION_CLASS: Record<string, string> = { accumulate: "v-good", hold: "v-hold", trim: "v-warn", avoid: "v-bad" };
const DECISION_RE = /\b(Accumulate|Hold|Trim|Avoid)\b/g;

// Tints every capitalised stance word across the note (e.g. inside the
// "What would change the thesis" triggers), reusing the verdict colours so an
// upgrade-to/downgrade-to call is scannable wherever it appears. Skips text
// already wrapped (verdict pill/stance) and links/code. Idempotent.
function decorateStanceWords(root: HTMLElement): void {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const targets: Text[] = [];
  let node: Node | null;
  while ((node = walker.nextNode())) {
    const parent = (node as Text).parentElement;
    if (!parent || parent.closest(".verdict-stance, .verdict-pill, a, code, pre")) continue;
    if (/\b(Accumulate|Hold|Trim|Avoid)\b/.test(node.nodeValue || "")) targets.push(node as Text);
  }
  targets.forEach((n) => {
    const text = n.nodeValue || "";
    const frag = document.createDocumentFragment();
    let last = 0;
    for (const m of text.matchAll(DECISION_RE)) {
      const idx = m.index ?? 0;
      if (idx > last) frag.appendChild(document.createTextNode(text.slice(last, idx)));
      frag.appendChild(el("span", "verdict-stance " + DECISION_CLASS[m[0].toLowerCase()], esc(m[0])));
      last = idx + m[0].length;
    }
    if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
    n.parentNode!.replaceChild(frag, n);
  });
}

// One call to colour-code a rendered analysis: recommendation pill on the verdict
// plus tinted bull/bear/trigger rails plus inline stance words. Order matters --
// verdict first (its block scan runs before decorateCases rewraps siblings), then
// stance words last so it skips anything the earlier passes already wrapped.
export function decorateAnalysis(root: HTMLElement): void {
  decorateVerdict(root);
  decorateCases(root);
  decorateStanceWords(root);
}

const PROVIDER_LABELS: Record<string, string> = { yahoo: "Yahoo Finance", sec_edgar: "SEC EDGAR", fmp: "FMP" };

// The model ends with a "Sources" section listing web citations, or a bare
// "None — analysis is from the provided data only." when it worked purely from
// the data we handed it. That "None" reads like a gap; when there are no actual
// links, swap in the real provenance -- which deterministic providers fed the
// note and how fresh the snapshot is -- so the section informs instead of shrugs.
export function decorateSources(root: HTMLElement, rec?: SourcesRec | null): void {
  const heads = [...root.querySelectorAll("h1,h2,h3,h4,h5,h6")];
  const sh = heads.find((h) => /^\s*sources?\b/i.test(h.textContent || ""));
  if (!sh) return;
  const block: Element[] = [];
  for (let n = sh.nextElementSibling; n && !/^H[1-6]$/.test(n.tagName); n = n.nextElementSibling) block.push(n);
  // Real web citations present? Leave the model's list untouched.
  if (block.some((n) => n.querySelector && n.querySelector("a[href]"))) return;
  const names = Object.keys(PROVIDER_LABELS)
    .filter((k) => rec && rec.sources && rec.sources[k])
    .map((k) => PROVIDER_LABELS[k]);
  const list = names.length ? names.join(", ") : "the cached data snapshot";
  const fresh = rec ? freshnessNote(rec.as_of) : "";
  const p = el("p", "sources-provenance");
  p.innerHTML =
    `Built only from the deterministic data snapshot \u2014 <strong>${esc(list)}</strong>` +
    (fresh ? `, pulled ${fresh}` : "") +
    `. No external web search was used \u2014 verify time-sensitive claims independently.`;
  block.forEach((n) => n.remove());
  sh.parentNode!.insertBefore(p, sh.nextSibling);
}

// Wraps the first regex match found in a text node under `host`. Returns true on
// a hit so callers can stop after the first occurrence.
function highlightFirstMatch(host: HTMLElement | Element, re: RegExp, cls: string): boolean {
  const walker = document.createTreeWalker(host, NodeFilter.SHOW_TEXT);
  let node: Node | null;
  while ((node = walker.nextNode())) {
    const m = (node.nodeValue || "").match(re);
    if (!m) continue;
    const tail = (node as Text).splitText(m.index ?? 0);
    tail.nodeValue = (tail.nodeValue || "").slice(m[0].length);
    const span = el("span", cls, esc(m[0]));
    tail.parentNode!.insertBefore(span, tail);
    return true;
  }
  return false;
}
