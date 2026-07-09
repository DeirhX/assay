"""Suite-wide isolation from a developer's local, gitignored ``secrets.env``.

``config.config_value`` resolves a flag as: live env -> ``tools/secrets.env`` ->
default. That file doesn't exist on a clean CI checkout, but on a dev box it
carries real flags (``ASSAY_AUTO_REFRESH``, ``ASSAY_NOTIFY``, ...). Tests that
pop a key from ``os.environ`` to assert the *default-off* behavior then silently
read the dev's file instead of the default, so they pass in CI and fail locally.

Point the config layer at a nonexistent path for every test so only
``os.environ`` -- which each test controls -- decides a flag's value. This is
autouse, so it also applies to the ``unittest.TestCase``-based tests here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import activity  # noqa: E402
import config  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_local_secrets(monkeypatch):
    missing = Path(__file__).resolve().parent / "_no-such-secrets.env"
    monkeypatch.setattr(config, "TOOLS_SECRETS", missing, raising=False)
    monkeypatch.setattr(config, "ROOT_SECRETS", missing, raising=False)
    yield


@pytest.fixture(autouse=True)
def _isolate_activity_log(monkeypatch, tmp_path):
    """Redirect the durable Activity feed to a throwaway file for every test.

    ``jobs.update_job`` logs finished jobs to ``activity.record_task``, so the
    many existing tests that drive a job to ``done`` would otherwise append to
    the developer's real ``data/cache/activity-log.jsonl``. Point it at tmp and
    clear the in-process view debounce so tests never see each other's writes."""
    monkeypatch.setattr(activity, "ACTIVITY_LOG", tmp_path / "activity-log.jsonl", raising=False)
    activity._last_view.clear()
    yield
