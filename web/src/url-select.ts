// URL-bound <select> wiring shared by analytics views (risk, attribution, tax).
// Reads a nav param, validates against option values, syncs the control, and on
// change updates replaceViewState (eliding the default) before reloading data.
import { navFromUrl, replaceViewState } from "./shell";

export type UrlSelectParam = "range" | "benchmark" | "soon";

type WiredSelect = HTMLSelectElement & { _urlBound?: boolean };

export interface UrlBoundSelectOpts {
  select: HTMLSelectElement | null;
  param: UrlSelectParam;
  defaultValue: string;
  onValue: (value: string) => void;
  reload: () => void;
}

function validatedValue(
  select: HTMLSelectElement,
  requested: string | undefined,
  defaultValue: string,
): string {
  return requested && Array.from(select.options).some((o) => o.value === requested)
    ? requested
    : defaultValue;
}

/** Wire one select to a URL param; safe to call on every view entry. */
export function initUrlBoundSelect(opts: UrlBoundSelectOpts): void {
  const { select, param, defaultValue, onValue, reload } = opts;
  if (!select) return;

  const nav = navFromUrl();
  const value = validatedValue(select, nav[param], defaultValue);
  select.value = value;
  onValue(value);

  const wired = select as WiredSelect;
  if (wired._urlBound) return;
  wired._urlBound = true;
  select.addEventListener("change", () => {
    const next = select.value || defaultValue;
    onValue(next);
    replaceViewState({ [param]: next === defaultValue ? "" : next });
    reload();
  });
}
