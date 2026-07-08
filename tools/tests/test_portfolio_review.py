"""Tests for the batch portfolio review: verdict parsing + heuristic conviction,
the review store round-trip, and the optimizer pool reading those convictions
back as a held-name signal. Offline: the LLM backend is stubbed off and the
review store is redirected to a temp file."""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

import _support  # noqa: F401
import analysis_jobs as aj
import jobs
import optimizer


class Verdict(unittest.TestCase):
    def test_extract_verdict_section(self):
        report = "# Title\n\n## Verdict\nAccumulate, high confidence — strong moat.\n\n## Bull case\n- x"
        v = aj._extract_verdict(report)
        self.assertIn("Accumulate", v)
        self.assertNotIn("Bull case", v)

    def test_stance_and_conviction(self):
        self.assertEqual(aj._verdict_stance("Accumulate, high confidence"), "accumulate")
        self.assertEqual(aj._stance_conviction("accumulate", "high confidence"), "high")
        self.assertEqual(aj._stance_conviction("accumulate", "medium"), "medium")
        self.assertEqual(aj._verdict_stance("Trim — overvalued"), "trim")
        self.assertEqual(aj._stance_conviction("trim", "low"), "avoid")
        self.assertEqual(aj._stance_conviction("hold", "medium"), "medium")

    def test_headline_stance_beats_body_rulebook_chatter(self):
        # The body discusses the rulebook ("suggests accumulate"); the headline
        # says Hold. The headline must win, or a Hold gets sized up.
        v = "**Hold — medium confidence.** Position is below band so the rulebook suggests accumulate later."
        self.assertEqual(aj._verdict_stance(v), "hold")
        v2 = "**Hold, medium confidence** — monopoly franchise and raised 2026 guidance."
        self.assertEqual(aj._verdict_stance(v2), "hold")

    def test_confidence_phrase_not_stray_high(self):
        # "52-week high" / "high-quality" must not pass for analyst confidence.
        v = "**Accumulate — medium confidence.** $MSFT trades 25% off its 52-week high, a high-quality name."
        self.assertEqual(aj._verdict_stance(v), "accumulate")
        self.assertEqual(aj._stance_conviction("accumulate", v), "medium")

    def test_synthesize_heuristic_only(self):
        orig = aj.ticker_analysis.available_backends
        aj.ticker_analysis.available_backends = lambda: {}
        try:
            convs, summary = aj._synthesize_portfolio({
                "NVDA": "Accumulate, high confidence",
                "INTC": "Trim, low confidence",
            })
        finally:
            aj.ticker_analysis.available_backends = orig
        self.assertEqual(convs["NVDA"]["conviction"], "high")
        self.assertEqual(convs["INTC"]["conviction"], "avoid")
        self.assertEqual(summary, "")


class ReviewStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = optimizer.PORTFOLIO_REVIEW_JSON
        optimizer.PORTFOLIO_REVIEW_JSON = Path(self.tmp.name) / "portfolio-review.json"

    def tearDown(self):
        optimizer.PORTFOLIO_REVIEW_JSON = self._orig
        self.tmp.cleanup()

    def test_round_trip(self):
        optimizer.save_portfolio_review(
            {"NVDA": {"conviction": "high", "rationale": "x"}, "INTC": {"conviction": "avoid"}},
            summary="trim INTC, add NVDA")
        convs = optimizer.load_review_convictions()
        self.assertEqual(convs, {"NVDA": "high", "INTC": "avoid"})

    def test_pool_uses_review_conviction_for_held_name(self):
        model = {"targets": {}, "sleeves": {}}
        holdings = {"positions": [{"symbol": "QQQ", "base_market_value": 1000.0}]}
        # No basket, no pin, no band -> a held name would default to "medium"
        # (carry). A review conviction should override that.
        pool = {e["symbol"]: e for e in optimizer.build_pool(
            model=model, holdings=holdings, basket_items=[],
            review_convictions={"QQQ": "high"})}
        self.assertEqual(pool["QQQ"]["conviction"], "high")
        self.assertEqual(pool["QQQ"]["conviction_source"], "review")

    def test_pool_uses_review_conviction_for_unheld_basket_pick(self):
        # The point of reviewing basket picks: an unheld discovery must compete
        # on a researched conviction, not ride its tier default ("curious"->low).
        pool = {e["symbol"]: e for e in optimizer.build_pool(
            model={"targets": {}, "sleeves": {}}, holdings={"positions": []},
            basket_items=[{"symbol": "FIND", "tier": "curious", "sleeve": "other"}],
            review_convictions={"FIND": "high"})}
        self.assertEqual(pool["FIND"]["conviction"], "high")
        self.assertEqual(pool["FIND"]["conviction_source"], "review")


class ReviewSymbols(unittest.TestCase):
    """The review's scope is the whole candidate pool: held names first
    (heaviest leading), then basket picks, deduped, junk symbols dropped."""

    def setUp(self):
        import basket
        self._held = aj._held_symbols
        self._load = basket.load_basket
        self._basket_mod = basket

    def tearDown(self):
        aj._held_symbols = self._held
        self._basket_mod.load_basket = self._load

    def test_union_held_then_picks_deduped(self):
        aj._held_symbols = lambda: ["NVDA", "QQQ"]
        self._basket_mod.load_basket = lambda: {"items": [
            {"symbol": "QQQ"},          # already held -> deduped
            {"symbol": "FIND"},
            {"symbol": "bad sym!!"},    # junk -> dropped by safe_symbol
            {"symbol": None},
        ]}
        self.assertEqual(aj._review_symbols(), ["NVDA", "QQQ", "FIND"])

    def test_basket_only_still_reviews(self):
        aj._held_symbols = lambda: []
        self._basket_mod.load_basket = lambda: {"items": [{"symbol": "FIND"}]}
        self.assertEqual(aj._review_symbols(), ["FIND"])


class ParallelFanout(unittest.TestCase):
    """The batch must run analyses concurrently, honour the worker cap, and still
    collect a verdict for every held name. We stub the per-name analysis with a
    fake that measures peak concurrency."""

    def setUp(self):
        self._save = {
            "held": aj._held_symbols,
            "latest": aj.latest_analysis,
            "one": aj._analyze_one,
            "synth": aj._synthesize_portfolio,
            "save": optimizer.save_portfolio_review,
            "workers": aj.PORTFOLIO_REVIEW_WORKERS,
            "basket": aj.basket.load_basket,
        }
        self.captured: dict = {}
        self.lock = threading.Lock()
        self.cur = 0
        self.peak = 0
        self.syms = [f"SYM{i:02d}" for i in range(16)]

        def fake_one(sym, *, progress=None, cancel=None):
            with self.lock:
                self.cur += 1
                self.peak = max(self.peak, self.cur)
            time.sleep(0.05)
            with self.lock:
                self.cur -= 1
            return "Accumulate, high confidence"

        aj._held_symbols = lambda: list(self.syms)
        # The review scope is _held_symbols() PLUS the staged basket picks. Pin the
        # basket empty so the scope is exactly self.syms -- otherwise a dev box with
        # a real staged basket leaks its tickers into the fanout and this assertion
        # fails locally (while passing on a clean CI checkout with no basket).
        aj.basket.load_basket = lambda: {"items": []}
        aj.latest_analysis = lambda s: None       # force a fresh analysis
        aj._analyze_one = fake_one
        aj._synthesize_portfolio = lambda notes: ({k: {"conviction": "high"} for k in notes}, "")
        optimizer.save_portfolio_review = lambda convs, *, summary="": self.captured.update(
            {"convs": convs, "summary": summary})

    def tearDown(self):
        aj._held_symbols = self._save["held"]
        aj.latest_analysis = self._save["latest"]
        aj._analyze_one = self._save["one"]
        aj._synthesize_portfolio = self._save["synth"]
        optimizer.save_portfolio_review = self._save["save"]
        aj.PORTFOLIO_REVIEW_WORKERS = self._save["workers"]
        aj.basket.load_basket = self._save["basket"]

    def test_runs_in_parallel_under_cap(self):
        aj.PORTFOLIO_REVIEW_WORKERS = 8
        job = jobs.new_job("portfolio_review", refresh=True)
        aj.run_portfolio_review_job(job["id"], refresh=True)

        # Every name got a verdict; finished cleanly.
        self.assertEqual(set(self.captured["convs"]), set(self.syms))
        self.assertEqual(jobs.get_public(job["id"])["state"], "done")
        # Actually concurrent (peak > 1) but never above the cap.
        self.assertGreater(self.peak, 1)
        self.assertLessEqual(self.peak, 8)


if __name__ == "__main__":
    unittest.main()
