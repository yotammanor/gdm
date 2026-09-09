"""Tests for migration rollback planning, execution, and CLI."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel
from typer.testing import CliRunner

from gdm import Field, GDMModel
from gdm.backends.drive import DriveBackend
from gdm.backends.registry import RegistryBackend
from gdm.backends.sheets import SheetsBackend
from gdm.cli import CLIContext, app
from gdm.client import GDMClient
from gdm.google_api import ExecutingDriveService
from gdm.migration_target import (
    build_production_migration_target,
)
from gdm.migrations import (
    InMemoryMigrationTarget,
    InMemorySheet,
    MigrationCheckpoint,
    MigrationEngine,
    MigrationOperation,
    MigrationOpKind,
    MigrationPlan,
    MigrationPlanner,
    ReverseOperationSpec,
    load_checkpoint,
    load_checkpoint_plan,
    operation_is_reversible,
    parse_reverse_operation,
    rollback_blocked_reason,
    serialize_checkpoint,
)
from gdm.project import Project
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


class RenamedV1(GDMModel, storage="sheet", mapping="partial"):
    name: str = Field(source="name")


class RenamedV2(GDMModel, storage="sheet", mapping="partial"):
    name: str = Field(source="Name")


@pytest.fixture
def sheet_target() -> InMemoryMigrationTarget:
    return InMemoryMigrationTarget(
        sheets={
            "sheet-1": InMemorySheet(
                headers=["title", "extra"],
                rows=[["Hello", "keep"], ["World", "also"]],
            )
        },
        model_bindings={"Article": "sheet-1"},
        files={},
        indexes={},
    )


def _additive_plan(*, dry_run: bool = False) -> MigrationPlan:
    return MigrationPlanner().plan_from_snapshots(
        model_name="Article",
        current=build_model_snapshot(ArticleV1),
        target=build_model_snapshot(ArticleV2),
        resource_id="sheet-1",
        dry_run=dry_run,
    )


def test_parse_reverse_legacy_rename_format() -> None:
    op = MigrationOperation(
        kind=MigrationOpKind.RENAME_FIELD,
        model_name="M",
        params={"old_name": "a", "new_name": "b"},
        reverse={"old_name": "b", "new_name": "a"},
    )
    spec = parse_reverse_operation(op)
    assert spec == ReverseOperationSpec(
        kind=MigrationOpKind.RENAME_FIELD,
        params={"old_name": "b", "new_name": "a"},
    )


def test_planner_emits_reverse_for_additive_and_rename() -> None:
    plan = _additive_plan()
    add_op = next(op for op in plan.operations if op.kind is MigrationOpKind.ADD_FIELD)
    assert add_op.reverse is not None
    assert add_op.reverse["kind"] == MigrationOpKind.DROP_FIELD.value
    assert add_op.reverse["destructive"] is True
    assert operation_is_reversible(add_op)

    rename_plan = MigrationPlanner().plan_from_snapshots(
        model_name="RenamedV1",
        current=build_model_snapshot(RenamedV1),
        target=build_model_snapshot(RenamedV2),
        resource_id="sheet-1",
    )
    rename_op = next(
        op for op in rename_plan.operations if op.kind is MigrationOpKind.RENAME_FIELD
    )
    spec = parse_reverse_operation(rename_op)
    assert spec is not None
    assert spec.params["old_name"] == "Name"
    assert spec.params["new_name"] == "name"


def _drop_reverse(source_name: str) -> dict[str, object]:
    return {
        "kind": MigrationOpKind.DROP_FIELD.value,
        "params": {
            "source_name": source_name,
            "storage": "sheet",
            "target_kind": "source",
        },
        "destructive": True,
    }


def test_apply_then_rollback_round_trip(sheet_target: InMemoryMigrationTarget) -> None:
    plan = _additive_plan(dry_run=False)
    engine = MigrationEngine(sheet_target)
    apply_report = engine.apply(
        plan,
        checkpoint=MigrationCheckpoint(plan_id="p1", model_name="Article"),
    )
    assert apply_report.succeeded
    assert "subtitle" in sheet_target.sheet_headers("sheet-1")
    assert "extra" in sheet_target.sheet_headers("sheet-1")

    cp = apply_report.checkpoint
    assert cp is not None
    rollback_report = engine.rollback(
        plan,
        cp,
        allow_destructive=True,
    )
    assert rollback_report.rollback_succeeded
    headers = sheet_target.sheet_headers("sheet-1")
    assert "subtitle" not in headers
    assert headers == ["title", "extra"]


def test_partial_apply_then_partial_rollback(
    sheet_target: InMemoryMigrationTarget,
) -> None:
    op1 = MigrationOperation(
        kind=MigrationOpKind.ADD_FIELD,
        model_name="Article",
        params={
            "source_name": "subtitle",
            "storage": "sheet",
            "target_kind": "source",
        },
        reverse=_drop_reverse("subtitle"),
        op_id="op-1",
    )
    op2 = MigrationOperation(
        kind=MigrationOpKind.ADD_FIELD,
        model_name="Article",
        params={
            "source_name": "note",
            "storage": "sheet",
            "target_kind": "source",
        },
        reverse=_drop_reverse("note"),
        op_id="op-2",
    )
    plan = MigrationPlan(model_name="Article", operations=(op1, op2), dry_run=False)
    engine = MigrationEngine(sheet_target)
    sheet_target.sheet_add_column("sheet-1", "subtitle")
    cp = MigrationCheckpoint(
        plan_id="p1",
        model_name="Article",
        completed_op_ids=("op-1",),
    )
    apply_report = engine.apply(plan, checkpoint=cp)
    assert apply_report.results[0].status == "skipped"
    assert apply_report.results[1].status == "ok"
    assert "subtitle" in sheet_target.sheet_headers("sheet-1")
    assert "note" in sheet_target.sheet_headers("sheet-1")

    rollback_report = engine.rollback(
        plan,
        apply_report.checkpoint,  # type: ignore[arg-type]
        allow_destructive=True,
    )
    assert rollback_report.rollback_succeeded
    assert "subtitle" not in sheet_target.sheet_headers("sheet-1")


def test_rollback_resume_from_checkpoint(sheet_target: InMemoryMigrationTarget) -> None:
    op1 = MigrationOperation(
        kind=MigrationOpKind.ADD_FIELD,
        model_name="Article",
        params={
            "source_name": "subtitle",
            "storage": "sheet",
            "target_kind": "source",
        },
        reverse=_drop_reverse("subtitle"),
        op_id="op-1",
    )
    op2 = MigrationOperation(
        kind=MigrationOpKind.ADD_FIELD,
        model_name="Article",
        params={
            "source_name": "note",
            "storage": "sheet",
            "target_kind": "source",
        },
        reverse=_drop_reverse("note"),
        op_id="op-2",
    )
    plan = MigrationPlan(model_name="Article", operations=(op1, op2), dry_run=False)
    sheet_target.sheet_add_column("sheet-1", "subtitle")
    sheet_target.sheet_add_column("sheet-1", "note")
    cp = MigrationCheckpoint(
        plan_id="p1",
        model_name="Article",
        completed_op_ids=("op-1", "op-2"),
        rolled_back_op_ids=("op-2",),
    )
    report = MigrationEngine(sheet_target).rollback(
        plan,
        cp,
        allow_destructive=True,
    )
    statuses = [r.status for r in report.results]
    assert statuses[0] == "skipped"
    assert statuses[1] == "ok"
    assert "subtitle" not in sheet_target.sheet_headers("sheet-1")
    assert "note" in sheet_target.sheet_headers("sheet-1")


def test_non_reversible_preflight_zero_writes(
    sheet_target: InMemoryMigrationTarget,
) -> None:
    op = MigrationOperation(
        kind=MigrationOpKind.TRANSFORM_FIELD,
        model_name="Article",
        params={
            "source_name": "title",
            "from_type": "str",
            "to_type": "int",
            "storage": "sheet",
            "target_kind": "source",
        },
        destructive=True,
        op_id="bad",
    )
    plan = MigrationPlan(model_name="Article", operations=(op,), dry_run=False)
    cp = MigrationCheckpoint(
        plan_id="p1",
        model_name="Article",
        completed_op_ids=("bad",),
    )
    before = list(sheet_target.sheets["sheet-1"].headers)
    report = MigrationEngine(sheet_target).rollback(plan, cp, allow_destructive=True)
    assert report.results[0].status == "blocked"
    assert sheet_target.sheets["sheet-1"].headers == before
    reason = rollback_blocked_reason(plan, cp, allow_destructive=True)
    assert reason is not None
    assert "not reversible" in reason


def test_rollback_dry_run_zero_writes(sheet_target: InMemoryMigrationTarget) -> None:
    plan = _additive_plan(dry_run=False)
    engine = MigrationEngine(sheet_target)
    apply_cp = engine.apply(
        plan,
        checkpoint=MigrationCheckpoint(plan_id="p1", model_name="Article"),
    ).checkpoint
    assert apply_cp is not None
    dry_plan = MigrationPlan(
        model_name=plan.model_name,
        operations=plan.operations,
        from_snapshot=plan.from_snapshot,
        to_snapshot=plan.to_snapshot,
        dry_run=True,
    )
    before = list(sheet_target.sheets["sheet-1"].headers)
    report = engine.rollback(dry_plan, apply_cp, allow_destructive=True)
    assert not report.rolled_back
    assert all(r.status == "dry_run" for r in report.results)
    assert sheet_target.sheets["sheet-1"].headers == before


def test_destructive_rollback_requires_approval(
    sheet_target: InMemoryMigrationTarget,
) -> None:
    plan = _additive_plan(dry_run=False)
    engine = MigrationEngine(sheet_target)
    apply_cp = engine.apply(
        plan,
        checkpoint=MigrationCheckpoint(plan_id="p1", model_name="Article"),
    ).checkpoint
    assert apply_cp is not None
    blocked = MigrationEngine(sheet_target).rollback(plan, apply_cp)
    assert blocked.results[0].status == "blocked"
    assert "allow_destructive" in blocked.results[0].message
    assert "subtitle" in sheet_target.sheet_headers("sheet-1")


def test_partial_rollback_failure_keeps_progress(
    sheet_target: InMemoryMigrationTarget,
) -> None:
    op1 = MigrationOperation(
        kind=MigrationOpKind.RENAME_FIELD,
        model_name="Article",
        params={
            "old_name": "title",
            "new_name": "Title",
            "storage": "sheet",
            "target_kind": "source",
        },
        reverse={
            "kind": MigrationOpKind.RENAME_FIELD.value,
            "params": {
                "old_name": "Title",
                "new_name": "title",
                "storage": "sheet",
                "target_kind": "source",
            },
        },
        op_id="op-1",
    )
    op2 = MigrationOperation(
        kind=MigrationOpKind.ADD_FIELD,
        model_name="Article",
        params={
            "source_name": "subtitle",
            "storage": "sheet",
            "target_kind": "source",
        },
        reverse=_drop_reverse("subtitle"),
        op_id="op-2",
    )
    plan = MigrationPlan(model_name="Article", operations=(op1, op2), dry_run=False)
    sheet_target.sheet_rename_column("sheet-1", "title", "Title")
    sheet_target.sheet_add_column("sheet-1", "subtitle")

    class BrokenTarget(InMemoryMigrationTarget):
        def sheet_rename_column(
            self, resource_id: str, old_name: str, new_name: str
        ) -> int:
            raise RuntimeError("rename failed")

    broken = BrokenTarget(
        sheets=sheet_target.sheets,
        model_bindings=sheet_target.model_bindings,
        files=sheet_target.files,
        indexes=sheet_target.indexes,
    )
    cp = MigrationCheckpoint(
        plan_id="p1",
        model_name="Article",
        completed_op_ids=("op-1", "op-2"),
    )
    report = MigrationEngine(broken).rollback(plan, cp, allow_destructive=True)
    assert report.partial_failure
    assert report.checkpoint is not None
    assert "op-2" in report.checkpoint.rolled_back_op_ids
    assert "op-1" not in report.checkpoint.rolled_back_op_ids
    assert "subtitle" not in broken.sheet_headers("sheet-1")


def test_checkpoint_round_trip_includes_rolled_back() -> None:
    cp = MigrationCheckpoint(
        plan_id="p",
        model_name="M",
        completed_op_ids=("a", "b"),
        rolled_back_op_ids=("b",),
    )
    restored = load_checkpoint(serialize_checkpoint(cp))
    assert restored.rolled_back_op_ids == ("b",)
    assert restored.rollback_is_done() is False


def test_cli_rollback_dry_run(
    tmp_path: Path, sheet_target: InMemoryMigrationTarget
) -> None:
    schemas = tmp_path / ".gdm" / "schemas"
    schemas.mkdir(parents=True)
    registry = SchemaRegistry(str(schemas))
    registry.save(build_model_snapshot(ArticleV1), applied=True)
    registry.save(build_model_snapshot(ArticleV2), applied=False)
    (tmp_path / ".gdm" / "manifest.json").write_text(
        json.dumps({"root_folder_id": "root"}) + "\n",
        encoding="utf-8",
    )
    ctx = CLIContext(
        project_dir=tmp_path,
        dry_run=False,
        migration_target_factory=lambda: sheet_target,
    )
    apply_result = runner.invoke(
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
    assert apply_result.exit_code == 0, apply_result.output
    ctx.dry_run = True
    rollback_result = runner.invoke(
        app,
        [
            "--project-dir",
            str(tmp_path),
            "migrate",
            "rollback",
            "--model",
            "Article",
            "--resource-id",
            "sheet-1",
            "--allow-destructive",
        ],
        obj=ctx,
        catch_exceptions=False,
    )
    assert rollback_result.exit_code == 0, rollback_result.output
    assert "Dry-run rollback" in rollback_result.output
    assert "subtitle" in sheet_target.sheet_headers("sheet-1")


def test_cli_rollback_restores_schema_on_success(
    tmp_path: Path, sheet_target: InMemoryMigrationTarget
) -> None:
    schemas = tmp_path / ".gdm" / "schemas"
    schemas.mkdir(parents=True)
    registry = SchemaRegistry(str(schemas))
    registry.save(build_model_snapshot(ArticleV1), applied=True)
    registry.save(build_model_snapshot(ArticleV2), applied=False)
    (tmp_path / ".gdm" / "manifest.json").write_text(
        json.dumps({"root_folder_id": "root"}) + "\n",
        encoding="utf-8",
    )
    ctx = CLIContext(
        project_dir=tmp_path,
        dry_run=False,
        migration_target_factory=lambda: sheet_target,
    )
    apply_result = runner.invoke(
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
    assert apply_result.exit_code == 0, apply_result.output
    cp_path = tmp_path / ".gdm" / "migrations" / "Article.checkpoint.json"
    assert cp_path.is_file()
    rollback_result = runner.invoke(
        app,
        [
            "--project-dir",
            str(tmp_path),
            "--apply",
            "migrate",
            "rollback",
            "--model",
            "Article",
            "--resource-id",
            "sheet-1",
            "--allow-destructive",
        ],
        obj=ctx,
        catch_exceptions=False,
    )
    assert rollback_result.exit_code == 0, rollback_result.output
    restored = registry.load("Article", applied=True)
    assert restored is not None
    assert not any(f.name == "subtitle" for f in restored.fields)
    assert not cp_path.is_file()


def test_production_rollback_drop_preserves_extra_column(tmp_path: Path) -> None:
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
    ]
    sheets = SheetsBackend(FakeSheetsService(table))
    raw = MagicMock()
    raw.files.return_value = drive_wrapper.files
    drive_service = ExecutingDriveService(raw)
    drive = DriveBackend(drive_service, project_id=str(project.project_id))
    registry_backend = RegistryBackend(project=project, drive=drive, sheets=sheets)
    client = GDMClient(
        project=project,
        drive=drive,
        sheets=sheets,
        registry_backend=registry_backend,
        drive_service=drive_service,
    )
    gdm_dir = tmp_path / ".gdm"
    gdm_dir.mkdir(parents=True)
    target = build_production_migration_target(
        client,
        bindings={"Article": "sheet-1"},
        indexes_dir=str(gdm_dir / "indexes"),
        checkpoint_dir=gdm_dir / "migrations",
    )
    before_headers = target.sheet_headers("sheet-1")
    assert "extra" in before_headers
    target.sheet_add_column("sheet-1", "subtitle", default="")
    op = MigrationOperation(
        kind=MigrationOpKind.ADD_FIELD,
        model_name="Article",
        params={
            "source_name": "subtitle",
            "storage": "sheet",
            "target_kind": "source",
        },
        reverse={
            "kind": MigrationOpKind.DROP_FIELD.value,
            "params": {
                "source_name": "subtitle",
                "storage": "sheet",
                "target_kind": "source",
            },
            "destructive": True,
        },
        op_id="op-sub",
    )
    plan = MigrationPlan(model_name="Article", operations=(op,), dry_run=False)
    cp = MigrationCheckpoint(
        plan_id="p1",
        model_name="Article",
        completed_op_ids=("op-sub",),
    )
    report = MigrationEngine(target).rollback(plan, cp, allow_destructive=True)
    assert report.rollback_succeeded
    after_headers = target.sheet_headers("sheet-1")
    assert "subtitle" not in after_headers
    assert "extra" in after_headers


def test_rollback_preflight_empty_and_mismatch() -> None:
    plan = MigrationPlan(model_name="M", operations=(), dry_run=False)
    empty = MigrationCheckpoint(plan_id="p", model_name="M")
    assert "No completed operations" in rollback_blocked_reason(
        plan, empty, allow_destructive=True
    )

    op = MigrationOperation(
        kind=MigrationOpKind.ADD_FIELD,
        model_name="M",
        params={"source_name": "x"},
        op_id="other",
    )
    plan2 = MigrationPlan(model_name="M", operations=(op,), dry_run=False)
    cp = MigrationCheckpoint(
        plan_id="p", model_name="M", completed_op_ids=("missing-id",)
    )
    assert "do not match" in rollback_blocked_reason(plan2, cp, allow_destructive=True)


def test_parse_reverse_invalid_kind_returns_none() -> None:
    op = MigrationOperation(
        kind=MigrationOpKind.ADD_FIELD,
        model_name="M",
        params={},
        reverse={"kind": "not-valid", "params": {}},
    )
    assert parse_reverse_operation(op) is None


def test_inmemory_unbind_collection() -> None:
    target = InMemoryMigrationTarget(
        sheets={},
        model_bindings={"Asset": "folder-1"},
        files={"folder-1": {"collection_path": "Assets"}},
        indexes={},
    )
    target.unbind_collection("Asset", folder_id="folder-1")
    assert "Asset" not in target.model_bindings
    assert "collection_path" not in target.files["folder-1"]


class CollBindV1(GDMModel, storage="collection", mapping="partial"):
    drive_id: str = Field(drive="id", primary_key=True)
    name: str = Field(drive="name")


class CollBindV2(
    GDMModel, storage="collection", collection="Assets", mapping="partial"
):
    drive_id: str = Field(drive="id", primary_key=True)
    name: str = Field(drive="name")
    tag: str | None = Field(default=None, metadata="tag")


def test_collection_bind_rollback_round_trip() -> None:
    applied = build_model_snapshot(CollBindV1)
    current = build_model_snapshot(CollBindV2)
    plan = MigrationPlanner().plan_from_snapshots(
        model_name="Asset",
        current=applied,
        target=current,
        resource_id="folder-1",
    )
    bind_op = next(
        op for op in plan.operations if op.kind is MigrationOpKind.BIND_COLLECTION
    )
    target = InMemoryMigrationTarget(
        sheets={},
        model_bindings={},
        files={},
        indexes={},
    )
    engine = MigrationEngine(target)
    apply_cp = MigrationCheckpoint(plan_id="p", model_name="Asset")
    apply_report = engine.apply(
        MigrationPlan(
            model_name=plan.model_name,
            operations=(bind_op,),
            dry_run=False,
            allow_destructive=True,
        ),
        checkpoint=apply_cp,
    )
    assert apply_report.succeeded
    assert target.model_bindings["Asset"] == "folder-1"
    rollback_report = engine.rollback(
        MigrationPlan(
            model_name=plan.model_name,
            operations=(bind_op,),
            dry_run=False,
            allow_destructive=True,
        ),
        apply_report.checkpoint,  # type: ignore[arg-type]
        allow_destructive=True,
    )
    assert rollback_report.rollback_succeeded
    assert "Asset" not in target.model_bindings


def test_load_checkpoint_plan_round_trip() -> None:
    plan = _additive_plan()
    cp = MigrationCheckpoint(plan_id="p", model_name="Article", completed_op_ids=("x",))
    raw = serialize_checkpoint(cp, plan)
    restored = load_checkpoint_plan(raw)
    assert restored is not None
    assert len(restored.operations) == len(plan.operations)
    assert restored.from_snapshot is not None


def test_unbind_unsupported_target_error() -> None:
    class NoUnbindTarget(InMemoryMigrationTarget):
        pass

    NoUnbindTarget.unbind_collection = None  # type: ignore[method-assign, assignment]

    target = NoUnbindTarget(
        sheets={"s": InMemorySheet(headers=[], rows=[])},
        model_bindings={"M": "f"},
        files={},
        indexes={},
    )
    op_with_reverse = MigrationOperation(
        kind=MigrationOpKind.BIND_COLLECTION,
        model_name="M",
        params={"folder_id": "f"},
        reverse={
            "kind": MigrationOpKind.UNBIND_COLLECTION.value,
            "params": {"folder_id": "f"},
            "destructive": True,
        },
        op_id="u1",
    )
    plan2 = MigrationPlan(model_name="M", operations=(op_with_reverse,), dry_run=False)
    cp = MigrationCheckpoint(plan_id="p", model_name="M", completed_op_ids=("u1",))
    report = MigrationEngine(target).rollback(plan2, cp, allow_destructive=True)
    assert report.partial_failure
    assert "not supported" in report.results[0].message.lower()


def test_move_retag_manual_reverse(sheet_target: InMemoryMigrationTarget) -> None:
    sheet_target.files["f1"] = {
        "parent_id": "old-parent",
        "app_properties": {"gdm_model": "Old", "custom": "stay"},
    }
    op = MigrationOperation(
        kind=MigrationOpKind.MOVE_RETAG,
        model_name="Asset",
        params={
            "file_id": "f1",
            "new_parent_id": "dest",
            "gdm_model": "Asset",
        },
        reverse={
            "kind": MigrationOpKind.MOVE_RETAG.value,
            "params": {
                "file_id": "f1",
                "new_parent_id": "old-parent",
                "gdm_model": "Old",
            },
            "destructive": True,
        },
        op_id="move-1",
    )
    plan = MigrationPlan(
        model_name="Asset",
        operations=(op,),
        dry_run=False,
        allow_destructive=True,
    )
    engine = MigrationEngine(sheet_target)
    apply_report = engine.apply(plan)
    assert apply_report.succeeded
    assert sheet_target.files["f1"]["parent_id"] == "dest"
    rollback = engine.rollback(
        plan,
        apply_report.checkpoint,  # type: ignore[arg-type]
        allow_destructive=True,
    )
    assert rollback.rollback_succeeded
    assert sheet_target.files["f1"]["parent_id"] == "old-parent"
    assert sheet_target.files["f1"]["app_properties"]["custom"] == "stay"


def test_production_unbind_collection(tmp_path: Path) -> None:
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
    sheets = SheetsBackend(FakeSheetsService([]))
    raw = MagicMock()
    raw.files.return_value = drive_wrapper.files
    drive_service = ExecutingDriveService(raw)
    drive = DriveBackend(drive_service, project_id=str(project.project_id))
    registry_backend = RegistryBackend(project=project, drive=drive, sheets=sheets)
    client = GDMClient(
        project=project,
        drive=drive,
        sheets=sheets,
        registry_backend=registry_backend,
        drive_service=drive_service,
    )
    gdm_dir = tmp_path / ".gdm"
    gdm_dir.mkdir(parents=True)
    target = build_production_migration_target(
        client,
        indexes_dir=str(gdm_dir / "indexes"),
        checkpoint_dir=gdm_dir / "migrations",
    )
    target.bind_collection("Asset", folder_id="folder-1", collection_path="Assets")
    assert client.project.registry.get("Asset") is not None
    target.unbind_collection("Asset", folder_id="folder-1")
    assert client.project.registry.get("Asset") is None
    assert "Asset" not in target.bindings
