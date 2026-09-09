"""Tests for Google-backed production migration target and CLI wiring."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import BaseModel
from typer.testing import CliRunner

from gdm import Field, GDMModel
from gdm.backends.drive import DriveBackend
from gdm.backends.registry import RegistryBackend
from gdm.backends.sheets import SheetsBackend
from gdm.cli import CLIContext, app
from gdm.client import GDMClient
from gdm.collection.metadata import DriveJsonCollectionMetadataStore
from gdm.explore import Explorer, InMemoryDriveService
from gdm.google_api import ExecutingDriveService
from gdm.indexes import IndexStore
from gdm.migration_target import (
    ProductionMigrationTarget,
    build_production_migration_target,
)
from gdm.migrations import (
    MigrationCheckpoint,
    MigrationEngine,
    MigrationOperation,
    MigrationOpKind,
    MigrationPlan,
    MigrationPlanner,
    load_checkpoint,
)
from gdm.project import Project, RegistryEntry, ResourceKind
from gdm.schema import SchemaRegistry, build_model_snapshot
from tests.fakes import FakeDrive, FakeSheetsService

runner = CliRunner()


class ArticleV1(BaseModel):
    title: str


ArticleV1.gdm_config = {
    "name": "Article",
    "storage": "sheet",
    "mapping_mode": "partial",
}  # type: ignore[attr-defined]


class ArticleV2(BaseModel):
    title: str
    subtitle: str | None = None


ArticleV2.gdm_config = {
    "name": "Article",
    "storage": "sheet",
    "mapping_mode": "partial",
}  # type: ignore[attr-defined]


class SheetIndexed(GDMModel, storage="sheet", mapping="partial"):
    email: str = Field(source="email", index=True)


class DriveIndexed(GDMModel, storage="collection", mapping="partial"):
    drive_id: str = Field(drive="id", primary_key=True)
    filename: str = Field(drive="name", index=True)


def _drive_service(wrapper: FakeDrive) -> ExecutingDriveService:
    raw = MagicMock()
    raw.files.return_value = wrapper.files
    return ExecutingDriveService(raw)


@pytest.fixture
def migration_setup(
    tmp_path: Path,
) -> tuple[GDMClient, ProductionMigrationTarget, Path]:
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
    table = [
        ["title", "extra"],
        ["Hello", "keep-me"],
        ["World", "also-keep"],
    ]
    sheets = SheetsBackend(FakeSheetsService(table))
    drive_service = _drive_service(drive_wrapper)
    drive = DriveBackend(drive_service, project_id=str(project.project_id))
    drive_wrapper.files.files["f1"] = {
        "id": "f1",
        "name": "doc",
        "mimeType": "text/plain",
        "parents": ["old-parent"],
        "appProperties": {"custom": "preserved", "gdm_model": "Old"},
    }
    drive_wrapper.files.files["sheet-1"] = {
        "id": "sheet-1",
        "name": "Tasks",
        "mimeType": "application/vnd.google-apps.spreadsheet",
        "parents": [root_id],
        "version": "1",
        "appProperties": {},
    }
    drive_wrapper.files.files["folder-1"] = {
        "id": "folder-1",
        "name": "Assets",
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [root_id],
        "appProperties": {},
    }
    registry = RegistryBackend(project=project, drive=drive, sheets=sheets)
    registry.bind_model(
        SheetIndexed,
        RegistryEntry(
            model_name=SheetIndexed.gdm_model_name(),
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
        drive_service=drive_service,
    )
    gdm_dir = tmp_path / ".gdm"
    gdm_dir.mkdir(parents=True)
    (gdm_dir / "manifest.json").write_text(
        json.dumps({"root_folder_id": root_id}, indent=2) + "\n",
        encoding="utf-8",
    )
    (gdm_dir / "migrations").mkdir()
    (gdm_dir / "indexes").mkdir()
    target = build_production_migration_target(
        client,
        bindings={SheetIndexed.gdm_model_name(): "sheet-1", "Article": "sheet-1"},
        indexes_dir=str(gdm_dir / "indexes"),
        checkpoint_dir=gdm_dir / "migrations",
    )
    return client, target, tmp_path


def test_resource_id_for_model_uses_registry(migration_setup: tuple) -> None:
    _client, target, _tmp = migration_setup
    assert target.resource_id_for_model(SheetIndexed.gdm_model_name()) == "sheet-1"
    assert target.resource_id_for_model("Article") == "sheet-1"


def test_sheet_add_column_and_idempotent(migration_setup: tuple) -> None:
    _client, target, _tmp = migration_setup
    first = target.sheet_add_column("sheet-1", "subtitle", default="")
    second = target.sheet_add_column("sheet-1", "subtitle", default="")
    assert first == 2
    assert second == 0
    headers = target.sheet_headers("sheet-1")
    assert "subtitle" in headers
    assert "extra" in headers


def test_sheet_rename_column(migration_setup: tuple) -> None:
    _client, target, _tmp = migration_setup
    changed = target.sheet_rename_column("sheet-1", "title", "Title")
    assert changed == 2
    assert "Title" in target.sheet_headers("sheet-1")


def test_sheet_backfill_system_columns_idempotent(migration_setup: tuple) -> None:
    _client, target, _tmp = migration_setup
    first = target.sheet_backfill_system_columns("sheet-1")
    second = target.sheet_backfill_system_columns("sheet-1")
    assert first >= 3
    assert second == 0
    headers = target.sheet_headers("sheet-1")
    assert "_gdm_id" in headers


def test_sheet_transform_and_drop(migration_setup: tuple) -> None:
    _client, target, _tmp = migration_setup
    target.sheet_add_column("sheet-1", "note", default="n")
    changed, errors = target.sheet_transform_column(
        "sheet-1", "note", lambda value: str(value).upper()
    )
    assert changed == 2
    assert not errors
    dropped = target.sheet_drop_column("sheet-1", "note")
    assert dropped == 2
    assert "note" not in target.sheet_headers("sheet-1")


def test_file_move_and_retag_preserves_app_properties(migration_setup: tuple) -> None:
    client, target, _tmp = migration_setup
    target.file_move("f1", "dest")
    meta = client.drive.get_metadata("f1")
    assert "dest" in meta.parents
    assert "old-parent" not in meta.parents
    target.file_retag("f1", gdm_model="Asset", gdm_schema="2")
    meta = client.drive.get_metadata("f1")
    assert meta.app_properties["custom"] == "preserved"
    assert meta.app_properties["gdm_model"] == "Asset"
    assert meta.app_properties["gdm_schema"] == "2"
    assert meta.app_properties["gdm_project"] == str(client.project.project_id)
    target.file_retag("f1", gdm_model="Asset", gdm_schema="2")
    assert len(target._write_log) >= 2


def test_bind_collection_persists_registry(migration_setup: tuple) -> None:
    client, target, _tmp = migration_setup
    target.bind_collection(
        "Asset",
        folder_id="folder-1",
        collection_path="Assets",
    )
    entry = client.project.registry.get("Asset")
    assert entry is not None
    assert entry.resource_id == "folder-1"
    target.bind_collection(
        "Asset",
        folder_id="folder-1",
        collection_path="Assets",
    )


def test_metadata_column_operations(migration_setup: tuple) -> None:
    _client, target, _tmp = migration_setup
    target.bind_collection("Asset", folder_id="folder-1", collection_path="Assets")
    store = target._metadata_store("Asset")
    store._header = ["_gdm_file_id", "_gdm_version"]
    store._rows = {
        "file-a": (2, {"_gdm_file_id": "file-a", "_gdm_version": "1"}, "1"),
    }
    added = target.metadata_add_column("Asset", "tag", default="x")
    assert added == 1
    assert target.metadata_add_column("Asset", "tag", default="x") == 0
    renamed = target.metadata_rename_column("Asset", "tag", "label")
    assert renamed == 1
    orphans, missing = target.metadata_reconcile_rows(
        "Asset", folder_file_ids={"file-a", "file-b"}
    )
    assert orphans == 0
    assert missing == 1


def test_json_metadata_column_operations(migration_setup: tuple) -> None:
    client, target, _tmp = migration_setup
    file_id = "json-metadata"
    client.project.drive.files.contents[file_id] = b"{}"
    store = DriveJsonCollectionMetadataStore(
        client.project.drive,
        file_id=file_id,
    )
    store.upsert_row("file-a", {"score": "1", "tag": "old"})
    store.upsert_row("file-b", {"score": "bad", "tag": "old"})
    target._metadata_stores["JsonAsset"] = store

    assert target.metadata_add_column("JsonAsset", "owner", "nobody") == 2
    assert target.metadata_rename_column("JsonAsset", "tag", "label") == 2
    changed, errors = target.metadata_transform_column(
        "JsonAsset",
        "score",
        int,
    )
    assert changed == 1
    assert errors
    assert "file-b" in errors[0]
    assert target.metadata_drop_column("JsonAsset", "owner") == 2
    assert "label" in target.metadata_headers("JsonAsset")
    assert "tag" not in target.metadata_headers("JsonAsset")


def test_rebuild_index_writes_sidecar(migration_setup: tuple) -> None:
    client, target, tmp = migration_setup
    table = [
        ["email", "_gdm_id", "_gdm_version", "_gdm_updated_at"],
        ["a@example.com", "r1", "1", "2024-01-01"],
    ]
    client.sheets.service._values.table = table  # type: ignore[attr-defined]
    count = target.rebuild_index(SheetIndexed.gdm_model_name(), "email")
    assert count >= 1
    store = IndexStore(str(tmp / ".gdm" / "indexes"))
    loaded = store.load(SheetIndexed.gdm_model_name(), "email")
    assert loaded is not None
    assert loaded.entries
    assert loaded.source_fingerprint == target.index_fingerprint(
        SheetIndexed.gdm_model_name()
    )
    runtime_fingerprint, _ = client.backend.compute_index_fingerprints(SheetIndexed)
    assert loaded.source_fingerprint == runtime_fingerprint


def test_cli_index_rebuild_reads_live_records(migration_setup: tuple) -> None:
    client, target, tmp = migration_setup
    client.sheets.service._values.table = [  # type: ignore[attr-defined]
        ["email", "_gdm_id", "_gdm_version", "_gdm_updated_at"],
        ["a@example.com", "r1", "1", "2024-01-01"],
    ]
    ctx = CLIContext(project_dir=tmp, dry_run=False)
    with patch("gdm.cli._build_default_migration_target", return_value=target):
        result = runner.invoke(
            app,
            [
                "--project-dir",
                str(tmp),
                "--apply",
                "index",
                "rebuild",
                "--model",
                SheetIndexed.gdm_model_name(),
                "--field",
                "email",
                "--resource-id",
                "sheet-1",
                "--unique",
            ],
            obj=ctx,
            catch_exceptions=False,
        )
    assert result.exit_code == 0, result.output
    assert "Indexed 1 record" in result.output
    index = target.index_store.load(SheetIndexed.gdm_model_name(), "email")
    assert index is not None
    assert index.unique is True


def test_collection_drive_field_index_reads_inventory(migration_setup: tuple) -> None:
    client, _target, tmp = migration_setup
    drive = client.project.drive
    drive.files.files["asset-folder"] = {
        "id": "asset-folder",
        "name": "Assets",
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [client.project.root_folder_id],
        "appProperties": {},
    }
    drive.files.files["asset-1"] = {
        "id": "asset-1",
        "name": "asset.png",
        "mimeType": "image/png",
        "parents": ["asset-folder"],
        "appProperties": {},
    }
    client.project.register_resource(
        RegistryEntry(
            model_name=DriveIndexed.gdm_model_name(),
            resource_id="asset-folder",
            kind=ResourceKind.FOLDER,
        )
    )
    client.project.ensure_collection_json_metadata_binding(
        DriveIndexed.gdm_model_name()
    )
    target = build_production_migration_target(
        client,
        indexes_dir=str(tmp / ".gdm" / "indexes"),
        schema_field_target_lookup=lambda _model, _field: ("drive", "name"),
    )
    assert target.rebuild_index(DriveIndexed.gdm_model_name(), "filename") == 1
    index = target.index_store.load(DriveIndexed.gdm_model_name(), "filename")
    assert index is not None
    assert index.lookup("asset.png") == ("asset-1",)
    runtime_fingerprint, _ = client.backend.compute_index_fingerprints(DriveIndexed)
    assert index.source_fingerprint == runtime_fingerprint

    drive.files.files["asset-1"]["name"] = "renamed.png"
    changed_fingerprint, _ = client.backend.compute_index_fingerprints(DriveIndexed)
    assert changed_fingerprint != runtime_fingerprint


def test_cli_adopt_uses_production_binder(migration_setup: tuple) -> None:
    client, _target, tmp = migration_setup
    client.project.drive.files.files["folder-1"] = {
        "id": "folder-1",
        "name": "Assets",
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [client.project.root_folder_id],
        "appProperties": {},
    }
    ctx = CLIContext(project_dir=tmp, dry_run=False)
    with patch("gdm.cli._build_default_client", return_value=client) as build_client:
        result = runner.invoke(
            app,
            [
                "--project-dir",
                str(tmp),
                "--apply",
                "adopt",
                "--model-name",
                "Asset",
                "--folder-id",
                "folder-1",
                "--metadata-backend",
                "json",
                "--adoption-mode",
            ],
            obj=ctx,
            catch_exceptions=False,
        )
    assert result.exit_code == 0, result.output
    build_client.assert_called_once_with(ctx, adoption_mode=True)
    entry = client.project.get_registered("Asset")
    assert entry.resource_id == "folder-1"
    assert entry.extra["metadata_backend"] == "json"


def test_cli_doctor_uses_live_project_by_default(migration_setup: tuple) -> None:
    client, target, tmp = migration_setup
    target.bind_collection("Asset", folder_id="folder-1", collection_path="Assets")
    client.project.ensure_collection_json_metadata_binding("Asset")
    report = Explorer(
        InMemoryDriveService(
            children={
                "folder-1": [
                    {"id": "a", "name": "duplicate", "mimeType": "image/png"},
                    {"id": "b", "name": "duplicate", "mimeType": "image/png"},
                ]
            }
        )
    ).explore("folder-1")
    with (
        patch("gdm.cli._build_default_client", return_value=client),
        patch.object(client, "explore", return_value=report) as explore,
    ):
        result = runner.invoke(
            app,
            [
                "--project-dir",
                str(tmp),
                "doctor",
                "--model",
                "Asset",
                "--folder-id",
                "folder-1",
            ],
            catch_exceptions=False,
        )
    assert result.exit_code == 0, result.output
    explore.assert_called_once()
    assert "Duplicate direct-child names" in result.output


def test_checkpoint_save_and_load(migration_setup: tuple) -> None:
    _client, target, tmp = migration_setup
    cp = MigrationCheckpoint(
        plan_id="plan-1",
        model_name="Article",
        completed_op_ids=("op-1",),
    )
    target.save_checkpoint(cp)
    path = tmp / ".gdm" / "migrations" / "Article.checkpoint.json"
    assert path.is_file()
    restored = target.load_checkpoint("Article")
    assert restored is not None
    assert restored.completed_op_ids == ("op-1",)


def test_engine_checkpoint_callback(migration_setup: tuple) -> None:
    _client, target, _tmp = migration_setup
    saved: list[MigrationCheckpoint] = []

    def on_checkpoint(cp: MigrationCheckpoint) -> None:
        saved.append(cp)

    op = MigrationOperation(
        kind=MigrationOpKind.ADD_FIELD,
        model_name="Article",
        params={
            "source_name": "subtitle",
            "storage": "sheet",
            "target_kind": "source",
        },
        op_id="op-1",
    )
    plan = MigrationPlan(
        model_name="Article",
        operations=(op,),
        dry_run=False,
        allow_destructive=True,
    )
    report = MigrationEngine(target).apply(plan, on_checkpoint=on_checkpoint)
    assert report.succeeded
    assert len(saved) == 1
    assert saved[0].completed_op_ids == ("op-1",)


def test_dry_run_zero_writes(migration_setup: tuple) -> None:
    _client, target, _tmp = migration_setup
    applied = build_model_snapshot(ArticleV1)
    current = build_model_snapshot(ArticleV2)
    plan = MigrationPlanner().plan_from_snapshots(
        model_name="Article",
        current=applied,
        target=current,
        resource_id="sheet-1",
        dry_run=True,
    )
    before = list(target._write_log)
    report = MigrationEngine(target).apply(plan)
    assert not report.applied
    assert target._write_log == before


def test_missing_params_safe_error(migration_setup: tuple) -> None:
    _client, target, _tmp = migration_setup
    op = MigrationOperation(
        kind=MigrationOpKind.ADD_FIELD,
        model_name="Article",
        params={},
    )
    plan = MigrationPlan(model_name="Article", operations=(op,), dry_run=False)
    report = MigrationEngine(target).apply(plan)
    assert report.partial_failure
    assert "missing required params" in report.results[0].message.lower()


def test_cli_apply_with_injected_factory(
    migration_setup: tuple, tmp_path: Path
) -> None:
    _client, target, _ = migration_setup
    schemas = tmp_path / ".gdm" / "schemas"
    schemas.mkdir(parents=True)
    registry = SchemaRegistry(str(schemas))
    registry.save(build_model_snapshot(ArticleV1), applied=True)
    registry.save(build_model_snapshot(ArticleV2), applied=False)
    (tmp_path / ".gdm" / "manifest.json").write_text(
        json.dumps({"root_folder_id": "root-folder"}) + "\n",
        encoding="utf-8",
    )
    ctx = CLIContext(
        project_dir=tmp_path,
        dry_run=False,
        migration_target_factory=lambda: target,
    )
    result = runner.invoke(
        app,
        [
            "--project-dir",
            str(tmp_path),
            "--apply",
            "migrate",
            "apply",
            "--model",
            "Article",
            "--resource-id",
            "sheet-1",
        ],
        obj=ctx,
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert "subtitle" in target.sheet_headers("sheet-1")
    checkpoint = tmp_path / ".gdm" / "migrations" / "Article.checkpoint.json"
    assert checkpoint.is_file()


def test_cli_apply_missing_manifest(tmp_path: Path) -> None:
    schemas = tmp_path / ".gdm" / "schemas"
    schemas.mkdir(parents=True)
    registry = SchemaRegistry(str(schemas))
    registry.save(build_model_snapshot(ArticleV1), applied=True)
    registry.save(build_model_snapshot(ArticleV2), applied=False)
    ctx = CLIContext(project_dir=tmp_path, dry_run=False)
    result = runner.invoke(
        app,
        [
            "--project-dir",
            str(tmp_path),
            "--apply",
            "migrate",
            "apply",
            "--model",
            "Article",
            "--resource-id",
            "sheet-1",
        ],
        obj=ctx,
        catch_exceptions=False,
    )
    assert result.exit_code == 1
    assert "manifest" in result.output.lower()


def test_cli_default_connect_failure(tmp_path: Path) -> None:
    schemas = tmp_path / ".gdm" / "schemas"
    schemas.mkdir(parents=True)
    gdm = tmp_path / ".gdm"
    gdm.mkdir(exist_ok=True)
    (gdm / "manifest.json").write_text(
        json.dumps({"root_folder_id": "root-123"}) + "\n",
        encoding="utf-8",
    )
    registry = SchemaRegistry(str(schemas))
    registry.save(build_model_snapshot(ArticleV1), applied=True)
    registry.save(build_model_snapshot(ArticleV2), applied=False)
    with patch("gdm.client.GDMClient.connect", side_effect=RuntimeError("no creds")):
        result = runner.invoke(
            app,
            [
                "--project-dir",
                str(tmp_path),
                "--apply",
                "migrate",
                "apply",
                "--model",
                "Article",
                "--resource-id",
                "sheet-1",
            ],
            catch_exceptions=False,
        )
    assert result.exit_code == 1
    assert "connect" in result.output.lower() or "creds" in result.output.lower()


def test_checkpoint_resume_skips_completed(migration_setup: tuple) -> None:
    _client, target, tmp = migration_setup
    op1 = MigrationOperation(
        kind=MigrationOpKind.ADD_FIELD,
        model_name="Article",
        params={
            "source_name": "subtitle",
            "storage": "sheet",
            "target_kind": "source",
        },
        op_id="op-1",
    )
    op2 = MigrationOperation(
        kind=MigrationOpKind.BACKFILL_SYSTEM_COLUMNS,
        model_name="Article",
        params={"storage": "sheet", "columns": ["_gdm_id"]},
        op_id="op-2",
    )
    plan = MigrationPlan(
        model_name="Article",
        operations=(op1, op2),
        dry_run=False,
        allow_destructive=True,
    )
    cp_path = tmp / ".gdm" / "migrations" / "Article.checkpoint.json"
    cp_path.write_text(
        json.dumps(
            {
                "plan_id": "plan-1",
                "model_name": "Article",
                "completed_op_ids": ["op-1"],
                "updated_at": "2024-01-01T00:00:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    restored = load_checkpoint(cp_path.read_text(encoding="utf-8"))
    report = MigrationEngine(target).apply(plan, checkpoint=restored, plan_id="plan-1")
    statuses = [r.status for r in report.results]
    assert statuses[0] == "skipped"
    assert statuses[1] == "ok"


def test_partial_failure_does_not_mark_schema_applied(
    migration_setup: tuple, tmp_path: Path
) -> None:
    _client, target, _ = migration_setup
    schemas = tmp_path / ".gdm" / "schemas"
    schemas.mkdir(parents=True)
    registry = SchemaRegistry(str(schemas))
    registry.save(build_model_snapshot(ArticleV1), applied=True)
    registry.save(build_model_snapshot(ArticleV2), applied=False)
    (tmp_path / ".gdm" / "manifest.json").write_text(
        json.dumps({"root_folder_id": "root-folder"}) + "\n",
        encoding="utf-8",
    )

    class BrokenTarget(ProductionMigrationTarget):
        def sheet_add_column(self, resource_id: str, header: str, default=None) -> int:
            raise RuntimeError("fail")

    broken = BrokenTarget(
        client=target.client,
        bindings=target.bindings,
        index_store=target.index_store,
        checkpoint_dir=target.checkpoint_dir,
    )
    ctx = CLIContext(
        project_dir=tmp_path,
        dry_run=False,
        migration_target_factory=lambda: broken,
    )
    result = runner.invoke(
        app,
        [
            "--project-dir",
            str(tmp_path),
            "--apply",
            "migrate",
            "apply",
            "--model",
            "Article",
            "--resource-id",
            "sheet-1",
        ],
        obj=ctx,
        catch_exceptions=False,
    )
    assert result.exit_code == 1
    still_applied = registry.load("Article", applied=True)
    assert still_applied is not None
    assert not any(f.name == "subtitle" for f in still_applied.fields)
