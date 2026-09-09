"""Tests for MemoryBackend."""

from __future__ import annotations

import pytest

from gdm.backends.base import RecordLocator
from gdm.backends.memory import MemoryBackend, materialize
from gdm.errors import ConflictError, GDMError
from gdm.fields import Field
from gdm.model import GDMModel


class Note(GDMModel, storage="file", codec="json", source="notes-folder"):
    id: str = Field(primary_key=True)
    body: str


class Row(GDMModel, storage="sheet", source="sheet-1", mapping="partial"):
    id: str = Field(primary_key=True, source="row_id")
    value: str


def test_seed_scan_and_read() -> None:
    backend = MemoryBackend()
    stored = backend.seed_record(Note, {"id": "n1", "body": "hello"})
    scanned = backend.scan(Note)
    assert len(scanned) == 1
    assert backend.read(Note, stored.locator).values["body"] == "hello"


def test_write_increments_version_and_preserves_unknown() -> None:
    backend = MemoryBackend()
    stored = backend.seed_record(
        Row,
        {"row_id": "1", "value": "a"},
        unknown={"extra_col": "keep"},
    )
    from gdm.backends.base import StoredRecord

    instance = materialize(Row, stored)
    instance.value = "b"
    result = backend.write(
        Row,
        StoredRecord(
            locator=stored.locator,
            values=instance.gdm_to_source(include_unknown=False),
            unknown={"extra_col": "keep"},
            version=stored.version,
            fingerprint=stored.fingerprint,
        ),
        expected_version="1",
    )
    reloaded = backend.read(Row, stored.locator)
    assert reloaded.values["value"] == "b"
    assert reloaded.unknown["extra_col"] == "keep"
    assert int(reloaded.version or "0") == 2
    assert result.version == "2"


def test_conflict_on_stale_version() -> None:
    backend = MemoryBackend()
    stored = backend.seed_record(Note, {"id": "1", "body": "v1"}, version="1")
    backend.mutate_external(Note, "1", values={"body": "human edit"}, bump_version=True)
    with pytest.raises(ConflictError) as excinfo:
        backend.write(
            Note,
            stored,
            expected_version="1",
        )
    assert excinfo.value.expected_version == "1"
    assert excinfo.value.actual_version == "2"


def test_delete_requires_existing_record() -> None:
    backend = MemoryBackend()
    locator = RecordLocator(storage="file", resource_id="missing", record_key="x")
    with pytest.raises(GDMError):
        backend.delete(Note, locator, expected_version=None)


def test_operation_log_records_mutations() -> None:
    backend = MemoryBackend()
    stored = backend.seed_record(Note, {"id": "1", "body": "x"})
    backend.delete(Note, stored.locator, expected_version="1")
    actions = [entry.action for entry in backend.operation_log]
    assert actions == ["delete"]
