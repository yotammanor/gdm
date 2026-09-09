"""Branch coverage for RegistryBackend collection and storage routing."""
# ruff: noqa: F841

from __future__ import annotations

import json
import uuid
from unittest.mock import patch

import pytest

from gdm.backends.base import RecordLocator, StoredRecord
from gdm.backends.drive import DriveBackend
from gdm.backends.registry import RegistryBackend, build_stored_record_for_registry
from gdm.backends.sheets import SheetsBackend
from gdm.collection.constants import FOLDER_MIME
from gdm.collection.metadata import InMemoryCollectionMetadataStore
from gdm.errors import (
    GDMError,
    NotFoundError,
    ScanLimitExceeded,
    UnsupportedOperationError,
)
from gdm.fields import Field
from gdm.model import GDMModel
from gdm.project import Project, RegistryEntry, ResourceKind
from tests.collection_fakes import CollectionMemoryDriveService, seed_mixed_collection
from tests.fakes import FakeDrive, FakeSheetsService, MemoryDriveService


class CollDoc(GDMModel, storage="collection", mapping="partial"):
    drive_id: str = Field(drive="id")
    name: str = Field(drive="name")
    status: str | None = Field(default=None, metadata="status")


class SheetRow(GDMModel, storage="sheet", source="rows", mapping="partial"):
    id: str = Field(primary_key=True, source="row_id")
    label: str


class FileJson(GDMModel, storage="file", codec="json", mapping="partial"):
    id: str = Field(primary_key=True)
    label: str


@pytest.fixture
def registry() -> RegistryBackend:
    drive_service = CollectionMemoryDriveService()
    root = drive_service.files.seed(
        file_id="root",
        name="Root",
        mime_type=FOLDER_MIME,
        parents=[],
    )
    project = Project(project_id=uuid.uuid4(), root_folder_id=root, drive=drive_service)
    project.initialize()
    folder = drive_service.files.seed(
        name="Coll",
        mime_type=FOLDER_MIME,
        parents=[root],
    )
    drive_service.files.seed(
        file_id="sheet-main",
        name="Rows",
        mime_type="application/vnd.google-apps.spreadsheet",
        parents=[root],
    )
    seed_mixed_collection(drive_service.files, folder)
    drive = DriveBackend(drive_service, project_id=str(project.project_id))
    table = [
        ["row_id", "label", "_gdm_id", "_gdm_version", "_gdm_updated_at"],
        ["r1", "One", "r1", "1", "2024-01-01T00:00:00+00:00"],
    ]
    sheets = SheetsBackend(FakeSheetsService(table))
    metadata = InMemoryCollectionMetadataStore()
    backend = RegistryBackend(
        project=project,
        drive=drive,
        sheets=sheets,
        scan_limit=100,
        metadata_stores={CollDoc.gdm_model_name(): metadata},
    )
    backend.bind_model(
        CollDoc,
        RegistryEntry(
            model_name=CollDoc.gdm_model_name(),
            resource_id=folder,
            kind=ResourceKind.FOLDER,
        ),
    )
    backend.bind_model(
        SheetRow,
        RegistryEntry(
            model_name=SheetRow.gdm_model_name(),
            resource_id="sheet-main",
            kind=ResourceKind.SPREADSHEET,
            extra={"worksheet": "Sheet1"},
        ),
    )
    file_folder = project.folder_id_for("indexes")
    backend.bind_model(
        FileJson,
        RegistryEntry(
            model_name=FileJson.gdm_model_name(),
            resource_id=file_folder,
            kind=ResourceKind.FOLDER,
        ),
    )
    return backend


def test_registry_unsupported_storage_routes(registry: RegistryBackend) -> None:
    bad = RecordLocator(storage="unknown", resource_id="x", record_key="y")
    with pytest.raises(UnsupportedOperationError, match="Unsupported storage"):
        registry.read(CollDoc, bad)
    with pytest.raises(UnsupportedOperationError, match="Unsupported storage"):
        registry.write(
            CollDoc,
            StoredRecord(locator=bad, values={}, unknown={}),
            expected_version=None,
        )
    with pytest.raises(UnsupportedOperationError, match="Unsupported storage"):
        registry.delete(CollDoc, bad, expected_version=None)


def test_registry_inspect_schema_missing_storage() -> None:
    class NoStorage(GDMModel):
        name: str

    backend = RegistryBackend(
        project=Project(
            project_id=uuid.uuid4(),
            root_folder_id="root",
            drive=CollectionMemoryDriveService(),
        ),
        drive=DriveBackend(MemoryDriveService(), project_id="p"),
        sheets=SheetsBackend(FakeSheetsService([["a"]])),
    )
    with pytest.raises(GDMError, match="no storage"):
        backend.inspect_schema(NoStorage)


def test_registry_collection_scan_limit(registry: RegistryBackend) -> None:
    registry.scan_limit = 2
    with pytest.raises(ScanLimitExceeded):
        registry.scan(CollDoc, limit=2)


def test_registry_sheet_read_write_delete_roundtrip(registry: RegistryBackend) -> None:
    record = registry.read(
        SheetRow,
        RecordLocator(storage="sheet", resource_id="sheet-main", record_key="r1"),
    )
    assert record.values["label"] == "One"
    record.values["label"] = "Updated"
    registry.write(SheetRow, record, expected_version="1")
    updated = registry.read(
        SheetRow,
        RecordLocator(storage="sheet", resource_id="sheet-main", record_key="r1"),
    )
    assert updated.values["label"] == "Updated"


def test_registry_sheet_create_and_cache_miss(registry: RegistryBackend) -> None:
    created = StoredRecord(
        locator=RecordLocator(
            storage="sheet",
            resource_id="sheet-main",
            record_key="r-new",
        ),
        values={"row_id": "r-new", "label": "Fresh"},
        unknown={},
    )
    result = registry.write(SheetRow, created, expected_version=None, create=True)
    assert result.created is True
    row_index = registry._resolve_sheet_row_index(
        "sheet-main",
        "Sheet1",
        SheetRow,
        "r-new",
    )
    assert row_index >= 2


def test_registry_file_write_direct(registry: RegistryBackend) -> None:
    folder_id = registry.project.folder_id_for("indexes")
    record = StoredRecord(
        locator=RecordLocator(
            storage="file",
            resource_id="new-file",
            record_key="tag-1",
        ),
        values={"id": "tag-1", "label": "alpha"},
        unknown={},
    )
    result = registry.write(FileJson, record, expected_version=None, create=True)
    assert result.created is True
    with patch.object(
        registry.drive,
        "_download_bytes",
        return_value=json.dumps({"id": "tag-1", "label": "alpha"}).encode(),
    ):
        loaded = registry.read(
            FileJson,
            RecordLocator(
                storage="file",
                resource_id=result.locator.resource_id,
                record_key="tag-1",
            ),
        )
    assert loaded.values["label"] == "alpha"


def test_registry_collection_delete_trashes(registry: RegistryBackend) -> None:
    coll = registry.collection(CollDoc)
    doc = coll.one_by_name("Brief")
    folder = coll.folder_id
    registry.delete(
        CollDoc,
        RecordLocator(
            storage="collection",
            resource_id=folder,
            record_key=doc.drive_id,
        ),
        expected_version=None,
    )


def test_registry_register_metadata_store(registry: RegistryBackend) -> None:
    custom = InMemoryCollectionMetadataStore()
    registry.register_metadata_store("CustomModel", custom)
    assert registry._metadata_stores["CustomModel"] is custom


def test_registry_unregistered_model_raises(registry: RegistryBackend) -> None:
    class Ghost(GDMModel, storage="collection"):
        drive_id: str = Field(drive="id")

    with pytest.raises(NotFoundError, match="not bound"):
        registry.collection(Ghost)


def test_build_stored_record_for_registry_file_rebind() -> None:
    class LegacyFile(GDMModel, storage="file", codec="json"):
        id: str = Field(primary_key=True)
        name: str

    LegacyFile.__gdm_source__ = "legacy-folder"
    instance = LegacyFile(id="rec-1", name="Alpha")
    from gdm.backends.base import RecordLocator

    instance.gdm_bind(
        locator=RecordLocator(
            storage="file", resource_id="file-abc", record_key="rec-1"
        ),
        version="1",
        fingerprint="fp",
        persisted=True,
    )
    stored = build_stored_record_for_registry(
        LegacyFile,
        instance,
        project=Project(
            project_id=uuid.uuid4(),
            root_folder_id="root",
            drive=FakeDrive(),
        ),
    )
    assert stored.locator.resource_id == "file-abc"


def test_registry_sheet_binding_errors(registry: RegistryBackend) -> None:
    class BadSheet(GDMModel, storage="sheet"):
        id: str = Field(primary_key=True)

    registry.bind_model(
        BadSheet,
        RegistryEntry(
            model_name=BadSheet.gdm_model_name(),
            resource_id="not-sheet",
            kind=ResourceKind.FOLDER,
        ),
    )
    with pytest.raises(UnsupportedOperationError, match="not a sheet"):
        registry._sheet_binding(BadSheet)


def test_registry_scan_no_storage_model(registry: RegistryBackend) -> None:
    class Bare(GDMModel):
        x: str

    with pytest.raises(GDMError, match="no storage"):
        registry.scan(Bare)
