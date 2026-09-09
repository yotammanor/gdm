"""Retry policy and execution helpers for Google API calls."""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from googleapiclient.errors import HttpError

from gdm.errors import GDMError, NotFoundError, PermissionDeniedError

_T = TypeVar("_T")
RETRYABLE_403_REASONS = frozenset(
    {
        "backendError",
        "rateLimitExceeded",
        "userRateLimitExceeded",
    }
)


@dataclass(frozen=True)
class RetryPolicy:
    """Exponential backoff policy for transient Google API failures."""

    max_attempts: int = 5
    initial_delay: float = 0.5
    max_delay: float = 32.0
    jitter: float = 0.1
    retry_statuses: frozenset[int] = frozenset({429, 500, 502, 503, 504})


def _http_status(exc: HttpError) -> int | None:
    status = getattr(exc, "status_code", None) or getattr(exc.resp, "status", None)
    return int(status) if status is not None else None


def _http_error_reasons(exc: HttpError) -> frozenset[str]:
    content = getattr(exc, "content", b"")
    if isinstance(content, bytes):
        try:
            content = content.decode("utf-8")
        except UnicodeDecodeError:
            return frozenset()
    try:
        payload = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return frozenset()
    if not isinstance(payload, dict):
        return frozenset()
    error = payload.get("error", {})
    if not isinstance(error, dict):
        return frozenset()
    reasons: set[str] = set()
    details = error.get("errors", [])
    if isinstance(details, list):
        for detail in details:
            if isinstance(detail, dict) and detail.get("reason"):
                reasons.add(str(detail["reason"]))
    status = error.get("status")
    if status:
        reasons.add(str(status))
    return frozenset(reasons)


def http_error_is_retryable(exc: HttpError, policy: RetryPolicy) -> bool:
    """Return whether a Google HTTP failure is transient under ``policy``."""
    status = _http_status(exc)
    if status in policy.retry_statuses:
        return True
    return status == 403 and bool(_http_error_reasons(exc) & RETRYABLE_403_REASONS)


def normalize_http_error(exc: HttpError) -> Exception:
    """Map ``HttpError`` instances to GDM exception types."""
    status = _http_status(exc)
    if status == 403 and _http_error_reasons(exc) & RETRYABLE_403_REASONS:
        return exc
    if status in {401, 403}:
        return PermissionDeniedError(str(exc))
    if status == 404:
        return NotFoundError(str(exc))
    return exc


def execute_or_return(value: _T) -> _T:
    """Execute a discovery ``HttpRequest`` or return an already-resolved value."""
    execute = getattr(value, "execute", None)
    if callable(execute):
        return execute()
    return value


def execute_with_retry(
    func: Callable[[], _T],
    *,
    policy: RetryPolicy | None = None,
    retryable: bool = True,
) -> _T:
    """Run ``func`` with backoff when repeating the operation is safe.

    Pass ``retryable=False`` for non-idempotent mutations such as creates,
    appends, and arbitrary batch updates. Those calls execute once so an
    ambiguous network failure cannot silently duplicate remote state.
    """
    active = policy or RetryPolicy()
    max_attempts = active.max_attempts if retryable else 1
    delay = active.initial_delay
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return func()
        except HttpError as exc:
            if not http_error_is_retryable(exc, active) or attempt + 1 >= max_attempts:
                normalized = normalize_http_error(exc)
                if normalized is not exc:
                    raise normalized from exc
                raise
            last_exc = exc
        except OSError as exc:
            if attempt + 1 >= max_attempts:
                raise
            last_exc = exc
        sleep_for = min(delay, active.max_delay) * (
            1 + random.uniform(-active.jitter, active.jitter)
        )
        time.sleep(max(sleep_for, 0.0))
        delay = min(delay * 2, active.max_delay)
    if last_exc is not None:
        raise last_exc
    msg = "Retry loop exhausted without executing func."
    raise GDMError(msg)


__all__ = [
    "RetryPolicy",
    "execute_or_return",
    "execute_with_retry",
    "http_error_is_retryable",
    "normalize_http_error",
]
