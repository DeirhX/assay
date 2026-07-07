import { $$, apiLoad, el, esc, fmtStamp, freshnessNote, simpleTable, statTile } from "./core";

// ---- tax calendar ----------------------------------------------------------
// The Czech 3-year exemption made proactive: every not-yet-exempt lot on a
// forward calendar. Gain lots near the mark are a "wait" (the gain goes tax
// free); loss lots near the mark are a "harvest before the deadline" (a realized
// loss stops offsetting gains once the lot turns exempt). Types are local to
// this view (like risk.ts) since /api/tax-calendar serves only it.

interface ExemptionRow {
  symbol: string;
  open_datetime: string | null;
  exempt_on: string | null;
  days_to_exempt: number;
  shares: number | null;
  market_value: number | null;
  gain: number | null;
  tax_if_sold_now: number | null;
  soon: boolean;
  currency: string;
}

interface HarvestRow {
  symbol: string;
  open_datetime: string | null;
  deadline: string | null;
  days_to_deadline: number;
  shares: number | null;
  market_value: number | null;
  loss: number | null;
  soon: boolean;
  currency: string;
}

interface TaxCalendar {
  as_of?: string;
  snapshot?: string | null;
  currency?: string;
  soon_days?: number;
  tax_rate?: number;
  exemptions?: ExemptionRow[];
  harvest?: HarvestRow[];
  totals?: {
    n_exemptions?: number;
    n_exemptions_soon?: number;
    n_harvest?: number;
    n_harvest_soon?: number;
    tax_free_soon?: number;
    tax_free_total?: number;
    harvestable_loss?: number;
    harvestable_loss_soon?: number;
  };
  year_end?: {
    date?: string;
    days_to_year_end?: number;
    harvestable_loss?: number;
    harvestable_by_year_end?: number;
  };
}

let _taxSoon = "60";

const fmtMoney = (v: number | null | undefined, ccy = "CZK") =>
  v == null ? "n/a" : Math.round(Number(v)).toLocaleString("en-US") + " " + ccy;
const shortDate = (iso: string | null | undefined) => (iso ? String(iso).slice(0, 10) : "n/a");

// Countdown severity: a small number of days is the *opportunity* (almost free —
// don't sell now), so lead with "warn/attention" as it shrinks rather than
// treating it as danger. Past the soon window it's just informational.
function daysClass(days: number, soonDays: number) {
  if (days <= Math.min(30, soonDays)) return "warn";
  if (days <= soonDays) return "";
  return "muted";
}

async function loadTax() {
  await apiLoad({
    path: "/api/tax-calendar?soon_days=" + encodeURIComponent(_taxSoon),
    status: $$("#tax-status"),
    clear: [$$("#tax-result")],
    loading: "Building the exemption calendar…",
    errorLabel: "Could not build the tax calendar",
    render: renderTax,
  });
}

function renderTax(r: TaxCalendar) {
  const out = $$("#tax-result");
  out.innerHTML = "";
  const ccy = r.currency || "CZK";
  const t = r.totals || {};
  const ye = r.year_end || {};

  // Meta line.
  const meta = el("div", "reb-meta");
  meta.innerHTML =
    `<span>as of ${esc(r.as_of || "n/a")}</span>` +
    `<span>snapshot ${freshnessNote(r.snapshot) || esc(fmtStamp(r.snapshot))}</span>` +
    `<span>soon = ${esc(r.soon_days ?? 60)}d</span>` +
    `<span>tax rate ${esc(Math.round((r.tax_rate ?? 0.15) * 100))}%</span>`;
  out.appendChild(meta);

  // Headline tiles.
  const stats = el("div", "risk-stats");
  stats.appendChild(statTile("Tax-free soon", fmtMoney(t.tax_free_soon, ccy),
    { family: "risk-stat", cls: (t.tax_free_soon || 0) > 0 ? "good" : "muted",
      title: `Czech tax you avoid by waiting for gain lots that clear the 3-year mark within ${r.soon_days ?? 60} days.` }));
  stats.appendChild(statTile("Exemptions upcoming", String(t.n_exemptions ?? 0),
    { family: "risk-stat", cls: "muted",
      title: `${t.n_exemptions_soon ?? 0} within the soon window; ${fmtMoney(t.tax_free_total, ccy)} of tax becomes free in total across all held gain lots.` }));
  stats.appendChild(statTile("Harvestable loss", fmtMoney(t.harvestable_loss, ccy),
    { family: "risk-stat", cls: (t.harvestable_loss || 0) > 0 ? "warn" : "muted",
      title: "Realized losses on <3y lots offset taxable gains this tax period. They stop being usable once the lot turns exempt." }));
  stats.appendChild(statTile("Days to year-end", String(ye.days_to_year_end ?? "n/a"),
    { family: "risk-stat", cls: "muted",
      title: `${esc(ye.date || "")}: ${fmtMoney(ye.harvestable_by_year_end, ccy)} of losses still harvestable by then.` }));
  out.appendChild(stats);

  // Year-end nudge, only when there's a loss to harvest and it's within reach.
  if ((ye.harvestable_by_year_end || 0) > 0 && (ye.days_to_year_end ?? 999) <= 60) {
    const note = el("div", "tax-year-end",
      `<strong>Year-end harvest:</strong> ${esc(ye.days_to_year_end)} days to ${esc(ye.date || "")} — up to ` +
      `${esc(fmtMoney(ye.harvestable_by_year_end, ccy))} of losses can still offset this year's taxable gains.`);
    out.appendChild(note);
  }

  // Exemption calendar: gain lots going tax-free (wait).
  const exs = r.exemptions || [];
  const exSec = el("div", "risk-section");
  exSec.appendChild(el("h3", undefined, "Exemptions — gain lots going tax-free"));
  exSec.appendChild(el("p", "hint",
    "Waiting past each date turns the gain tax-free. The tax column is what a trim of that lot costs today."));
  if (!exs.length) {
    exSec.appendChild(el("div", "empty-state", "No taxable-gain lots — everything held at a gain has already cleared the 3-year mark."));
  } else {
    exSec.appendChild(simpleTable<ExemptionRow>({
      className: "tax-cal-table",
      head: `<tr><th>Name</th><th>Opened</th><th>Tax-free on</th><th class="num">In</th>` +
            `<th class="num">Value</th><th class="num">Gain</th><th class="num">Tax if sold now</th></tr>`,
      rows: exs,
      cells: (e) => {
        const cls = daysClass(e.days_to_exempt, r.soon_days ?? 60);
        return `<td class="tax-sym">${esc(e.symbol)}${e.soon ? ' <span class="tax-flag">soon</span>' : ""}</td>` +
          `<td class="muted">${esc(shortDate(e.open_datetime))}</td>` +
          `<td>${esc(shortDate(e.exempt_on))}</td>` +
          `<td class="num ${cls}">${esc(e.days_to_exempt)}d</td>` +
          `<td class="num muted">${esc(fmtMoney(e.market_value, ""))}</td>` +
          `<td class="num">${esc(fmtMoney(e.gain, ""))}</td>` +
          `<td class="num tax-cost">${esc(fmtMoney(e.tax_if_sold_now, ""))}</td>`;
      },
    }));
  }
  out.appendChild(exSec);

  // Harvest deadlines: loss lots whose usable-loss window is closing (act).
  const hvs = r.harvest || [];
  const hvSec = el("div", "risk-section");
  hvSec.appendChild(el("h3", undefined, "Harvest deadlines — loss lots turning exempt"));
  hvSec.appendChild(el("p", "hint",
    "A realized loss offsets taxable gains only while the lot is under 3 years. After the deadline the loss is stranded."));
  if (!hvs.length) {
    hvSec.appendChild(el("div", "empty-state", "No taxable-loss lots — nothing to harvest before an exemption deadline."));
  } else {
    hvSec.appendChild(simpleTable<HarvestRow>({
      className: "tax-cal-table",
      head: `<tr><th>Name</th><th>Opened</th><th>Deadline</th><th class="num">In</th>` +
            `<th class="num">Value</th><th class="num">Loss</th></tr>`,
      rows: hvs,
      cells: (h) => {
        const cls = daysClass(h.days_to_deadline, r.soon_days ?? 60);
        return `<td class="tax-sym">${esc(h.symbol)}${h.soon ? ' <span class="tax-flag warn">soon</span>' : ""}</td>` +
          `<td class="muted">${esc(shortDate(h.open_datetime))}</td>` +
          `<td>${esc(shortDate(h.deadline))}</td>` +
          `<td class="num ${cls}">${esc(h.days_to_deadline)}d</td>` +
          `<td class="num muted">${esc(fmtMoney(h.market_value, ""))}</td>` +
          `<td class="num tax-cost">${esc(fmtMoney(h.loss, ""))}</td>`;
      },
    }));
  }
  out.appendChild(hvSec);
}

function initTaxControls() {
  const sel = $$<HTMLSelectElement & { _wired?: boolean }>("#tax-soon");
  if (sel && !sel._wired) {
    sel._wired = true;
    sel.value = _taxSoon;
    sel.addEventListener("change", () => { _taxSoon = sel.value || "60"; loadTax(); });
  }
}

export { loadTax, renderTax, initTaxControls };
