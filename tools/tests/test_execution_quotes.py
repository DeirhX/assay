import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import execution_quotes as eq  # noqa: E402

NOW = dt.datetime(2026, 7, 1, 12, tzinfo=dt.timezone.utc)
MAX_AGE = 120.0


def _rung(**overrides):
    base = {
        "conid": 555,
        "bid": 2.40,
        "ask": 2.60,
        "stageable": True,
        "executable": True,
        "quote_timestamp": NOW.isoformat(),
    }
    base.update(overrides)
    return base


def test_midpoint_executable_limit_rounds_down_to_tick():
    assert eq.midpoint_executable_limit(2.40, 2.60, eps=1e-9) == 2.50
    assert eq.midpoint_executable_limit(2.40, 2.60, eps=1e-6) == 2.50


def test_rebalance_decorator_sets_fresh_limit_and_generic_warning():
    fresh = _rung()
    eq.decorate_ladder_rung(
        fresh,
        now=NOW,
        max_age_seconds=MAX_AGE,
        staging_warning=eq.rebalance_staging_warning,
    )
    assert fresh["quote_fresh"] is True
    assert fresh["limit_price"] == 2.50

    stale = _rung(quote_timestamp="2020-01-01T00:00:00+00:00")
    eq.decorate_ladder_rung(
        stale,
        now=NOW,
        max_age_seconds=MAX_AGE,
        staging_warning=eq.rebalance_staging_warning,
    )
    assert stale["quote_fresh"] is False
    assert stale["limit_price"] is None
    assert "preview requires a fresh two-sided quote" in stale["staging_warning"]


def test_exit_decorator_distinguishes_missing_quotes_and_stale_quotes():
    missing = _rung(bid=None, ask=None, executable=False)
    eq.decorate_ladder_rung(
        missing,
        now=NOW,
        max_age_seconds=MAX_AGE,
        two_sided_mode="truthy",
        assume_naive_utc=True,
        staging_warning=eq.exit_staging_warning,
        eps=1e-6,
    )
    assert missing["limit_price"] is None
    assert "Staging is allowed" in missing["staging_warning"]

    stale = _rung(quote_timestamp="2020-01-01T00:00:00+00:00")
    eq.decorate_ladder_rung(
        stale,
        now=NOW,
        max_age_seconds=MAX_AGE,
        two_sided_mode="truthy",
        assume_naive_utc=True,
        staging_warning=eq.exit_staging_warning,
        eps=1e-6,
    )
    assert stale["limit_price"] is None
    assert "Staging will refresh it from IBKR" in stale["staging_warning"]


def test_numeric_mode_accepts_zero_sides_rebalance_style():
    zero_sided = _rung(bid=0.0, ask=2.60)
    eq.decorate_ladder_rung(
        zero_sided,
        now=NOW,
        max_age_seconds=MAX_AGE,
        two_sided_mode="numeric",
        staging_warning=eq.rebalance_staging_warning,
    )
    assert zero_sided["limit_price"] == 1.30

    truthy = _rung(bid=0.0, ask=2.60)
    eq.decorate_ladder_rung(
        truthy,
        now=NOW,
        max_age_seconds=MAX_AGE,
        two_sided_mode="truthy",
        staging_warning=eq.exit_staging_warning,
        eps=1e-6,
    )
    assert truthy["limit_price"] is None
    assert "staging_warning" not in truthy


def test_freshness_matches_placement_boundaries():
    at_ttl = _rung(quote_timestamp=(NOW - dt.timedelta(seconds=MAX_AGE)).isoformat())
    eq.decorate_ladder_rung(at_ttl, now=NOW, max_age_seconds=MAX_AGE)
    assert at_ttl["quote_age_seconds"] == MAX_AGE
    assert at_ttl["quote_fresh"] is False
    assert at_ttl["limit_price"] is None

    future = _rung(quote_timestamp=(NOW + dt.timedelta(seconds=1)).isoformat())
    eq.decorate_ladder_rung(future, now=NOW, max_age_seconds=MAX_AGE)
    assert future["quote_age_seconds"] == 0.0
    assert future["quote_fresh"] is False
    assert future["limit_price"] is None


def test_freshness_accepts_naive_exit_clock_as_utc():
    naive_now = NOW.replace(tzinfo=None)
    rung = _rung(quote_timestamp=(NOW - dt.timedelta(seconds=1)).isoformat())
    eq.decorate_ladder_rung(
        rung,
        now=naive_now,
        max_age_seconds=MAX_AGE,
        assume_naive_utc=True,
    )
    assert rung["quote_fresh"] is True
