// Shared display formatters for money, signed amounts, and percentage deltas.
// Canonical time/CZK/shares helpers live in core (the runtime leaf); this module
// re-exports them and adds named wrappers used by history tables and analytics
// views. Import from here when you need the full set; import from core when you
// only need the established primitives.
import {
  fmtCZK,
  fmtShares,
  fmtStamp,
  freshnessNote,
  relAge,
} from "../core";

export { fmtCZK, fmtShares, fmtStamp, freshnessNote, relAge };

type Num = number | string | null | undefined;

const bad = (v: Num) => v == null || Number.isNaN(Number(v));

/** Whole units, locale-grouped — portfolio ledger scale (history NAV, activity). */
export const fmtMoney = (v: Num): string =>
  bad(v) ? "n/a" : Math.round(Number(v)).toLocaleString();

/** Signed whole units with explicit + for non-negative values. */
export const fmtSigned = (v: Num): string =>
  bad(v) ? "n/a" : (Number(v) >= 0 ? "+" : "") + Math.round(Number(v)).toLocaleString();

/** Rounded amount with a trailing currency code (tax calendar, attribution). */
export const fmtMoneyCcy = (v: Num, ccy = "CZK"): string =>
  bad(v) ? "n/a" : Math.round(Number(v)).toLocaleString("en-US") + " " + ccy;

/** Signed percentage to one decimal (risk FX contribution columns). */
export const fmtSignedPct1 = (v: Num): string =>
  bad(v) ? "n/a" : (Number(v) >= 0 ? "+" : "") + Number(v).toFixed(1) + "%";

/** Signed percentage to two decimals (attribution TWR tiles). */
export const fmtSignedPct2 = (v: Num): string =>
  bad(v) ? "n/a" : (Number(v) >= 0 ? "+" : "") + Number(v).toFixed(2) + "%";
