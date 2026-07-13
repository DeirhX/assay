"""Small, dependency-free input coercions shared across trade/execution paths."""

from __future__ import annotations

from typing import Any


def coerce_optional_limit_price(
    raw: Any,
    *,
    numeric_error: str | None = None,
    positive_error: str | None = None,
) -> float | None:
    """Coerce an optional limit price to ``float`` or ``None``.

    When ``raw`` is absent, returns ``None``. When ``numeric_error`` is set,
    non-numeric input raises ``ValueError`` with that message; otherwise the
    underlying ``float()`` error propagates. When ``positive_error`` is set,
    non-positive values raise; when omitted, the coerced value is returned as-is.
    """
    if raw is None:
        return None
    try:
        limit = float(raw)
    except (TypeError, ValueError):
        if numeric_error is None:
            raise
        raise ValueError(numeric_error) from None
    if positive_error is not None and limit <= 0:
        raise ValueError(positive_error)
    return limit
