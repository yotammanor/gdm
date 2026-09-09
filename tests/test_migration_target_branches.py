"""Branch coverage for production migration target edge paths."""

from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gdm.backends.drive import DriveBackend
from gdm.backends.registry import RegistryBackend
from gdm.backends.sheets import SheetsBackend
from gdm.client import GDMClient
from gdm.errors import GDMError, NotFoundError
from gdm.google_api import ExecutingDriveService
from gdm.migration_target import (
    ProductionMigrationTarget,
    build_production_migration_target,
)
from gdm.project import Project, RegistryEntry, ResourceKind
from tests.fakes import FakeDrive, FakeSheetsService


def _setup_client(
    tmp_path: Path,
    *,
    table: list[list[str]] | None = None,
) -> tuple[GDMClient, ProductionMigrationTarget, FakeDrive]:
    drive_wrapper = FakeDrive()
    root_id = "root-folder"
    drive_wrapper.files.files[root_id] = {
        "id": root_id,
        "name": "Root",
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [],
        "appProperties": {},
    }
    project = Project(
        project_id=uuid.uuid4(),
        root_folder_id=root_id,
        drive=drive_wrapper,
    )
    project.initialize()
    drive_wrapper.files.files["folder-1"] = {
        "id": "folder-1",
        "name": "Assets",
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [root_id],
        "appProperties": {},
    }
    sheets = SheetsBackend(FakeSheetsService(table or []))
    raw = MagicMock()
    raw.files.return_value = drive_wrapper.files
    drive_service = ExecutingDriveService(raw)
    drive = DriveBackend(drive_service, project_id=str(project.project_id))
    registry = RegistryBackend(project=project, drive=drive, sheets=sheets)
    client = GDMClient(
        project=project,
        drive=drive,
        sheets=sheets,
        registry_backend=registry,
        drive_service=drive_service,
    )
    gdm_dir = tmp_path / ".gdm"
    gdm_dir.mkdir(parents=True)
    target = build_production_migration_target(
        client,
        indexes_dir=str(gdm_dir / "indexes"),
        checkpoint_dir=gdm_dir / "migrations",
    )
    return client, target, drive_wrapper


def test_resource_id_from_registry_when_not_in_bindings(tmp_path: Path) -> None:
    client, target, _drive = _setup_client(tmp_path)
    client.project.register_resource(
        RegistryEntry(
            model_name="Article",
            resource_id="sheet-9",
            kind=ResourceKind.SPREADSHEET,
        )
    )
    assert target.resource_id_for_model("Article") == "sheet-9"


def test_registry_entry_missing_raises(tmp_path: Path) -> None:
    _client, target, _drive = _setup_client(tmp_path)
    with pytest.raises(NotFoundError, match="not registered"):
        target._registry_entry("Missing")


def test_sheet_add_column_on_empty_table(tmp_path: Path) -> None:
    client, target, _drive = _setup_client(tmp_path, table=[])
    client.project.register_resource(
        RegistryEntry(
            model_name="Article",
            resource_id="empty-sheet",
            kind=ResourceKind.SPREADSHEET,
            extra={"worksheet": "Sheet1"},
        )
    )
    target.bindings["Article"] = "empty-sheet"
    changed = target.sheet_add_column("empty-sheet", "title", default="")
    assert changed == 0
    assert target.sheet_headers("empty-sheet") == ["title"]


def test_sheet_rename_noop_branches(tmp_path: Path) -> None:
    _client, target, _drive = _setup_client(
        tmp_path,
        table=[["title", "Title"], ["Hello", "x"]],
    )
    _client.project.register_resource(
        RegistryEntry(
            model_name="Article",
            resource_id="sheet-1",
            kind=ResourceKind.SPREADSHEET,
        )
    )
    target.bindings["Article"] = "sheet-1"
    assert target.sheet_rename_column("sheet-1", "missing", "x") == 0
    assert target.sheet_rename_column("sheet-1", "title", "Title") == 0


def test_sheet_transform_missing_column_and_row_errors(tmp_path: Path) -> None:
    _client, target, _drive = _setup_client(
        tmp_path,
        table=[["title"], ["Hello"], ["bad"]],
    )
    _client.project.register_resource(
        RegistryEntry(
            model_name="Article",
            resource_id="sheet-1",
            kind=ResourceKind.SPREADSHEET,
        )
    )
    target.bindings["Article"] = "sheet-1"
    changed, errors = target.sheet_transform_column("sheet-1", "missing", lambda v: v)
    assert changed == 0
    assert errors

    def boom(value: object) -> object:
        if value == "bad":
            raise ValueError("nope")
        return value

    changed, errors = target.sheet_transform_column("sheet-1", "title", boom)
    assert changed == 0
    assert any("nope" in err for err in errors)


def test_file_move_noop_when_already_in_parent(tmp_path: Path) -> None:
    _client, target, drive_wrapper = _setup_client(tmp_path)
    drive_wrapper.files.files["f1"] = {
        "id": "f1",
        "name": "doc",
        "mimeType": "text/plain",
        "parents": ["dest"],
        "appProperties": {},
    }
    before = len(target._write_log)
    target.file_move("f1", "dest")
    assert len(target._write_log) == before


def test_file_retag_noop_when_unchanged(tmp_path: Path) -> None:
    client, target, drive_wrapper = _setup_client(tmp_path)
    project_id = str(client.project.project_id)
    drive_wrapper.files.files["f1"] = {
        "id": "f1",
        "name": "doc",
        "mimeType": "text/plain",
        "parents": ["dest"],
        "appProperties": {
            "gdm_project": project_id,
            "gdm_model": "Asset",
            "gdm_schema": "1",
        },
    }
    before = len(target._write_log)
    target.file_retag("f1", gdm_model="Asset", gdm_schema="1")
    assert len(target._write_log) == before


def test_bind_collection_updates_collection_path_only(tmp_path: Path) -> None:
    client, target, _drive = _setup_client(tmp_path)
    target.bind_collection("Asset", folder_id="folder-1", collection_path="Assets")
    before = len(target._write_log)
    target.bind_collection("Asset", folder_id="folder-1", collection_path="Assets/v2")
    assert len(target._write_log) == before + 1
    entry = client.project.registry.get("Asset")
    assert entry is not None
    assert entry.extra.get("collection_path") == "Assets/v2"


def test_unbind_collection_wrong_folder_is_noop(tmp_path: Path) -> None:
    client, target, _drive = _setup_client(tmp_path)
    target.bind_collection("Asset", folder_id="folder-1", collection_path="Assets")
    before = len(client.project.registry.entries)
    target.unbind_collection("Asset", folder_id="other-folder")
    assert len(client.project.registry.entries) == before


def test_checkpoint_without_dir_is_noop(tmp_path: Path) -> None:
    _client, target, _drive = _setup_client(tmp_path)
    target.checkpoint_dir = None
    from gdm.migrations import MigrationCheckpoint

    target.save_checkpoint(MigrationCheckpoint(plan_id="p", model_name="M"))
    assert target.load_checkpoint("M") is None


def test_metadata_transform_and_drop(tmp_path: Path) -> None:
    _client, target, _drive = _setup_client(tmp_path)
    target.bind_collection("Asset", folder_id="folder-1", collection_path="Assets")
    store = target._metadata_store("Asset")
    store._header = ["_gdm_file_id", "_gdm_version", "tag"]
    store._rows = {
        "file-a": (
            2,
            {"_gdm_file_id": "file-a", "_gdm_version": "1", "tag": "old"},
            "1",
        ),
    }
    changed, errors = target.metadata_transform_column(
        "Asset",
        "tag",
        lambda value: str(value).upper(),
    )
    assert changed == 1
    assert not errors
    dropped = target.metadata_drop_column("Asset", "tag")
    assert dropped == 1


def test_metadata_transform_missing_column(tmp_path: Path) -> None:
    _client, target, _drive = _setup_client(tmp_path)
    target.bind_collection("Asset", folder_id="folder-1", collection_path="Assets")
    store = target._metadata_store("Asset")
    store._header = ["_gdm_file_id", "_gdm_version"]
    store._rows = {}
    changed, errors = target.metadata_transform_column(
        "Asset",
        "missing",
        lambda value: value,
    )
    assert changed == 0
    assert errors


def test_rebuild_index_with_schema_field_lookup(tmp_path: Path) -> None:
    client, target, _drive = _setup_client(
        tmp_path,
        table=[
            ["email", "_gdm_id", "_gdm_version", "_gdm_updated_at"],
            ["a@example.com", "r1", "1", "2024-01-01"],
        ],
    )
    from gdm import Field, GDMModel

    class Indexed(GDMModel, storage="sheet", mapping="partial"):
        email: str = Field(source="email", index=True)

    client.project.register_resource(
        RegistryEntry(
            model_name=Indexed.gdm_model_name(),
            resource_id="sheet-1",
            kind=ResourceKind.SPREADSHEET,
            extra={"worksheet": "Sheet1"},
        )
    )
    target.bindings[Indexed.gdm_model_name()] = "sheet-1"

    def _lookup(_model: str, field: str) -> str | None:
        return "email" if field == "email" else None

    target.schema_field_lookup = _lookup
    count = target.rebuild_index(Indexed.gdm_model_name(), "email")
    assert count >= 1


def test_model_for_resource_missing_binding_raises(tmp_path: Path) -> None:
    _client, target, _drive = _setup_client(tmp_path)
    with pytest.raises(GDMError, match="No model binding"):
        target._model_for_resource("unknown-resource")
