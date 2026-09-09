"""Registry backend tests for collection storage."""

from __future__ import annotations

import uuid

import pytest

from gdm.backends.drive import DriveBackend
from gdm.backends.registry import RegistryBackend
from gdm.backends.sheets import SheetsBackend
from gdm.collection.constants import FOLDER_MIME
from gdm.collection.metadata import InMemoryCollectionMetadataStore
from gdm.errors import UnsupportedOperationError
from gdm.fields import Field
from gdm.model import GDMModel
from gdm.project import Project, RegistryEntry, ResourceKind
from gdm.session import Session
from tests.collection_fakes import CollectionMemoryDriveService, seed_mixed_collection
from tests.fakes import FakeSheetsService


class DocAsset(GDMModel, storage="collection", mapping="partial"):
    drive_id: str = Field(drive="id")
    name: str = Field(drive="name")
    mime_type: str = Field(drive="mimeType")
    status: str | None = Field(default=None, metadata="status")


@pytest.fixture
def collection_registry() -> RegistryBackend:
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
        name="Docs",
        mime_type=FOLDER_MIME,
        parents=[root],
    )
    seed_mixed_collection(drive_service.files, folder)
    drive = DriveBackend(drive_service, project_id=str(project.project_id))
    sheets = SheetsBackend(FakeSheetsService([["_gdm_file_id", "_gdm_version"]]))
    metadata = InMemoryCollectionMetadataStore()
    backend = RegistryBackend(
        project=project,
        drive=drive,
        sheets=sheets,
        metadata_stores={DocAsset.gdm_model_name(): metadata},
    )
    backend.bind_model(
        DocAsset,
        RegistryEntry(
            model_name=DocAsset.gdm_model_name(),
            resource_id=folder,
            kind=ResourceKind.FOLDER,
        ),
    )
    return backend


def test_registry_collection_scan_and_inspect(
    collection_registry: RegistryBackend,
) -> None:
    info = collection_registry.inspect_schema(DocAsset)
    assert info.storage == "collection"
    records = collection_registry.scan(DocAsset)
    assert len(records) >= 5


def test_registry_collection_session_update(
    collection_registry: RegistryBackend,
) -> None:
    coll = collection_registry.collection(DocAsset)
    doc = coll.one_by_name("Brief")
    with Session(collection_registry) as session:
        doc.status = "review"
        session.add(doc)
        session.commit()
    reloaded = coll.get(doc.drive_id)
    assert reloaded.status == "review"


def test_scan_files_rejects_collection_models(
    collection_registry: RegistryBackend,
) -> None:
    with pytest.raises(UnsupportedOperationError, match="collection storage"):
        collection_registry._scan_files(DocAsset, limit=10)


def test_project_collection_metadata_binding(
    collection_registry: RegistryBackend,
) -> None:
    project = collection_registry.project
    sheet_id, worksheet = project.ensure_collection_metadata_binding(
        DocAsset.gdm_model_name()
    )
    assert sheet_id
    assert worksheet == "metadata"
