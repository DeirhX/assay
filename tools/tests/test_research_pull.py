"""Tests for the trust engine -- the part of the app that justifies its name.

These cover the cross-source sanity checks, the market-cap reconciliation /
quarantine path (the $349B-class bug), metric/profile merging, and the peer
score, plus an integration smoke test over a real scraped dossier."""

from __future__ import annotations

import unittest

import _support  # noqa: F401  (sys.path side effect)
import research_pull as rp


def node(value, source):
    return {"value": value, "source": source}


def severities(findings):
    return [f["severity"] for f in findings]


def by_metric(findings, metric):
    return [f for f in findings if f["metric"] == metric]


class CrossChecks(unittest.TestCase):
    def test_healthy_identity_yields_no_error(self):
        y = {"price": node(100.0, "yahoo"), "shares_out_b": node(10.0, "yahoo"),
             "market_cap_usd_b": node(1000.0, "yahoo"), "revenue_ttm_usd_b": node(50.0, "yahoo")}
        s = {"shares_out_b": node(10.0, "sec_edgar"), "revenue_ttm_usd_b": node(50.0, "sec_edgar")}
        findings = rp._cross_checks("AMD", {"last": 100.0}, y, s)
        self.assertNotIn("ERROR", severities(findings))
        # share agreement is reported as INFO, not silence
        self.assertTrue(any(f["metric"] == "shares_out" and f["severity"] == "INFO" for f in findings))

    def test_impossible_market_cap_is_error(self):
        # price x shares = 1000, but reported cap claims 3490 -> internally impossible
        y = {"price": node(100.0, "yahoo"), "shares_out_b": node(10.0, "yahoo"),
             "market_cap_usd_b": node(3490.0, "yahoo")}
        findings = rp._cross_checks("BUG", {"last": 100.0}, y, None)
        mc = by_metric(findings, "market_cap")
        self.assertTrue(mc and mc[0]["severity"] == "ERROR")

    def test_share_count_disagreement_warns(self):
        y = {"shares_out_b": node(10.0, "yahoo")}
        s = {"shares_out_b": node(12.0, "sec_edgar")}
        findings = rp._cross_checks("X", {}, y, s)
        warn = by_metric(findings, "shares_out")
        self.assertTrue(warn and warn[0]["severity"] == "WARN")

    def test_revenue_disagreement_warns_beyond_tolerance(self):
        y = {"revenue_ttm_usd_b": node(50.0, "yahoo")}
        s = {"revenue_ttm_usd_b": node(70.0, "sec_edgar")}  # 40% off > 15% tol
        findings = rp._cross_checks("X", {}, y, s)
        self.assertTrue(by_metric(findings, "revenue_ttm"))

    def test_foreign_filer_without_sec_anchor_flags_coverage(self):
        y = {"price": node(50.0, "yahoo")}
        findings = rp._cross_checks("ASML", {"last": 50.0}, y, None)
        cov = by_metric(findings, "coverage")
        self.assertTrue(cov and cov[0]["severity"] == "INFO")

    def test_stale_price_is_info(self):
        y = {"price": node(100.0, "yahoo")}
        findings = rp._cross_checks("X", {"last": 110.0}, y, None)  # 10% gap > 3%
        self.assertTrue(by_metric(findings, "price"))


class ReconcileMarketCap(unittest.TestCase):
    def _error_check(self):
        return [{"severity": "ERROR", "metric": "market_cap", "message": "impossible."}]

    def test_reconciles_from_fmp_anchor(self):
        merged = {"market_cap_usd_b": node(3490.0, "yahoo"), "ps": node(20.0, "yahoo")}
        y = {"shares_out_b": node(10.0, "yahoo"), "market_cap_usd_b": node(3490.0, "yahoo")}
        f = {"market_cap_usd_b": node(1000.0, "fmp")}
        checks = self._error_check()
        rp._reconcile_market_cap(merged, y, None, f, 100.0, checks)
        self.assertAlmostEqual(merged["market_cap_usd_b"]["value"], 1000.0, places=1)
        self.assertEqual(merged["market_cap_usd_b"]["source"], "reconciled")
        self.assertEqual(checks[0]["severity"], "WARN")  # downgraded once resolved
        self.assertNotIn("quarantined", merged["ps"])

    def test_unresolvable_quarantines_cap_and_ps(self):
        merged = {"market_cap_usd_b": node(3490.0, "yahoo"), "ps": node(20.0, "yahoo")}
        y = {"shares_out_b": node(10.0, "yahoo"), "market_cap_usd_b": node(3490.0, "yahoo")}
        checks = self._error_check()
        rp._reconcile_market_cap(merged, y, None, None, None, checks)  # no anchor at all
        self.assertIsNone(merged["market_cap_usd_b"]["value"])
        self.assertTrue(merged["market_cap_usd_b"]["quarantined"])
        self.assertEqual(merged["market_cap_usd_b"]["display"], "unreliable")
        self.assertTrue(merged["ps"]["quarantined"])

    def test_no_error_means_no_mutation(self):
        merged = {"market_cap_usd_b": node(1000.0, "yahoo")}
        before = dict(merged["market_cap_usd_b"])
        rp._reconcile_market_cap(merged, None, None, None, 100.0, [])
        self.assertEqual(merged["market_cap_usd_b"], before)


class Merging(unittest.TestCase):
    def test_merge_metrics_respects_source_preference(self):
        by = {"pe_ttm": [node(30.0, "fmp"), node(28.0, "yahoo")]}  # prefs: yahoo first
        merged = rp._merge_metrics(by)
        self.assertEqual(merged["pe_ttm"]["value"], 28.0)
        self.assertEqual(merged["pe_ttm"]["source"], "yahoo")
        self.assertEqual(merged["pe_ttm"]["all_sources"], {"fmp": 30.0, "yahoo": 28.0})
        self.assertIsInstance(merged["pe_ttm"]["display"], str)

    def test_merge_profile_first_non_empty_wins(self):
        p1 = {"summary": "primary", "sector": ""}
        p2 = {"summary": "fallback", "industry": "Semis"}
        out = rp._merge_profile(p1, p2)
        self.assertEqual(out["summary"], "primary")
        self.assertEqual(out["industry"], "Semis")
        self.assertNotIn("sector", out)  # empty string never filled it

    def test_merge_profile_all_empty_is_none(self):
        self.assertIsNone(rp._merge_profile(None, {}, {"x": ""}))

    def test_collect_ignores_malformed_nodes(self):
        out = rp._collect({"a": node(1, "yahoo"), "bad": {"value": 1}}, {"a": node(2, "fmp")})
        self.assertEqual(len(out["a"]), 2)
        self.assertNotIn("bad", out)


class Score(unittest.TestCase):
    def _rec(self, rule, status):
        return {
            "metrics": {"rev_growth_yoy_pct": node(20.0, "yahoo"), "pe_fwd": node(22.0, "yahoo")},
            "portfolio": {"status": status, "target": {"rule": rule}},
            "momentum": {"chg_3m_pct": 5.0, "chg_12m_pct": 10.0},
        }

    def test_accumulate_below_band_beats_avoid(self):
        good = rp._research_score(self._rec("accumulate", "below_band"), "INFO")
        bad = rp._research_score(self._rec("avoid", "above_band"), "INFO")
        self.assertGreater(good, bad)

    def test_score_clamped_to_unit_range(self):
        for rule in ("accumulate", "avoid", "reduce", "hold"):
            sc = rp._research_score(self._rec(rule, "below_band"), "ERROR")
            self.assertGreaterEqual(sc, 0)
            self.assertLessEqual(sc, 100)

    def test_data_error_penalizes_score(self):
        clean = rp._research_score(self._rec("accumulate", "below_band"), "INFO")
        dirty = rp._research_score(self._rec("accumulate", "below_band"), "ERROR")
        self.assertGreater(clean, dirty)


class WorstSeverity(unittest.TestCase):
    def test_picks_most_severe(self):
        checks = [{"severity": "INFO"}, {"severity": "ERROR"}, {"severity": "WARN"}]
        self.assertEqual(rp._worst_severity(checks), "ERROR")

    def test_empty_is_info(self):
        self.assertEqual(rp._worst_severity([]), "INFO")


class RealDossierSmoke(unittest.TestCase):
    """Run derived functions over a real scraped record to catch shape drift."""

    @classmethod
    def setUpClass(cls):
        cls.rec = _support.sample_record()

    def setUp(self):
        if not self.rec:
            self.skipTest("no scraped dossier available under data/research/")

    def test_worst_severity_over_real_checks(self):
        self.assertIn(rp._worst_severity(self.rec.get("cross_checks", [])),
                      {"ERROR", "WARN", "INFO"})

    def test_history_for_returns_list(self):
        # cache may be empty on a fresh checkout; we only assert the contract.
        self.assertIsInstance(rp.history_for(self.rec["symbol"]), list)


if __name__ == "__main__":
    unittest.main()
