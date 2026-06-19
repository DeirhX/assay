// Shared dossier card primitives used across the deep-dive seams and the
// composer: a generic <details> card, the data-trust severity tag, the metric
// source line, and the business-profile card. Extracted from deepdive.ts.
import { el, esc, sectionCard } from "../core";
import { linkifyTickers } from "../analyses";

interface Profile {
  summary?: string;
  sector?: string;
  industry?: string;
  country?: string;
  employees?: number | string;
  website?: string;
}

// A <details>-based card. `open` decides the initial state; the meta sits on the
// summary line so a collapsed card still tells you what's inside.
export function collapsibleCard(
  titleHtml: string,
  { meta = "", open = false }: { meta?: string; open?: boolean } = {},
): { details: HTMLDetailsElement; body: HTMLElement } {
  const details = el("details", "card collapse");
  details.open = !!open;
  const summary = el("summary", "collapse-head");
  summary.innerHTML =
    `<span class="collapse-title">${titleHtml}</span>` +
    (meta ? `<span class="collapse-meta">${meta}</span>` : "") +
    `<span class="collapse-caret" aria-hidden="true">\u203a</span>`;
  details.appendChild(summary);
  const body = el("div", "collapse-body");
  details.appendChild(body);
  return { details, body };
}

export function dataQualityTag(checks: { severity?: string }[]): string {
  const sev = checks.some((c) => c.severity === "ERROR") ? "ERROR" : checks.some((c) => c.severity === "WARN") ? "WARN" : "INFO";
  const txt: Record<string, string> = { ERROR: "conflicts found", WARN: "minor disagreement", INFO: "clean" };
  return ` &nbsp;<span class="dot ${sev}"></span><span style="font-size:12px;color:var(--muted)">${txt[sev]}</span>`;
}

export function sourceLine(node: { all_sources?: Record<string, number>; source?: unknown }): string {
  const all = node.all_sources || {};
  const keys = Object.keys(all);
  if (keys.length <= 1) return `source: ${esc(node.source)}`;
  // multiple sources -> show each and flag spread
  const vals = keys.map((k) => all[k]);
  const max = Math.max(...vals.map(Math.abs)), min = Math.min(...vals.map(Math.abs));
  const disagree = max > 0 && (max - min) / max > 0.05;
  const parts = keys.map((k) => `${k}:${Number(all[k]).toPrecision(4)}`).join("  ");
  return `<span class="${disagree ? "disagree" : ""}">${esc(parts)}</span>`;
}

export function renderBusiness(rec: { profile?: Profile }): HTMLElement | null {
  const p = rec.profile || {};
  if (!p.summary && !p.sector && !p.industry) return null;

  const card = sectionCard("Business", "biz-card");

  const bits: string[] = [];
  if (p.sector) bits.push(esc(p.sector));
  if (p.industry) bits.push(esc(p.industry));
  if (p.country) bits.push(esc(p.country));
  if (p.employees) bits.push(`${Number(p.employees).toLocaleString()} employees`);
  if (bits.length) card.appendChild(el("div", "biz-meta", bits.join(" · ")));
  if (p.website) {
    const host = String(p.website).replace(/^https?:\/\//, "").replace(/\/$/, "");
    card.appendChild(el("div", "biz-meta",
      `<a href="${esc(p.website)}" target="_blank" rel="noopener">${esc(host)} \u2197</a>`));
  }

  if (p.summary) {
    const body = el("p", "biz-summary clamp", esc(p.summary));
    card.appendChild(body);
    linkifyTickers(body);
    if (p.summary.length > 320) {
      const toggle = el("button", "linklike biz-toggle", "Show more");
      toggle.type = "button";
      toggle.addEventListener("click", () => {
        const open = body.classList.toggle("expanded");
        toggle.textContent = open ? "Show less" : "Show more";
      });
      card.appendChild(toggle);
    }
  }
  return card;
}
