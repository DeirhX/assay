/** Shared e2e rebalance plan scaffolding and trade-queue stubs. */

export const rebalanceRow = {
  key: "AAPL", name: "AAPL", kind: "target", rule: "accumulate", held: true,
  current_pct: 2, current_czk: 20_000, low: 3, high: 5, mid: 4, status: "BELOW",
  drift_pct: -1, action: "buy", suggest_delta_pct: 1, suggest_delta_czk: 10_000,
  mark_price: 190, mark_currency: "USD",
  last_quote: { price: 191.25, currency: "USD", source: "quote cache" },
  note: null, members: null, interactive: true,
};

export const rebalancePlan = {
  nav: 1_000_000, invested: 1_000_000, currency: "CZK",
  snapshot: "2026-07-10T00:00:00+00:00", as_of: "2026-07-10",
  cash_target_pct: 5, funding_order: [], cash: null,
  rows: [rebalanceRow], untargeted: [], untargeted_pct: 0, provenance: {},
};

export const rebalanceProjection = {
  currency: "CZK",
  trades: [{ symbol: "AAPL", delta_czk: 10_000 }],
  summary: {
    bands_in_before: 0, bands_in_after: 1, bands_total: 1,
    net_cash_czk: -10_000, realized_taxable_gain_czk: 0,
  },
  after: { rows: [{ ...rebalanceRow, current_pct: 3, current_czk: 30_000, status: "IN" }] },
  before_status: { AAPL: "BELOW" },
  cash: null, caveats: [],
};

/** Empty, unreviewed trade queue — common rebalance-view boot state. */
export const emptyUnreviewedQueue = {
  trades: [] as { symbol: string; delta_czk: number }[],
  revision: "",
  reviewed: false,
};

/** Unreviewed queue carrying staged trades (revision fixed for assertions). */
export function unreviewedQueue(
  trades: { symbol: string; delta_czk: number }[],
  revision = "rev-1",
) {
  return { trades, revision, reviewed: false as const };
}
