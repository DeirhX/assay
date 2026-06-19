"""Tests for the shared disk helpers (store.py): the identifier validators that
guard every file path the server builds from user input (segment slugs, ticker
symbols), atomic writes that survive concurrency, and corrupt-vs-missing read
semantics."""

from __future__ import annotations

import io
import json
import threading
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory

import _support  # noqa: F401  -- puts tools/ on sys.path
import store


class Slugify(unittest.TestCase):
    def test_normalizes_to_kebab(self):
        self.assertEqual(store.slugify("Fintech & Payments!"), "fintech-payments")
        self.assertEqual(store.slugify("  AI  Compute  "), "ai-compute")

    def test_rejects_empty_and_overlong(self):
        with self.assertRaises(ValueError):
            store.slugify("   ")
        with self.assertRaises(ValueError):
            store.slugify("x" * 65)


class SafeSymbol(unittest.TestCase):
    def test_uppercases_and_accepts_valid(self):
        self.assertEqual(store.safe_symbol("amd"), "AMD")
        self.assertEqual(store.safe_symbol("brk.b"), "BRK.B")
        self.assertEqual(store.safe_symbol("eur=x"), "EUR=X")
        # Broker feeds (IBKR) use spaces for class shares, e.g. "BRK B" before
        # alias resolution to "BRK-B"; safe_symbol must accept them.
        self.assertEqual(store.safe_symbol("brk b"), "BRK B")

    def test_rejects_garbage(self):
        # Path separators, traversal, control punctuation, and over-long input
        # stay rejected (spaces are intentionally allowed -- see above).
        for bad in ["", "a/b", "a\\b", "a:b", "../etc", "x" * 17, "a;b"]:
            with self.assertRaises(ValueError):
                store.safe_symbol(bad)


class WriteJson(unittest.TestCase):
    def test_round_trip(self):
        with TemporaryDirectory() as d:
            p = Path(d) / "a.json"
            store.write_json(p, {"b": 1, "a": 2})
            self.assertEqual(store.load(p), {"a": 2, "b": 1})

    def test_creates_parent_dirs(self):
        with TemporaryDirectory() as d:
            p = Path(d) / "nested" / "deep" / "x.json"
            store.write_json(p, [1, 2, 3])
            self.assertEqual(store.load(p), [1, 2, 3])

    def test_no_temp_files_left_behind(self):
        with TemporaryDirectory() as d:
            p = Path(d) / "a.json"
            store.write_json(p, {"k": "v"})
            leftovers = [x.name for x in Path(d).iterdir() if ".tmp-" in x.name]
            self.assertEqual(leftovers, [])

    def test_concurrent_writes_never_corrupt(self):
        # Eight threads hammer the same file; with the lock + atomic replace the
        # final file must always be complete, valid JSON from exactly one writer
        # -- never a half-written or interleaved blob -- and no temp litter.
        with TemporaryDirectory() as d:
            p = Path(d) / "race.json"
            store.write_json(p, {"n": -1, "pad": []})
            errors: list[Exception] = []

            def writer(n: int) -> None:
                try:
                    for _ in range(50):
                        store.write_json(p, {"n": n, "pad": list(range(200))})
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

            threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(errors, [])
            data = json.loads(p.read_text(encoding="utf-8"))  # must parse cleanly
            self.assertIn(data["n"], range(8))
            self.assertEqual(data["pad"], list(range(200)))
            self.assertEqual([x for x in Path(d).iterdir() if ".tmp-" in x.name], [])


class Load(unittest.TestCase):
    def test_missing_returns_default_silently(self):
        with TemporaryDirectory() as d:
            p = Path(d) / "nope.json"
            buf = io.StringIO()
            with redirect_stderr(buf):
                self.assertEqual(store.load(p, default={}), {})
            self.assertEqual(buf.getvalue(), "")  # missing is normal, not noisy

    def test_corrupt_returns_default_but_warns(self):
        with TemporaryDirectory() as d:
            p = Path(d) / "bad.json"
            p.write_text("{not valid json", encoding="utf-8")
            buf = io.StringIO()
            with redirect_stderr(buf):
                self.assertEqual(store.load(p, default={"fallback": True}),
                                 {"fallback": True})
            self.assertIn("unreadable", buf.getvalue())

    def test_corrupt_strict_raises(self):
        with TemporaryDirectory() as d:
            p = Path(d) / "bad.json"
            p.write_text("{nope", encoding="utf-8")
            with self.assertRaises(json.JSONDecodeError):
                store.load(p, strict=True)


if __name__ == "__main__":
    unittest.main()
