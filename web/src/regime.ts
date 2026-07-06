import { $, api, esc } from "./core";

// ---- regime strip ----------------------------------------------------------
// A deliberately dumb macro backdrop above the segment leaderboard: rates,
// credit, USD, volatility, each with a 1-year trend arrow. Context for the
// human, never a signal. Non-critical: any failure just hides the strip rather
// than surfacing an error, since it decorates a view rather than being one.

interface RegimeChip {
  id: string;
  label: string;
  note?: string;
  value?: number | null;
  display?: string | null;
  as_of?: string | null;
  trend?: "up" | "down" | "flat";
  rising?: string;
  change_display?: string | null;
  url?: string | null;
}

interface RegimePayload {
  as_of?: string | null;
  caption?: string;
  strip?: RegimeChip[];
  errors?: string[];
  cached?: boolean;
}

const ARROW: Record<string, string> = { up: "\u2197", down: "\u2198", flat: "\u2192" };

async function loadRegime() {
  const host = $("#regime-strip");
  if (!host) return;
  try {
    const r = await api<RegimePayload>("/api/regime");
    renderRegime(r);
  } catch {
    host.hidden = true;   // context is optional; never block the segment view
  }
}

function renderRegime(r: RegimePayload) {
  const host = $("#regime-strip");
  if (!host) return;
  const chips = r.strip || [];
  if (!chips.length) {
    host.hidden = true;
    return;
  }
  host.hidden = false;
  const cells = chips.map((c) => {
    const trend = c.trend || "flat";
    const arrow = ARROW[trend] || ARROW.flat;
    const change = c.change_display ? ` <span class="regime-chg">${esc(c.change_display)}</span>` : "";
    const title = `${esc(c.note || "")}${c.rising ? ` — up = ${esc(c.rising)}` : ""}`;
    return `<div class="regime-chip" title="${title}">` +
      `<span class="regime-k">${esc(c.label)}</span>` +
      `<span class="regime-v">${esc(c.display ?? "n/a")} <span class="regime-arrow ${esc(trend)}">${arrow}</span></span>` +
      change +
    `</div>`;
  }).join("");
  host.innerHTML =
    `<div class="regime-row">${cells}</div>` +
    `<div class="regime-caption">${esc(r.caption || "")}</div>`;
}

export { loadRegime, renderRegime };
