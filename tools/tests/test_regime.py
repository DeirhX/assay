"""Tests for the regime strip: pure trend derivation and snapshot -> strip
shaping. No network (build_strip is pure; build_regime's fetch is injected)."""

from __future__ import annotations

import unittest

import _support  # noqa: F401
import regime


def _node(value, change_1y, *, display=None, change_display=None):
    return {
        "value": value,
        "display": display if display is not None else f"{value:.2f}",
        "as_of": "2026-06-13",
        "change_1y": change_1y,
        "change_1y_display": change_display or f"{change_1y:+.2f}",
        "url": "https://fred.stlouisfed.org/series/X",
    }


def _snapshot(**overrides):
    series = {
        "DGS10": _node(4.3, 0.5),
        "BAMLH0A0HYM2": _node(3.1, -0.4),
        "DTWEXBGS": _node(120.0, 0.0),
        "VIXCLS": _node(15.0, -2.0),
    }
    series.update(overrides)
    return {"source": "fred", "as_of": "2026-06-13T00:00:00", "series": series, "errors": []}


class Trend(unittest.TestCase):
    def test_directions(self):
        self.assertEqual(regime._trend(0.5), "up")
        self.assertEqual(regime._trend(-0.5), "down")
        self.assertEqual(regime._trend(0.0), "flat")

    def test_missing_change_is_flat(self):
        self.assertEqual(regime._trend(None), "flat")
        self.assertEqual(regime._trend("nope"), "flat")


class BuildStrip(unittest.TestCase):
    def test_full_snapshot_yields_panel_in_order(self):
        strip = regime.build_strip(_snapshot())
        self.assertEqual([c["id"] for c in strip],
                         [s["id"] for s in regime.PANEL])
        by_id = {c["id"]: c for c in strip}
        self.assertEqual(by_id["DGS10"]["trend"], "up")
        self.assertEqual(by_id["BAMLH0A0HYM2"]["trend"], "down")
        self.assertEqual(by_id["DTWEXBGS"]["trend"], "flat")
        self.assertEqual(by_id["DGS10"]["label"], "Rates")

    def test_missing_series_are_skipped_not_faked(self):
        snap = _snapshot()
        del snap["series"]["VIXCLS"]
        strip = regime.build_strip(snap)
        self.assertNotIn("VIXCLS", {c["id"] for c in strip})
        self.assertEqual(len(strip), len(regime.PANEL) - 1)

    def test_empty_snapshot_is_empty_strip(self):
        self.assertEqual(regime.build_strip({}), [])
        self.assertEqual(regime.build_strip({"series": {}}), [])


class BuildRegime(unittest.TestCase):
    def test_uses_injected_fetch_and_shapes_payload(self):
        captured = {}

        def fake_fetch(ids):
            captured["ids"] = list(ids)
            return _snapshot()

        orig_write = regime.store.write_json
        regime.store.write_json = lambda *a, **k: None   # don't touch the real cache
        try:
            out = regime.build_regime(fetch=fake_fetch, use_cache=False)
        finally:
            regime.store.write_json = orig_write
        self.assertEqual(captured["ids"], [s["id"] for s in regime.PANEL])
        self.assertTrue(out["strip"])
        self.assertEqual(out["caption"], regime.CAPTION)
        self.assertFalse(out["cached"])


if __name__ == "__main__":
    unittest.main()
