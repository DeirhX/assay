# Auto-balance roadmap

Status: proposal · Scope: making the rebalancer's automation *mean what it says*
and closing the loop on the trade desk. This is a planning document, not a
commitment; every execution-touching item stays supervised and opt-in, in
keeping with the project's "not a robo-broker" stance.

## Where the app actually sits today

Assay is a rebalancing **workbench with supervised execution** — not an
unattended auto-balancer, and deliberately so. Mapping the pipeline onto an
automation ladder:

| Rung | Capability | Today |
|---|---|---|
| 1. Observe positions | IBKR Flex pull → `data/current-holdings.json` | Manual trigger, read-only (`tools/ibkr_portfolio.py`) |
| 2. Define targets | `data/target-model.json` bands/rules | Human-authored, or LLM-assisted via the 2-gate strategy flow (`tools/orchestrate.py`, `tools/target_construct.py`) |
| 3. Compute trades | drift → suggested CZK deltas | **Deterministic, automatic** (`tools/rebalance.py` `plan()`/`_suggest()`) |
| 4. Simulate consequences | what-if weights, cash, Czech tax lots | **Automatic** (`tools/whatif.py`, `tools/tax_lots.py`) |
| 5. Decide / approve | — | Human (always) |
| 6. Execute orders | gated, paper-first IBKR CPAPI trade desk | **Present since #36** (`tools/ibkr_trade.py`, `web/src/trade.ts`) — supervised, preview→token→confirm |
| 7. Close the loop | resync holdings after trading | **Manual** |

The recent trade desk (#36) is the important change: rung 6 is no longer
missing. It places live orders via the IBKR Client Portal Web API but is gated
hard — `IBKR_TRADING_ENABLED` is off by default, live accounts sit behind a
second `IBKR_ALLOW_LIVE` flag (paper `DU…` accounts work without it), every
basket must be previewed, and `_trade_place` refuses unless the caller echoes a
preview token that binds the exact basket+account and passes an explicit
`confirm` (`tools/serve.py` `_trade_place`). Orders are re-derived server-side,
never trusted from the client.

So the honest gaps left for "auto-balance" are **not** "build execution." They
are: (a) the planner's cash target is decorative, (b) the staged basket is
incomplete (sleeves/untargeted aren't sized), (c) the loop isn't closed after a
fill, and (d) a few execution-desk hardening items. None of these require, or
should introduce, unattended trading.

## Non-goals (explicit)

- **No unattended / scheduled trading.** No cron, timer, or auto-fire. A human
  stages, previews, and confirms every basket. This roadmap does not change that.
- **No LLM in the trade-sizing or execution path.** Conviction may inform
  *targets* (strategy flow, already gated); trade math stays deterministic.
- **No widening of the read-only Flex reader** into a write path. Execution lives
  only in the CPAPI desk behind its flags.

---

## Phase 1 — Make the cash target real (high leverage, low risk, deterministic)

**Problem.** `cash_target_pct` is passed straight through `rebalance.plan()` for
display and never enters trade sizing:

```text
# tools/rebalance.py, plan() return
"cash_target_pct": cash_target,      # display only
"funding_order": model.get("funding_order", []),
```

Worse, there is a **basis mismatch**: position weights are computed as a percent
of the *invested book* (`portfolio.invested_value`, cash excluded from the
denominator), while the cash target is documented as a percent of *NAV*. Cash is
therefore not a quantity the engine can steer — any "hold X% cash" intent is
entirely on the human. `check_model()` only uses `cash_target` for a feasibility
check (Σ lows + cash ≤ 100%), not for sizing.

**Decision to make first (design, not code).** Pick one and record it in the
model's `rules_legend`:
1. **Cash as a first-class line** (recommended): keep position weights on the
   invested book, but surface cash as its own row with a target band, and have
   the planner's net-cash math compare the *projected* cash against the target,
   flagging a rebalance that would breach it. Smallest blast radius; weights
   semantics unchanged.
2. **Reframe weights to % of NAV** (cash in the denominator). Cleaner
   conceptually, but touches every weight, every band in `target-model.json`,
   `risk.py`, the static site, and all snapshots. Higher risk; defer unless (1)
   proves insufficient.

**Tasks (assuming option 1):**
- `tools/rebalance.py`: compute `cash_pct` (cash / NAV or cash / invested,
  whichever the decision picks — state it), add a synthetic cash row or a
  top-level `cash` block to `plan()` with `low`/`high` from `cash_target_pct`
  (± a band), and a `status` (BELOW/IN/ABOVE). Keep `_suggest` untouched for
  securities; cash is informational drift, not a tradeable "buy CASH" row.
- `tools/rebalance.py` `check_model()`: keep the feasibility check; add a WARN
  when current cash is outside the cash band.
- `web/src/rebalance.ts` `renderRebalance()`: show projected cash vs target in
  the existing `reb-stats` block (the `Net cash` tile already exists), and color
  it against the target band so "this basket leaves you under your cash floor" is
  visible at a glance.
- Tests: `tools/tests/test_rebalance.py` — cash row/status math and the
  feasibility/band WARN; a what-if that breaches the cash floor surfaces it.

**Scope guard.** Cash is never auto-traded to hit the target. Phase 1 only
*measures and flags*. No new buy/sell action is emitted for a cash drift.

---

## Phase 2 — Complete and close the basket (medium leverage)

The trade desk can only execute what the planner stages, and today the staged
basket is partial. Closing these makes "plan → place" trustworthy end-to-end.

- **Sleeve member sizing.** `plan()` emits an *aggregate* suggestion for a
  sleeve and leaves member allocation to the human ("spread across members").
  Add a deterministic split (proportional to current member weights, honoring
  `member_caps`) so a sleeve drift becomes concrete per-symbol deltas that can
  be staged. Keep it opt-in in the UI; never silently expand a sleeve into
  orders. (`tools/rebalance.py`, `web/src/rebalance.ts`.)
- **Untargeted funding.** Untargeted held names get no suggestion. When net cash
  is short, surface them as candidate funding (respecting `funding_order`) rather
  than leaving the human to eyeball the bucket. Recommendation only.
- **Wire `funding_order` / `member_caps` into sizing**, not just narrative/
  validation. Today they affect CLI ordering and `check_model` warnings but never
  `_suggest`. This is what makes a multi-name basket self-consistent with the
  model's stated funding priorities.
- **Close the loop after a fill.** After `_trade_place` succeeds, prompt (don't
  auto-run) a holdings resync so `current-holdings.json` reflects the new
  positions, and offer to log the basket to the journal (the what-if view already
  has a "Log to journal" affordance). (`tools/serve.py` `_trade_place`,
  `web/src/trade.ts`.)

**Scope guard.** Everything here is recommendation/convenience. No item places an
order on its own; the preview→token→confirm gate in `_trade_place` remains the
only path to a live order.

---

## Phase 3 — Harden the execution desk (lower leverage, safety-focused)

The desk that shipped in #36 is well-gated; these tighten the remaining edges.

- **Surface IBKR's confirmation prompts.** `ibkr_trade.place_orders(...,
  auto_confirm=True)` auto-affirms IBKR's downstream margin/price-cap questions
  after the human confirms the *basket*. Consider a two-step: place with
  `auto_confirm=False`, return the prompts to the UI, and require a second human
  click to affirm material warnings (margin calls, price caps). The human should
  see "this trips a margin warning" before it is confirmed.
- **Preview freshness.** The preview token binds basket+account but not price/
  time. A basket previewed at 10:00 and placed at 15:00 sizes shares off a stale
  snapshot. Add a short token TTL (or a "prices are N minutes old" banner) so a
  stale preview can't silently place mis-sized orders.
- **Order-type policy.** `build_orders` defaults to `MKT`/`DAY`. For a rebalance,
  consider surfacing limit-order options (and a marketable-limit default) so a
  thin name can't fill far from the snapshot mark the basket was sized against.
- **Partial-fill / reconciliation.** Document and (later) handle the case where
  some orders fill and others reject, so the journal + resync reflect reality.

**Scope guard.** Hardening only; no new automation. If anything, this phase makes
the desk *more* deliberate, not less.

---

## Verification (every phase)

```powershell
py -3 -m pytest tools/tests -q
npm install; npm run lint; npm run typecheck; npm run test; npm run build
```

Execution-touching changes (Phase 3, and the loop-close in Phase 2) must be
validated on a **paper** account (`DU…`) with `IBKR_ALLOW_LIVE` unset before any
live use, and the trade-desk tests (`tools/tests/test_ibkr_trade.py`) extended to
cover new gating.

## Priority

1. **Phase 1** — small, deterministic, removes a genuinely misleading "feature"
   (a cash target that does nothing). Do this first.
2. **Phase 2** — makes the staged basket complete enough to trust the
   plan→place handoff.
3. **Phase 3** — safety hardening of the existing desk; valuable but the desk is
   already conservative, so lower urgency.
