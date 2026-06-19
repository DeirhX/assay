import { $, api, apiLoad, el, esc, fmtCZK, fmtStamp, sensitive, statTile } from "./core";

// ---- decision journal + calibration ----------------------------------------
const correctClass = (c) => (c === true ? "good" : c === false ? "bad" : "muted");
const moveClass = (m) => (m == null ? "muted" : m > 0 ? "good" : m < 0 ? "bad" : "muted");

async function loadJournal() {
  await apiLoad({
    path: "/api/journal",
    status: $("#journal-status"),
    clear: [$("#journal-result")],
    loading: "Loading journal…",
    errorLabel: "Could not load journal",
    render: renderJournal,
  });
}

function renderJournal(data) {
  const out = $("#journal-result");
  out.innerHTML = "";
  const cal = data.calibration || {};
  const scoredById = {};
  (cal.scored || []).forEach((s) => { scoredById[s.id] = s; });

  // Calibration headline.
  const stats = el("div", "reb-stats");
  stats.appendChild(jStat("Hit rate",
    cal.hit_rate_pct == null ? "n/a" : cal.hit_rate_pct + "%",
    cal.hit_rate_pct == null ? "muted" : cal.hit_rate_pct >= 55 ? "good" : cal.hit_rate_pct >= 45 ? "warn" : "bad",
    `${cal.n_correct ?? 0}/${cal.n_scored ?? 0} directional calls right (so far).`));
  stats.appendChild(jStat("Decisions logged", String(cal.n_entries ?? 0), "muted",
    "Total journal entries."));
  stats.appendChild(jStat("Avg move after buys",
    cal.avg_move_buys_pct == null ? "n/a" : (cal.avg_move_buys_pct > 0 ? "+" : "") + cal.avg_move_buys_pct + "%",
    moveClass(cal.avg_move_buys_pct), "Mean price move since your buy/add decisions."));
  stats.appendChild(jStat("Avg move after trims",
    cal.avg_move_trims_pct == null ? "n/a" : (cal.avg_move_trims_pct > 0 ? "+" : "") + cal.avg_move_trims_pct + "%",
    // For a trim, a fall (negative) is good — invert the colour.
    cal.avg_move_trims_pct == null ? "muted" : cal.avg_move_trims_pct < 0 ? "good" : "bad",
    "Mean price move since your trim/sell decisions. Negative = you got out before a drop."));
  out.appendChild(stats);

  out.appendChild(el("p", "hint",
    "Hit rate scores only directional calls (buy/add/trim/sell) that have a decision price and " +
    "a later price (recorded outcome or the live snapshot mark). Calm-market scores flatter you " +
    "than a full cycle will."));

  const entries = data.entries || [];
  if (!entries.length) {
    out.appendChild(el("div", "empty-state",
      "<strong>No decisions logged yet</strong>" +
      "Use the form above, or \u201cLog to journal\u201d from a simulated basket."));
    return;
  }

  const list = el("div", "jrnl-list");
  entries.forEach((e) => list.appendChild(entryCard(e, scoredById[e.id])));
  out.appendChild(list);
}

function entryCard(e, scored) {
  const card = el("div", "jrnl-entry");
  const head = el("div", "jrnl-entry-head");
  const move = scored && scored.move_pct;
  head.innerHTML =
    `<span class="jrnl-sym">${esc(e.symbol)}</span>` +
    `<span class="chip">${esc(e.action)}</span>` +
    (e.size_czk != null ? `<span class="muted">${sensitive(`${fmtCZK(e.size_czk)} CZK`, "size")}</span>` : "") +
    (e.price != null ? `<span class="muted">@ ${esc(e.price)}</span>` : "") +
    `<span class="jrnl-when">${esc(fmtStamp(e.created_at))}</span>`;
  if (scored && scored.correct != null) {
    head.innerHTML += `<span class="chip ${correctClass(scored.correct)}">` +
      `${scored.correct ? "worked" : "wrong"} ${move == null ? "" : (move > 0 ? "+" : "") + move + "%"}</span>`;
  } else if (move != null) {
    head.innerHTML += `<span class="chip ${moveClass(move)}">${move > 0 ? "+" : ""}${move}%</span>`;
  }
  card.appendChild(head);

  if (e.thesis) card.appendChild(el("div", "jrnl-thesis", esc(e.thesis)));
  if (e.expected) card.appendChild(el("div", "jrnl-expected", "Expected: " + esc(e.expected)));

  const meta = [];
  if (e.review_after) meta.push("review after " + esc(e.review_after));
  if (e.outcome && e.outcome.price != null) meta.push("outcome @ " + esc(e.outcome.price));
  if (meta.length) card.appendChild(el("div", "hint", meta.join(" \u00b7 ")));

  // Record-outcome control: stamp a later price so this gets scored.
  const oc = el("div", "jrnl-outcome");
  const input = el("input", "jrnl-outcome-input");
  input.type = "number";
  input.step = "any";
  input.placeholder = "price now";
  const btn = el("button", "ghost", "Record outcome");
  btn.type = "button";
  btn.addEventListener("click", async () => {
    const price = parseFloat(input.value);
    if (!Number.isFinite(price)) { input.focus(); return; }
    btn.disabled = true;
    try {
      const data = await api("/api/journal/outcome", "POST", { id: e.id, price });
      renderJournal(data);
    } catch (err) {
      btn.disabled = false;
      alert("Could not record outcome: " + err.message);
    }
  });
  oc.appendChild(input);
  oc.appendChild(btn);
  card.appendChild(oc);
  return card;
}

const jStat = (label, value, cls, title) => statTile(label, value, { cls, title });

function initJournalControls() {
  const add = $<HTMLElement & { _wired?: boolean }>("#jrnl-add");
  if (add && !add._wired) {
    add._wired = true;
    add.addEventListener("click", submitEntry);
  }
}

async function submitEntry() {
  const status = $("#jrnl-status");
  status.classList.remove("err");
  const fieldVal = (sel: string) => $<HTMLInputElement>(sel)?.value ?? "";
  const payload = {
    symbol: fieldVal("#jrnl-symbol"),
    action: fieldVal("#jrnl-action"),
    size_czk: fieldVal("#jrnl-size"),
    price: fieldVal("#jrnl-price"),
    review_after: fieldVal("#jrnl-review"),
    thesis: fieldVal("#jrnl-thesis"),
    expected: fieldVal("#jrnl-expected"),
  };
  status.textContent = "Saving…";
  try {
    const data = await api("/api/journal", "POST", payload);
    status.textContent = "Logged.";
    ["jrnl-symbol", "jrnl-size", "jrnl-price", "jrnl-review", "jrnl-thesis", "jrnl-expected"]
      .forEach((id) => { const elx = $<HTMLInputElement>("#" + id); if (elx) elx.value = ""; });
    renderJournal(data);
  } catch (e) {
    status.textContent = "Could not save: " + e.message;
    status.classList.add("err");
  }
}

// Pre-fill the journal form from elsewhere (e.g. a simulated basket), navigating
// to the tab via its button so we avoid importing shell (cycle-free).
function openJournalWith(prefill) {
  const tab = document.querySelector<HTMLElement>('.tab[data-view="journal"]');
  if (tab) tab.click();
  setTimeout(() => {
    initJournalControls();
    const set = (id, v) => { const elx = $<HTMLInputElement>("#" + id); if (elx != null && v != null) elx.value = v; };
    if (prefill.symbol != null) set("jrnl-symbol", prefill.symbol);
    if (prefill.action != null) set("jrnl-action", prefill.action);
    if (prefill.size_czk != null) set("jrnl-size", prefill.size_czk);
    if (prefill.price != null) set("jrnl-price", prefill.price);
    if (prefill.thesis != null) set("jrnl-thesis", prefill.thesis);
    if (prefill.expected != null) set("jrnl-expected", prefill.expected);
    const s = $("#jrnl-thesis");
    if (s) s.focus();
  }, 0);
}

export { loadJournal, renderJournal, initJournalControls, openJournalWith };
