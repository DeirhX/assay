"""Tests for the durable Activity feed and its wiring into the job registry.

The feed is the one server-side record of tickers viewed + tasks finished, so
these cover: append/read round-trips, the view debounce, the trim ceiling, that
finishing a job logs exactly one task event (and re-touching it doesn't), and
that a torn final line never breaks the reader.
"""

from __future__ import annotations

import _support  # noqa: F401
import activity
import jobs


def test_record_view_round_trips():
    assert activity.record_view("amd", "AMD Inc") is True
    events = activity.recent()
    assert len(events) == 1
    assert events[0]["type"] == "view"
    assert events[0]["symbol"] == "AMD"          # uppercased
    assert events[0]["name"] == "AMD Inc"


def test_blank_symbol_is_ignored():
    assert activity.record_view("  ") is False
    assert activity.recent() == []


def test_view_debounce_drops_immediate_repeat():
    assert activity.record_view("NVDA") is True
    assert activity.record_view("NVDA") is False   # within the debounce window
    assert len(activity.recent()) == 1


def test_view_debounce_is_per_symbol():
    assert activity.record_view("NVDA") is True
    assert activity.record_view("AMD") is True     # different symbol, not debounced
    assert len(activity.recent()) == 2


def test_recent_is_newest_first_and_capped():
    for i in range(5):
        activity._append({"ts": f"2026-01-0{i}", "type": "view", "symbol": f"S{i}"})
    out = activity.recent(limit=3)
    assert [e["symbol"] for e in out] == ["S4", "S3", "S2"]


def test_reader_skips_torn_lines():
    activity.record_view("AMD")
    with activity.ACTIVITY_LOG.open("a", encoding="utf-8") as fh:
        fh.write("{not valid json\n")
        fh.write("\n")
    out = activity.recent()
    assert len(out) == 1
    assert out[0]["symbol"] == "AMD"


def test_trim_keeps_only_newest_max_events(monkeypatch):
    monkeypatch.setattr(activity, "MAX_EVENTS", 4)
    monkeypatch.setattr(activity, "_TRIM_SLACK", 0.5)   # trim once past 6 lines
    for i in range(10):
        activity._append({"ts": f"t{i}", "type": "view", "symbol": f"S{i}"})
    lines = activity.ACTIVITY_LOG.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4
    # The survivors are the most recent four.
    assert [line for line in lines if "S9" in line]
    assert not [line for line in lines if '"S0"' in line]


def test_finished_job_logs_one_task_event():
    job = jobs.new_job("ticker_analysis", symbol="RTX")
    jobs.update_job(job["id"], state="running")      # not terminal -> no event
    assert activity.recent() == []
    jobs.update_job(job["id"], state="done", message="ok")
    events = activity.recent()
    assert len(events) == 1
    assert events[0]["type"] == "task"
    assert events[0]["kind"] == "ticker_analysis"
    assert events[0]["symbol"] == "RTX"
    assert events[0]["state"] == "done"


def test_terminal_transition_logs_only_once():
    job = jobs.new_job("ibkr_sync")
    jobs.update_job(job["id"], state="done")
    jobs.update_job(job["id"], state="done", message="touched again")
    assert len(activity.recent()) == 1


def test_error_job_carries_error_text():
    job = jobs.new_job("deep_research", segment="ai-chips")
    jobs.update_job(job["id"], state="error", error="boom")
    ev = activity.recent()[0]
    assert ev["state"] == "error"
    assert ev["error"] == "boom"
    assert ev["segment"] == "ai-chips"
