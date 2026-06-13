"""Tests for the FRED macro provider (HTTP mocked, no network touched)."""

from __future__ import annotations

import unittest
from unittest import mock

import _support  # noqa: F401  (sys.path side effect)
from providers import fred


def _csv(series_id: str, rows: list[tuple[str, str]]) -> bytes:
    lines = [f"observation_date,{series_id}"]
    lines += [f"{d},{v}" for d, v in rows]
    return ("\n".join(lines) + "\n").encode("utf-8")


class FredParsing(unittest.TestCase):
    def test_fetch_rows_skips_missing_and_dots(self):
        payload = _csv("DGS10", [
            ("2025-06-10", "4.20"),
            ("2025-06-11", "."),
            ("2025-06-12", ""),
            ("2025-06-13", "4.31"),
        ])
        with mock.patch.object(fred, "http_get", return_value=payload):
            rows = fred._fetch_rows("DGS10")
        self.assertEqual(rows, [("2025-06-10", 4.20), ("2025-06-13", 4.31)])

    def test_unexpected_header_raises(self):
        with mock.patch.object(fred, "http_get", return_value=b"observation_date,WRONG\n2025-06-13,1\n"):
            with self.assertRaises(fred.ProviderError):
                fred._fetch_rows("DGS10")

    def test_series_snapshot_pct_change(self):
        spec = {"id": "DGS10", "label": "10Y", "unit": "%", "category": "rates", "note": "n"}
        rows = [("2024-06-13", "4.00"), ("2025-06-13", "4.50")]
        with mock.patch.object(fred, "http_get", return_value=_csv("DGS10", rows)):
            node = fred._series_snapshot(spec)
        self.assertEqual(node["value"], 4.5)
        self.assertEqual(node["display"], "4.50%")
        self.assertEqual(node["change_1y"], 0.5)
        self.assertEqual(node["change_1y_display"], "+0.50 pp")

    def test_index_unit_uses_yoy_pct(self):
        spec = {"id": "CPIAUCSL", "label": "CPI", "unit": "index", "category": "inflation", "note": "n"}
        rows = [("2024-06-13", "100.0"), ("2025-06-13", "103.0")]
        with mock.patch.object(fred, "http_get", return_value=_csv("CPIAUCSL", rows)):
            node = fred._series_snapshot(spec)
        self.assertEqual(node["change_1y_pct"], 3.0)
        self.assertEqual(node["change_1y_display"], "+3.0% YoY")


class FredHelpers(unittest.TestCase):
    def test_display(self):
        self.assertEqual(fred._display(4.2, "%"), "4.20%")
        self.assertEqual(fred._display(1.5, "pp"), "1.50pp")
        self.assertEqual(fred._display(100.0, "index"), "100.00")

    def test_prior_year_value_picks_nearest_before_cutoff(self):
        rows = [("2024-01-01", 1.0), ("2024-06-13", 2.0), ("2025-06-13", 3.0)]
        self.assertEqual(fred._prior_year_value(rows, "2025-06-13"), 2.0)

    def test_prior_year_value_none_when_no_history(self):
        rows = [("2025-06-13", 3.0)]
        self.assertIsNone(fred._prior_year_value(rows, "2025-06-13"))

    def test_series_catalog_shape(self):
        self.assertTrue(all({"id", "label", "unit", "category", "note"} <= set(s) for s in fred.SERIES))


if __name__ == "__main__":
    unittest.main()
