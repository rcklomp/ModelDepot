"""Tests for the robust download path (retry + resume).

Stubs snapshot_download so no network is hit. Verifies:
  - transient errors are retried up to max_retries, then succeed.
  - fatal errors (404 / gated / bad revision) fail immediately, no retry.
  - the per-file resume behavior comes from snapshot_download itself (we just
    confirm the loop calls it again after a transient failure).
  - progress lines are written to the log.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ingester import hf_source
from ingester.hf_source import HfSourceError


class _FakeHTTPError(Exception):
    """Minimal stand-in for an HF HTTP error: carries a .response.status_code.

    My classifier only reads `getattr(exc, "response", None).status_code`, so we
    don't need to build a real HfHubHTTPError (whose constructor demands a full
    httpx Response with .request/.headers).
    """

    class _Resp:
        def __init__(self, status: int):
            self.status_code = status

    def __init__(self, status: int, msg: str = ""):
        super().__init__(msg or f"HTTP {status}")
        self.response = self._Resp(status)


def _make_http_error(status: int):
    return _FakeHTTPError(status)


def test_retry_succeeds_after_transient_failures(tmp_path: Path):
    """snapshot_download fails twice with a 503, then succeeds on attempt 3."""
    calls = {"n": 0}

    def fake_snapshot(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise _make_http_error(503)
        # Third call: "succeed" — just return.
        return str(tmp_path)

    # Monkeypatch time.sleep so the test is instant.
    import time
    original_sleep = time.sleep
    time.sleep = lambda _s: None
    try:
        hf_source._download_with_resume(
            fake_snapshot, repo_id="o/r", revision="abc",
            staging_dir=tmp_path, token=None, allow_patterns=None,
            ignore_patterns=None, endpoint=None, max_retries=6,
        )
    finally:
        time.sleep = original_sleep

    assert calls["n"] == 3  # failed, failed, succeeded


def test_retry_gives_up_after_max_retries(tmp_path: Path):
    """Persistent transient errors exhaust retries and raise HfSourceError."""
    calls = {"n": 0}

    def fake_snapshot(*args, **kwargs):
        calls["n"] += 1
        raise _make_http_error(503)

    import time
    original_sleep = time.sleep
    time.sleep = lambda _s: None
    try:
        with pytest.raises(HfSourceError, match="gave up after 2 retries"):
            hf_source._download_with_resume(
                fake_snapshot, repo_id="o/r", revision="abc",
                staging_dir=tmp_path, token=None, allow_patterns=None,
                ignore_patterns=None, endpoint=None, max_retries=2,
            )
    finally:
        time.sleep = original_sleep

    assert calls["n"] == 3  # initial + 2 retries


def test_fatal_4xx_not_retried(tmp_path: Path):
    """A 404 (wrapped as HfHubHTTPError) is fatal — no retry."""
    calls = {"n": 0}

    def fake_snapshot(*args, **kwargs):
        calls["n"] += 1
        raise _make_http_error(404)

    with pytest.raises(HfSourceError, match="fatal"):
        hf_source._download_with_resume(
            fake_snapshot, repo_id="o/r", revision="abc",
            staging_dir=tmp_path, token=None, allow_patterns=None,
            ignore_patterns=None, endpoint=None, max_retries=6,
        )
    assert calls["n"] == 1  # no retry on a fatal error


def test_network_error_is_retriable(tmp_path: Path):
    """A raw httpx ConnectError is retriable (network drop)."""
    import httpx
    calls = {"n": 0}

    def fake_snapshot(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] < 2:
            raise httpx.ConnectError("connection reset")
        return

    import time
    original_sleep = time.sleep
    time.sleep = lambda _s: None
    try:
        hf_source._download_with_resume(
            fake_snapshot, repo_id="o/r", revision="abc",
            staging_dir=tmp_path, token=None, allow_patterns=None,
            ignore_patterns=None, endpoint=None, max_retries=6,
        )
    finally:
        time.sleep = original_sleep
    assert calls["n"] == 2


def test_progress_log_is_written(tmp_path: Path):
    """Each attempt and the completion emit a line to the progress log."""
    log = tmp_path / "progress.log"
    calls = {"n": 0}

    def fake_snapshot(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] < 2:
            raise _make_http_error(429)
        return

    import time
    original_sleep = time.sleep
    time.sleep = lambda _s: None
    try:
        hf_source._download_with_resume(
            fake_snapshot, repo_id="o/r", revision="abc",
            staging_dir=tmp_path, token=None, allow_patterns=None,
            ignore_patterns=None, endpoint=None, max_retries=6,
            progress_log=log,
        )
    finally:
        time.sleep = original_sleep

    text = log.read_text()
    assert "attempt=1" in text
    assert "status=retry" in text and "will resume from HF cache" in text
    assert "attempt=2" in text and "status=complete" in text


def test_is_retriable_classification():
    """The error classifier behaves correctly for known shapes."""
    # 429 and 5xx -> retriable
    assert hf_source._is_retriable(_make_http_error(429)) is True
    assert hf_source._is_retriable(_make_http_error(502)) is True
    # 404 / 403 -> fatal
    assert hf_source._is_retriable(_make_http_error(404)) is False
    assert hf_source._is_retriable(_make_http_error(403)) is False
    # network errors -> retriable
    import httpx
    assert hf_source._is_retriable(httpx.ConnectError("x")) is True
    assert hf_source._is_retriable(httpx.ReadTimeout("x")) is True
    # bare OSError (e.g. connection reset at socket level) -> retriable
    assert hf_source._is_retriable(OSError("reset")) is True
