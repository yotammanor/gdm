"""Branch coverage for public imports and gdm.testing helpers."""

from __future__ import annotations

import gdm
from gdm.testing import memory_backend, memory_session, session_context


def test_public_package_exports() -> None:
    expected = set(gdm.__all__)
    for name in expected:
        assert hasattr(gdm, name), f"missing export {name}"
    assert gdm.__version__ == "0.1.0"


def test_memory_session_read_only_and_scan_limit() -> None:
    from gdm.backends.memory import MemoryBackend
    from gdm.errors import ReadOnlySessionError
    from gdm.fields import Field
    from gdm.model import GDMModel

    class Item(GDMModel, storage="file", codec="json"):
        id: str = Field(primary_key=True)
        name: str

    with memory_session(read_only=True) as session:
        try:
            session.add(Item(id="1", name="a"))
            raised = False
        except ReadOnlySessionError:
            raised = True
        assert raised

    with memory_session(scan_limit=1) as session:
        backend = session.backend
        assert isinstance(backend, MemoryBackend)


def test_session_context_without_backend() -> None:
    from gdm.fields import Field
    from gdm.model import GDMModel

    class Row(GDMModel, storage="sheet", source="rows"):
        id: str = Field(primary_key=True)
        value: str

    session = session_context()
    session.add(Row(id="1", value="v"))
    session.commit()
    assert session.get(Row, "1") is not None


def test_memory_backend_context_yields_fresh_backend() -> None:
    with memory_backend() as first, memory_backend() as second:
        assert first is not second
