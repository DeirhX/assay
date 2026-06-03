"""Shared helpers for the data providers: HTTP, metric nodes, formatting."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

# A browser-ish UA. Yahoo refuses obviously-scripted clients; the SEC asks for a
# descriptive one with contact info (set via SEC_USER_AGENT env if you like).
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class ProviderError(RuntimeError):
    """A provider could not return usable data. Callers degrade gracefully."""


def http_get(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    opener: urllib.request.OpenerDirector | None = None,
    timeout: int = 20,
    retries: int = 2,
) -> bytes:
    """GET with a couple of retries. Raises ProviderError on give-up."""
    last: Exception | None = None
    hdrs = {"User-Agent": BROWSER_UA, "Accept": "*/*"}
    if headers:
        hdrs.update(headers)
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            fn = opener.open if opener else urllib.request.urlopen
            with fn(req, timeout=timeout) as resp:  # type: ignore[operator]
                return resp.read()
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last = exc
            if attempt < retries:
                time.sleep(0.6 * (attempt + 1))
    raise ProviderError(f"GET failed after {retries + 1} tries: {url} ({last})")


def get_json(url: str, **kwargs: Any) -> Any:
    raw = http_get(url, **kwargs)
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:  # noqa: PERF203
        raise ProviderError(f"non-JSON response from {url}: {exc}") from exc


def metric(value: float | None, source: str, *, display: str | None = None,
           as_of: str | None = None, note: str | None = None) -> dict[str, Any] | None:
    """A single comparable metric node, or None when the value is missing."""
    if value is None:
        return None
    node: dict[str, Any] = {"value": round(float(value), 6), "source": source}
    if display is not None:
        node["display"] = display
    if as_of is not None:
        node["as_of"] = as_of
    if note is not None:
        node["note"] = note
    return node


def usd_b(value_usd: float | None) -> float | None:
    """USD -> USD billions."""
    return None if value_usd is None else value_usd / 1e9


def fmt_b(value_b: float | None, prefix: str = "$") -> str:
    if value_b is None:
        return "n/a"
    if abs(value_b) >= 1000:
        return f"{prefix}{value_b / 1000:.2f}T"
    return f"{prefix}{value_b:.1f}B"


def fmt_x(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}x"


def fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.1f}%"


def fmt_price(value: float | None) -> str:
    return "n/a" if value is None else f"${value:,.2f}"
