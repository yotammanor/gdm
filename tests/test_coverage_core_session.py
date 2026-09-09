"""Branch coverage for gdm.session and Result helpers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from gdm.backends.base import RecordLocator, WriteResult
from gdm.backends.memory import MemoryBackend, materialize
from gdm.errors import (
    ConflictError,
    GDMError,
    PartialCommitError,
    ReadOnlySessionError,
    ResultError,
)
from gdm.fields import Field
from gdm.model import GDMModel
from gdm.session import Result, Session


class SheetRow(GDMModel, storage="sheet", source="rows", mapping="partial"):
    id: str = Field(primary_key=True, source="row_id")
    value: str


class JsonDoc(GDMModel, storage="file", codec="json", source="docs"):
    id: str = Field(primary_key=True)
    body: str


class CollectionItem(GDMModel, storage="collection", mapping="partial"):
    id: str = Field(primary_key=True)
    title: str = Field(metadata="title")


def test_result_first_and_one_errors() -> None:
    assert Result([]).first() is None
    with pytest.raises(ResultError, match="Expected one"):
        Result([]).one()
    with pytest.raises(ResultError, match="Expected one"):
        Result([1, 2]).one()


def test_session_exit_rolls_back_on_exception() -> None:
    backend = MemoryBackend()

    def _boom() -> None:
        with Session(backend) as session:
            session.add(SheetRow(id="1", value="a"))
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        _boom()
    assert not backend.scan(SheetRow)


def test_session_exit_success_does_not_commit() -> None:
    backend = MemoryBackend()
    with Session(backend) as session:
        session.add(SheetRow(id="1", value="a"))
    assert not backend.scan(SheetRow)


def test_refresh_requires_locator() -> None:
    backend = MemoryBackend()
    with Session(backend) as session:
        row = SheetRow(id="1", value="a")
        with pytest.raises(GDMError, match="Cannot refresh"):
            session.refresh(row)


def test_refresh_reloads_persisted_instance() -> None:
    backend = MemoryBackend()
    stored = backend.seed_record(
        SheetRow,
        {"row_id": "1", "value": "before"},
        version="1",
    )
    backend.mutate_external(SheetRow, "1", values={"value": "after"})
    instance = materialize(SheetRow, stored)
    with Session(backend) as session:
        session.add(instance)
        session.refresh(instance)
    assert instance.value == "after"


def test_get_scan_limit_exceeded() -> None:
    backend = MemoryBackend()
    for index in range(4):
        backend.seed_record(SheetRow, {"row_id": str(index), "value": "x"})
    with Session(backend, scan_limit=3) as session:
        with pytest.raises(Exception, match="Scan limit"):
            session.get(SheetRow, "3")


def test_prefetch_empty_keys_returns_empty() -> None:
    backend = MemoryBackend()
    with Session(backend) as session:
        assert session.prefetch(SheetRow, []) == {}
        assert session.prefetch(SheetRow, ["", "  "]) == {}


def test_backend_scan_limit_property_used() -> None:
    backend = MemoryBackend()
    backend.scan_limit = 2  # type: ignore[attr-defined]
    for index in range(4):
        backend.seed_record(SheetRow, {"row_id": str(index), "value": "x"})
    with Session(backend) as session:
        with pytest.raises(Exception, match="Scan limit"):
            session.get(SheetRow, "3")


def test_add_all_tracks_multiple_instances() -> None:
    backend = MemoryBackend()
    with Session(backend) as session:
        session.add_all(
            [
                SheetRow(id="1", value="a"),
                SheetRow(id="2", value="b"),
            ],
        )
        session.commit()
    assert len(backend.scan(SheetRow)) == 2


def test_partial_commit_first_conflict_reraises_without_wrapper() -> None:
    backend = MemoryBackend()
    stored = backend.seed_record(
        JsonDoc,
        {"id": "1", "body": "v1"},
        version="1",
    )
    with Session(backend) as session:
        doc = materialize(JsonDoc, stored)
        session.add(doc)
        backend.mutate_external(
            JsonDoc, "1", values={"body": "external"}, bump_version=True
        )
        with pytest.raises(ConflictError):
            session.commit()


def test_partial_commit_non_conflict_wraps_generic_error() -> None:
    backend = MemoryBackend()
    stored = backend.seed_record(JsonDoc, {"id": "1", "body": "x"}, version="1")
    with Session(backend) as session:
        doc = materialize(JsonDoc, stored)
        session.add(doc)
        session.add(JsonDoc(id="2", body="new"))

        def boom(*_args: object, **_kwargs: object) -> WriteResult:
            raise RuntimeError("write failed")

        backend.write = boom  # type: ignore[method-assign]
        with pytest.raises(PartialCommitError) as excinfo:
            session.commit()
        assert excinfo.value.cause is not None


def test_commit_delete_invalid_locator_raises() -> None:
    backend = MemoryBackend()
    session = Session(backend)
    doc = JsonDoc(id="1", body="x")
    state = session._track(doc, persisted=False)
    operation = MagicMock()
    operation.action = "delete"
    operation.model_name = JsonDoc.gdm_model_name()
    operation.locator = "not-a-locator"
    with patch.object(session, "_state_for_operation", return_value=state):
        with pytest.raises(GDMError, match="record locator"):
            session._commit_delete(operation)


def test_state_for_operation_missing_instance() -> None:
    backend = MemoryBackend()
    session = Session(backend)
    op = MagicMock()
    op.model_name = "Missing"
    op.locator = RecordLocator(storage="file", resource_id="r", record_key="k")
    op.action = "update"
    with pytest.raises(GDMError, match="No tracked instance"):
        session._state_for_operation(op)


def test_refresh_collection_model_with_inventory() -> None:
    from unittest.mock import patch

    from gdm.collection.backend import CollectionBackend
    from tests.fakes import FakeDrive

    drive_wrapper = FakeDrive()
    collection_backend = CollectionBackend(
        drive_wrapper,
        project_id="proj-1",
    )
    backend = MemoryBackend()
    stored = backend.seed_record(
        CollectionItem,
        {"id": "c1", "title": "First"},
        version="1",
    )
    inventory_item = MagicMock()

    class CollectionMemoryBackend(MemoryBackend):
        _collection = collection_backend

    coll_backend = CollectionMemoryBackend()
    coll_backend._resources = backend._resources
    coll_backend.mutate_external(
        CollectionItem,
        "c1",
        values={"title": "Updated"},
    )
    instance = materialize(
        CollectionItem, stored, collection_backend=collection_backend
    )
    with patch.object(
        collection_backend,
        "inventory",
        return_value=MagicMock(get=lambda _key: inventory_item),
    ):
        with Session(coll_backend) as session:
            session.add(instance)
            session.refresh(instance)
    assert instance.title == "Updated"


def test_read_only_blocks_delete() -> None:
    backend = MemoryBackend()
    stored = backend.seed_record(SheetRow, {"row_id": "1", "value": "x"})
    doc = materialize(SheetRow, stored)
    with Session(backend, read_only=True) as session:
        with pytest.raises(ReadOnlySessionError):
            session.delete(doc)


def test_create_operation_matches_unbound_instance() -> None:
    backend = MemoryBackend()
    with Session(backend) as session:
        row = SheetRow(id="new-id", value="created")
        session.add(row)
        session.commit()
    stored = backend.scan(SheetRow)[0]
    assert stored.locator.record_key == "new-id"


def test_update_tracks_existing_snapshot_version() -> None:
    backend = MemoryBackend()
    stored = backend.seed_record(
        SheetRow,
        {"row_id": "1", "value": "v1"},
        version="3",
    )
    with Session(backend) as session:
        row = materialize(SheetRow, stored)
        session.add(row)
        row.value = "v2"
        session.add(row)
        pending = session._pending_operations()
        assert any(op.action == "update" for op in pending)
        session.commit()
    assert backend.read(SheetRow, stored.locator).values["value"] == "v2"


def test_delete_untracked_without_locator_skipped() -> None:
    backend = MemoryBackend()
    session = Session(backend)
    orphan = SheetRow(id="ghost", value="x")
    session._track(orphan, persisted=False)
    session._tracked[id(orphan)].deleted = True
    ops = session._pending_operations()
    assert ops == []
