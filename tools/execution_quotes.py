#!/usr/bin/env python3
"""Shared ladder-rung quote decoration for execution previews."""
from __future__ import annotations

import datetime as dt
import math
from collections.abc import Callable
from typing import Any, Literal

import timeutil

TwoSidedMode = Literal["numeric", "truthy"]
StagingWarningFn = Callable[[dict[str, Any], bool, bool], str | None]


def midpoint_executable_limit(bid: float, ask: float, *, eps: float = 1e-9) -> float:
    """Tick-rounded sell limit at the executable midpoint."""
    return math.floor(((bid + ask) / 2.0 + eps) * 100) / 100


def _has_two_sided_quote(rung: dict[str, Any], mode: TwoSidedMode) -> bool:
    bid, ask = rung.get("bid"), rung.get("ask")
    if mode == "numeric":
        return isinstance(bid, (int, float)) and isinstance(ask, (int, float))
    return bool(bid and ask)


def quote_age_seconds(
    quote_timestamp: Any,
    *,
    now: dt.datetime | None = None,
    assume_naive_utc: bool = False,
) -> float | None:
    current = now
    if assume_naive_utc and current is not None and current.tzinfo is None:
        current = current.replace(tzinfo=dt.timezone.utc)
    return timeutil.age_seconds(quote_timestamp, now=current)


def decorate_ladder_rung(
    rung: dict[str, Any],
    *,
    now: dt.datetime | None,
    max_age_seconds: float,
    two_sided_mode: TwoSidedMode = "numeric",
    assume_naive_utc: bool = False,
    staging_warning: StagingWarningFn | None = None,
    eps: float = 1e-9,
) -> None:
    """Annotate one ladder rung with quote age, freshness, limit, and warnings."""
    age = quote_age_seconds(
        rung.get("quote_timestamp"),
        now=now,
        assume_naive_utc=assume_naive_utc,
    )
    quote_fresh = age is not None and age <= max_age_seconds
    rung["quote_age_seconds"] = round(age, 1) if age is not None else None
    rung["quote_fresh"] = quote_fresh

    has_two_sided = _has_two_sided_quote(rung, two_sided_mode)
    if rung.get("executable") and quote_fresh and has_two_sided:
        rung["limit_price"] = midpoint_executable_limit(
            float(rung["bid"]),
            float(rung["ask"]),
            eps=eps,
        )
        return

    rung["limit_price"] = None
    if staging_warning is not None:
        message = staging_warning(rung, quote_fresh, has_two_sided)
        if message:
            rung["staging_warning"] = message


def decorate_ladder_rungs(
    rungs: list[dict[str, Any]],
    *,
    now: dt.datetime | None,
    max_age_seconds: float,
    two_sided_mode: TwoSidedMode = "numeric",
    assume_naive_utc: bool = False,
    staging_warning: StagingWarningFn | None = None,
    eps: float = 1e-9,
) -> None:
    for rung in rungs:
        decorate_ladder_rung(
            rung,
            now=now,
            max_age_seconds=max_age_seconds,
            two_sided_mode=two_sided_mode,
            assume_naive_utc=assume_naive_utc,
            staging_warning=staging_warning,
            eps=eps,
        )


def rebalance_staging_warning(
    rung: dict[str, Any],
    quote_fresh: bool,
    has_two_sided: bool,
) -> str | None:
    del quote_fresh, has_two_sided
    if rung.get("stageable"):
        return (
            "The exact IBKR contract can be staged, but preview requires a "
            "fresh two-sided quote."
        )
    return None


def exit_staging_warning(
    rung: dict[str, Any],
    quote_fresh: bool,
    has_two_sided: bool,
) -> str | None:
    del has_two_sided
    contract_stageable = bool(rung.get("stageable") and rung.get("conid"))
    if not contract_stageable:
        return None
    bid, ask = rung.get("bid"), rung.get("ask")
    if bid is None or ask is None:
        return (
            "No live bid/ask right now. Staging is allowed, but preview and placement "
            "remain blocked until IBKR returns a fresh two-sided quote."
        )
    if not quote_fresh:
        return (
            "The displayed quote is stale. Staging will refresh it from IBKR "
            "before calculating a limit price."
        )
    return None
