"""Tests for the whole-book portfolio optimizer: pool construction (source merge
+ conviction precedence), the deterministic sizer (proportional under caps, pin
clamps, avoid/trim/drop handling), and the proposal shape the staging/preview
back half consumes. Offline: model/holdings/basket are passed in directly."""

from __future__ import annotations

import unittest
from pathlib import Path

import _support  # noqa: F401
import optimizer
import target_model

# Keep the suite hermetic: never let a real, runtime-written portfolio review
# (data/portfolio-review.json) bleed convictions into build_pool()/optimize().
_REAL_REVIEW = optimizer.PORTFOLIO_REVIEW_JSON


def setUpModule():
    optimizer.PORTFOLIO_REVIEW_JSON = Path(__file__).with_name("__no_such_review__.json")


def tearDownModule():
    optimizer.PORTFOLIO_REVIEW_JSON = _REAL_REVIEW


def _model():
    return {
        "cash_target_pct": 10.0,
        "targets": {
            "NVDA": {"low": 8, "high": 12, "rule": "accumulate"},
            "INTC": {"low": 1, "high": 3, "rule": "trim_only"},
        },
        "sleeves": {"semis": {"members": ["NVDA", "AMD"], "member_caps": {"AMD": 4.0}}},
        "provenance": {"NVDA": {"source": "user-pin", "stance": "accumulate", "floor_pct": 9.0}},
    }


def _holdings():
    return {"positions": [
        {"symbol": "NVDA", "base_market_value": 6000.0},
        {"symbol": "INTC", "base_market_value": 2000.0},
        {"symbol": "AMD", "base_market_value": 1000.0},
        {"symbol": "QQQ", "base_market_value": 1000.0},
    ]}


def _basket():
    return [
        {"symbol": "AMD", "tier": "want", "sleeve": "semis"},
        {"symbol": "ASML", "tier": "curious", "source": "analyses",
         "segment": "semis-equip", "run": "semis-equip-2026-06-01"},
    ]


class Pool(unittest.TestCase):
    def test_union_and_sources(self):
        pool = optimizer.build_pool(model=_model(), holdings=_holdings(), basket_items=_basket())
        syms = {e["symbol"] for e in pool}
        # held (NVDA/INTC/AMD/QQQ) + targets (NVDA/INTC) + basket (AMD/ASML) + pin (NVDA)
        self.assertEqual(syms, {"NVDA", "INTC", "AMD", "QQQ", "ASML"})
        amd = next(e for e in pool if e["symbol"] == "AMD")
        self.assertIn("held", amd["sources"])
        self.assertTrue(any(s in amd["sources"] for s in ("basket", "manual")))

    def test_conviction_precedence(self):
        pool = {e["symbol"]: e for e in
                optimizer.build_pool(model=_model(), holdings=_holdings(), basket_items=_basket())}
        # pin stance accumulate beats the model band read
        self.assertEqual((pool["NVDA"]["conviction"], pool["NVDA"]["conviction_source"]), ("high", "pin"))
        # model rule trim_only -> avoid
        self.assertEqual((pool["INTC"]["conviction"], pool["INTC"]["conviction_source"]), ("avoid", "model"))
        # basket want -> medium via tier
        self.assertEqual((pool["AMD"]["conviction"], pool["AMD"]["conviction_source"]), ("medium", "tier"))
        # curious -> low
        self.assertEqual(pool["ASML"]["conviction"], "low")
        # held-only carry
        self.assertEqual((pool["QQQ"]["conviction"], pool["QQQ"]["conviction_source"]), ("medium", "held"))

    def test_include_curious_false_drops_parked_pick(self):
        pool = {e["symbol"] for e in optimizer.build_pool(
            model=_model(), holdings=_holdings(), basket_items=_basket(), include_curious=False)}
        self.assertNotIn("ASML", pool)  # curious + not held/targeted -> dropped
        self.assertIn("AMD", pool)


class Sizing(unittest.TestCase):
    def test_proposal_shape_and_budget(self):
        prop = optimizer.optimize(model=_model(), holdings=_holdings(), basket_items=_basket(),
                                  cash_target_pct=10.0)
        self.assertEqual(prop["schema_version"], 2)
        meta = prop["optimizer_meta"]
        self.assertEqual(meta["invested_budget_pct"], 90.0)
        # Sized midpoints never exceed the invested budget.
        self.assertLessEqual(meta["sized_midpoint_total_pct"], meta["invested_budget_pct"] + 0.5)
        for ch in prop["changes"]:
            self.assertIn(ch["action"], ("add_target", "modify_target", "remove_target"))
            self.assertIn("proposed_target", ch)

    def test_member_cap_respected(self):
        prop = optimizer.optimize(model=_model(), holdings=_holdings(), basket_items=_basket())
        amd = next(c for c in prop["changes"] if c["symbol"] == "AMD")
        # The cap bounds the target WEIGHT (band midpoint); the no-trade band
        # extends +/-15% around it, so the midpoint -- not the high -- is capped.
        mid = (amd["proposed_target"]["low"] + amd["proposed_target"]["high"]) / 2.0
        self.assertLessEqual(mid, 4.0 + 1e-6)

    def test_pin_floor_clamps_band(self):
        prop = optimizer.optimize(model=_model(), holdings=_holdings(), basket_items=_basket())
        nvda = next(c for c in prop["changes"] if c["symbol"] == "NVDA")
        self.assertGreaterEqual(nvda["proposed_target"]["low"], 9.0 - 1e-6)

    def test_avoid_held_trims_unheld_skipped(self):
        prop = optimizer.optimize(model=_model(), holdings=_holdings(), basket_items=_basket())
        intc = next(c for c in prop["changes"] if c["symbol"] == "INTC")
        self.assertEqual(intc["proposed_target"]["rule"], "trim_only")
        # An avoid name that is NOT held should not appear at all.
        m = _model()
        m["targets"]["XYZ"] = {"low": 0, "high": 0, "rule": "avoid"}
        prop2 = optimizer.optimize(model=m, holdings=_holdings(), basket_items=_basket())
        self.assertNotIn("XYZ", {c["symbol"] for c in prop2["changes"]})

    def test_drop_avoid_removes_unpinned_held(self):
        prop = optimizer.optimize(model=_model(), holdings=_holdings(), basket_items=_basket(),
                                  drop_avoid=True)
        intc = next(c for c in prop["changes"] if c["symbol"] == "INTC")
        self.assertEqual(intc["action"], "remove_target")

    def test_exclude_drops_name(self):
        prop = optimizer.optimize(model=_model(), holdings=_holdings(), basket_items=_basket(),
                                  exclude={"ASML"})
        self.assertNotIn("ASML", {c["symbol"] for c in prop["changes"]})

    def test_use_llm_falls_back_when_no_backend(self):
        # With no backend configured, llm_pool_convictions returns {} and the
        # deterministic convictions stand -- use_llm must never crash or empty out.
        orig = optimizer.ticker_analysis.available_backends
        optimizer.ticker_analysis.available_backends = lambda: {}
        try:
            prop = optimizer.optimize(model=_model(), holdings=_holdings(),
                                      basket_items=_basket(), use_llm=True)
        finally:
            optimizer.ticker_analysis.available_backends = orig
        self.assertEqual(prop["optimizer_meta"]["synthesis"], "deterministic")
        self.assertTrue(prop["changes"])

    def test_llm_overlay_changes_conviction_source(self):
        orig_b = optimizer.ticker_analysis.available_backends
        orig_r = optimizer.llm_pool_convictions
        optimizer.ticker_analysis.available_backends = lambda: {"x": True}
        optimizer.llm_pool_convictions = lambda pool, **k: {
            "AMD": {"conviction": "high", "rationale": "great", "conflict": ""}}
        try:
            prop = optimizer.optimize(model=_model(), holdings=_holdings(),
                                      basket_items=_basket(), use_llm=True)
        finally:
            optimizer.ticker_analysis.available_backends = orig_b
            optimizer.llm_pool_convictions = orig_r
        self.assertEqual(prop["optimizer_meta"]["synthesis"], "llm")
        amd = next(c for c in prop["changes"] if c["symbol"] == "AMD")
        self.assertEqual(amd["conviction"], "high")
        self.assertEqual(amd["conviction_source"], "llm")

    def test_proposal_is_previewable(self):
        # The proposal must flow through the existing preview path untouched.
        prop = optimizer.optimize(model=_model(), holdings=_holdings(), basket_items=_basket())
        draft = dict(_model())
        applied, skipped = target_model._apply_changes_to_model(draft, prop["changes"], blocked=set())
        self.assertTrue(applied)


def _flat_pool(n_medium: int, highs=("AAA", "BBB"), held=None):
    """A synthetic buy-only pool for exercising the concentration gates directly."""
    held = held or {}
    out = []
    for h in highs:
        out.append({"symbol": h, "conviction": "high", "conviction_source": "x",
                    "held_pct": held.get(h), "sleeve": "", "current_target": None, "rationale": ""})
    for i in range(n_medium):
        s = f"M{i:02d}"
        out.append({"symbol": s, "conviction": "medium", "conviction_source": "x",
                    "held_pct": held.get(s), "sleeve": "", "current_target": None, "rationale": ""})
    return out


class Concentration(unittest.TestCase):
    _M = {"targets": {}, "sleeves": {}}

    def test_max_names_caps_funded_count(self):
        changes, meta = optimizer.size_pool(_flat_pool(30), self._M, cash_target_pct=5.0,
                                            max_names=10)
        self.assertLessEqual(meta["funded_count"], 10)
        funded = {c["symbol"] for c in changes if c["action"] != "remove_target"}
        # The two high-conviction names always make the cut over the mediums.
        self.assertIn("AAA", funded)
        self.assertIn("BBB", funded)

    def test_min_position_prunes_dust(self):
        # 40 medium names sharing a 95% budget land at ~2.2% each; a 3% floor must
        # prune the smallest and lift every survivor to at least the floor.
        changes, meta = optimizer.size_pool(_flat_pool(40), self._M, cash_target_pct=5.0,
                                            min_position_pct=3.0)
        mids = [(c["proposed_target"]["low"] + c["proposed_target"]["high"]) / 2.0
                for c in changes if c["action"] != "remove_target"]
        self.assertTrue(mids)
        self.assertGreaterEqual(min(mids), 3.0 - 1e-6)
        self.assertLess(meta["funded_count"], 42)  # dust got pruned

    def test_aggressive_curve_rewards_high_more(self):
        pool = _flat_pool(10)
        bal, _ = optimizer.size_pool(pool, self._M, cash_target_pct=5.0,
                                     conviction_curve="balanced")
        agg, _ = optimizer.size_pool(pool, self._M, cash_target_pct=5.0,
                                     conviction_curve="aggressive")

        def mid(changes, sym):
            c = next(x for x in changes if x["symbol"] == sym)
            return (c["proposed_target"]["low"] + c["proposed_target"]["high"]) / 2.0

        self.assertGreater(mid(agg, "AAA"), mid(bal, "AAA"))

    def test_pin_never_pruned_by_floor(self):
        # A pinned low-conviction name must survive even a punishing min-position
        # floor that would otherwise drop it.
        model = {"targets": {}, "sleeves": {},
                 "provenance": {"PINLOW": {"source": "user-pin", "stance": "hold"}}}
        pool = _flat_pool(20) + [{"symbol": "PINLOW", "conviction": "low",
                                  "conviction_source": "pin", "held_pct": 0.5,
                                  "sleeve": "", "current_target": None, "rationale": ""}]
        changes, meta = optimizer.size_pool(pool, model, cash_target_pct=5.0,
                                            min_position_pct=3.0, max_names=5)
        funded = {c["symbol"] for c in changes if c["action"] != "remove_target"}
        self.assertIn("PINLOW", funded)


if __name__ == "__main__":
    unittest.main()
