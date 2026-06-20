// Shared archived-Q&A card: a continuable thread (question + answer exchanges)
// over a pluggable backend, used by both the report reader and the per-ticker
// dossier. The caller supplies the load/post/clear/delete thunks and optional
// per-turn meta/usage formatters. Extracted from analyses.ts.
import { api, el, esc, relAge } from "../core";
import { pollDeepJob } from "../errors";
import { mdToHtml } from "./markdown";
import { linkifyTickers } from "./linkify";

interface QaTurn {
  role?: string;
  text?: string;
  backend_label?: string | null;
  model?: string | null;
  ts?: string | number | null;
  usage?: unknown;
}

interface QaThread {
  turns?: QaTurn[];
}

interface QaCardOpts {
  title: string;
  emptyHint: string;
  placeholder: string;
  pollLabel: string;
  confirmMsg: string;
  prepare?: () => Promise<unknown> | void;
  loadThread: () => Promise<QaThread>;
  postQuestion: (q: string) => Promise<any>;
  clearThread: () => Promise<QaThread>;
  deleteTurn?: (idx: number) => Promise<QaThread>;
  turnMeta?: (t: QaTurn) => (string | null | undefined)[];
  usageHtml?: (t: QaTurn) => string;
}

export function createQaCard(opts: QaCardOpts): HTMLElement {
  const card = el("div", "card qa-card");
  const head = el("div", "analysis-head");
  head.appendChild(el("h2", "section", esc(opts.title)));
  const clearBtn = el("button", "ghost", "Clear thread");
  clearBtn.type = "button";
  clearBtn.title = "Discard the archived Q&A and start fresh";
  head.appendChild(clearBtn);
  card.appendChild(head);

  const emptyHint = el("p", "hint", opts.emptyHint);
  const threadWrap = el("details", "qa-collapse");
  threadWrap.open = true;
  const threadSummary = el("summary", "qa-collapse-head collapse-head");
  const thread = el("div", "qa-thread");
  threadWrap.appendChild(threadSummary);
  threadWrap.appendChild(thread);
  const status = el("div", "dd-status analysis-status");
  const form = el("div", "qa-form");
  const input = el("textarea", "qa-input");
  input.rows = 2;
  input.placeholder = opts.placeholder;
  const askBtn = el("button", "primary", "Ask");
  askBtn.type = "button";
  form.appendChild(input);
  form.appendChild(askBtn);
  card.appendChild(emptyHint);
  card.appendChild(threadWrap);
  card.appendChild(form);
  card.appendChild(status);

  // Each exchange (a question + its answer) renders as its own collapsible
  // <details> so answers can be expanded/collapsed individually and deleted one
  // at a time. The summary holds the question; the answer lives in the body.
  function renderExchange(question: QaTurn, answer: QaTurn | null, userIdx: number): HTMLElement {
    const ex = el("details", "qa-exchange");
    ex.open = true;
    const sum = el("summary", "qa-exchange-head");
    sum.innerHTML =
      `<span class="qa-caret" aria-hidden="true">\u203a</span>` +
      `<span class="qa-q-text">${esc(question.text)}</span>`;
    const del = el("button", "qa-del", "\u00d7");
    del.type = "button";
    del.title = "Delete this question and its answer";
    del.setAttribute("aria-label", "Delete this question and its answer");
    del.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      deleteExchange(userIdx);
    });
    sum.appendChild(del);
    ex.appendChild(sum);

    const bodyWrap = el("div", "qa-exchange-body");
    if (answer) {
      const meta = (opts.turnMeta ? opts.turnMeta(answer) : []).filter(Boolean).map(esc).join(" \u00b7 ");
      bodyWrap.appendChild(el("div", "qa-role", "Analyst" + (meta ? ` <span class="muted">${meta}</span>` : "")));
      const prose = el("div", "prose qa-prose");
      prose.innerHTML = mdToHtml(answer.text || "");
      linkifyTickers(prose);
      bodyWrap.appendChild(prose);
      const usage = opts.usageHtml ? opts.usageHtml(answer) : "";
      if (usage) bodyWrap.insertAdjacentHTML("beforeend", usage);
    } else {
      bodyWrap.appendChild(el("div", "hint", "No answer was recorded \u2014 the question may have failed or been cancelled."));
    }
    ex.appendChild(bodyWrap);
    return ex;
  }

  function renderThread(turns: QaTurn[]): void {
    thread.innerHTML = "";
    if (!turns.length) {
      clearBtn.hidden = true;
      emptyHint.hidden = false;
      threadWrap.hidden = true;
      return;
    }
    clearBtn.hidden = false;
    emptyHint.hidden = true;
    threadWrap.hidden = false;
    const exchanges = turns.filter((t) => t.role === "user").length;
    threadSummary.innerHTML =
      `<span class="collapse-title">Conversation history</span>` +
      `<span class="collapse-meta">${exchanges} question${exchanges === 1 ? "" : "s"}</span>` +
      `<span class="collapse-caret" aria-hidden="true">\u203a</span>`;
    // Pair each user turn with the assistant turn that follows it. The user
    // turn's index in the full array is the stable handle the server deletes by.
    for (let i = 0; i < turns.length; i++) {
      if (turns[i].role !== "user") continue;
      const answer = (i + 1 < turns.length && turns[i + 1].role === "assistant") ? turns[i + 1] : null;
      thread.appendChild(renderExchange(turns[i], answer, i));
    }
  }

  async function deleteExchange(userIdx: number): Promise<void> {
    if (!opts.deleteTurn) return;
    if (!confirm("Delete this question and its answer?")) return;
    status.classList.remove("err");
    try {
      const data = await opts.deleteTurn(userIdx);
      renderThread(data.turns || []);
    } catch (e) {
      status.classList.add("err");
      status.textContent = "delete failed: " + (e as Error).message;
    }
  }

  async function load(): Promise<void> {
    let data: QaThread;
    try { data = await opts.loadThread(); }
    catch (_e) { data = { turns: [] }; }
    if (opts.prepare) await opts.prepare();
    renderThread(data.turns || []);
  }

  let currentJobId: string | null = null;
  let busy = false;

  function setBusy(on: boolean): void {
    busy = on;
    if (on) {
      askBtn.textContent = "Cancel";
      askBtn.classList.remove("primary");
      askBtn.classList.add("ghost");
      askBtn.title = "Stop this question and ask something else";
    } else {
      askBtn.textContent = "Ask";
      askBtn.classList.add("primary");
      askBtn.classList.remove("ghost");
      askBtn.title = "";
      currentJobId = null;
    }
  }

  async function ask(): Promise<void> {
    if (busy) return;
    const q = input.value.trim();
    if (!q) return;
    setBusy(true);
    status.classList.remove("err");
    status.innerHTML = `<span class="spinner"></span> thinking&hellip;`;
    try {
      const start = await opts.postQuestion(q);
      currentJobId = start.id;
      await pollDeepJob(start.id, status, async () => {
        status.textContent = "";
        input.value = "";
        await load();
      }, opts.pollLabel);
    } catch (e) {
      status.classList.add("err");
      status.textContent = "question failed: " + (e as Error).message;
    } finally {
      setBusy(false);
    }
  }

  // Cancel the in-flight question (kills the CLI subprocess server-side). The
  // poll loop observes the cancelled state on its next tick and winds down,
  // re-enabling the Ask button so a different question can be asked.
  async function cancelAsk(): Promise<void> {
    if (!currentJobId) return;
    status.classList.remove("err");
    status.innerHTML = `<span class="spinner"></span> cancelling&hellip;`;
    try {
      await api("/api/deep-job/cancel", "POST", { id: currentJobId });
    } catch (_e) { /* poll loop still winds the job down */ }
  }

  askBtn.addEventListener("click", () => (busy ? cancelAsk() : ask()));
  input.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); if (!busy) ask(); }
  });
  clearBtn.addEventListener("click", async () => {
    if (!confirm(opts.confirmMsg)) return;
    try {
      const data = await opts.clearThread();
      renderThread(data.turns || []);
    } catch (e) {
      status.classList.add("err");
      status.textContent = "clear failed: " + (e as Error).message;
    }
  });

  load();
  return card;
}

export function renderDeepQaCard(stem: string, title: string): HTMLElement {
  return createQaCard({
    title: "Ask about this report",
    emptyHint:
      "No questions yet. Ask anything about the report \u2014 a company's positioning, a claim worth " +
      "verifying, or how a name fits your portfolio. The thread is archived so you can pick it up later.",
    placeholder: `Ask a follow-up about "${title}" \u2014 grounded in the report above. Ctrl/\u2318+Enter to send.`,
    pollLabel: `Q&A \u00b7 ${stem}`,
    confirmMsg: "Clear the archived Q&A thread for this report?",
    loadThread: () => api("/api/deep-qa?stem=" + encodeURIComponent(stem)),
    postQuestion: (q: string) => api("/api/deep-qa", "POST", { stem, question: q }),
    clearThread: () => api("/api/deep-qa", "POST", { stem, clear: true }),
    deleteTurn: (idx: number) => api("/api/deep-qa", "POST", { stem, delete: idx }),
    turnMeta: (t: QaTurn) => [t.backend_label, t.ts ? relAge(String(t.ts)) : null],
  });
}
