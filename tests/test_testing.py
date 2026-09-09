"""Tests for gdm.testing helpers."""

from __future__ import annotations

from gdm.backends.memory import MemoryBackend
from gdm.fields import Field
from gdm.model import GDMModel
from gdm.testing import memory_backend, memory_session, session_context


class Item(GDMModel, storage="file", codec="json"):
    id: str = Field(primary_key=True)
    name: str


def test_memory_backend_context() -> None:
    with memory_backend() as backend:
        assert isinstance(backend, MemoryBackend)


def test_memory_session_context() -> None:
    with memory_session() as session:
        session.add(Item(id="1", name="a"))
        session.commit()
        assert session.get(Item, "1") is not None


def test_session_context_reuses_backend() -> None:
    backend = MemoryBackend()
    session = session_context(backend)
    session.add(Item(id="1", name="a"))
    session.commit()
    assert backend.scan(Item)
