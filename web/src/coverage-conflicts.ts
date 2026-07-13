import { esc } from "./core";
import type { CoveredCallCoverageViolation } from "./api-types";

export type CoverageResolution = "keep-calls" | "keep-shares";

export function coverageResolutionLegIds(
  violation: CoveredCallCoverageViolation,
  resolution: CoverageResolution,
): string[] {
  return resolution === "keep-calls"
    ? violation.stock_leg_ids
    : violation.call_leg_ids;
}

export function coverageConflictsHtml(
  violations: CoveredCallCoverageViolation[],
): string {
  if (!violations.length) return "";
  return `<div class="coverage-conflicts">` +
    `<span class="reb-route-eyebrow">Share coverage conflict</span>` +
    `<strong>Reconcile share sales and covered calls</strong>` +
    `<p>These combinations exceed the shares held. They cannot be approved or sent to IBKR.</p>` +
    violations.map((violation) =>
      `<div class="coverage-conflict" data-coverage-symbol="${esc(violation.symbol)}">` +
        `<div><strong>${esc(violation.symbol)}</strong>` +
          `<span>${violation.current_shares.toLocaleString()} shares held · ` +
          (violation.planned_stock_sell_shares
            ? `${violation.planned_stock_sell_shares.toLocaleString()} queued for direct sale · `
            : "") +
          (violation.working_stock_sell_shares
            ? `${violation.working_stock_sell_shares.toLocaleString()} in working IBKR stock sells · `
            : "") +
          (violation.held_short_call_contracts
            ? `${violation.held_short_call_contracts} held short call contract(s) · `
            : "") +
          (violation.working_short_call_contracts
            ? `${violation.working_short_call_contracts} working IBKR call sell(s) · `
            : "") +
          `${violation.selected_call_contracts} queued call contract(s) · ` +
          `<b>${violation.excess_shares.toLocaleString()} shares over capacity</b></span></div>` +
        `<div class="coverage-conflict-actions">` +
          ((violation.working_stock_order_ids || []).length
            ? `<button class="ghost danger" type="button" ` +
              `data-coverage-cancel-order-ids="${esc(violation.working_stock_order_ids!.join(","))}" ` +
              `data-coverage-cancel-kind="stock sell" ` +
              `data-coverage-symbol="${esc(violation.symbol)}">` +
              `Cancel working stock sell${violation.working_stock_order_ids!.length === 1 ? "" : "s"}` +
              ` · keep calls</button>`
            : "") +
          ((violation.working_call_order_ids || []).length
            ? `<button class="ghost danger" type="button" ` +
              `data-coverage-cancel-order-ids="${esc(violation.working_call_order_ids!.join(","))}" ` +
              `data-coverage-cancel-kind="call sell" ` +
              `data-coverage-symbol="${esc(violation.symbol)}">` +
              `Cancel working call sell${violation.working_call_order_ids!.length === 1 ? "" : "s"}` +
              ` · keep queued calls</button>`
            : "") +
          (violation.stock_leg_ids.length
            ? `<button class="ghost" type="button" data-coverage-action="keep-calls" ` +
              `data-coverage-symbol="${esc(violation.symbol)}" ` +
              `data-coverage-leg-ids="${esc(violation.stock_leg_ids.join(","))}">` +
              `Keep calls · exclude share sale</button>`
            : "") +
          (violation.call_leg_ids.length
            ? `<button class="ghost" type="button" data-coverage-action="keep-shares" ` +
              `data-coverage-symbol="${esc(violation.symbol)}" ` +
              `data-coverage-leg-ids="${esc(violation.call_leg_ids.join(","))}">` +
              `${(violation.working_stock_order_ids || []).length
                || (violation.working_call_order_ids || []).length
                ? "Keep working orders"
                : "Keep share sale"} · exclude queued calls</button>`
            : "") +
        `</div>` +
        `<span class="coverage-conflict-status"></span>` +
      `</div>`
    ).join("") +
  `</div>`;
}
