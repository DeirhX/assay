"""The deep-research worker must honor a cancel predicate *before* launching a
browser, so a cancelled job tears down promptly and frees its concurrency slot.

These exercise the pre-launch seam only -- it returns before the lazy Playwright
import, so they run without Playwright installed. The in-loop checks need a live
browser and aren't unit-tested here."""

from __future__ import annotations

import unittest

import _support  # noqa: F401  -- puts tools/ on sys.path
import pplx_deep_research as pplx


class EarlyCancel(unittest.TestCase):
    def test_run_deep_research_cancels_before_launch(self):
        res = pplx.run_deep_research("research nuclear supply chain",
                                     cancel=lambda: True)
        self.assertEqual(res, {"status": "cancelled"})

    def test_fetch_by_url_cancels_before_launch(self):
        res = pplx.fetch_by_url("https://www.perplexity.ai/search/abc123",
                                cancel=lambda: True)
        self.assertEqual(res, {"status": "cancelled"})

    def test_ensure_login_cancels_before_launch(self):
        res = pplx.ensure_login(cancel=lambda: True)
        self.assertEqual(res, {"status": "cancelled"})

    def test_default_is_not_cancelled(self):
        # The default predicate never cancels; an empty prompt still short-circuits
        # with its own error (proving the cancel seam didn't swallow that path).
        self.assertEqual(pplx.run_deep_research(""),
                         {"status": "error", "detail": "empty prompt"})
        self.assertFalse(pplx._never())


if __name__ == "__main__":
    unittest.main()
