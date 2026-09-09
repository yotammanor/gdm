"""Tests for Session commit and query behavior."""

from __future__ import annotations

import pytest

from gdm.backends.memory import MemoryBackend, materialize
from gdm.errors import (
    ConflictError,
    PartialCommitError,
    ReadOnlySessionError,
    ResultError,
)
from gdm.fields import Field
from gdm.model import GDMModel
from gdm.query import select
from gdm.session import Session


class Task(GDMModel, storage="sheet", source="tasks", mapping="partial"):
    id: str = Field(primary_key=True, source="task_id")
    status: str
    title: str


class Tag(GDMModel, storage="file", codec="json", source="tags"):
    id: str = Field(primary_key=True)
    name: str


def test_session_create_update_delete_roundtrip() -> None:
    backend = MemoryBackend()
    with Session(backend) as session:
        task = Task(id="t1", status="todo", title="One")
        session.add(task)
        session.commit()

    with Session(backend) as session:
        loaded = session.get(Task, "t1")
        assert loaded is not None
        loaded.status = "done"
        session.add(loaded)
        session.commit()

    with Session(backend) as session:
        loaded = session.get(Task, "t1")
        assert loaded is not None
        assert loaded.status == "done"
        session.delete(loaded)
        session.commit()
        assert session.get(Task, "t1") is None


def test_session_refreshes_snapshot_after_commit() -> None:
    backend = MemoryBackend()
    backend.seed_record(
        Task,
        {"task_id": "t1", "status": "todo", "title": "One"},
        version="1",
    )
    with Session(backend) as session:
        task = session.get(Task, "t1")
        assert task is not None
        task.title = "Updated"
        session.add(task)
        session.commit()
        session.delete(task)
        session.commit()
    assert backend.scan(Task) == []


def test_update_preserves_unknown_fields() -> None:
    backend = MemoryBackend()
    backend.seed_record(
        Task,
        {"task_id": "1", "status": "todo", "title": "A"},
        unknown={"legacy_note": "preserve"},
    )
    with Session(backend) as session:
        task = session.get(Task, "1")
        assert task is not None
        task.title = "B"
        session.add(task)
        session.commit()

    stored = backend.read(Task, backend.scan(Task)[0].locator)
    assert stored.unknown["legacy_note"] == "preserve"
    assert stored.values["title"] == "B"


def test_human_edit_conflict_on_commit() -> None:
    backend = MemoryBackend()
    stored = backend.seed_record(
        Task,
        {"task_id": "1", "status": "todo", "title": "A"},
        version="1",
    )
    with Session(backend) as session:
        task = materialize(Task, stored)
        session.add(task)
        backend.mutate_external(
            Task,
            "1",
            values={"title": "human"},
            bump_version=True,
        )
        with pytest.raises(ConflictError):
            session.commit()


def test_partial_commit_error_reports_progress() -> None:
    backend = MemoryBackend()
    backend.seed_record(Tag, {"id": "1", "name": "a"}, version="1")
    backend.seed_record(Tag, {"id": "2", "name": "b"}, version="1")

    with Session(backend) as session:
        one = session.get(Tag, "1")
        two = session.get(Tag, "2")
        assert one is not None
        assert two is not None
        one.name = "A"
        two.name = "B"
        session.add(one)
        session.add(two)
        backend.mutate_external(Tag, "2", values={"name": "human"}, bump_version=True)
        with pytest.raises(PartialCommitError) as excinfo:
            session.commit()
        err = excinfo.value
        assert len(err.completed) == 1
        assert len(err.pending) == 1


def test_read_only_session_blocks_writes() -> None:
    backend = MemoryBackend()
    with (
        Session(backend, read_only=True) as session,
        pytest.raises(ReadOnlySessionError),
    ):
        session.add(Task(id="1", status="todo", title="x"))


def test_result_helpers() -> None:
    backend = MemoryBackend()
    backend.seed_record(Task, {"task_id": "1", "status": "todo", "title": "A"})
    backend.seed_record(Task, {"task_id": "2", "status": "todo", "title": "B"})
    with Session(backend) as session:
        assert session.exec(select(Task).where(Task.id == "1")).one().id == "1"
        assert (
            session.exec(select(Task).where(Task.id == "missing")).one_or_none() is None
        )
        with pytest.raises(ResultError):
            session.exec(select(Task)).one_or_none()


def test_rollback_clears_uncommitted_binding() -> None:
    backend = MemoryBackend()
    with Session(backend) as session:
        task = Task(id="1", status="todo", title="x")
        session.add(task)
        session.rollback()
        assert task.gdm_is_persisted is False
