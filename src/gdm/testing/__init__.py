"""Test helpers and fixtures for GDM."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from gdm.backends.memory import MemoryBackend
from gdm.session import Session

__all__ = [
    "MemoryBackend",
    "memory_backend",
    "memory_session",
    "session_context",
]


@contextmanager
def memory_backend() -> Iterator[MemoryBackend]:
    """Yield a fresh :class:`MemoryBackend` for tests."""
    yield MemoryBackend()


@contextmanager
def memory_session(
    *,
    read_only: bool = False,
    scan_limit: int | None = None,
) -> Iterator[Session]:
    """Yield a :class:`Session` backed by an in-memory backend."""
    backend = MemoryBackend()
    with Session(backend, read_only=read_only, scan_limit=scan_limit) as session:
        yield session


def session_context(
    backend: MemoryBackend | None = None,
    *,
    read_only: bool = False,
    scan_limit: int | None = None,
) -> Session:
    """Return a session using ``backend`` or a new :class:`MemoryBackend`."""
    active = backend or MemoryBackend()
    return Session(active, read_only=read_only, scan_limit=scan_limit)
