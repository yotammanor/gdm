"""Tests for retry helpers."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from googleapiclient.errors import HttpError

from gdm.errors import NotFoundError, PermissionDeniedError
from gdm.retry import (
    RetryPolicy,
    execute_or_return,
    execute_with_retry,
    normalize_http_error,
)


def test_execute_or_return_plain_value() -> None:
    assert execute_or_return({"ok": True}) == {"ok": True}


def test_execute_or_return_http_request() -> None:
    request = MagicMock()
    request.execute.return_value = {"id": "1"}
    assert execute_or_return(request) == {"id": "1"}


def test_normalize_http_error_maps_status_codes() -> None:
    resp = MagicMock(status=404)
    assert isinstance(normalize_http_error(HttpError(resp, b"x")), NotFoundError)
    resp403 = MagicMock(status=403)
    assert isinstance(
        normalize_http_error(HttpError(resp403, b"x")), PermissionDeniedError
    )


def test_execute_with_retry_retries_transient_http_error() -> None:
    resp = MagicMock(status=503)
    calls = {"count": 0}

    def flaky() -> dict[str, str]:
        calls["count"] += 1
        if calls["count"] == 1:
            raise HttpError(resp, b"busy")
        return {"ok": "1"}

    policy = RetryPolicy(max_attempts=3, initial_delay=0.0, max_delay=0.0, jitter=0.0)
    assert execute_with_retry(flaky, policy=policy)["ok"] == "1"


def test_execute_with_retry_retries_rate_limited_403() -> None:
    resp = MagicMock(status=403)
    content = json.dumps(
        {"error": {"errors": [{"reason": "userRateLimitExceeded"}]}}
    ).encode()
    calls = {"count": 0}

    def flaky() -> dict[str, str]:
        calls["count"] += 1
        if calls["count"] == 1:
            raise HttpError(resp, content)
        return {"ok": "1"}

    policy = RetryPolicy(max_attempts=2, initial_delay=0.0, max_delay=0.0, jitter=0.0)
    assert execute_with_retry(flaky, policy=policy) == {"ok": "1"}
    assert calls["count"] == 2


def test_non_retryable_mutation_executes_once() -> None:
    resp = MagicMock(status=503)
    calls = {"count": 0}

    def failed_mutation() -> None:
        calls["count"] += 1
        raise HttpError(resp, b"busy")

    policy = RetryPolicy(max_attempts=5, initial_delay=0.0, max_delay=0.0, jitter=0.0)
    with pytest.raises(HttpError):
        execute_with_retry(failed_mutation, policy=policy, retryable=False)
    assert calls["count"] == 1
