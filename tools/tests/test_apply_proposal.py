"""Tests for the shared in-place model mutator in target_model.py
(``_apply_changes_to_model``): the modify_target merge (which used to be skipped
as "unsupported action"), the add_target/blocked/sleeve branches, schema-key
stripping, and unsupported-action recording. This is the primitive the staging
layer composes; promotion to the live model is covered by test_target_staging.
Offline and pure -- no disk paths or site regeneration involved."""

from __future__ import annotations

import unittest

import _support  # noqa: F401
import target_model


class ApplyChangesToModel(unittest.TestCase):
    def test_modify_target_merges_and_preserves_untouched_keys(self):
        model = {"targets": {"AAA": {"low": 1, "high": 3, "rule": "hold", "note": "keep me"}}}
        changes = [{"action": "modify_target", "symbol": "AAA",
                    "proposed_target": {"low": 5, "high": 8, "rule": "accumulate"}}]
        applied, skipped = target_model._apply_changes_to_model(model, changes, blocked=set())
        self.assertEqual(applied, ["AAA"])
        self.assertEqual(skipped, [])
        # New band/rule applied; the pre-existing note survives the merge.
        self.assertEqual(model["targets"]["AAA"],
                         {"low": 5, "high": 8, "rule": "accumulate", "note": "keep me"})

    def test_modify_target_strips_non_schema_keys(self):
        # `conviction` (and any other non-schema metadata) is stripped, but
        # `sleeve` is now a write key: a normalized allocation-sleeve tag is
        # allowed to persist onto the band (see sleeve_aliases / staging).
        model = {"targets": {"AAA": {"low": 1, "high": 3, "rule": "hold"}}}
        changes = [{"action": "modify_target", "symbol": "AAA",
                    "proposed_target": {"low": 4, "high": 6, "rule": "hold",
                                        "conviction": "high", "sleeve": "semis-compute"}}]
        target_model._apply_changes_to_model(model, changes, blocked=set())
        self.assertNotIn("conviction", model["targets"]["AAA"])
        self.assertEqual(model["targets"]["AAA"]["sleeve"], "semis-compute")

    def test_add_target_new_and_existing(self):
        model = {"targets": {"AAA": {"low": 1, "high": 3, "rule": "hold"}}}
        changes = [
            {"action": "add_target", "symbol": "BBB", "proposed_target": {"low": 2, "high": 4, "rule": "hold"}},
            {"action": "add_target", "symbol": "AAA", "proposed_target": {"low": 9, "high": 9, "rule": "hold"}},
        ]
        applied, skipped = target_model._apply_changes_to_model(model, changes, blocked=set())
        self.assertEqual(applied, ["BBB"])
        self.assertEqual(skipped[0]["symbol"], "AAA")
        self.assertIn("already exists", skipped[0]["reason"])

    def test_blocked_symbol_is_skipped(self):
        model = {"targets": {}}
        changes = [{"action": "add_target", "symbol": "AAA",
                    "proposed_target": {"low": 1, "high": 2, "rule": "hold"}}]
        applied, skipped = target_model._apply_changes_to_model(model, changes, blocked={"AAA"})
        self.assertEqual(applied, [])
        self.assertIn("blocked", skipped[0]["reason"])

    def test_sleeve_upsert(self):
        model = {"sleeves": {}}
        changes = [{"action": "set_sleeve", "sleeve": "fintech",
                    "proposed_sleeve": {"low": 10, "high": 20, "rule": "accumulate"}}]
        applied, _ = target_model._apply_changes_to_model(model, changes, blocked=set())
        self.assertEqual(applied, ["[fintech]"])
        self.assertEqual(model["sleeves"]["fintech"]["high"], 20)

    def test_unknown_action_is_recorded_not_dropped(self):
        model = {"targets": {}}
        changes = [{"action": "delete_everything", "symbol": "AAA"}]
        applied, skipped = target_model._apply_changes_to_model(model, changes, blocked=set())
        self.assertEqual(applied, [])
        self.assertIn("unsupported action", skipped[0]["reason"])


if __name__ == "__main__":
    unittest.main()
