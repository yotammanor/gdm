"""Tests for registry-backed project backend."""

from __future__ import annotations

import uuid

import pytest

from gdm.backends.drive import DriveBackend
from gdm.backends.memory import MemoryBackend
from gdm.backends.registry import RegistryBackend
from gdm.backends.sheets import SheetsBackend
from gdm.errors import ConflictError, ScanLimitExceeded
from gdm.fields import Field
from gdm.model import GDMModel
from gdm.project import Project, RegistryEntry, ResourceKind
from gdm.session import Session
from tests.fakes import FakeDrive, FakeSheetsService, MemoryDriveService


class SheetTask(GDMModel, storage="sheet", source="tasks", mapping="partial"):
    id: str = Field(primary_key=True, source="task_id")
    title: str


class FileTag(GDMModel, storage="file", codec="json", mapping="partial"):
    id: str = Field(primary_key=True)
    name: str


@pytest.fixture
def registry_backend() -> RegistryBackend:
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
    return backend


def test_registry_sheet_scan_and_session_roundtrip(
    registry_backend: RegistryBackend,
) -> None:
    records = registry_backend.scan(SheetTask)
    assert len(records) == 1
    assert records[0].values["title"] == "One"
    with Session(registry_backend) as session:
        task = session.get(SheetTask, "t1")
        assert task is not None
        assert task.title == "One"


def test_registry_sheet_delete_rejects_stale_row(
    registry_backend: RegistryBackend,
) -> None:
    with Session(registry_backend) as session:
        task = session.get(SheetTask, "t1")
        assert task is not None
        registry_backend.sheets.values.table[1][3] = "2"
        session.delete(task)
        with pytest.raises(ConflictError, match="Conflict deleting row"):
            session.commit()
    assert len(registry_backend.sheets.values.table) == 2


def test_registry_file_write_read(registry_backend: RegistryBackend) -> None:
    from unittest.mock import patch

    payload = {"id": "tag-1", "name": "alpha"}
    with (
        patch.object(
            registry_backend.drive,
            "_download_bytes",
            return_value=__import__("json").dumps(payload).encode(),
        ),
        Session(registry_backend) as session,
    ):
        tag = FileTag(id="tag-1", name="alpha")
        session.add(tag)
        session.commit()
        loaded = session.get(FileTag, "tag-1")
        assert loaded is not None
        assert loaded.name == "alpha"


def test_scan_limit_exceeded_on_drive_scan(registry_backend: RegistryBackend) -> None:
    drive = registry_backend.drive
    for index in range(3):
        drive.write_json(
            parent_id=registry_backend.project.folder_id_for("indexes"),
            model_name=FileTag.gdm_model_name(),
            record_id=f"id-{index}",
            payload={"id": f"id-{index}", "name": f"n{index}"},
        )
    registry_backend.scan_limit = 2
    with pytest.raises(ScanLimitExceeded):
        registry_backend.scan(FileTag)


def test_optional_primary_key_gets_uuid_on_add() -> None:
    class Task(GDMModel, storage="sheet", source="tasks"):
        id: str | None = Field(default=None, primary_key=True)
        title: str

    backend = MemoryBackend()
    with Session(backend) as session:
        task = Task(title="Ship")
        session.add(task)
        assert task.id is not None
        session.commit()
    stored = backend.scan(Task)[0]
    assert stored.locator.record_key == task.id
