// Thesis & action editor: the human's judgement (summary / action / drivers /
// downside triggers), kept deliberately separate from the deterministic numbers
// and persisted via /api/thesis. Extracted from deepdive.ts.
import { $$, api, el, esc } from "../core";
import { collapsibleCard } from "./cards";

interface Thesis {
  summary?: string;
  action?: string;
  drivers?: string[];
  downside_triggers?: string[];
  as_of?: string | null;
}

interface ThesisRec {
  symbol?: string;
  thesis?: Thesis;
}

export function renderThesis(rec: ThesisRec): HTMLElement {
  const t = rec.thesis || {};
  const hasContent = !!(t.summary || t.action || (t.drivers || []).length || (t.downside_triggers || []).length);
  const meta = t.as_of ? "saved " + new Date(t.as_of).toLocaleDateString() : "empty";
  const { details: card, body } = collapsibleCard(
    "Thesis &amp; action — your judgement (kept separate from the numbers)",
    { meta, open: hasContent },
  );
  const g = el("div", "thesis-grid");
  g.innerHTML =
    `<div><label>Summary</label><textarea id="th-summary" rows="4" placeholder="What's the story? Momentum vs valuation.">${esc(t.summary || "")}</textarea></div>` +
    `<div><label>Action</label><textarea id="th-action" rows="4" placeholder="Add / hold / trim / sell / wait — and sizing.">${esc(t.action || "")}</textarea></div>` +
    `<div><label>Drivers (one per line)</label><textarea id="th-drivers" rows="4" placeholder="Real reasons it moved">${esc((t.drivers || []).join("\n"))}</textarea></div>` +
    `<div><label>Downside triggers (one per line)</label><textarea id="th-triggers" rows="4" placeholder="What breaks the thesis">${esc((t.downside_triggers || []).join("\n"))}</textarea></div>`;
  body.appendChild(g);
  const actions = el("div", "thesis-actions");
  const saveBtn = el("button", "primary", "Save thesis");
  const note = el("span", "status", t.as_of ? "last saved " + new Date(t.as_of).toLocaleString() : "");
  saveBtn.addEventListener("click", async () => {
    saveBtn.disabled = true;
    note.classList.remove("err");
    note.textContent = "saving...";
    try {
      const payload = {
        summary: $$<HTMLTextAreaElement>("#th-summary").value,
        action: $$<HTMLTextAreaElement>("#th-action").value,
        drivers: $$<HTMLTextAreaElement>("#th-drivers").value.split("\n").map((s) => s.trim()).filter(Boolean),
        downside_triggers: $$<HTMLTextAreaElement>("#th-triggers").value.split("\n").map((s) => s.trim()).filter(Boolean),
      };
      const updated = await api("/api/thesis/" + encodeURIComponent(rec.symbol || ""), "POST", payload);
      note.textContent = "saved " + new Date(updated.thesis.as_of).toLocaleString();
    } catch (e) {
      note.textContent = "save failed: " + (e as Error).message;
      note.classList.add("err");
    } finally {
      saveBtn.disabled = false;
    }
  });
  actions.appendChild(saveBtn);
  actions.appendChild(note);
  body.appendChild(actions);
  return card;
}
