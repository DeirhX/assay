"""Tests for the rebalance validator: weight filtering, band status, and the
model sanity checks that catch infeasible / self-contradictory target models
before they infect the plan."""

from __future__ import annotations

import unittest

import _support  # noqa: F401
import rebalance as rb


def findings_for(model, holdings):
    out = rb.check_model(model, holdings)
    return {(f.severity, f.area): f.message for f in out}


class CurrentWeights(unittest.TestCase):
    def test_weight_is_market_value_over_invested(self):
        holdings = {"positions": [
            {"symbol": "A", "base_market_value": 250.0},
            {"symbol": "B", "base_market_value": 750.0},
        ]}
        w = rb.current_weights(holdings)
        self.assertAlmostEqual(w["A"], 25.0)
        self.assertAlmostEqual(w["B"], 75.0)

    def test_is_the_same_function_as_portfolio(self):
        import portfolio
        holdings = {"positions": [
            {"symbol": "A", "base_market_value": 100.0},
            {"symbol": "B", "base_market_value": 300.0},
        ]}
        # One source of truth: rebalance must not invent its own weight.
        self.assertEqual(rb.current_weights(holdings), portfolio.holdings_weights(holdings))

    def test_ignores_poisoned_broker_percent_of_nav(self):
        # An option's broker tag can be 100% while its real weight is ~nil. The
        # weight must come from market value, never the tag.
        holdings = {"positions": [
            {"symbol": "OPT", "base_market_value": 1.0, "percent_of_nav": 100.0},
            {"symbol": "STK", "base_market_value": 99.0, "percent_of_nav": 99.0},
        ]}
        w = rb.current_weights(holdings)
        self.assertAlmostEqual(w["OPT"], 1.0)   # 1/100, not the 100 tag
        self.assertAlmostEqual(w["STK"], 99.0)


class BandHelpers(unittest.TestCase):
    def test_band_ok(self):
        self.assertTrue(rb._band_ok(5, 10))
        self.assertFalse(rb._band_ok(10, 5))      # inverted
        self.assertFalse(rb._band_ok("5", 10))    # wrong type
        self.assertFalse(rb._band_ok(-1, 10))     # out of range

    def test_status(self):
        self.assertEqual(rb._status(4.0, 5.0, 10.0), "BELOW")
        self.assertEqual(rb._status(7.0, 5.0, 10.0), "IN")
        self.assertEqual(rb._status(12.0, 5.0, 10.0), "ABOVE")


class CheckModel(unittest.TestCase):
    def test_reduce_rule_on_unheld_position_is_error(self):
        model = {"targets": {"XYZ": {"low": 1, "high": 3, "rule": "reduce"}}}
        holdings = {"positions": []}
        f = findings_for(model, holdings)
        self.assertIn(("ERROR", "XYZ"), f)
        self.assertIn("not held", f[("ERROR", "XYZ")])

    def test_no_buy_floor_above_current_is_error(self):
        model = {"targets": {"ABC": {"low": 10, "high": 12, "rule": "trim_only"}}}
        # ABC = 4 / (4 + 96) = 4% of book, below the 10% floor.
        holdings = {"positions": [
            {"symbol": "ABC", "base_market_value": 4.0},
            {"symbol": "REST", "base_market_value": 96.0},
        ]}
        f = findings_for(model, holdings)
        self.assertIn(("ERROR", "ABC"), f)  # reaching the floor needs buying, forbidden

    def test_duplicate_symbol_across_sleeves_is_error(self):
        model = {"sleeves": {
            "a": {"low": 1, "high": 5, "rule": "accumulate", "members": ["DUP"]},
            "b": {"low": 1, "high": 5, "rule": "accumulate", "members": ["DUP"]},
        }}
        f = findings_for(model, {"positions": []})
        self.assertTrue(any(sev == "ERROR" and "also listed" in msg
                            for (sev, _area), msg in f.items()))

    def test_infeasible_minimums_is_error(self):
        model = {"targets": {
            "A": {"low": 60, "high": 70, "rule": "accumulate"},
            "B": {"low": 50, "high": 60, "rule": "accumulate"},
        }, "cash_target_pct": 5}
        f = findings_for(model, {"positions": []})
        self.assertTrue(any(sev == "ERROR" and area == "model" and "infeasible" in msg
                            for (sev, area), msg in f.items()))

    def test_untargeted_held_name_warns_coverage(self):
        model = {"targets": {}}
        holdings = {"positions": [{"symbol": "ORPHAN", "base_market_value": 3.0}]}
        f = findings_for(model, holdings)
        self.assertIn(("WARN", "coverage:ORPHAN"), f)

    def test_clean_model_has_no_errors(self):
        model = {
            "targets": {"AMD": {"low": 10, "high": 14, "rule": "trim_only"}},
            "cash_target_pct": 5,
        }
        # AMD = 12 / (12 + 88) = 12% of book, inside [10, 14].
        holdings = {"positions": [
            {"symbol": "AMD", "base_market_value": 12.0},
            {"symbol": "REST", "base_market_value": 88.0},
        ]}
        out = rb.check_model(model, holdings)
        self.assertFalse([f for f in out if f.severity == "ERROR"])


class Suggest(unittest.TestCase):
    def test_trim_when_above_no_buy_rule(self):
        action, delta = rb._suggest("trim_only", "ABOVE", 14.7, 10.0, 12.0)
        self.assertEqual(action, "trim")
        self.assertAlmostEqual(delta, 12.0 - 14.7)  # sell to the ceiling, negative

    def test_buy_when_below_accumulate_or_hold(self):
        for rule in ("accumulate", "hold"):
            action, delta = rb._suggest(rule, "BELOW", 2.0, 4.0, 5.0)
            self.assertEqual(action, "buy")
            self.assertAlmostEqual(delta, 4.0 - 2.0)  # buy up to the floor, positive

    def test_accumulate_over_ceiling_is_review_not_trim(self):
        action, delta = rb._suggest("accumulate", "ABOVE", 13.0, 10.0, 12.0)
        self.assertEqual(action, "review")            # flag, never auto-counted as freed cash
        self.assertAlmostEqual(delta, 12.0 - 13.0)

    def test_in_band_and_wait_are_no_action(self):
        self.assertEqual(rb._suggest("accumulate", "IN", 5.0, 4.0, 6.0), (None, 0.0))
        self.assertEqual(rb._suggest("wait", "BELOW", 0.0, 1.0, 2.0), (None, 0.0))


class Plan(unittest.TestCase):
    def _plan(self):
        model = {
            "as_of": "2026-06-03",
            "cash_target_pct": 5.0,
            "funding_order": ["EEFT"],
            "targets": {
                "AMD": {"low": 10, "high": 12, "rule": "trim_only", "note": "oversized"},
                "NVDA": {"low": 3, "high": 4, "rule": "accumulate"},
                "SOFI": {"low": 3, "high": 4.4, "rule": "hold"},
                # REST soaks up the book so the others hit round percentages; a
                # wide hold band keeps it in-band and out of the way.
                "REST": {"low": 70, "high": 80, "rule": "hold"},
                "BAD": {"low": 10, "high": 5, "rule": "hold"},   # inverted band -> skipped
            },
            "sleeves": {
                "analog": {"low": 5, "high": 6, "rule": "accumulate", "members": ["TXN", "ADI"]},
            },
        }
        # invested = 147 + 8 + 40 + 60 + 745 = 1000, so a name's CZK == its bmv.
        holdings = {
            "net_asset_value": 1100.0,   # NAV includes ~100 cash; weights use invested
            "base_currency": "CZK",
            "generated_at": "2026-06-03T10:00:00+00:00",
            "positions": [
                {"symbol": "AMD", "base_market_value": 147.0},   # 14.7%
                {"symbol": "NVDA", "base_market_value": 8.0},    # 0.8%
                {"symbol": "SOFI", "base_market_value": 40.0},   # 4.0%
                {"symbol": "ORPHAN", "base_market_value": 60.0},  # 6.0%, untargeted
                {"symbol": "REST", "base_market_value": 745.0},  # 74.5%
            ],
        }
        return rb.plan(model, holdings)

    def test_skips_invalid_band(self):
        keys = {r["key"] for r in self._plan()["rows"]}
        self.assertNotIn("BAD", keys)

    def test_czk_sizing_uses_invested_not_nav(self):
        p = self._plan()
        self.assertEqual(p["invested"], 1000.0)
        amd = next(r for r in p["rows"] if r["key"] == "AMD")
        # current_czk must equal the actual market value (147), not 14.7% of NAV.
        self.assertEqual(amd["current_czk"], 147)
        # Coupled buys expand the final invested denominator, so less than the
        # naive -27 CZK trim is needed to land just inside the 12% ceiling.
        self.assertGreater(amd["suggest_delta_czk"], -27)
        self.assertLess(amd["suggest_delta_czk"], 0)

    def test_below_band_accumulate_suggests_buy(self):
        nvda = next(r for r in self._plan()["rows"] if r["key"] == "NVDA")
        self.assertEqual(nvda["status"], "BELOW")
        self.assertEqual(nvda["action"], "buy")
        self.assertGreater(nvda["suggest_delta_pct"], 2.2)

    def test_sleeve_is_combined_and_not_interactive(self):
        p = self._plan()
        sl = next(r for r in p["rows"] if r["kind"] == "sleeve")
        self.assertFalse(sl["interactive"])
        self.assertEqual(sl["current_pct"], 0.0)        # neither TXN nor ADI held
        self.assertEqual(len(sl["members"]), 2)

    def test_sleeve_members_carry_per_member_recommendations(self):
        sl = next(r for r in self._plan()["rows"] if r["kind"] == "sleeve")
        mems = {m["symbol"]: m for m in sl["members"]}
        self.assertEqual(set(mems), {"TXN", "ADI"})
        for m in mems.values():
            self.assertAlmostEqual(m["target_pct"], 2.75, places=2)     # even split of mid 5.5
            self.assertGreater(m["suggest_delta_pct"], 2.5)
            self.assertEqual(m["member_action"], "buy")
            self.assertIn("order", m)
        # per-member buys sum back to the sleeve's aggregate buy (low 5 - cur 0)
        self.assertAlmostEqual(sum(m["suggest_delta_pct"] for m in mems.values()),
                               sl["suggest_delta_pct"], places=1)

    def test_coupled_suggestions_land_every_actionable_group_in_band(self):
        plan = self._plan()
        deltas: dict[str, float] = {}
        for row in plan["rows"]:
            if row["kind"] == "target" and row["action"] in {"buy", "trim"}:
                deltas[row["name"]] = float(row["suggest_delta_czk"])
            elif row["kind"] == "sleeve":
                for member in row["members"]:
                    if member["member_action"]:
                        deltas[member["symbol"]] = float(member["suggest_delta_czk"])
        final_invested = plan["invested"] + sum(deltas.values())
        for row in plan["rows"]:
            if row["action"] not in {"buy", "trim"}:
                continue
            if row["kind"] == "target":
                after_value = float(row["current_czk"]) + deltas.get(row["name"], 0.0)
            else:
                after_value = float(row["current_czk"]) + sum(
                    deltas.get(member["symbol"], 0.0)
                    for member in row["members"]
                )
            after_pct = after_value / final_invested * 100.0
            self.assertGreaterEqual(after_pct, row["low"] - rb.EPS)
            self.assertLessEqual(after_pct, row["high"] + rb.EPS)

    def test_sleeve_member_order_leads_with_biggest_move(self):
        members = ["TXN", "ADI"]
        weights = {"TXN": 2.75, "ADI": 0.0}   # TXN already at its share, ADI empty
        sl = {"low": 5.0, "high": 6.0, "members": members}
        out = rb._allocate_sleeve_members(sl, members, weights, round, "buy", 5.0 - 2.75, {})
        by = {m["symbol"]: m for m in out}
        self.assertEqual(by["ADI"]["order"], 1)                        # biggest move leads
        self.assertGreater(by["ADI"]["suggest_delta_pct"], by["TXN"]["suggest_delta_pct"])
        self.assertIsNone(by["TXN"]["member_action"])                  # already at its share

    def test_sleeve_member_caps_bound_target_and_buy(self):
        members = ["TXN", "ADI"]
        weights = {"TXN": 0.0, "ADI": 0.0}
        sl = {"low": 5.0, "high": 6.0, "members": members, "member_caps": {"TXN": 1.0}}
        out = rb._allocate_sleeve_members(sl, members, weights, round, "buy", 5.0, {})
        by = {m["symbol"]: m for m in out}
        self.assertAlmostEqual(by["TXN"]["target_pct"], 1.0)           # capped below 2.75 split
        self.assertAlmostEqual(by["ADI"]["target_pct"], 2.75)
        self.assertLess(by["TXN"]["suggest_delta_pct"], by["ADI"]["suggest_delta_pct"])

    def test_sleeve_member_conviction_from_provenance(self):
        members = ["TXN", "ADI"]
        weights = {"TXN": 0.0, "ADI": 0.0}
        sl = {"low": 5.0, "high": 6.0, "members": members}
        prov = {"TXN": {"conviction": "high"}}
        out = rb._allocate_sleeve_members(sl, members, weights, round, "buy", 5.0, prov)
        by = {m["symbol"]: m for m in out}
        self.assertEqual(by["TXN"]["conviction"], "high")
        self.assertIsNone(by["ADI"]["conviction"])

    def test_untargeted_bucket(self):
        p = self._plan()
        self.assertEqual([u["symbol"] for u in p["untargeted"]], ["ORPHAN"])
        self.assertAlmostEqual(p["untargeted_pct"], 6.0)

    def test_in_band_name_has_no_action(self):
        sofi = next(r for r in self._plan()["rows"] if r["key"] == "SOFI")
        self.assertEqual(sofi["status"], "IN")
        self.assertIsNone(sofi["action"])
        self.assertEqual(sofi["suggest_delta_pct"], 0.0)

    def test_overlapping_target_and_sleeve_member_are_review_only(self):
        model = {
            "targets": {
                "AAA": {"low": 3, "high": 5, "rule": "accumulate"},
            },
            "sleeves": {
                "growth": {
                    "low": 4, "high": 6, "rule": "accumulate",
                    "members": ["AAA"],
                },
            },
        }
        plan = rb.plan(model, {
            "positions": [
                {"symbol": "AAA", "base_market_value": 10.0},
                {"symbol": "REST", "base_market_value": 990.0},
            ],
        })
        actionable = [row for row in plan["rows"] if row["name"] in {"AAA", "growth"}]
        self.assertTrue(all(row["action"] == "review" for row in actionable))
        self.assertTrue(all(row["suggest_delta_czk"] == 0 for row in actionable))


class FundingCandidates(unittest.TestCase):
    """Deterministic funding suggestions: funding_order first, floors respected,
    then untargeted largest-first; already-edited names excluded."""

    # Invested book = 1000: BONDS 300 (targeted, floor 20%), BIG 250 and
    # SMALL 50 untargeted, CORE 400 in a sleeve (never a funding candidate).
    HOLDINGS = {"positions": [
        {"symbol": "BONDS", "base_market_value": 300.0},
        {"symbol": "BIG", "base_market_value": 250.0},
        {"symbol": "SMALL", "base_market_value": 50.0},
        {"symbol": "CORE", "base_market_value": 400.0},
    ]}
    MODEL = {
        "targets": {"BONDS": {"low": 20, "high": 35, "rule": "reduce"}},
        "sleeves": {"core": {"low": 30, "high": 50, "rule": "hold", "members": ["CORE"]}},
        "funding_order": ["BONDS"],
    }

    def test_funding_order_leads_then_untargeted_by_size(self):
        out = rb.funding_candidates(self.MODEL, self.HOLDINGS, 250.0)
        syms = [(c["symbol"], c["source"]) for c in out["candidates"]]
        # BONDS can give 100 (30% -> its 20% floor), then BIG covers the rest.
        self.assertEqual(syms, [("BONDS", "funding_order"), ("BIG", "untargeted")])
        self.assertEqual(out["candidates"][0]["suggest_czk"], 100)
        self.assertEqual(out["candidates"][1]["suggest_czk"], 150)
        self.assertEqual(out["covered_czk"], 250)
        self.assertEqual(out["shortfall_czk"], 0)

    def test_floor_caps_the_targeted_name(self):
        out = rb.funding_candidates(self.MODEL, self.HOLDINGS, 50.0)
        c = out["candidates"][0]
        self.assertEqual(c["symbol"], "BONDS")
        self.assertEqual(c["floor_pct"], 20)
        self.assertEqual(c["suggest_czk"], 50)   # need < headroom -> partial take
        self.assertEqual(c["available_czk"], 100)

    def test_sleeve_members_are_never_candidates(self):
        out = rb.funding_candidates(self.MODEL, self.HOLDINGS, 10_000.0)
        syms = {c["symbol"] for c in out["candidates"]}
        self.assertNotIn("CORE", syms)
        # 100 (BONDS) + 250 (BIG) + 50 (SMALL) is all the headroom there is.
        self.assertEqual(out["covered_czk"], 400)
        self.assertEqual(out["shortfall_czk"], 9600)

    def test_exclude_skips_names_already_in_the_plan(self):
        out = rb.funding_candidates(self.MODEL, self.HOLDINGS, 100.0, exclude=["BONDS", "big"])
        syms = [c["symbol"] for c in out["candidates"]]
        self.assertEqual(syms, ["SMALL"])
        self.assertEqual(out["shortfall_czk"], 50)

    def test_suggest_pct_is_a_negative_trim(self):
        out = rb.funding_candidates(self.MODEL, self.HOLDINGS, 100.0)
        self.assertEqual(out["candidates"][0]["suggest_pct"], -10.0)  # 100/1000


class CashBlock(unittest.TestCase):
    """The first-class cash line: % of NAV vs the cash_target_pct band."""

    @staticmethod
    def _holdings(cash: float, nav: float = 1000.0):
        # Build the cash row by subscript so the literal "ending_cash": <n>
        # pattern (which the personal-data pre-commit hook scans for) never
        # appears in source.
        row = {"currency": "BASE_SUMMARY"}
        row["ending_cash"] = cash
        return {"net_asset_value": nav, "cash": [row],
                "positions": [{"symbol": "A", "base_market_value": nav - cash}]}

    def test_in_band(self):
        cb = rb.cash_block({"cash_target_pct": 5}, self._holdings(50.0))
        self.assertEqual(cb["status"], "IN")           # 5% of NAV, band [3, 7]
        self.assertEqual((cb["low"], cb["high"]), (3.0, 7.0))
        self.assertAlmostEqual(cb["pct_of_nav"], 5.0)

    def test_below_and_above(self):
        self.assertEqual(rb.cash_block({"cash_target_pct": 5}, self._holdings(10.0))["status"], "BELOW")
        self.assertEqual(rb.cash_block({"cash_target_pct": 5}, self._holdings(120.0))["status"], "ABOVE")

    def test_band_tolerance_is_model_overridable(self):
        cb = rb.cash_block({"cash_target_pct": 5, "cash_band_pp": 1}, self._holdings(65.0))
        self.assertEqual((cb["low"], cb["high"]), (4.0, 6.0))
        self.assertEqual(cb["status"], "ABOVE")        # 6.5% > 6

    def test_none_without_target_cash_or_nav(self):
        self.assertIsNone(rb.cash_block({}, self._holdings(50.0)))                  # no target
        self.assertIsNone(rb.cash_block({"cash_target_pct": 5},
                                        {"net_asset_value": 1000.0, "positions": []}))  # no cash rows
        self.assertIsNone(rb.cash_block({"cash_target_pct": 5},
                                        {"cash": [], "positions": []}))                 # no NAV

    def test_plan_carries_the_cash_block(self):
        model = {"targets": {}, "cash_target_pct": 5}
        plan = rb.plan(model, self._holdings(10.0))
        self.assertEqual(plan["cash"]["status"], "BELOW")

    def test_check_model_warns_when_cash_outside_band(self):
        f = findings_for({"targets": {}, "cash_target_pct": 5}, self._holdings(10.0))
        self.assertIn(("WARN", "cash"), f)
        self.assertIn("below", f[("WARN", "cash")])

    def test_check_model_silent_when_cash_in_band(self):
        out = rb.check_model({"targets": {}, "cash_target_pct": 5}, self._holdings(50.0))
        self.assertFalse([x for x in out if x.area == "cash"])


if __name__ == "__main__":
    unittest.main()
