"""Tests for high-level GDM client."""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

from gdm.client import GDMClient
from gdm.google_api import ExecutingDriveService
from gdm.project import Project
from tests.fakes import FakeDrive, FakeSheetsService


def _drive_service(wrapper: FakeDrive) -> ExecutingDriveService:
    raw = MagicMock()
    raw.files.return_value = wrapper.files
    raw.permissions.return_value = MagicMock()
    raw.changes.return_value = MagicMock()
    return ExecutingDriveService(raw)


def test_gdm_client_create_wires_project() -> None:
    drive_wrapper = FakeDrive()
    root_id = "root-folder"
    drive_wrapper.files.files[root_id] = {
        "id": root_id,
        "name": "Root",
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [],
    }
    service = _drive_service(drive_wrapper)
    sheets = FakeSheetsService(
        [["task_id", "title", "_gdm_id", "_gdm_version", "_gdm_updated_at"]]
    )
    with (
        patch("gdm.client.build_credentials", return_value=MagicMock()),
        patch("gdm.client.build_drive_service", return_value=service),
        patch("gdm.client.build_sheets_service", return_value=sheets),
        patch("gdm.client.build_docs_service", return_value=MagicMock()),
        patch("gdm.client.build_slides_service", return_value=MagicMock()),
    ):
        client = GDMClient.create(root_folder_id=root_id)
    assert client.project.root_folder_id == root_id
    assert client.project.gdm_folder_id is not None


def test_gdm_client_session_uses_registry_backend() -> None:
    drive_wrapper = FakeDrive()
    root_id = "root"
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
    backend = MagicMock()
    backend.scan_limit = 100
    client = GDMClient(
        project=project,
        drive=MagicMock(),
        sheets=MagicMock(),
        registry_backend=backend,
        drive_service=MagicMock(),
    )
    session = client.session(scan_limit=50)
    assert session.backend is backend
