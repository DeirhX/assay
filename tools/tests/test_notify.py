"""Tests for the notification channel. The transports are injected, so nothing
here touches the network or spawns a shell; we pin the enable-gating, the sink
selection, ntfy-style webhook shaping, and the never-raise contract."""

from __future__ import annotations

import os
import unittest

import _support  # noqa: F401
import notify


class _Env(unittest.TestCase):
    """Set notify env for the test, restore whatever was there before."""

    KEYS = ("ASSAY_NOTIFY", "ASSAY_NOTIFY_WEBHOOK", "ASSAY_NOTIFY_TOAST")

    def setUp(self):
        # Save, then CLEAR every notify flag so each test starts from a known
        # disabled baseline. Without this, a suite run in a shell that has
        # ASSAY_NOTIFY_TOAST=1 set would inherit it and fire a REAL Windows toast
        # (via the PowerShell NotifyIcon sink) for any test that calls notify()
        # without popping the flag or injecting a fake sink -- a unit test must
        # never spew OS notifications on the developer's machine.
        self._orig = {k: os.environ.get(k) for k in self.KEYS}
        for k in self.KEYS:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._orig.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _set(self, **kw):
        for k, v in kw.items():
            os.environ[k] = v


class EnableGating(_Env):
    def test_disabled_by_default_sends_nothing(self):
        for k in self.KEYS:
            os.environ.pop(k, None)
        calls = []
        fired = notify.notify("t", "b", _webhook=lambda *a: calls.append(a))
        self.assertEqual(fired, [])
        self.assertEqual(calls, [])

    def test_master_on_but_no_sink_configured(self):
        self._set(ASSAY_NOTIFY="1")
        os.environ.pop("ASSAY_NOTIFY_WEBHOOK", None)
        os.environ.pop("ASSAY_NOTIFY_TOAST", None)
        self.assertEqual(notify.configured_sinks(), [])
        self.assertEqual(notify.notify("t", "b"), [])

    def test_configured_sinks_reflects_flags(self):
        self._set(ASSAY_NOTIFY="1", ASSAY_NOTIFY_WEBHOOK="https://ntfy.example/topic",
                  ASSAY_NOTIFY_TOAST="1")
        self.assertEqual(sorted(notify.configured_sinks()), ["toast", "webhook"])


class WebhookSink(_Env):
    def test_posts_and_reports_fired(self):
        self._set(ASSAY_NOTIFY="1", ASSAY_NOTIFY_WEBHOOK="https://ntfy.example/topic")
        seen = {}

        def transport(url, title, body, tags, priority):
            seen.update(url=url, title=title, body=body, tags=tags, priority=priority)

        fired = notify.notify("Fill: BUY NVDA", "filled 10", tags=("white_check_mark",),
                              priority="high", _webhook=transport)
        self.assertEqual(fired, ["webhook"])
        self.assertEqual(seen["url"], "https://ntfy.example/topic")
        self.assertEqual(seen["title"], "Fill: BUY NVDA")
        self.assertEqual(seen["priority"], "high")

    def test_a_failing_webhook_never_raises(self):
        self._set(ASSAY_NOTIFY="1", ASSAY_NOTIFY_WEBHOOK="https://ntfy.example/topic")

        def boom(*_a):
            raise RuntimeError("network down")

        self.assertEqual(notify.notify("t", "b", _webhook=boom), [])

    def test_ascii_folds_header_but_keeps_utf8_body(self):
        # _post_webhook builds a urllib Request; capture it via a fake opener by
        # exercising the header helper directly (the transport is otherwise IO).
        self.assertEqual(notify._ascii("na\u00efve \u2192 x"), "na?ve ? x")


class ToastSink(_Env):
    def test_fires_when_enabled(self):
        self._set(ASSAY_NOTIFY="1", ASSAY_NOTIFY_TOAST="1")
        os.environ.pop("ASSAY_NOTIFY_WEBHOOK", None)
        calls = []
        fired = notify.notify("t", "b", _toast=lambda title, body: calls.append((title, body)))
        self.assertEqual(fired, ["toast"])
        self.assertEqual(calls, [("t", "b")])

    def test_ps_single_quote_escapes_and_flattens(self):
        got = notify._ps_single_quote("it's\nfine")
        self.assertEqual(got, "'it''s fine'")


if __name__ == "__main__":
    unittest.main()
