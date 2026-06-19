#!/usr/bin/env python3
"""HTTP error vocabulary shared by the server and the services it calls.

A handler -- or any service function a handler invokes -- raises one of these
instead of repeating a ``try/except -> send_error(status, str(exc))`` block.
serve.py's dispatcher translates an HttpError into ``{"error": str(exc)}`` with
``exc.status``. ``Conflict``/``BadGateway`` subclass RuntimeError and
``BadRequest`` subclasses ValueError, so older ``except RuntimeError`` /
``except ValueError`` call sites (and tests) keep working unchanged.

Stdlib only and imports nothing from the project, so it stays a safe leaf that
serve.py and the trade service can both import without cycles.
"""

from __future__ import annotations


class HttpError(Exception):
    """Base for an error that maps to a specific HTTP status."""
    status = 500


class BadRequest(HttpError, ValueError):
    """A client-side request problem that should map to HTTP 400, not 500."""
    status = 400


class Forbidden(HttpError):
    """A guard refused the request (maps to HTTP 403)."""
    status = 403


class Conflict(HttpError, RuntimeError):
    """The request collides with in-flight work or current state (HTTP 409)."""
    status = 409


class BadGateway(HttpError, RuntimeError):
    """An upstream broker/service call failed (HTTP 502)."""
    status = 502
