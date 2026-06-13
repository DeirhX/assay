"""Shared data-hygiene primitives for the validators and the live puller.

These are the small, identical helpers that several tools independently grew: the
ERROR/WARN/INFO severity rank, the worst-severity reducer over a list of check
dicts, and the relative-difference used by every numeric cross-check. Kept
dependency-free (stdlib only) so any tool can import it without an import cycle.
"""

from __future__ import annotations

# Canonical severity rank: lower is more severe. Unknown severities sort last.
SEV_RANK = {"ERROR": 0, "WARN": 1, "INFO": 2}


def worst_severity(checks: list[dict[str, str]]) -> str:
    """The most severe severity across a list of check dicts ("INFO" if empty).

    Forgiving: a check missing a ``severity`` key is treated as INFO, and an
    unrecognized severity sorts after the known ones rather than raising.
    """
    if not checks:
        return "INFO"
    return min((c.get("severity", "INFO") for c in checks), key=lambda s: SEV_RANK.get(s, 9))


def rel_diff(a: float, b: float) -> float:
    """Relative difference ``|a - b| / |b|``; infinite when the base is zero."""
    if b == 0:
        return float("inf")
    return abs(a - b) / abs(b)
