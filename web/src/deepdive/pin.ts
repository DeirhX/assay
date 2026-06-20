// Conviction pin control: durable allocation intent on a symbol (whether/how
// much to own), distinct from the price-levels ladder (at what price to act).
// A pin anchors future strategy runs and is never auto-dropped; a run may still
// challenge it (surfaced in the working draft). Writes standing judgement on the
// live model via the staging edit endpoint.
import { api, el, esc } from "../core";

interface PinRec {
  source?: string;
  stance?: string;
  floor_pct?: number | null;
  ceiling_pct?: number | null;
  rationale?: string | null;
}

interface PinRec2 { symbol?: string }

const STANCES = ["accumulate", "hold", "wait", "trim_only", "avoid"];

const PIN_HELP =
  "A conviction pin records your standing intent for this name (whether and how " +
  "much to own). Future plans respect it and never silently drop it \u2014 they can " +
  "still challenge it, surfaced in the working draft. Separate from the price ladder.";

export function pinBlock(rec: PinRec2, existingPin: PinRec | null | undefined): HTMLElement {
  const sym = rec.symbol || "";
  const block = el("div", "pin-block");
  let pin: PinRec | null = existingPin || null;
  let open = false;

  function chip(): string {
    if (!pin) return `<span class="muted">not pinned</span>`;
    const floor = typeof pin.floor_pct === "number" ? " \u2265" + pin.floor_pct + "%" : "";
    const ceil = typeof pin.ceiling_pct === "number" ? " \u2264" + pin.ceiling_pct + "%" : "";
    return `<span class="chip warn" title="${esc(pin.rationale || "")}">${esc(pin.stance || "hold")}${floor}${ceil}</span>`;
  }

  function draw() {
    const has = !!pin;
    const stance = (pin && pin.stance) || "accumulate";
    const floor = pin && typeof pin.floor_pct === "number" ? pin.floor_pct : "";
    const ceil = pin && typeof pin.ceiling_pct === "number" ? pin.ceiling_pct : "";
    // Collapsed strip: a one-liner with the current state and a toggle. The full
    // controls only render when expanded, so the pin doesn't compete with the
    // price-levels card on every visit.
    block.innerHTML =
      `<div class="pin-strip">` +
        `<span class="pin-pin">\ud83d\udccc</span>` +
        `<span class="pin-title" title="${esc(PIN_HELP)}">Conviction</span>` +
        chip() +
        `<button class="ghost pin-toggle" type="button">${open ? "Close" : has ? "Edit" : "Pin " + esc(sym)}</button>` +
      `</div>` +
      (open
        ? `<div class="pin-edit">` +
            `<div class="pin-controls">` +
              `<label>Stance <select class="pin-stance">${STANCES.map((s) => `<option value="${s}"${s === stance ? " selected" : ""}>${s}</option>`).join("")}</select></label>` +
              `<label>Floor % <input class="pin-floor" type="number" step="0.5" value="${floor}" placeholder="opt"></label>` +
              `<label>Ceiling % <input class="pin-ceil" type="number" step="0.5" value="${ceil}" placeholder="opt"></label>` +
            `</div>` +
            `<input class="pin-rationale" type="text" placeholder="why (optional)" value="${esc((pin && pin.rationale) || "")}">` +
            `<div class="pin-actions">` +
              `<button class="primary pin-save" type="button">${has ? "Update pin" : "Pin conviction"}</button>` +
              (has ? `<button class="ghost pin-clear" type="button">Unpin</button>` : "") +
              `<span class="status pin-status"></span>` +
            `</div>` +
          `</div>`
        : "");

    (block.querySelector(".pin-toggle") as HTMLElement).addEventListener("click", () => { open = !open; draw(); });
    if (!open) return;

    const status = block.querySelector(".pin-status") as HTMLElement;
    (block.querySelector(".pin-save") as HTMLElement).addEventListener("click", async () => {
      const stanceV = (block.querySelector(".pin-stance") as HTMLSelectElement).value;
      const floorV = (block.querySelector(".pin-floor") as HTMLInputElement).value.trim();
      const ceilV = (block.querySelector(".pin-ceil") as HTMLInputElement).value.trim();
      const why = (block.querySelector(".pin-rationale") as HTMLInputElement).value.trim();
      status.textContent = "saving…";
      try {
        const res = await api<{ pin: PinRec }>("/api/staging/edit", "POST", {
          op: "pin", key: sym, stance: stanceV,
          floor_pct: floorV === "" ? null : Number(floorV),
          ceiling_pct: ceilV === "" ? null : Number(ceilV),
          rationale: why,
        });
        pin = res.pin || null;
        open = false;
        draw();
      } catch (e) {
        status.textContent = "failed: " + (e instanceof Error ? e.message : String(e));
        status.classList.add("err");
      }
    });
    const clearBtn = block.querySelector(".pin-clear") as HTMLElement | null;
    if (clearBtn) clearBtn.addEventListener("click", async () => {
      status.textContent = "removing…";
      try {
        await api("/api/staging/edit", "POST", { op: "unpin", key: sym });
        pin = null;
        open = false;
        draw();
      } catch (e) {
        status.textContent = "failed: " + (e instanceof Error ? e.message : String(e));
        status.classList.add("err");
      }
    });
  }
  draw();
  return block;
}
