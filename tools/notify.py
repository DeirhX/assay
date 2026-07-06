#!/usr/bin/env python3
"""Outbound notification channel -- turn supervision from "remember to check the
app" into "get interrupted when something needs you".

Everything the scheduler and the order watcher learn currently dies inside the
process. This is the one small seam that pushes the handful of events a human
must know promptly (a fill, a rejection, the gateway session dropping while
orders are working) to wherever they actually are.

stdlib-only, and OFF by default. Opt in with ``ASSAY_NOTIFY=1`` plus at least one
sink:

* ``ASSAY_NOTIFY_WEBHOOK`` -- an HTTP endpoint that receives a POST. The payload
  is ntfy-compatible (https://ntfy.sh): the message is the request body, with the
  title/tags/priority as headers, so a self-hosted or public ntfy topic works with
  no extra config. A generic webhook that just logs the body works too.
* ``ASSAY_NOTIFY_TOAST=1`` -- a best-effort Windows balloon/toast via a PowerShell
  subprocess. Windows-only and intentionally forgiving: if it fails, the webhook
  (or nothing) still happens.

The public entry point ``notify()`` never raises: a broken sink must not take down
the scheduler tick that called it. The transport seams are injectable so tests
never touch the network or spawn a shell.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config

_PRIORITIES = ("min", "low", "default", "high", "urgent", "max")


# --------------------------------------------------------------------------- #
# Flags / configuration
# --------------------------------------------------------------------------- #
def enabled() -> bool:
    """Master switch. Nothing is sent unless this is explicitly set."""
    return config.flag_enabled("ASSAY_NOTIFY", "0")


def _webhook_url() -> str:
    return config.config_value("ASSAY_NOTIFY_WEBHOOK", "").strip()


def _toast_enabled() -> bool:
    return config.flag_enabled("ASSAY_NOTIFY_TOAST", "0")


def configured_sinks() -> list[str]:
    """Which sinks would actually fire right now -- for a status/settings readout.
    Empty when notifications are disabled or nothing is configured."""
    if not enabled():
        return []
    sinks: list[str] = []
    if _webhook_url():
        sinks.append("webhook")
    if _toast_enabled():
        sinks.append("toast")
    return sinks


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def notify(title: str, body: str, *, tags: tuple[str, ...] = (), priority: str = "default",
           _webhook=None, _toast=None) -> list[str]:
    """Best-effort dispatch of one notification to every configured sink. Returns
    the sinks that succeeded (``[]`` when disabled or all failed). Never raises --
    a failing sink is logged to stderr so a watcher tick keeps going."""
    if not enabled():
        return []
    fired: list[str] = []
    url = _webhook_url()
    if url:
        try:
            (_webhook or _post_webhook)(url, title, body, tags, priority)
            fired.append("webhook")
        except Exception as exc:  # noqa: BLE001 -- a sink must never break the caller
            _log(f"webhook sink failed: {exc}")
    if _toast_enabled():
        try:
            (_toast or _post_toast)(title, body)
            fired.append("toast")
        except Exception as exc:  # noqa: BLE001
            _log(f"toast sink failed: {exc}")
    return fired


# --------------------------------------------------------------------------- #
# Sinks
# --------------------------------------------------------------------------- #
def _post_webhook(url: str, title: str, body: str, tags: tuple[str, ...], priority: str) -> None:
    """POST to an ntfy-compatible endpoint: message in the body, metadata in
    headers. HTTP header values must be latin-1-safe, so the title/tags are
    ASCII-folded; the full (possibly unicode) message rides in the UTF-8 body."""
    req = urllib.request.Request(url, data=body.encode("utf-8"), method="POST")
    req.add_header("Content-Type", "text/plain; charset=utf-8")
    if title:
        req.add_header("Title", _ascii(title))
    if tags:
        req.add_header("Tags", _ascii(",".join(tags)))
    if priority in _PRIORITIES:
        req.add_header("Priority", priority)
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 -- user-configured endpoint
        resp.read()


def _post_toast(title: str, body: str) -> None:
    """Best-effort Windows notification via a PowerShell balloon tip. Uses
    System.Windows.Forms.NotifyIcon (no third-party module, no AppId). PowerShell
    escaping is treacherous, so the script is written to a UTF-8 temp file and run
    by path rather than passed inline (per the project's Windows console rules)."""
    if sys.platform != "win32":
        raise RuntimeError("toast notifications are Windows-only")
    script = _toast_script(title, body)
    tmp = tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8")
    try:
        tmp.write(script)
        tmp.close()
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", tmp.name],
            capture_output=True, timeout=20, check=False,
        )
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def _toast_script(title: str, body: str) -> str:
    t = _ps_single_quote(title)
    b = _ps_single_quote(body)
    return (
        "Add-Type -AssemblyName System.Windows.Forms\n"
        "Add-Type -AssemblyName System.Drawing\n"
        "$n = New-Object System.Windows.Forms.NotifyIcon\n"
        "$n.Icon = [System.Drawing.SystemIcons]::Information\n"
        f"$n.BalloonTipTitle = {t}\n"
        f"$n.BalloonTipText = {b}\n"
        "$n.Visible = $true\n"
        "$n.ShowBalloonTip(10000)\n"
        "Start-Sleep -Seconds 6\n"
        "$n.Dispose()\n"
    )


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _ascii(text: str) -> str:
    """Fold to ASCII so a value is safe in an HTTP header (latin-1 transport)."""
    return text.encode("ascii", "replace").decode("ascii")


def _ps_single_quote(text: str) -> str:
    """Wrap text as a PowerShell single-quoted literal (doubling embedded quotes),
    stripping newlines so a multi-line message can't break out of the statement."""
    flat = str(text).replace("\r", " ").replace("\n", " ")
    return "'" + flat.replace("'", "''") + "'"


def _log(msg: str) -> None:
    sys.stderr.write(f"[notify] {msg}\n")
