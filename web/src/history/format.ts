// Shared money/currency formatting for the portfolio-history view, used by the
// NAV chart, the stat cards, and the activity/trade tables. Rounded to whole
// units (the ledger's cents are noise at portfolio scale) and locale-grouped.
// Extracted from history.ts so the chart can live in its own module.
import { esc } from "../core";

type Num = number | string | null | undefined;

export const fmtMoney = (v: Num): string =>
  v == null || Number.isNaN(Number(v)) ? "n/a" : Math.round(Number(v)).toLocaleString();

export const fmtSigned = (v: Num): string =>
  v == null || Number.isNaN(Number(v))
    ? "n/a"
    : (Number(v) >= 0 ? "+" : "") + Math.round(Number(v)).toLocaleString();

// A muted currency-code chip. Not sensitive (a code isn't a value), so it stays
// visible under privacy mode while the number beside it blurs.
export const ccyTag = (code: string | null | undefined): string =>
  code ? ` <span class="ccy">${esc(code)}</span>` : "";
