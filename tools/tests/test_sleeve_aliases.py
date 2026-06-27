"""Tests for sleeve-label normalization: source labels map to canonical tags,
already-canonical tags are recognized as known (not flagged as one-offs), and
genuinely unknown labels are surfaced. Offline: aliases passed in directly."""

from __future__ import annotations

import unittest

import _support  # noqa: F401
import sleeve_aliases


class Normalize(unittest.TestCase):
    def test_source_label_maps_to_canonical(self):
        tag, known = sleeve_aliases.normalize("compute")
        self.assertEqual(tag, "semis-compute")
        self.assertTrue(known)

    def test_already_canonical_tag_is_known(self):
        # The model stores the canonical tag directly; it must not be reported
        # as an unmapped one-off just because it's a value, not a key.
        for tag in ("semis-compute", "semis-etf", "semis-equipment-litho",
                    "semis-foundry", "semis-materials", "analog"):
            with self.subTest(tag=tag):
                out, known = sleeve_aliases.normalize(tag)
                self.assertEqual(out, tag)
                self.assertTrue(known)

    def test_empty_is_known_noop(self):
        self.assertEqual(sleeve_aliases.normalize(""), ("", True))
        self.assertEqual(sleeve_aliases.normalize("   "), ("", True))

    def test_genuinely_unknown_is_flagged(self):
        tag, known = sleeve_aliases.normalize("totally-made-up-sleeve")
        self.assertEqual(tag, "totally-made-up-sleeve")
        self.assertFalse(known)

    def test_case_insensitive(self):
        self.assertEqual(sleeve_aliases.normalize("COMPUTE"), ("semis-compute", True))
        self.assertEqual(sleeve_aliases.normalize("Semis-ETF"), ("semis-etf", True))

    def test_custom_alias_value_recognized(self):
        aliases = {"foo": "my-canonical-sleeve"}
        self.assertEqual(sleeve_aliases.normalize("foo", aliases=aliases),
                         ("my-canonical-sleeve", True))
        self.assertEqual(sleeve_aliases.normalize("my-canonical-sleeve", aliases=aliases),
                         ("my-canonical-sleeve", True))


if __name__ == "__main__":
    unittest.main()
