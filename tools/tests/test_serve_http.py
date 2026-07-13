"""Tests for serve.py's HTTP request guards: malformed JSON bodies must be a
400 (not a silent {} or a 500), oversized bodies are refused, and main()
refuses to bind a non-loopback host. Runs a real ThreadingHTTPServer on an
ephemeral loopback port -- offline, no data submodule needed."""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import _support  # noqa: F401
import deep_runs
import holdings_sync
import peer_stats
import segments_service
import serve
import ticker_directory
from _http import ServeHttpCase


class RequestGuards(ServeHttpCase):
    def test_malformed_json_is_400(self):
        status, payload = self.post_json("/api/deep-job/cancel", b"{not json at all")
        self.assertEqual(status, 400)
        self.assertIn("malformed JSON", payload["error"])

    def test_non_object_json_is_400(self):
        status, payload = self.post_json("/api/deep-job/cancel", b'["a", "list"]')
        self.assertEqual(status, 400)
        self.assertIn("must be an object", payload["error"])

    def test_oversized_body_is_400(self):
        # Lie about the size in the header; the guard must fire before reading.
        status, payload = self.post_json(
            "/api/deep-job/cancel", b"{}",
            headers={"Content-Length": str(serve._MAX_BODY_BYTES + 1)},
        )
        self.assertEqual(status, 400)
        self.assertIn("too large", payload["error"])

    def test_valid_body_still_works(self):
        status, payload = self.post_json("/api/deep-job/cancel", b'{"id": ""}')
        self.assertEqual(status, 400)  # empty id is rejected by the endpoint...
        self.assertIn("missing job id", payload["error"])  # ...not by the body guard


class HoldingsSyncJob(unittest.TestCase):
    """The IBKR sync runs as a registered background job, not a blocking request.
    The underlying Flex pull (_sync_holdings) is mocked so these stay offline."""

    def _wait(self, job_id, *, timeout=4.0, state=None):
        deadline = time.time() + timeout
        terminal = (state,) if state else ("done", "error", "cancelled")
        while time.time() < deadline:
            pub = serve.jobs.get_public(job_id)
            if pub and pub["state"] in terminal:
                return pub
            time.sleep(0.02)
        self.fail(f"job {job_id} never reached {terminal}")

    def test_sync_runs_as_registered_job_and_carries_result(self):
        def fake_sync(progress=None):
            if progress:
                progress("working…")
            return {"site": {"ok": True, "written": []}, "generated_at": "2026-06-13T00:00:00+00:00"}

        with mock.patch.object(holdings_sync, "_sync_holdings", side_effect=fake_sync):
            job = serve._start_holdings_sync()
            self.assertEqual(job["kind"], "ibkr_sync")
            pub = self._wait(job["id"])
        self.assertEqual(pub["state"], "done")
        self.assertTrue(pub["result"]["site"]["ok"])

    def test_only_one_sync_at_a_time(self):
        release = threading.Event()

        def blocker(progress=None):
            release.wait(timeout=5)
            return {"site": None, "generated_at": None}

        with mock.patch.object(holdings_sync, "_sync_holdings", side_effect=blocker):
            job = serve._start_holdings_sync()
            self._wait(job["id"], state="running")
            with self.assertRaises(RuntimeError):
                serve._start_holdings_sync()   # second sync is refused while one runs
            release.set()
            self._wait(job["id"])

    def test_sync_failure_becomes_error_state(self):
        with mock.patch.object(holdings_sync, "_sync_holdings",
                               side_effect=ValueError("IBKR credentials not configured")):
            job = serve._start_holdings_sync()
            pub = self._wait(job["id"])
        self.assertEqual(pub["state"], "error")
        self.assertIn("credentials", pub["error"])


class JobsListEndpoint(ServeHttpCase):
    """GET /api/jobs is the central Task Center feed: every in-memory job, newest
    first, capped to JOBS_LIST_LIMIT. The in-process registry is shared, so these
    tests pin their own jobs into the future to assert ordering/cap deterministically."""

    def test_feed_returns_started_job_with_routing_fields(self):
        def fake_sync(progress=None):
            return {"site": {"ok": True, "written": []}, "generated_at": "2026-06-13T00:00:00+00:00"}

        with mock.patch.object(holdings_sync, "_sync_holdings", side_effect=fake_sync):
            job = serve._start_holdings_sync()
            # wait for it to settle so the result is attached
            deadline = time.time() + 4
            while time.time() < deadline and (serve.jobs.get_public(job["id"]) or {}).get("state") != "done":
                time.sleep(0.02)

        status, payload = self.get_json("/api/jobs")
        self.assertEqual(status, 200)
        self.assertIn("jobs", payload)
        self.assertIsInstance(payload["jobs"], list)
        mine = next((j for j in payload["jobs"] if j["id"] == job["id"]), None)
        self.assertIsNotNone(mine, "started job missing from /api/jobs feed")
        self.assertEqual(mine["kind"], "ibkr_sync")
        self.assertEqual(mine["state"], "done")
        self.assertIn("created_at", mine)
        # routing identifiers exposed for the Task Center navigation map
        for key in ("symbol", "stem", "run_id"):
            self.assertIn(key, mine)

    def test_newest_first_ordering(self):
        older = serve.jobs.new_job("test_order", created_at="2099-01-01T00:00:01+00:00")
        newer = serve.jobs.new_job("test_order", created_at="2099-01-01T00:00:02+00:00")
        _, payload = self.get_json("/api/jobs")
        ids = [j["id"] for j in payload["jobs"]]
        self.assertIn(newer["id"], ids)
        self.assertIn(older["id"], ids)
        self.assertLess(ids.index(newer["id"]), ids.index(older["id"]))

    def test_feed_is_capped_to_limit(self):
        # Pin three jobs into the far future so they are guaranteed the newest,
        # then shrink the cap to 2 and prove only the two newest survive.
        j1 = serve.jobs.new_job("test_cap", created_at="2099-02-01T00:00:01+00:00")
        j2 = serve.jobs.new_job("test_cap", created_at="2099-02-01T00:00:02+00:00")
        j3 = serve.jobs.new_job("test_cap", created_at="2099-02-01T00:00:03+00:00")
        with mock.patch.object(serve, "JOBS_LIST_LIMIT", 2):
            _, payload = self.get_json("/api/jobs")
        ids = [j["id"] for j in payload["jobs"]]
        self.assertEqual(len(ids), 2)
        self.assertEqual(ids, [j3["id"], j2["id"]])
        self.assertNotIn(j1["id"], ids)


class DeepArtifactJsonGuard(unittest.TestCase):
    """A Deep Research report is narrative markdown. A bad scrape/paste once
    stored a JSON segment-universe blob as the `.md`, which the Analyses view
    then rendered raw. save_deep_artifact must refuse a JSON-document body."""

    def test_detects_bare_json_object(self):
        self.assertTrue(deep_runs._looks_like_json_doc('{"title": "Space", "members": []}'))

    def test_detects_bare_json_array(self):
        self.assertTrue(deep_runs._looks_like_json_doc('[{"symbol": "RKLB"}]'))

    def test_detects_fenced_json(self):
        self.assertTrue(deep_runs._looks_like_json_doc('```json\n{"a": 1}\n```'))

    def test_allows_markdown_narrative(self):
        report = ("# Space Exploration\n\nRocket Lab ($RKLB) is the clearest "
                  "pure-play launch name.\n\n| Company | Ticker |\n|---|---|\n"
                  "| Rocket Lab | RKLB |\n")
        self.assertFalse(deep_runs._looks_like_json_doc(report))

    def test_allows_prose_starting_with_brace_like_text(self):
        # Brace-led but not valid JSON -> still a narrative, must be allowed.
        self.assertFalse(deep_runs._looks_like_json_doc("{this is not json, just prose}"))

    def test_save_rejects_json_report(self):
        with self.assertRaises(ValueError) as ctx:
            deep_runs.save_deep_artifact({
                "segment": "space-exploration",
                "date": "2026-06-13",
                "report": '{"title": "Space", "sleeves": [], "members": []}',
            })
        self.assertIn("JSON document", str(ctx.exception))


class RouteRegistry(unittest.TestCase):
    """The declarative GET/POST route tables drive dispatch via getattr, so the
    safety net for that refactor is: every table entry maps to a real Handler
    method, and prefix tables stay sorted longest-first (the invariant the
    longest-match-wins dispatcher relies on)."""

    def _all_handler_names(self):
        return (
            list(serve._GET_EXACT.values())
            + [name for _, name in serve._GET_PREFIX]
            + list(serve._POST_EXACT.values())
            + [name for _, name in serve._POST_PREFIX]
        )

    def test_every_route_resolves_to_a_real_handler(self):
        for name in self._all_handler_names():
            handler = getattr(serve.Handler, name, None)
            self.assertTrue(callable(handler), f"missing/uncallable handler {name}")

    def test_no_duplicate_handler_names(self):
        names = self._all_handler_names()
        dupes = {n for n in names if names.count(n) > 1}
        self.assertEqual(dupes, set(), f"a handler is wired to more than one route: {dupes}")

    def test_prefix_tables_are_longest_first(self):
        for table in (serve._GET_PREFIX, serve._POST_PREFIX):
            lengths = [len(prefix) for prefix, _ in table]
            self.assertEqual(lengths, sorted(lengths, reverse=True))

    def test_overlapping_prefix_resolves_to_most_specific(self):
        # /api/pull-segment/x must not be swallowed by /api/pull/.
        resolved = None
        for prefix, name in serve._POST_PREFIX:
            if "/api/pull-segment/foo".startswith(prefix):
                resolved = name
                break
        self.assertEqual(resolved, "_post_pull_segment")


class DeepQa(ServeHttpCase):
    """Follow-up Q&A about a saved Deep Research run: GET returns an (empty)
    thread for an unknown stem, and starting a question for a run with no saved
    report is a clean 400, not a 500 or a started job."""

    def test_empty_thread_for_unknown_stem(self):
        status, payload = self.get_json("/api/deep-qa?stem=does-not-exist-2026-01-01")
        self.assertEqual(status, 200)
        self.assertEqual(payload["turns"], [])

    def test_question_without_report_is_400(self):
        status, payload = self.request(
            "/api/deep-qa", method="POST",
            body={"stem": "no-such-run-2026-01-01", "question": "why?"})
        self.assertEqual(status, 400)
        self.assertIn("no saved report", payload["error"])

    def test_delete_on_unknown_stem_is_noop_200(self):
        status, payload = self.request(
            "/api/deep-qa", method="POST",
            body={"stem": "does-not-exist-2026-01-01", "delete": 0})
        self.assertEqual(status, 200)
        self.assertEqual(payload["turns"], [])


class DeepRunDelete(ServeHttpCase):
    """Deleting a saved Deep Research run removes the report plus every sidecar
    (sources, review, proposal, Q&A) for that stem, returns the refreshed run
    list, and 400s on an unknown/empty stem. DEEP_DIR is redirected to a temp
    dir so the real data/ tree is untouched."""

    def setUp(self):
        # DEEP_DIR must sit under REPO_ROOT because deep_runs() reports each run
        # path relative to REPO_ROOT; redirect both to a temp tree so the real
        # data/ dir is untouched and relative_to() still resolves. The delete
        # handler resolves these from deep_runs (where the function now lives).
        self._dir = tempfile.TemporaryDirectory()
        self._orig_deep = deep_runs.DEEP_DIR
        self._orig_root = deep_runs.REPO_ROOT
        deep_runs.REPO_ROOT = Path(self._dir.name)
        deep_runs.DEEP_DIR = deep_runs.REPO_ROOT / "data" / "research" / "deep"
        deep_runs.DEEP_DIR.mkdir(parents=True)

    def tearDown(self):
        deep_runs.DEEP_DIR = self._orig_deep
        deep_runs.REPO_ROOT = self._orig_root
        self._dir.cleanup()

    def _seed(self, stem, suffixes):
        for suffix in suffixes:
            (deep_runs.DEEP_DIR / f"{stem}{suffix}").write_text("x", encoding="utf-8")

    def test_delete_removes_all_artifacts(self):
        stem = "demo-segment-2026-01-02"
        self._seed(stem, [".md", ".sources.json", ".review.md",
                          ".target-proposal.json", ".qa.json"])
        # An unrelated run must survive the delete.
        self._seed("other-segment-2026-01-02", [".md", ".sources.json"])

        status, payload = self.post_json("/api/deep-run/delete", {"stem": stem})
        self.assertEqual(status, 200)
        self.assertEqual(payload["stem"], stem)
        self.assertEqual(
            sorted(payload["removed"]),
            sorted([f"{stem}{s}" for s in deep_runs._DEEP_RUN_SUFFIXES]))
        self.assertEqual(list(deep_runs.DEEP_DIR.glob(f"{stem}*")), [])
        self.assertTrue((deep_runs.DEEP_DIR / "other-segment-2026-01-02.md").exists())

    def test_delete_unknown_stem_is_400(self):
        status, payload = self.post_json(
            "/api/deep-run/delete", {"stem": "nope-2026-01-01"})
        self.assertEqual(status, 400)
        self.assertIn("unknown run", payload["error"])

    def test_delete_empty_stem_is_400(self):
        status, payload = self.post_json("/api/deep-run/delete", {"stem": ""})
        self.assertEqual(status, 400)
        self.assertIn("stem is required", payload["error"])


class DropQaExchange(unittest.TestCase):
    """The pure exchange-trimming helper: removes a question + its answer by the
    question's array index, ignores bad targets, and drops the resumable session
    so the next turn reseeds from the trimmed history."""

    def _thread(self):
        return {
            "session": "abc",
            "turns": [
                {"role": "user", "text": "q1"},
                {"role": "assistant", "text": "a1"},
                {"role": "user", "text": "q2"},
                {"role": "assistant", "text": "a2"},
            ],
        }

    def test_deletes_pair_and_resets_session(self):
        t = self._thread()
        self.assertTrue(serve._drop_qa_exchange(t, 0))
        self.assertEqual([x["text"] for x in t["turns"]], ["q2", "a2"])
        self.assertNotIn("session", t)

    def test_deletes_trailing_question_without_answer(self):
        t = {"turns": [{"role": "user", "text": "q1"}]}
        self.assertTrue(serve._drop_qa_exchange(t, 0))
        self.assertEqual(t["turns"], [])

    def test_rejects_non_user_index(self):
        t = self._thread()
        self.assertFalse(serve._drop_qa_exchange(t, 1))  # points at an assistant turn
        self.assertEqual(len(t["turns"]), 4)

    def test_rejects_out_of_range_and_garbage(self):
        t = self._thread()
        self.assertFalse(serve._drop_qa_exchange(t, 99))
        self.assertFalse(serve._drop_qa_exchange(t, "nope"))
        self.assertFalse(serve._drop_qa_exchange(t, None))
        self.assertEqual(len(t["turns"]), 4)


class RouteDispatch(ServeHttpCase):
    """End-to-end proof the table-driven dispatcher is wired correctly over the
    wire: a filesystem-only GET route returns 200, and an unknown route 404s."""

    def test_known_get_route_dispatches(self):
        status, payload = self.get_json("/api/segments")
        self.assertEqual(status, 200)
        self.assertIn("segments", payload)

    def test_unknown_get_route_is_404(self):
        status, payload = self.get_json("/api/does-not-exist")
        self.assertEqual(status, 404)
        self.assertIn("unknown endpoint", payload["error"])

    def test_unknown_post_route_is_404(self):
        status, payload = self.post_json("/api/nope", b"{}")
        self.assertEqual(status, 404)
        self.assertIn("unknown endpoint", payload["error"])


class ErrorLogEndpoint(ServeHttpCase):
    """GET returns recent incidents newest-first; POST {clear:true} wipes it.
    The log path is redirected to a temp file so the real data/ dir is untouched."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self._orig = serve.errorlog.LOG_PATH
        serve.errorlog.LOG_PATH = Path(self._dir.name) / "error_log.jsonl"

    def tearDown(self):
        serve.errorlog.LOG_PATH = self._orig
        self._dir.cleanup()

    def test_get_returns_recent_entries_newest_first(self):
        serve.errorlog.warn("llm_backend", "cursor auth", backend="cursor")
        serve.errorlog.error("server", "boom")
        status, payload = self.get_json("/api/error-log")
        self.assertEqual(status, 200)
        self.assertEqual([e["message"] for e in payload["entries"]], ["boom", "cursor auth"])

    def test_post_clear_wipes(self):
        serve.errorlog.error("server", "boom")
        status, payload = self.request("/api/error-log", method="POST", body={"clear": True})
        self.assertEqual(status, 200)
        self.assertEqual(payload["entries"], [])


class PeerStats(unittest.TestCase):
    """Rank-percentile of a metric within each segment's peers, plus the mean
    across segments. Helpers are stubbed so the math is checked without touching
    the data submodule."""

    def test_metric_value_coercion(self):
        self.assertEqual(peer_stats._metric_value({"metrics": {"ps": {"value": 4.5}}}, "ps"), 4.5)
        self.assertEqual(peer_stats._metric_value({"metrics": {"ps": 7}}, "ps"), 7.0)
        self.assertIsNone(peer_stats._metric_value({"metrics": {"ps": {"value": None}}}, "ps"))
        self.assertIsNone(peer_stats._metric_value({"metrics": {}}, "ps"))
        self.assertIsNone(peer_stats._metric_value({"metrics": {"ps": {"value": "x"}}}, "ps"))

    def test_no_segments_yields_empty_metrics(self):
        with mock.patch.object(peer_stats, "_segments_for_symbol", return_value=[]):
            res = peer_stats._peer_stats("ZZZ")
        self.assertEqual(res["segments"], [])
        self.assertEqual(res["metrics"], {})

    def test_percentile_median_and_aggregate(self):
        vals = {"AAA": 10.0, "BBB": 20.0, "CCC": 30.0, "DDD": 40.0, "SUB": 25.0}

        def fake_load(path):
            v = vals.get(Path(path).stem)
            return {"metrics": {"pe_ttm": {"value": v}}} if v is not None else {}

        seg = [("seg1", "Segment One", ["AAA", "BBB", "CCC", "DDD", "SUB"]),
               ("seg2", "Segment Two", ["AAA", "SUB"])]  # 2 members -> pct 0/1 boundary
        with mock.patch.object(peer_stats, "_load", side_effect=fake_load), \
             mock.patch.object(peer_stats, "_segments_for_symbol", return_value=seg):
            res = peer_stats._peer_stats("SUB")
        m = res["metrics"]["pe_ttm"]
        self.assertEqual(m["value"], 25.0)
        # seg1 sorted 10,20,25,30,40 -> 25 is the median -> 0.5 pctile, median 25
        s1 = next(s for s in m["per_segment"] if s["segment"] == "seg1")
        self.assertAlmostEqual(s1["pct"], 0.5, places=3)
        self.assertEqual(s1["n"], 5)
        self.assertEqual(s1["median"], 25.0)
        self.assertEqual(s1["members_total"], 5)
        self.assertTrue(s1["reliable"])           # 5 peers with data clears the bar
        # seg2 sorted 10,25 -> 25 is the top -> 1.0 pctile
        s2 = next(s for s in m["per_segment"] if s["segment"] == "seg2")
        self.assertAlmostEqual(s2["pct"], 1.0, places=3)
        self.assertFalse(s2["reliable"])          # only 2 peers -> not reliable
        # aggregate = mean(0.5, 1.0) = 0.75; the metric is reliable because at
        # least one segment had a big enough sample (seg1, n=5).
        self.assertAlmostEqual(m["aggregate"]["pct"], 0.75, places=3)
        self.assertEqual(m["aggregate"]["n_segments"], 2)
        self.assertEqual(m["n"], 5)
        self.assertTrue(m["reliable"])

    def test_small_sample_is_flagged_unreliable(self):
        # A segment with only 3 members that have data: the percentile is still
        # computed, but flagged unreliable so the UI won't present it as a hard
        # 0th/100th -- this is the SHEL "always 0 or 100" case.
        vals = {"XOM": 584.0, "CVX": 359.0, "SUB": 229.0}

        def fake_load(path):
            v = vals.get(Path(path).stem)
            return {"metrics": {"market_cap_usd_b": {"value": v}}} if v is not None else {}

        # roster of 6, but only 3 have research data
        seg = [("oil", "Oil majors", ["XOM", "CVX", "SUB", "COP", "EOG", "SLB"])]
        with mock.patch.object(peer_stats, "_load", side_effect=fake_load), \
             mock.patch.object(peer_stats, "_segments_for_symbol", return_value=seg):
            res = peer_stats._peer_stats("SUB")
        m = res["metrics"]["market_cap_usd_b"]
        self.assertAlmostEqual(m["aggregate"]["pct"], 0.0, places=3)  # subject is smallest
        self.assertEqual(m["n"], 3)
        self.assertEqual(m["members_total"], 6)
        self.assertFalse(m["reliable"])
        self.assertFalse(m["per_segment"][0]["reliable"])

    def test_single_peer_segment_is_skipped(self):
        def fake_load(path):
            return {"metrics": {"ps": {"value": 5.0}}}

        seg = [("solo", "Solo", ["SUB"])]  # only the subject -> not comparable
        with mock.patch.object(peer_stats, "_load", side_effect=fake_load), \
             mock.patch.object(peer_stats, "_segments_for_symbol", return_value=seg):
            res = peer_stats._peer_stats("SUB")
        self.assertNotIn("ps", res["metrics"])


class TickerDeepResearch(unittest.TestCase):
    """Single-name Deep Research: the prompt builder is namespaced `ticker-<sym>`
    and a run with no segment definition still gets a human title + symbol so the
    deep-dive can claim it. Offline -- holdings are stubbed empty."""

    def test_ticker_prompt_shape(self):
        with mock.patch.object(ticker_directory, "holdings_weights", return_value={}):
            rec = ticker_directory.ticker_deep_prompt("amd")
        self.assertEqual(rec["segment"], "ticker-amd")
        self.assertEqual(rec["symbol"], "AMD")
        self.assertRegex(rec["date"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertIn("AMD", rec["prompt"])
        self.assertIn("FORMAT", rec["prompt"])
        # Freshness directive bans anchoring time-sensitive numbers to the
        # training cutoff (the "returns to mid-2025 in a 2026 report" failure).
        self.assertIn("FRESHNESS", rec["prompt"])
        self.assertIn(rec["date"], rec["prompt"])
        # No holdings -> no "I currently hold" context line.
        self.assertNotIn("currently hold", rec["prompt"])

    def test_segment_prompt_has_freshness_directive(self):
        seg = {"title": "Fintech & Payments", "members": [{"symbol": "PYPL"}]}
        with mock.patch.object(segments_service, "load", return_value=seg), \
                mock.patch.object(segments_service, "holdings_weights", return_value={}):
            rec = serve._segment_prompt("fintech-payments")
        self.assertIn("FRESHNESS", rec["prompt"])
        self.assertIn("most recent close", rec["prompt"])

    def test_ticker_prompt_appends_held_context(self):
        with mock.patch.object(ticker_directory, "holdings_weights", return_value={"AMD": 12.5}):
            rec = ticker_directory.ticker_deep_prompt("AMD")
        self.assertIn("currently hold AMD at 12.50%", rec["prompt"])

    def test_ticker_prompt_rejects_non_ticker(self):
        with mock.patch.object(ticker_directory, "holdings_weights", return_value={}):
            with self.assertRaises(ValueError):
                ticker_directory.ticker_deep_prompt("not a ticker!")

    def test_enrich_ticker_run_without_segment_def(self):
        rec = {"stem": "ticker-amd-2026-06-13", "files": {"report": "x"}}
        # No data/segments/ticker-amd.json exists, so this exercises the fallback.
        with mock.patch.object(deep_runs, "_load", return_value={}):
            deep_runs._enrich_deep_run(rec)
        self.assertEqual(rec["kind"], "ticker")
        self.assertEqual(rec["symbol"], "AMD")
        self.assertEqual(rec["title"], "AMD \u2014 deep research")
        self.assertEqual(rec["segment"], "ticker-amd")
        self.assertEqual(rec["date"], "2026-06-13")

    def test_enrich_segment_run_is_not_marked_ticker(self):
        rec = {"stem": "fintech-payments-2026-06-13", "files": {"report": "x"}}
        with mock.patch.object(deep_runs, "_load", return_value={}):
            deep_runs._enrich_deep_run(rec)
        self.assertEqual(rec["kind"], "segment")
        self.assertEqual(rec["symbol"], "")


class DeepPromptEndpoint(ServeHttpCase):
    """GET /api/deep-prompt routes a `ticker=` query to the single-name builder
    and a `segment=` query to the segment builder; a bad segment is a clean 400."""

    def test_ticker_query_builds_single_name_prompt(self):
        with mock.patch.object(ticker_directory, "holdings_weights", return_value={}):
            status, payload = self.get_json("/api/deep-prompt?ticker=AMD")
        self.assertEqual(status, 200)
        self.assertEqual(payload["segment"], "ticker-amd")
        self.assertEqual(payload["symbol"], "AMD")

    def test_unknown_segment_is_400(self):
        status, payload = self.get_json("/api/deep-prompt?segment=does-not-exist")
        self.assertEqual(status, 400)
        self.assertIn("unknown segment", payload["error"])


class StagingEndpoints(ServeHttpCase):
    """The rewired staging endpoints: GET /api/staging diff, POST /api/staging/edit
    (pin/unpin/revert), commit, discard. Disk paths are redirected to a temp dir
    and the site regenerator is stubbed, so this stays offline."""

    def setUp(self):
        import target_model
        import target_staging as ts
        self.ts, self.tm = ts, target_model
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self._orig = {
            (ts, "TARGET_MODEL_JSON"): ts.TARGET_MODEL_JSON,
            (ts, "STAGED_JSON"): ts.STAGED_JSON,
            (ts, "HOLDINGS_JSON"): ts.HOLDINGS_JSON,
            (ts, "_regenerate_site"): ts._regenerate_site,
            (target_model, "TARGET_MODEL_JSON"): target_model.TARGET_MODEL_JSON,
            (target_model, "TARGET_MODEL_BACKUP_DIR"): target_model.TARGET_MODEL_BACKUP_DIR,
            (target_model, "REPO_ROOT"): target_model.REPO_ROOT,
        }
        ts.TARGET_MODEL_JSON = self.tm.TARGET_MODEL_JSON = root / "target-model.json"
        ts.STAGED_JSON = root / "target-model.staged.json"
        ts.HOLDINGS_JSON = root / "current-holdings.json"
        ts._regenerate_site = lambda: {"ok": True, "written": []}
        self.tm.TARGET_MODEL_BACKUP_DIR = root / "backups"
        self.tm.REPO_ROOT = root
        from store import write_json
        write_json(ts.TARGET_MODEL_JSON, {
            "as_of": "2026-01-01", "cash_target_pct": 5.0,
            "targets": {"TSM": {"low": 6, "high": 8, "rule": "accumulate"}},
            "sleeves": {},
        })

    def tearDown(self):
        for (mod, name), val in self._orig.items():
            setattr(mod, name, val)
        self.tmp.cleanup()

    def test_empty_draft_diff(self):
        status, payload = self.get_json("/api/staging")
        self.assertEqual(status, 200)
        self.assertFalse(payload["has_draft"])
        self.assertEqual(payload["counts"]["total"], 0)

    def test_pin_then_unpin_via_edit(self):
        status, payload = self.request("/api/staging/edit", method="POST", body={
            "op": "pin", "key": "TSM", "stance": "accumulate", "floor_pct": 3.0})
        self.assertEqual(status, 200)
        self.assertEqual(payload["pin"]["stance"], "accumulate")
        _s, diff = self.get_json("/api/staging")
        self.assertIn("TSM", diff["pins"])
        _s, payload = self.request("/api/staging/edit", method="POST", body={"op": "unpin", "key": "TSM"})
        self.assertTrue(payload["cleared"])

    def test_manual_edit_stages_then_commit(self):
        status, payload = self.request("/api/staging/edit", method="POST", body={
            "op": "edit", "change": {"action": "add_target", "symbol": "NVDA",
                                     "proposed_target": {"low": 8, "high": 10, "rule": "accumulate"}}})
        self.assertEqual(status, 200)
        self.assertIn("NVDA", payload["applied"])
        _s, diff = self.get_json("/api/staging")
        self.assertTrue(diff["has_draft"])
        self.assertEqual(diff["counts"]["total"], 1)
        # Revert it, draft becomes empty-diff.
        self.request("/api/staging/edit", method="POST", body={"op": "revert", "key": "NVDA"})
        _s, diff = self.get_json("/api/staging")
        self.assertEqual(diff["counts"]["total"], 0)

    def test_commit_promotes_to_live(self):
        self.request("/api/staging/edit", method="POST", body={
            "op": "edit", "change": {"action": "add_target", "symbol": "NVDA",
                                     "proposed_target": {"low": 8, "high": 10, "rule": "accumulate"}}})
        status, payload = self.request("/api/staging/commit", method="POST", body={"confirm": True})
        self.assertEqual(status, 200)
        self.assertTrue(payload["committed"])
        from store import load
        live = load(self.ts.TARGET_MODEL_JSON)
        self.assertIn("NVDA", live["targets"])

    def test_discard_clears_draft(self):
        self.request("/api/staging/edit", method="POST", body={
            "op": "edit", "change": {"action": "add_target", "symbol": "NVDA",
                                     "proposed_target": {"low": 8, "high": 10, "rule": "accumulate"}}})
        status, payload = self.request("/api/staging/discard", method="POST", body={})
        self.assertEqual(status, 200)
        self.assertTrue(payload["discarded"])


class PortfolioPrereqGuards(unittest.TestCase):
    """Holdings/model prerequisite helpers preserve per-endpoint 404 semantics."""

    def test_helper_messages(self):
        self.assertEqual(serve._holdings_prereq_error({}), serve.MSG_HOLDINGS_REQUIRED)
        self.assertIsNone(serve._holdings_prereq_error({"positions": []}))
        self.assertEqual(serve._model_prereq_error(None), serve.MSG_MODEL_REQUIRED)
        self.assertIsNone(serve._model_prereq_error({"targets": {}}))
        self.assertEqual(
            serve._both_prereq_error(None, {"targets": {}}),
            serve.MSG_BOTH_REQUIRED,
        )
        self.assertIsNone(serve._both_prereq_error({"positions": []}, {"targets": {}}))


class PortfolioPrereqHttp(ServeHttpCase):
    """Wire-level checks that guarded routes keep their exact 404 messages."""

    def _patch_inputs(self, *, holdings=None, model=None):
        return mock.patch.object(serve, "_load", return_value=holdings), \
            mock.patch.object(serve.target_staging, "active_model", return_value=model)

    def test_rebalance_reports_missing_model_first(self):
        load_patch, model_patch = self._patch_inputs(holdings=None, model=None)
        with load_patch, model_patch:
            status, payload = self.get_json("/api/rebalance")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], serve.MSG_MODEL_REQUIRED)

    def test_rebalance_reports_missing_holdings_second(self):
        load_patch, model_patch = self._patch_inputs(
            holdings=None, model={"targets": {}},
        )
        with load_patch, model_patch:
            status, payload = self.get_json("/api/rebalance")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], serve.MSG_HOLDINGS_REQUIRED)

    def test_risk_requires_holdings_only(self):
        load_patch, model_patch = self._patch_inputs(holdings=None, model=None)
        with load_patch, model_patch:
            status, payload = self.get_json("/api/risk")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], serve.MSG_HOLDINGS_REQUIRED)

    def test_whatif_requires_both(self):
        load_patch, model_patch = self._patch_inputs(holdings=None, model=None)
        with load_patch, model_patch:
            status, payload = self.post_json("/api/whatif", {"trades": []})
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], serve.MSG_BOTH_REQUIRED)

    def test_overview_stays_200_without_prereqs(self):
        load_patch, model_patch = self._patch_inputs(holdings=None, model=None)
        with load_patch, model_patch:
            status, payload = self.get_json("/api/overview")
        self.assertEqual(status, 200)
        self.assertIn("snapshot", payload)

    def test_execution_plan_replace_rebalance_value_error(self):
        load_patch, model_patch = self._patch_inputs(holdings=None, model=None)
        with load_patch, model_patch:
            status, payload = self.post_json(
                "/api/execution-plan",
                {"action": "replace_rebalance"},
            )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], serve.MSG_BOTH_VALUE_ERROR)

    def test_execution_plan_queue_selected_value_error(self):
        load_patch, model_patch = self._patch_inputs(
            holdings=None, model={"targets": {}},
        )
        with load_patch, model_patch:
            status, payload = self.post_json(
                "/api/execution-plan",
                {"action": "queue_selected"},
            )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], serve.MSG_HOLDINGS_VALUE_ERROR)

    def test_trade_place_submits_only_items_on_residual_orders(self):
        body = {
            "trades": [{
                "leg_id": "stock:AMD",
                "symbol": "AMD",
                "provenance": [{"execution_item_id": "item-amd"}],
            }],
            "token": "preview-token",
            "confirm": True,
        }
        result = {
            "orders": [{"symbol": "AMD"}],
            "placed": [{"order_id": "1"}],
            "staged_basket_cleared": True,
        }
        with mock.patch.object(serve, "_trade_place", return_value=result), \
             mock.patch.object(
                 serve.execution_plan,
                 "execution_item_ids_for_orders",
                 return_value=["item-amd"],
             ) as resolve, \
             mock.patch.object(serve.execution_plan, "mark_submitted") as submitted, \
             mock.patch.object(serve.execution_plan, "reconcile_queue") as reconcile:
            status, payload = self.post_json("/api/trade/place", body)
        self.assertEqual(status, 200)
        self.assertEqual(payload, result)
        resolve.assert_called_once_with(body["trades"], result["orders"])
        submitted.assert_called_once_with(["item-amd"])
        reconcile.assert_called_once_with([])

    def test_trade_place_prefers_acknowledged_correlation_and_hides_internal_context(self):
        body = {
            "trades": [{
                "leg_id": "stock:AMD", "symbol": "AMD",
                "provenance": [{"execution_item_id": "item-amd"}],
            }],
            "token": "preview-token",
            "confirm": True,
        }
        result = {
            "account": "DU1",
            "orders": [{"symbol": "AMD"}],
            "placed": [{
                "order_id": "1",
                "assay_order": {"cOID": "assay-amd", "symbol": "AMD"},
            }],
            "staged_basket_cleared": True,
        }
        records = [{
            "broker_order_id": "1",
            "execution_item_ids": ["item-amd"],
        }]
        with mock.patch.object(serve, "_trade_place", return_value=result), \
             mock.patch.object(
                 serve.order_correlation, "record_placements", return_value=records,
             ) as correlate, \
             mock.patch.object(
                 serve.execution_plan, "execution_item_ids_for_orders",
                 return_value=["wrong-fallback"],
             ), \
             mock.patch.object(serve.execution_plan, "mark_submitted") as submitted, \
             mock.patch.object(serve.execution_plan, "reconcile_queue"):
            status, payload = self.post_json("/api/trade/place", body)
        self.assertEqual(status, 200)
        correlate.assert_called_once_with("DU1", body["trades"], mock.ANY)
        submitted.assert_called_once_with(["item-amd"])
        self.assertNotIn("assay_order", payload["placed"][0])
        self.assertEqual(payload["correlations"], records)


class HostGuard(unittest.TestCase):
    def test_non_loopback_host_is_refused(self):
        # The guard fires before any socket is bound, so this never serves.
        with mock.patch("sys.argv", ["serve.py", "--host", "0.0.0.0"]):
            self.assertEqual(serve.main(), 2)


if __name__ == "__main__":
    unittest.main()
