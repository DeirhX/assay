import { describe, expect, it } from "vitest";
import {
  fmtCZK,
  fmtMoney,
  fmtMoneyCcy,
  fmtShares,
  fmtSigned,
  fmtSignedPct1,
  fmtSignedPct2,
  fmtStamp,
  relAge,
} from "../src/display/format";

describe("display/format money helpers", () => {
  it("fmtMoney and fmtSigned round to whole locale-grouped units", () => {
    expect(fmtMoney(1234)).toBe((1234).toLocaleString());
    expect(fmtSigned(-500)).toBe("-" + (500).toLocaleString());
    expect(fmtMoney(null)).toBe("n/a");
  });

  it("fmtCZK uses compact precision without a currency suffix", () => {
    expect(fmtCZK(999)).toBe("999");
    expect(fmtCZK(1500)).toBe((1500).toLocaleString());
  });

  it("fmtMoneyCcy appends the currency code", () => {
    expect(fmtMoneyCcy(1200, "CZK")).toBe("1,200 CZK");
    expect(fmtMoneyCcy(1200, "")).toBe("1,200 ");
  });

  it("fmtSignedPct helpers keep sign and decimal precision", () => {
    expect(fmtSignedPct1(1.25)).toBe("+1.3%");
    expect(fmtSignedPct2(-0.04)).toBe("-0.04%");
  });

  it("fmtShares reports billions with two decimals", () => {
    expect(fmtShares(1.5)).toBe("1.50B");
  });
});

describe("display/format time helpers", () => {
  it("fmtStamp renders local date and time", () => {
    const iso = "2026-07-13T08:30:00.000Z";
    expect(fmtStamp(iso)).toMatch(/2026-07-13 \d{2}:\d{2}/);
    expect(fmtStamp(null)).toBe("n/a");
  });

  it("relAge returns empty for junk input", () => {
    expect(relAge(null)).toBe("");
    expect(relAge("not-a-date")).toBe("");
  });
});
