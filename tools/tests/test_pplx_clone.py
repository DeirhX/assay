"""Tests for the per-run profile clone that lets several Perplexity browser runs
execute at once. Filesystem-only: pplx_deep_research imports Playwright lazily
inside its run functions, so importing the module here needs nothing installed.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _support  # noqa: F401
import pplx_deep_research as pdr


def _make_base(root: Path) -> Path:
    """A miniature Chrome user-data-dir: session-bearing files plus fat caches."""
    base = root / "base"
    (base / "Default" / "Network").mkdir(parents=True)
    (base / "Default" / "Cache").mkdir(parents=True)
    (base / "Cache").mkdir(parents=True)
    (base / "Local State").write_text("{}", encoding="utf-8")
    (base / "Default" / "Network" / "Cookies").write_text("cookie", encoding="utf-8")
    (base / "Default" / "Preferences").write_text("{}", encoding="utf-8")
    (base / "Cache" / "blob.bin").write_text("x" * 1000, encoding="utf-8")
    (base / "Default" / "Cache" / "data.bin").write_text("y" * 1000, encoding="utf-8")
    return base


class CloneBaseProfile(unittest.TestCase):
    def test_copies_session_files_and_skips_caches(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = _make_base(Path(tmp))
            clone = pdr.clone_base_profile(base)
            try:
                self.assertNotEqual(clone.resolve(), base.resolve())
                # Session-bearing files survive.
                self.assertTrue((clone / "Local State").exists())
                self.assertTrue((clone / "Default" / "Network" / "Cookies").exists())
                self.assertTrue((clone / "Default" / "Preferences").exists())
                # Cache subtrees (at any depth) are skipped.
                self.assertFalse((clone / "Cache").exists())
                self.assertFalse((clone / "Default" / "Cache").exists())
            finally:
                pdr.cleanup_clone(clone)

    def test_cleanup_removes_the_clone_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = _make_base(Path(tmp))
            clone = pdr.clone_base_profile(base)
            self.assertTrue(clone.exists())
            pdr.cleanup_clone(clone)
            self.assertFalse(clone.exists())
            self.assertFalse(clone.parent.exists())

    def test_missing_base_yields_empty_clone(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "does-not-exist"
            clone = pdr.clone_base_profile(base)
            try:
                self.assertTrue(clone.is_dir())
                self.assertEqual(list(clone.iterdir()), [])
            finally:
                pdr.cleanup_clone(clone)

    def test_cleanup_tolerates_none(self):
        pdr.cleanup_clone(None)  # must not raise

    def test_forget_profile_removes_saved_login_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = _make_base(Path(tmp))
            pdr.forget_profile(base)
            self.assertFalse(base.exists())

    def test_forget_profile_refuses_home_directory(self):
        with self.assertRaisesRegex(ValueError, "unsafe profile path"):
            pdr.forget_profile(Path.home())

    def test_missing_deep_research_menu_means_no_entitlement(self):
        page = mock.Mock()
        search = mock.Mock()
        search.count.return_value = 1
        menu = mock.Mock()
        menu.click.side_effect = RuntimeError("missing")
        page.get_by_role.side_effect = lambda role, **kw: search if role == "button" else menu
        page.evaluate.return_value = "not found"
        self.assertFalse(pdr._deep_research_access(page))


if __name__ == "__main__":
    unittest.main()
