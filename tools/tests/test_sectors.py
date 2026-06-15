"""Tests for the symbol->sector resolver (pure logic; network is injected)."""

import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import sectors as S


class Normalize(unittest.TestCase):
    def test_uppercases_and_strips_option_residue(self):
        self.assertEqual(S.normalize("amd   240816P00135000"), "AMD")
        self.assertEqual(S.normalize("  aapl "), "AAPL")
        self.assertEqual(S.normalize(""), "")
        self.assertEqual(S.normalize(None), "")


class SeedFromResearch(unittest.TestCase):
    def _research(self, tmp, recs):
        d = Path(tmp) / "research"
        d.mkdir()
        for sym, profile in recs.items():
            (d / f"{sym}.json").write_text(json.dumps({"profile": profile}), encoding="utf-8")
        return d

    def test_seeds_only_records_with_a_sector(self):
        with TemporaryDirectory() as tmp:
            d = self._research(tmp, {
                "ADI": {"sector": "Technology"},
                "NOSEC": {"summary": "no sector here"},
            })
            cache = {"map": {}}
            n = S.seed_from_research(cache, d)
            self.assertEqual(n, 1)
            self.assertEqual(cache["map"]["ADI"], {"sector": "Technology", "source": "research",
                                                   "ts": cache["map"]["ADI"]["ts"]})
            self.assertNotIn("NOSEC", cache["map"])

    def test_is_idempotent(self):
        with TemporaryDirectory() as tmp:
            d = self._research(tmp, {"ADI": {"sector": "Technology"}})
            cache = {"map": {}}
            S.seed_from_research(cache, d)
            self.assertEqual(S.seed_from_research(cache, d), 0)  # no churn second time

    def test_research_overrides_a_stale_yahoo_entry(self):
        with TemporaryDirectory() as tmp:
            d = self._research(tmp, {"ADI": {"sector": "Technology"}})
            cache = {"map": {"ADI": {"sector": "Industrials", "source": "yahoo", "ts": "x"}}}
            S.seed_from_research(cache, d)
            self.assertEqual(cache["map"]["ADI"]["source"], "research")
            self.assertEqual(cache["map"]["ADI"]["sector"], "Technology")


class SectorOf(unittest.TestCase):
    cache = {"map": {
        "AMD": {"sector": "Technology", "source": "research"},
        "FOO": {"sector": "Energy", "source": "yahoo"},
        "BAR": {"sector": "", "source": "unresolved"},
    }}

    def test_resolves_via_exact_and_option_residue(self):
        self.assertEqual(S.sector_of("AMD", self.cache), "Technology")
        self.assertEqual(S.sector_of("AMD  240816P00135000", self.cache), "Technology")

    def test_falls_back_to_base_before_exchange_suffix(self):
        self.assertEqual(S.sector_of("FOO.DE", self.cache), "Energy")

    def test_unknown_and_unresolved_return_empty(self):
        self.assertEqual(S.sector_of("BAR", self.cache), "")
        self.assertEqual(S.sector_of("ZZZ", self.cache), "")


class NeedsLookup(unittest.TestCase):
    def test_returns_unseen_and_stale_unresolved_only(self):
        now = datetime(2026, 1, 20, tzinfo=timezone.utc)
        fresh = (now - timedelta(days=2)).isoformat()
        stale = (now - timedelta(days=30)).isoformat()
        cache = {"map": {
            "AMD": {"sector": "Technology", "source": "research"},
            "FRESHBAD": {"sector": "", "source": "unresolved", "ts": fresh},
            "STALEBAD": {"sector": "", "source": "unresolved", "ts": stale},
        }}
        out = S.needs_lookup(["AMD", "FRESHBAD", "STALEBAD", "NEW", "new", ""], cache, now=now)
        self.assertEqual(out, ["STALEBAD", "NEW"])  # resolved + fresh-bad skipped; dedup + blanks dropped


class Backfill(unittest.TestCase):
    def test_records_resolved_and_unresolved_and_respects_limit(self):
        cache = {"map": {}}
        calls = []

        def fetch(sym):
            calls.append(sym)
            return {"AAA": "Technology", "BBB": None}.get(sym)

        stats = S.backfill(["AAA", "BBB", "CCC"], cache, fetch=fetch, limit=2)
        self.assertEqual(calls, ["AAA", "BBB"])  # CCC deferred by the limit
        self.assertEqual(stats, {"considered": 3, "fetched": 2, "resolved": 1, "unresolved": 1})
        self.assertEqual(cache["map"]["AAA"]["sector"], "Technology")
        self.assertEqual(cache["map"]["BBB"]["source"], "unresolved")
        self.assertNotIn("CCC", cache["map"])

    def test_a_raising_fetch_is_recorded_unresolved_not_fatal(self):
        cache = {"map": {}}

        def boom(sym):
            raise RuntimeError("yahoo said no")

        stats = S.backfill(["AAA"], cache, fetch=boom)
        self.assertEqual(stats["unresolved"], 1)
        self.assertEqual(cache["map"]["AAA"]["source"], "unresolved")


class CacheIO(unittest.TestCase):
    def test_roundtrip_and_corrupt_file_is_empty(self):
        with TemporaryDirectory() as tmp:
            p = Path(tmp) / "cache" / "sectors.json"
            S.save_cache(p, {"map": {"AMD": {"sector": "Technology", "source": "research"}}})
            loaded = S.load_cache(p)
            self.assertEqual(loaded["map"]["AMD"]["sector"], "Technology")
            self.assertIsNotNone(loaded["updated_at"])
            p.write_text("{not json", encoding="utf-8")
            self.assertEqual(S.load_cache(p), {"updated_at": None, "map": {}})

    def test_missing_file_is_empty(self):
        self.assertEqual(S.load_cache(Path("/no/such/sectors.json")),
                         {"updated_at": None, "map": {}})


if __name__ == "__main__":
    unittest.main()