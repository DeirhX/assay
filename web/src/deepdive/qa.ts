// Per-ticker Q&A card: an archived, continuable thread backed by the cheap CLI
// analysis backends. Thin wrapper over the shared createQaCard scaffold plus the
// token/cache usage line. Extracted from deepdive.ts.
import { createQaCard, ensureTickerSet } from "../analyses";
import { api } from "../core";
import { modelLabel } from "../shell";
import { relTime } from "../viewed";

interface QaUsage {
  cache_read_input_tokens?: number | null;
  cache_creation_input_tokens?: number | null;
  input_tokens?: number | null;
  output_tokens?: number | null;
}

interface QaTurn {
  backend_label?: string | null;
  model?: string | null;
  ts?: string | number | null;
  usage?: QaUsage | null;
}

// Anthropic prompt-cache + token usage for one turn, rendered as a compact line.
export function qaUsageHtml(u: QaUsage | null | undefined): string {
  if (!u || typeof u !== "object") return "";
  const r = u.cache_read_input_tokens, w = u.cache_creation_input_tokens;
  const inp = u.input_tokens, out = u.output_tokens;
  if ([r, w, inp, out].every((v) => v == null)) return "";
  const fmt = (n: number | null | undefined) =>
    (n == null ? "0" : n >= 1000 ? (n / 1000).toFixed(1).replace(/\.0$/, "") + "k" : String(n));
  const parts: string[] = [];
  if (r != null || w != null) {
    const hit = (r || 0) > 0;
    parts.push(`<span class="${hit ? "qa-cache-hit" : ""}">cache ${fmt(r)} read \u00b7 ${fmt(w)} write</span>`);
  }
  if (inp != null) parts.push(`${fmt(inp)} new in`);
  if (out != null) parts.push(`${fmt(out)} out`);
  return `<div class="qa-usage" title="Anthropic prompt-cache + token usage for this turn">${parts.join(" \u00b7 ")}</div>`;
}

// Archived, continuable Q&A about the ticker. Same cheap CLI backends as the
// in-depth note; the whole thread is persisted server-side so it can be resumed
// across sessions. Renders the archive, then an input to ask the next question.
export function renderQaCard(rec: { symbol?: string }): HTMLElement {
  const sym = rec.symbol || "";
  return createQaCard({
    title: "Ask about " + sym,
    emptyHint:
      "No questions yet. Ask anything about the numbers, momentum, valuation, or how it sits " +
      "in your portfolio. The thread is archived so you can pick it up later.",
    placeholder: `Ask a follow-up about ${sym} \u2014 grounded in the data above. Ctrl/\u2318+Enter to send.`,
    pollLabel: `Q&A \u00b7 ${sym}`,
    confirmMsg: `Clear the archived Q&A thread for ${sym}?`,
    // The ticker set must be loaded before linkifyTickers runs in the thread.
    prepare: ensureTickerSet,
    loadThread: () => api("/api/qa/" + encodeURIComponent(sym)),
    postQuestion: (q: string) => api("/api/qa/" + encodeURIComponent(sym), "POST", { question: q }),
    clearThread: () => api("/api/qa/" + encodeURIComponent(sym), "POST", { clear: true }),
    deleteTurn: (idx: number) => api("/api/qa/" + encodeURIComponent(sym), "POST", { delete: idx }),
    turnMeta: (t: QaTurn) => [t.backend_label, modelLabel(t.model), t.ts ? relTime(t.ts) : null],
    usageHtml: (t: QaTurn) => qaUsageHtml(t.usage),
  });
}
