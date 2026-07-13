// Structurally identical analytics chrome: meta strips, caveat banners, and
// section shells shared by risk, attribution, tax, and history. Only helpers
// where markup is byte-for-byte the same pattern — views with extra affordances
// keep their own builders.
import { el, esc } from "../core";

/** Horizontal meta strip (`reb-meta`) built from pre-escaped span inner HTML. */
export function metaStrip(spans: string[]): HTMLElement {
  const meta = el("div", "reb-meta");
  meta.innerHTML = spans.map((s) => `<span>${s}</span>`).join("");
  return meta;
}

/** Caveat banner used by risk and attribution (identical markup). */
export function caveatBanner(
  caveats: string[],
  opts: { always?: boolean } = {},
): HTMLElement | null {
  if (!caveats.length && !opts.always) return null;
  const banner = el("div", "banner banner-warn risk-caveat");
  banner.innerHTML =
    `<strong>Read this before you trust a number below.</strong>` +
    `<ul>${caveats.map((c) => `<li>${esc(c)}</li>`).join("")}</ul>`;
  return banner;
}

/** Standard analytics section: titled block with an optional hint paragraph. */
export function analyticsSection(
  title: string,
  hint?: string,
  extraClass = "risk-section",
): HTMLElement {
  const sec = el("div", extraClass);
  sec.appendChild(el("h3", undefined, title));
  if (hint) sec.appendChild(el("p", "hint", hint));
  return sec;
}
