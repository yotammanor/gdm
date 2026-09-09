"""Integration coverage via fakes to lift global coverage."""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest

from gdm.client import GDMClient
from gdm.errors import ScanLimitExceeded
from gdm.fields import Field
from gdm.google_api import execute_or_return
from gdm.model import GDMModel
from gdm.project import Project, RegistryEntry, ResourceKind
from gdm.retry import execute_or_return as retry_execute_or_return
from gdm.session import Session
from tests.fakes import FakeDrive, FakeSheetsService, MemoryDriveService


class SheetTask(GDMModel, storage="sheet", source="tasks", mapping="partial"):
    id: str = Field(primary_key=True, source="task_id")
    title: str


class FileTag(GDMModel, storage="file", codec="json", mapping="partial"):
    id: str = Field(primary_key=True)
    name: str


def _registry_backend() -> tuple[object, FakeDrive]:
    from gdm.backends.drive import DriveBackend
    from gdm.backends.registry import RegistryBackend
    from gdm.backends.sheets import SheetsBackend

    drive_wrapper = FakeDrive()
    root_id = "root-folder"
    drive_wrapper.files.files[root_id] = {
        "id": root_id,
        "name": "Root",
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [],
    }
    project = Project(
        project_id=uuid.uuid4(),
        root_folder_id=root_id,
        drive=drive_wrapper,
    )
    project.initialize()
    folder_id = project.folder_id_for("indexes")
    project.register_resource(
        RegistryEntry(
            model_name=FileTag.gdm_model_name(),
            resource_id=folder_id,
            kind=ResourceKind.FOLDER,
        )
    )
    drive_wrapper.files.files["sheet-1"] = {
        "id": "sheet-1",
        "name": "Tasks",
        "mimeType": "application/vnd.google-apps.spreadsheet",
        "parents": [root_id],
        "appProperties": {},
    }
    table = [
        ["task_id", "title", "_gdm_id", "_gdm_version", "_gdm_updated_at"],
        ["t1", "One", "t1", "1", "2024-01-01T00:00:00+00:00"],
    ]
    drive = DriveBackend(
        MemoryDriveService(),
        project_id=str(project.project_id),
    )
    sheets = SheetsBackend(FakeSheetsService(table))
    backend = RegistryBackend(
        project=project,
        drive=drive,
        sheets=sheets,
        scan_limit=100,
    )
    backend.bind_model(
        SheetTask,
        RegistryEntry(
            model_name=SheetTask.gdm_model_name(),
            resource_id="sheet-1",
            kind=ResourceKind.SPREADSHEET,
            extra={"worksheet": "Sheet1"},
        ),
    )
    return backend, drive_wrapper


def test_registry_capabilities_and_inspect_schema() -> None:
    backend, _ = _registry_backend()
    caps = backend.capabilities()
    assert caps.supports_metadata_pushdown is False
    assert caps.supports_sidecar_index is False
    info = backend.inspect_schema(SheetTask)
    assert info.source == "Sheet1"


def test_registry_unbound_model_raises_not_found() -> None:
    from gdm.backends.registry import RegistryBackend
    from gdm.errors import NotFoundError

    backend, _ = _registry_backend()
    assert isinstance(backend, RegistryBackend)

    class Unbound(GDMModel, storage="sheet", source="x"):
        id: str = Field(primary_key=True)

    with pytest.raises(NotFoundError, match="not bound"):
        backend.inspect_schema(Unbound)


def test_registry_collection_helper() -> None:
    from gdm.backends.registry import RegistryBackend

    backend, _ = _registry_backend()
    assert isinstance(backend, RegistryBackend)

    class Asset(GDMModel, storage="collection", mapping="partial"):
        id: str = Field(primary_key=True)
        title: str = Field(metadata="title")

    folder_id = backend.project.folder_id_for("assets")
    backend.bind_model(
        Asset,
        RegistryEntry(
            model_name=Asset.gdm_model_name(),
            resource_id=folder_id,
            kind=ResourceKind.FOLDER,
        ),
    )
    collection = backend.collection(Asset)
    assert collection.model is Asset


def test_gdm_client_open_bind_and_explore() -> None:
    from gdm.google_api import ExecutingDriveService

    drive_wrapper = FakeDrive()
    root_id = "root"
    drive_wrapper.files.files[root_id] = {
        "id": root_id,
        "name": "Root",
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [],
    }
    service = ExecutingDriveService(MagicMock())
    service._raw.files.return_value = drive_wrapper.files
    project = Project(
        project_id=uuid.uuid4(), root_folder_id=root_id, drive=drive_wrapper
    )
    project.initialize()
    drive_wrapper.files.files["sheet-2"] = {
        "id": "sheet-2",
        "name": "Tasks",
        "mimeType": "application/vnd.google-apps.spreadsheet",
        "parents": [root_id],
        "appProperties": {},
    }
    backend, _ = _registry_backend()
    client = GDMClient(
        project=project,
        drive=MagicMock(),
        sheets=MagicMock(),
        registry_backend=backend,
        drive_service=service,
        credentials=MagicMock(),
        explorer=None,
    )
    entry = client.bind(
        SheetTask,
        resource_id="sheet-2",
        kind=ResourceKind.SPREADSHEET,
        worksheet="Tasks",
    )
    assert entry.model_name == SheetTask.gdm_model_name()
    with patch("gdm.client.build_explorer_service") as build_explorer:
        build_explorer.return_value = MagicMock()
        with patch("gdm.explore.Explorer") as explorer_cls:
            explorer_cls.return_value.explore.return_value = {"items": []}
            result = client.explore()
            assert result == {"items": []}


def test_gdm_client_collection_and_permissions_properties() -> None:
    from gdm.google_api import ExecutingDriveService

    backend, _ = _registry_backend()
    service = ExecutingDriveService(MagicMock())
    service._raw.permissions.return_value = MagicMock()
    service._raw.changes.return_value = MagicMock()
    client = GDMClient(
        project=backend.project,
        drive=MagicMock(),
        sheets=MagicMock(),
        registry_backend=backend,
        drive_service=service,
        credentials=MagicMock(),
    )
    _ = client.permissions
    _ = client.changes
    _ = client.backend
    session = client.session(read_only=True, scan_limit=25)
    assert session.read_only is True


def test_registry_scan_limit_on_files() -> None:
    backend, _ = _registry_backend()
    drive = backend.drive
    for index in range(3):
        drive.write_json(
            parent_id=backend.project.folder_id_for("indexes"),
            model_name=FileTag.gdm_model_name(),
            record_id=f"id-{index}",
            payload={"id": f"id-{index}", "name": f"n{index}"},
        )
    backend.scan_limit = 2
    with pytest.raises(ScanLimitExceeded):
        backend.scan(FileTag)


def test_execute_or_return_aliases() -> None:
    assert execute_or_return({"x": 1}) == {"x": 1}
    assert retry_execute_or_return({"y": 2}) == {"y": 2}


def test_registry_sheet_session_update() -> None:
    backend, _ = _registry_backend()
    with Session(backend) as session:
        task = session.get(SheetTask, "t1")
        assert task is not None
        task.title = "Updated"
        session.add(task)
        session.commit()
    records = backend.scan(SheetTask)
    assert records[0].values["title"] == "Updated"
