"""Branch coverage for gdm.backends.memory."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gdm.backends.base import RecordLocator, StoredRecord
from gdm.backends.memory import MemoryBackend, build_stored_record, materialize
from gdm.errors import ConflictError, GDMError
from gdm.fields import Field
from gdm.model import SYSTEM_ID_FIELD, GDMModel


class Plain(GDMModel):
    title: str


class Encoded(GDMModel, storage="file", codec="json", source="encoded"):
    id: str = Field(primary_key=True)
    body: str


class SheetRow(GDMModel, storage="sheet", source="sheet-1"):
    id: str = Field(primary_key=True, source="row_id")
    value: str


class Coll(GDMModel, storage="collection"):
    id: str = Field(primary_key=True)
    title: str = Field(metadata="title")
    drive_id: str | None = Field(default=None, drive="id")


def test_capabilities_and_inspect_schema() -> None:
    backend = MemoryBackend()
    caps = backend.capabilities()
    assert caps.supports_scan is True
    info = backend.inspect_schema(SheetRow)
    assert info.model_name == SheetRow.gdm_model_name()
    assert info.storage == "sheet"


def test_inspect_and_ensure_resource_without_storage() -> None:
    backend = MemoryBackend()
    with pytest.raises(GDMError, match="no storage"):
        backend.inspect_schema(Plain)
    with pytest.raises(GDMError, match="no storage"):
        backend.ensure_resource(Plain)


def test_legacy_file_codec_uses_file_resource_storage() -> None:
    backend = MemoryBackend()
    resource = backend.ensure_resource(Encoded)
    assert resource.storage == "file"


def test_collection_resource_storage_kind() -> None:
    backend = MemoryBackend()
    resource = backend.ensure_resource(Coll)
    assert resource.storage == "collection"


def test_read_missing_record_raises() -> None:
    backend = MemoryBackend()
    stored = backend.seed_record(Encoded, {"id": "1", "body": "x"})
    backend._resources[
        (stored.locator.storage, stored.locator.resource_id)
    ].records.clear()
    with pytest.raises(GDMError, match="not found"):
        backend.read(Encoded, stored.locator)


def test_get_resource_missing_raises() -> None:
    backend = MemoryBackend()
    locator = RecordLocator(storage="file", resource_id="missing", record_key="1")
    with pytest.raises(GDMError, match="Resource"):
        backend.delete(Encoded, locator, expected_version=None)


def test_write_create_when_exists_raises() -> None:
    backend = MemoryBackend()
    stored = backend.seed_record(Encoded, {"id": "1", "body": "x"})
    with pytest.raises(GDMError, match="already exists"):
        backend.write(Encoded, stored, expected_version=None, create=True)


def test_write_update_when_missing_raises() -> None:
    backend = MemoryBackend()
    locator = RecordLocator(storage="file", resource_id="encoded", record_key="ghost")
    record = StoredRecord(locator=locator, values={"id": "ghost", "body": "x"})
    with pytest.raises(GDMError, match="not found"):
        backend.write(Encoded, record, expected_version=None, create=False)


def test_delete_conflict_on_stale_version() -> None:
    backend = MemoryBackend()
    stored = backend.seed_record(Encoded, {"id": "1", "body": "x"}, version="1")
    backend.mutate_external(Encoded, "1", bump_version=True)
    with pytest.raises(ConflictError):
        backend.delete(Encoded, stored.locator, expected_version="1")


def test_scan_respects_limit_and_sorting() -> None:
    backend = MemoryBackend()
    backend.seed_record(Encoded, {"id": "b", "body": "2"})
    backend.seed_record(Encoded, {"id": "a", "body": "1"})
    limited = backend.scan(Encoded, limit=1)
    assert len(limited) == 1
    full = backend.scan(Encoded)
    assert [r.locator.record_key for r in full] == ["a", "b"]


def test_mutate_external_without_bump() -> None:
    backend = MemoryBackend()
    backend.seed_record(Encoded, {"id": "1", "body": "v1"}, version="5")
    updated = backend.mutate_external(
        Encoded,
        "1",
        values={"body": "v2"},
        unknown={"legacy": "keep"},
        bump_version=False,
    )
    assert updated.values["body"] == "v2"
    assert updated.version == "5"
    assert updated.unknown["legacy"] == "keep"


def test_derive_record_key_from_source_and_system_id() -> None:
    backend = MemoryBackend()
    key_from_source = backend._derive_record_key(
        SheetRow,
        {"row_id": "rk", "value": "v"},
    )
    assert key_from_source == "rk"
    key_from_system = backend._derive_record_key(
        SheetRow,
        {SYSTEM_ID_FIELD: "sys", "value": "v"},
    )
    assert key_from_system == "sys"


def test_build_stored_record_collection_includes_drive_fields() -> None:
    item = Coll(id="1", title="hello", drive_id="drive-1")
    item.gdm_bind_collection(
        locator=RecordLocator(
            storage="collection", resource_id="folder", record_key="1"
        ),
        drive_version="1",
        drive_fingerprint="fp",
        metadata_version="1",
        metadata_fingerprint="mfp",
        persisted=False,
    )
    stored = build_stored_record(Coll, item)
    assert stored.values["drive_id"] == "drive-1"
    assert stored.values["title"] == "hello"


def test_build_stored_record_sheet_sets_system_fields() -> None:
    row = SheetRow(id="1", value="v")
    row.gdm_bind(
        locator=RecordLocator(storage="sheet", resource_id="sheet-1", record_key="1"),
        version="7",
        fingerprint="fp",
        persisted=False,
    )
    stored = build_stored_record(SheetRow, row)
    assert stored.values[SYSTEM_ID_FIELD] == "1"
    assert stored.values["_gdm_version"] == "7"


def test_build_stored_record_without_storage_raises() -> None:
    plain = Plain(title="x")
    with pytest.raises(GDMError, match="no storage"):
        build_stored_record(Plain, plain)


def test_materialize_collection_binds_state() -> None:
    backend = MemoryBackend()
    stored = backend.seed_record(Coll, {"id": "1", "title": "T"})
    instance = materialize(Coll, stored, collection_backend=MagicMock())
    assert instance.gdm_is_persisted
    assert instance.gdm_locator == stored.locator


def test_seed_record_generates_uuid_without_pk() -> None:
    class Loose(GDMModel, storage="sheet", source="loose"):
        value: str

    backend = MemoryBackend()
    stored = backend.seed_record(Loose, {"value": "only"})
    assert stored.locator.record_key
