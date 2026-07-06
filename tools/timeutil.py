#!/usr/bin/env python3
"""Shared UTC timestamp helpers.

Extracted because the same three tiny operations were copied into half a dozen
modules with subtly different behaviour:

* parsing an ISO-8601 stamp that may or may not carry a ``Z`` suffix and may or
  may not carry a timezone,
* measuring its age in seconds / whole days,
* deciding whether a cache entry is still within a TTL.

The dangerous divergence was the ``Z`` suffix: ``overview``/``quote_cache``
tolerated ``...Z`` (via ``.replace("Z", "+00:00")``) while ``risk`` and
``exit_plan`` fed it straight to ``datetime.fromisoformat`` (which rejects
``Z`` before Python 3.11). Their own writers emit ``+00:00`` so it never bit in
practice, but any stamp from another source would have silently read as "not
fresh". One parser closes that trap for good.

Everything here is pure (an injectable ``now`` keeps callers testable) and
stdlib-only.
"""

from __future__ import annotations

import datetime as dt
from typing import Any


def now_utc() -> dt.datetime:
    """Current time as an aware UTC datetime."""
    return dt.datetime.now(dt.timezone.utc)


def now_iso() -> str:
    """Current UTC time as a second-resolution ISO-8601 string."""
    return now_utc().isoformat(timespec="seconds")


def parse_iso_utc(stamp: Any) -> dt.datetime | None:
    """An ISO-8601 stamp as an aware UTC datetime; ``None`` if unparsable.

    Tolerates a trailing ``Z`` and promotes a naive stamp to UTC, so callers
    never have to remember which of those two footguns applies to their source.
    """
    if not stamp:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def age_seconds(stamp: Any, now: dt.datetime | None = None) -> float | None:
    """Seconds since ``stamp`` (never negative); ``None`` if unparsable."""
    parsed = parse_iso_utc(stamp)
    if parsed is None:
        return None
    return max(0.0, ((now or now_utc()) - parsed).total_seconds())


def age_days(stamp: Any, now: dt.datetime | None = None) -> int | None:
    """Whole days since ``stamp`` (never negative); ``None`` if unparsable."""
    secs = age_seconds(stamp, now)
    if secs is None:
        return None
    return int(secs // 86400)


def cache_fresh(stamp: Any, ttl_seconds: float, now: dt.datetime | None = None) -> bool:
    """True when ``stamp`` parses and is between 0 and ``ttl_seconds`` old.

    A stamp in the future (clock skew, hand-edited file) is treated as not
    fresh rather than absurdly fresh, matching the old per-module ``_fresh``
    guards' ``0 <= age`` check. Uses the *raw* (unclamped) delta so a future
    stamp yields a negative age and fails the lower bound -- ``age_seconds``
    clamps to zero, which would wrongly read as brand-new.
    """
    parsed = parse_iso_utc(stamp)
    if parsed is None:
        return False
    age = ((now or now_utc()) - parsed).total_seconds()
    return 0 <= age < ttl_seconds
