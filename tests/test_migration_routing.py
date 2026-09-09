"""Planner → engine → production target routing for migrations."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from gdm import Field, GDMModel
from gdm.backends.drive import DriveBackend
from gdm.backends.registry import RegistryBackend
from gdm.backends.sheets import SheetsBackend
from gdm.client import GDMClient
from gdm.collection.constants import METADATA_ID_COLUMN, METADATA_VERSION_COLUMN
from gdm.collection.metadata import InMemoryCollectionMetadataStore
from gdm.google_api import ExecutingDriveService
from gdm.migration_target import (
    ProductionMigrationTarget,
    build_production_migration_target,
)
from gdm.migrations import (
    MigrationEngine,
    MigrationOperation,
    MigrationOpKind,
    MigrationPlan,
    MigrationPlanner,
    build_type_coercion_transformer,
)
from gdm.project import Project, RegistryEntry, ResourceKind
from gdm.schema import build_model_snapshot
from gdm.session import Session
from gdm.tooling.doctor import run_collection_doctor
from tests.fakes import FakeDrive, FakeSheetsService


class CollV1(GDMModel, storage="collection", mapping="partial"):
    drive_id: str = Field(drive="id", primary_key=True)
    note: str | None = Field(default=None, metadata="note")


class CollV2(GDMModel, storage="collection", mapping="partial"):
    drive_id: str = Field(drive="id", primary_key=True)
    note: str | None = Field(default=None, metadata="label")
    status: str | None = Field(default=None, metadata="status")
    starred: bool | None = Field(default=None, drive="starred")


class ScoreV1(GDMModel, storage="sheet", mapping="partial"):
    id: str = Field(primary_key=True, source="id")
    amount: int = Field(source="amount")


class ScoreV2(GDMModel, storage="sheet", mapping="partial"):
    id: str = Field(primary_key=True, source="id")
    amount: float = Field(source="amount")


class EmptySheetRow(GDMModel, storage="sheet", source="rows", mapping="partial"):
    id: str = Field(primary_key=True, source="row_id")
    title: str = Field(source="title")


class LegacyMeta(GDMModel, storage="collection", mapping="partial"):
    drive_id: str = Field(drive="id", primary_key=True)
    legacy: str | None = Field(default=None, metadata="legacy")


class LegacyRemoved(GDMModel, storage="collection", mapping="partial"):
    drive_id: str = Field(drive="id", primary_key=True)


def _drive_service(wrapper: FakeDrive) -> ExecutingDriveService:
    raw = MagicMock()
    raw.files.return_value = wrapper.files
    return ExecutingDriveService(raw)


@pytest.fixture
def production_setup(tmp_path: Path) -> dict[str, Any]:
    drive_wrapper = FakeDrive()
    root_id = "root-folder"
    drive_wrapper.files.files[root_id] = {
        "id": root_id,
        "name": "Root",
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [],
        "appProperties": {},
    }
    folder_id = "assets-folder"
    drive_wrapper.files.files[folder_id] = {
        "id": folder_id,
        "name": "Assets",
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [root_id],
        "appProperties": {},
    }
    project = Project(
        project_id=uuid.uuid4(), root_folder_id=root_id, drive=drive_wrapper
    )
    project.initialize()
    for sheet_id, name in (
        ("sheet-1", "Scores"),
        ("empty-sheet", "Empty"),
    ):
        drive_wrapper.files.files[sheet_id] = {
            "id": sheet_id,
            "name": name,
            "mimeType": "application/vnd.google-apps.spreadsheet",
            "parents": [root_id],
            "appProperties": {},
        }
    sheets = SheetsBackend(
        FakeSheetsService(
            [
                ["amount", "_gdm_id", "_gdm_version", "_gdm_updated_at"],
                ["1", "r1", "1", "2024"],
            ]
        )
    )
    drive = DriveBackend(
        _drive_service(drive_wrapper), project_id=str(project.project_id)
    )
    registry = RegistryBackend(project=project, drive=drive, sheets=sheets)
    registry.bind_model(
        CollV2,
        RegistryEntry(
            model_name=CollV2.gdm_model_name(),
            resource_id=folder_id,
            kind=ResourceKind.FOLDER,
        ),
    )
    registry.bind_model(
        ScoreV2,
        RegistryEntry(
            model_name=ScoreV2.gdm_model_name(),
            resource_id="sheet-1",
            kind=ResourceKind.SPREADSHEET,
            extra={"worksheet": "Sheet1"},
        ),
    )
    client = GDMClient(
        project=project,
        drive=drive,
        sheets=sheets,
        registry_backend=registry,
        drive_service=drive.service,
    )
    target = build_production_migration_target(
        client,
        bindings={
            CollV2.gdm_model_name(): folder_id,
            ScoreV2.gdm_model_name(): "sheet-1",
            "LegacyMeta": folder_id,
        },
        indexes_dir=str(tmp_path / "indexes"),
    )
    return {
        "client": client,
        "target": target,
        "drive_wrapper": drive_wrapper,
        "sheets": sheets,
        "folder_id": folder_id,
        "registry": registry,
    }


def test_planner_skips_collection_drive_field_changes() -> None:
    plan = MigrationPlanner().plan_from_snapshots(
        model_name=CollV2.gdm_model_name(),
        current=build_model_snapshot(CollV1),
        target=build_model_snapshot(CollV2),
        resource_id="folder-1",
    )
    kinds = {op.kind for op in plan.operations}
    assert MigrationOpKind.BACKFILL_METADATA_COLUMN in kinds
    assert MigrationOpKind.RENAME_FIELD in kinds
    assert MigrationOpKind.ADD_FIELD not in kinds
    rename = next(
        op for op in plan.operations if op.kind is MigrationOpKind.RENAME_FIELD
    )
    assert rename.params["target_kind"] == "metadata"
    assert rename.params["storage"] == "collection"


def test_engine_collection_metadata_add_rename(
    production_setup: dict[str, Any],
) -> None:
    target: ProductionMigrationTarget = production_setup["target"]
    folder_id = production_setup["folder_id"]
    target.bind_collection(
        CollV2.gdm_model_name(), folder_id=folder_id, collection_path="Assets"
    )
    store = target._metadata_store(CollV2.gdm_model_name())
    store._header = [METADATA_ID_COLUMN, METADATA_VERSION_COLUMN, "note"]
    store._rows = {
        "file-a": (
            2,
            {
                METADATA_ID_COLUMN: "file-a",
                METADATA_VERSION_COLUMN: "1",
                "note": "launch",
            },
            "1",
        ),
    }
    plan = MigrationPlanner().plan_from_snapshots(
        model_name=CollV2.gdm_model_name(),
        current=build_model_snapshot(CollV1),
        target=build_model_snapshot(CollV2),
        resource_id=folder_id,
        dry_run=False,
    )
    report = MigrationEngine(target).apply(plan)
    assert report.succeeded
    headers = target.metadata_headers(CollV2.gdm_model_name())
    assert "label" in headers
    assert "status" in headers
    assert "note" not in headers


def test_engine_sheet_transform_coerces_int_to_float(
    production_setup: dict[str, Any],
) -> None:
    target: ProductionMigrationTarget = production_setup["target"]
    plan = MigrationPlanner().plan_from_snapshots(
        model_name=ScoreV2.gdm_model_name(),
        current=build_model_snapshot(ScoreV1),
        target=build_model_snapshot(ScoreV2),
        resource_id="sheet-1",
        dry_run=False,
        allow_destructive=True,
    )
    transform = next(
        op for op in plan.operations if op.kind is MigrationOpKind.TRANSFORM_FIELD
    )
    assert transform.params["storage"] == "sheet"
    report = MigrationEngine(target).apply(plan)
    assert report.succeeded
    table = production_setup["sheets"].service._values.table  # type: ignore[attr-defined]
    amount_idx = table[0].index("amount")
    assert isinstance(table[1][amount_idx], float)


def test_transform_unsupported_coercion_fails_closed() -> None:
    assert build_type_coercion_transformer("dict", "int") is None


def test_drop_field_on_collection_routes_to_metadata(
    production_setup: dict[str, Any],
) -> None:
    target: ProductionMigrationTarget = production_setup["target"]
    folder_id = production_setup["folder_id"]
    model_name = LegacyMeta.gdm_model_name()
    target.bind_collection(model_name, folder_id=folder_id, collection_path="Assets")
    store = target._metadata_store(model_name)
    store._header = [METADATA_ID_COLUMN, METADATA_VERSION_COLUMN, "legacy"]
    store._rows = {
        "file-a": (
            2,
            {
                METADATA_ID_COLUMN: "file-a",
                METADATA_VERSION_COLUMN: "1",
                "legacy": "x",
            },
            "1",
        ),
    }
    plan = MigrationPlanner().plan_from_snapshots(
        model_name=model_name,
        current=build_model_snapshot(LegacyMeta),
        target=build_model_snapshot(LegacyRemoved),
        resource_id=folder_id,
        dry_run=False,
        allow_destructive=True,
    )
    report = MigrationEngine(target).apply(plan)
    assert report.succeeded
    assert "legacy" not in target.metadata_headers(model_name)


def test_file_retag_sets_gdm_project_preserving_custom_props(
    production_setup: dict[str, Any],
) -> None:
    target: ProductionMigrationTarget = production_setup["target"]
    drive_wrapper: FakeDrive = production_setup["drive_wrapper"]
    file_id = "file-retag"
    drive_wrapper.files.files[file_id] = {
        "id": file_id,
        "name": "doc",
        "mimeType": "text/plain",
        "parents": ["old"],
        "appProperties": {"custom": "preserved", "gdm_model": "Old"},
    }
    target.file_retag(file_id, gdm_model="Asset", gdm_schema="2")
    props = drive_wrapper.files.files[file_id]["appProperties"]
    assert props["custom"] == "preserved"
    assert props["gdm_model"] == "Asset"
    assert props["gdm_schema"] == "2"
    assert props["gdm_project"] == str(production_setup["client"].project.project_id)


def test_empty_sheet_first_create_initializes_headers(
    production_setup: dict[str, Any],
) -> None:
    registry: RegistryBackend = production_setup["registry"]
    empty_sheets = SheetsBackend(FakeSheetsService([], titles=["Sheet1"]))
    registry.sheets = empty_sheets
    registry.bind_model(
        EmptySheetRow,
        RegistryEntry(
            model_name=EmptySheetRow.gdm_model_name(),
            resource_id="empty-sheet",
            kind=ResourceKind.SPREADSHEET,
            extra={"worksheet": "Sheet1"},
        ),
    )
    with Session(registry) as session:
        row = EmptySheetRow(id="r-new", title="Hello")
        session.add(row)
        session.commit()
    headers = empty_sheets.get_headers("empty-sheet", "Sheet1").headers
    assert "row_id" in headers
    assert "title" in headers
    assert "_gdm_id" in headers


def test_ensure_worksheet_adds_missing_tab_only() -> None:
    service = FakeSheetsService([], titles=["Sheet1"])
    backend = SheetsBackend(service)
    backend.ensure_worksheet("ss-1", "Sheet1")
    assert service._spreadsheets.add_sheet_calls == []
    backend.ensure_worksheet("ss-1", "Tasks")
    assert service._spreadsheets.add_sheet_calls == ["Tasks"]
    assert "Tasks" in service._spreadsheets.titles


def test_trash_leaves_metadata_orphan_reported_by_doctor(
    production_setup: dict[str, Any],
) -> None:
    registry: RegistryBackend = production_setup["registry"]
    drive_wrapper: FakeDrive = production_setup["drive_wrapper"]
    folder_id = production_setup["folder_id"]
    file_id = "file-x"
    drive_wrapper.files.files[file_id] = {
        "id": file_id,
        "name": "orphan.txt",
        "mimeType": "text/plain",
        "parents": [folder_id],
        "appProperties": {},
    }
    metadata = InMemoryCollectionMetadataStore()
    metadata.upsert_row(file_id, {"tag": "orphan"})
    registry.register_metadata_store(CollV2.gdm_model_name(), metadata)
    registry._collection.trash(file_id)
    report = run_collection_doctor(
        model_name=CollV2.gdm_model_name(),
        folder_id=production_setup["folder_id"],
        folder_file_ids=[],
        mime_types=[],
        metadata_store=metadata,
    )
    assert file_id in report.orphan_metadata_rows


def test_engine_unsupported_transform_fails_closed(
    production_setup: dict[str, Any],
) -> None:
    target: ProductionMigrationTarget = production_setup["target"]
    op = MigrationOperation(
        kind=MigrationOpKind.TRANSFORM_FIELD,
        model_name=ScoreV2.gdm_model_name(),
        params={
            "source_name": "amount",
            "storage": "sheet",
            "target_kind": "source",
            "from_type": "dict",
            "to_type": "int",
        },
        destructive=True,
    )
    plan = MigrationPlan(
        model_name=ScoreV2.gdm_model_name(),
        operations=(op,),
        dry_run=False,
        allow_destructive=True,
    )
    report = MigrationEngine(target).apply(plan)
    assert report.results[0].status == "error"
    assert "coercion" in report.results[0].message.lower()


def test_production_refuses_drop_system_metadata_column(
    production_setup: dict[str, Any],
) -> None:
    target: ProductionMigrationTarget = production_setup["target"]
    folder_id = production_setup["folder_id"]
    model_name = CollV2.gdm_model_name()
    target.bind_collection(model_name, folder_id=folder_id, collection_path="Assets")
    with pytest.raises(Exception, match="Refusing to drop required metadata column"):
        target.metadata_drop_column(model_name, METADATA_ID_COLUMN)


def test_build_type_coercion_supports_scalar_unions() -> None:
    transform = build_type_coercion_transformer("int | str", "float")
    assert transform is not None
    assert transform("3") == 3.0
    assert transform(2) == 2.0
