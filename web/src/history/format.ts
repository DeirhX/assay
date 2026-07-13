// Shared money/currency formatting for the portfolio-history view, used by the
// NAV chart, the stat cards, and the activity/trade tables. Rounded to whole
// units (the ledger's cents are noise at portfolio scale) and locale-grouped.
// Extracted from history.ts so the chart can live in its own module.
import { esc } from "../core";
export { fmtMoney, fmtSigned } from "../display/format";

// A muted currency-code chip. Not sensitive (a code isn't a value), so it stays
// visible under privacy mode while the number beside it blurs.
export const ccyTag = (code: string | null | undefined): string =>
  code ? ` <span class="ccy">${esc(code)}</span>` : "";
