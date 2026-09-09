"""Client collection API tests."""

from __future__ import annotations

import uuid

from gdm.backends.drive import DriveBackend
from gdm.backends.registry import RegistryBackend
from gdm.backends.sheets import SheetsBackend
from gdm.client import GDMClient
from gdm.collection.constants import FOLDER_MIME
from gdm.fields import Field
from gdm.model import GDMModel
from gdm.project import Project, RegistryEntry, ResourceKind
from tests.collection_fakes import CollectionMemoryDriveService
from tests.fakes import FakeSheetsService


class Note(GDMModel, storage="collection"):
    drive_id: str = Field(drive="id")
    name: str = Field(drive="name")
    body: str | None = Field(default=None, metadata="body")


def test_client_collection_factory() -> None:
    drive_service = CollectionMemoryDriveService()
    root = drive_service.files.seed(
        file_id="root",
        name="Root",
        mime_type=FOLDER_MIME,
        parents=[],
    )
    folder = drive_service.files.seed(
        name="Notes",
        mime_type=FOLDER_MIME,
        parents=[root],
    )
    project = Project(project_id=uuid.uuid4(), root_folder_id=root, drive=drive_service)
    project.initialize()
    drive = DriveBackend(drive_service, project_id=str(project.project_id))
    sheets = SheetsBackend(FakeSheetsService([["_gdm_file_id", "_gdm_version"]]))
    backend = RegistryBackend(project=project, drive=drive, sheets=sheets)
    backend.bind_model(
        Note,
        RegistryEntry(
            model_name=Note.gdm_model_name(),
            resource_id=folder,
            kind=ResourceKind.FOLDER,
        ),
    )
    client = GDMClient(
        project=project,
        drive=drive,
        sheets=sheets,
        registry_backend=backend,
        drive_service=drive_service,  # type: ignore[arg-type]
    )
    coll = client.collection(Note)
    created = coll.create_native("Hello", kind="document")
    assert created.name == "Hello"
