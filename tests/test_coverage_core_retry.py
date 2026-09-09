"""Branch coverage for gdm.retry."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from googleapiclient.errors import HttpError

from gdm.errors import GDMError, PermissionDeniedError
from gdm.retry import RetryPolicy, execute_with_retry, normalize_http_error


def test_normalize_http_error_401() -> None:
    resp = MagicMock(status=401)
    assert isinstance(
        normalize_http_error(HttpError(resp, b"x")), PermissionDeniedError
    )


def test_normalize_http_error_passes_through_other_statuses() -> None:
    resp = MagicMock(status=500)
    err = HttpError(resp, b"err")
    assert normalize_http_error(err) is err


def test_execute_with_retry_raises_non_retry_status_immediately() -> None:
    resp = MagicMock(status=400)

    def fail() -> None:
        raise HttpError(resp, b"bad request")

    policy = RetryPolicy(max_attempts=5, initial_delay=0.0, max_delay=0.0, jitter=0.0)
    with pytest.raises(HttpError):
        execute_with_retry(fail, policy=policy)


def test_execute_with_retry_retries_oserror() -> None:
    calls = {"count": 0}

    def flaky() -> str:
        calls["count"] += 1
        if calls["count"] == 1:
            raise OSError("temporary")
        return "ok"

    policy = RetryPolicy(max_attempts=3, initial_delay=0.0, max_delay=0.0, jitter=0.0)
    with patch("gdm.retry.time.sleep"):
        assert execute_with_retry(flaky, policy=policy) == "ok"


def test_execute_with_retry_oserror_exhausted() -> None:
    policy = RetryPolicy(max_attempts=2, initial_delay=0.0, max_delay=0.0, jitter=0.0)

    def always_fail() -> None:
        raise OSError("down")

    with patch("gdm.retry.time.sleep"), pytest.raises(OSError, match="down"):
        execute_with_retry(always_fail, policy=policy)


def test_execute_with_retry_http_exhaustion_raises_last() -> None:
    resp = MagicMock(status=503)

    def always_busy() -> None:
        raise HttpError(resp, b"busy")

    policy = RetryPolicy(max_attempts=2, initial_delay=0.0, max_delay=0.0, jitter=0.0)
    with patch("gdm.retry.time.sleep"), pytest.raises(HttpError):
        execute_with_retry(always_busy, policy=policy)


def test_execute_with_retry_gdm_error_when_loop_exhausted_without_exc() -> None:
    policy = RetryPolicy(max_attempts=0, initial_delay=0.0, max_delay=0.0, jitter=0.0)
    with pytest.raises(GDMError, match="Retry loop exhausted"):
        execute_with_retry(lambda: "unused", policy=policy)


def test_execute_with_retry_applies_backoff_and_jitter() -> None:
    resp = MagicMock(status=429)
    calls = {"count": 0}

    def flaky() -> dict[str, str]:
        calls["count"] += 1
        if calls["count"] < 3:
            raise HttpError(resp, b"rate")
        return {"ok": "1"}

    policy = RetryPolicy(
        max_attempts=5,
        initial_delay=1.0,
        max_delay=2.0,
        jitter=0.0,
    )
    with patch("gdm.retry.time.sleep") as sleep:
        with patch("gdm.retry.random.uniform", return_value=0.0):
            assert execute_with_retry(flaky, policy=policy)["ok"] == "1"
    assert sleep.call_count == 2
