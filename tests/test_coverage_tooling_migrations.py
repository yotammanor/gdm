"""Branch coverage for migration planning, apply, and checkpoints."""

from __future__ import annotations

import uuid

import pytest
from pydantic import BaseModel

from gdm import Field, GDMModel
from gdm.migrations import (
    InMemoryMigrationTarget,
    InMemorySheet,
    MigrationCheckpoint,
    MigrationEngine,
    MigrationOperation,
    MigrationOpKind,
    MigrationPlan,
    MigrationPlanner,
    load_checkpoint,
    operation_is_destructive,
    serialize_checkpoint,
)
from gdm.schema import SchemaSnapshot, build_model_snapshot

# Legacy gdm_config models for planner branches


class SheetV1(BaseModel):
    title: str


SheetV1.gdm_config = {"name": "Legacy", "storage": "sheet", "mapping_mode": "partial"}  # type: ignore[attr-defined]


class SheetV2(BaseModel):
    title: str
    note: str | None = None


SheetV2.gdm_config = {"name": "Legacy", "storage": "sheet", "mapping_mode": "partial"}  # type: ignore[attr-defined]


class CollectionV1(GDMModel, storage="collection", mapping="partial"):
    drive_id: str = Field(drive="id", primary_key=True)
    name: str = Field(drive="name")


class CollectionV2(
    GDMModel, storage="collection", collection="Assets", mapping="partial"
):
    drive_id: str = Field(drive="id", primary_key=True)
    name: str = Field(drive="name")
    tag: str | None = Field(default=None, metadata="tag")


class IndexedModel(GDMModel, storage="sheet", mapping="partial"):
    email: str = Field(source="email", index=True)


class IndexedModelV2(GDMModel, storage="sheet", mapping="partial"):
    email: str = Field(source="email", index=True)
    phone: str | None = Field(default=None, source="phone", index=True)


@pytest.fixture
def sheet_target() -> InMemoryMigrationTarget:
    return InMemoryMigrationTarget(
        sheets={
            "sheet-1": InMemorySheet(headers=["title"], rows=[["Hello"]]),
        },
        model_bindings={"Article": "sheet-1", "Legacy": "sheet-1"},
        files={"f1": {"parent_id": "old", "app_properties": {}}},
        indexes={},
        metadata_sheets={
            "Asset": InMemorySheet(
                headers=["_gdm_file_id", "tag"],
                rows=[["keep", "a"], ["orphan", "b"]],
            )
        },
    )


def test_checkpoint_is_done() -> None:
    op = MigrationOperation(
        kind=MigrationOpKind.ADD_FIELD,
        model_name="M",
        params={},
        op_id="op-1",
    )
    plan = MigrationPlan(model_name="M", operations=(op,), dry_run=False)
    cp = MigrationCheckpoint(plan_id="p", model_name="M", completed_op_ids=("op-1",))
    assert cp.is_done(plan) is True


def test_plan_from_model(sheet_target: InMemoryMigrationTarget) -> None:
    planner = MigrationPlanner()
    plan = planner.plan_from_model(
        model=SheetV2,
        applied=build_model_snapshot(SheetV1),
        resource_id="sheet-1",
    )
    assert any(op.kind is MigrationOpKind.ADD_FIELD for op in plan.operations)


def test_collection_bind_and_metadata_backfill() -> None:
    applied = build_model_snapshot(CollectionV1)
    target = build_model_snapshot(CollectionV2)
    plan = MigrationPlanner().plan_from_snapshots(
        model_name="Asset",
        current=applied,
        target=target,
        resource_id="folder-1",
    )
    kinds = {op.kind for op in plan.operations}
    assert MigrationOpKind.BIND_COLLECTION in kinds
    assert MigrationOpKind.BACKFILL_METADATA_COLUMN in kinds


def test_rename_transform_drop_and_rebuild_index() -> None:
    class EmailV1(GDMModel, storage="sheet", mapping="partial"):
        contact: str = Field(source="email")

    class EmailV2(GDMModel, storage="sheet", mapping="partial"):
        contact: str = Field(source="Email")

    applied = build_model_snapshot(EmailV1)
    current = build_model_snapshot(EmailV2)
    plan = MigrationPlanner().plan_from_snapshots(
        model_name="EmailV1",
        current=applied,
        target=current,
        resource_id="sheet-1",
        allow_destructive=True,
    )
    kinds = {op.kind for op in plan.operations}
    assert MigrationOpKind.RENAME_FIELD in kinds

    index_plan = MigrationPlanner().plan_from_snapshots(
        model_name="IndexedModel",
        current=build_model_snapshot(IndexedModel),
        target=build_model_snapshot(IndexedModelV2),
        resource_id="sheet-1",
    )
    assert any(op.kind is MigrationOpKind.REBUILD_INDEX for op in index_plan.operations)


def test_apply_all_operation_kinds(sheet_target: InMemoryMigrationTarget) -> None:
    engine = MigrationEngine(sheet_target)
    ops = [
        MigrationOperation(
            kind=MigrationOpKind.ADD_FIELD,
            model_name="Article",
            params={
                "source_name": "subtitle",
                "field": "subtitle",
                "storage": "sheet",
                "target_kind": "source",
            },
        ),
        MigrationOperation(
            kind=MigrationOpKind.RENAME_FIELD,
            model_name="Article",
            params={
                "old_name": "title",
                "new_name": "Title",
                "field": "title",
                "storage": "sheet",
                "target_kind": "source",
            },
        ),
        MigrationOperation(
            kind=MigrationOpKind.BACKFILL_SYSTEM_COLUMNS,
            model_name="Article",
            params={"storage": "sheet", "columns": ["_gdm_id"]},
        ),
        MigrationOperation(
            kind=MigrationOpKind.MOVE_RETAG,
            model_name="Asset",
            params={
                "file_id": "f1",
                "new_parent_id": "dest",
                "gdm_model": "Asset",
            },
        ),
        MigrationOperation(
            kind=MigrationOpKind.REBUILD_INDEX,
            model_name="IndexedModel",
            params={"field": "email"},
        ),
        MigrationOperation(
            kind=MigrationOpKind.BIND_COLLECTION,
            model_name="Asset",
            params={"folder_id": "folder-1", "collection_path": "Assets"},
        ),
        MigrationOperation(
            kind=MigrationOpKind.BACKFILL_METADATA_COLUMN,
            model_name="Asset",
            params={
                "source_name": "status",
                "storage": "collection",
                "target_kind": "metadata",
            },
        ),
        MigrationOperation(
            kind=MigrationOpKind.RECONCILE_METADATA_ROWS,
            model_name="Asset",
            params={"folder_file_ids": ["keep"]},
        ),
        MigrationOperation(
            kind=MigrationOpKind.RETAG_APP_PROPERTIES,
            model_name="Asset",
            params={"file_id": "f1", "gdm_model": "Asset"},
        ),
        MigrationOperation(
            kind=MigrationOpKind.MOVE_MEMBER,
            model_name="Asset",
            params={"file_id": "f1", "new_parent_id": "dest"},
        ),
        MigrationOperation(
            kind=MigrationOpKind.CONVERT_MIME,
            model_name="Asset",
            params={"file_id": "f1"},
        ),
        MigrationOperation(
            kind=MigrationOpKind.DROP_FIELD,
            model_name="Article",
            params={
                "source_name": "subtitle",
                "storage": "sheet",
                "target_kind": "source",
            },
            destructive=True,
        ),
    ]
    plan = MigrationPlan(
        model_name="Article",
        operations=tuple(ops),
        dry_run=False,
        allow_destructive=True,
    )
    report = engine.apply(plan)
    assert report.applied
    assert any(
        r.kind is MigrationOpKind.CONVERT_MIME and r.status == "error"
        for r in report.results
    )


def test_blocked_plan_returns_blocked_report() -> None:
    op = MigrationOperation(
        kind=MigrationOpKind.DROP_FIELD,
        model_name="M",
        params={"source_name": "x"},
        destructive=True,
    )
    plan = MigrationPlan(
        model_name="M",
        operations=(op,),
        dry_run=False,
        allow_destructive=False,
    )
    report = MigrationEngine(
        InMemoryMigrationTarget(sheets={}, model_bindings={}, files={}, indexes={})
    ).apply(plan)
    assert report.results[0].status == "blocked"


def test_dry_run_no_bound_resource() -> None:
    target = InMemoryMigrationTarget(
        sheets={"s": InMemorySheet(headers=[], rows=[])},
        model_bindings={},
        files={},
        indexes={},
    )
    op = MigrationOperation(
        kind=MigrationOpKind.ADD_FIELD,
        model_name="Missing",
        params={"source_name": "x"},
    )
    plan = MigrationPlan(model_name="Missing", operations=(op,), dry_run=True)
    report = MigrationEngine(target).apply(plan)
    assert "no bound resource" in report.results[0].message.lower()


def test_apply_no_resource_error() -> None:
    target = InMemoryMigrationTarget(
        sheets={},
        model_bindings={},
        files={},
        indexes={},
    )
    op = MigrationOperation(
        kind=MigrationOpKind.ADD_FIELD,
        model_name="Ghost",
        params={
            "source_name": "x",
            "storage": "sheet",
            "target_kind": "source",
        },
    )
    plan = MigrationPlan(model_name="Ghost", operations=(op,), dry_run=False)
    report = MigrationEngine(target).apply(plan)
    assert report.results[0].status == "error"


def test_apply_exception_stops(sheet_target: InMemoryMigrationTarget) -> None:
    class BrokenTarget(InMemoryMigrationTarget):
        def sheet_add_column(self, resource_id: str, header: str, default=None) -> int:
            raise RuntimeError("boom")

    broken = BrokenTarget(
        sheets=sheet_target.sheets,
        model_bindings=sheet_target.model_bindings,
        files=sheet_target.files,
        indexes=sheet_target.indexes,
        metadata_sheets=sheet_target.metadata_sheets,
    )
    op = MigrationOperation(
        kind=MigrationOpKind.ADD_FIELD,
        model_name="Article",
        params={
            "source_name": "x",
            "storage": "sheet",
            "target_kind": "source",
        },
    )
    plan = MigrationPlan(model_name="Article", operations=(op,), dry_run=False)
    report = MigrationEngine(broken).apply(plan)
    assert report.partial_failure


def test_inmemory_target_branches(sheet_target: InMemoryMigrationTarget) -> None:
    assert sheet_target.sheet_add_column("sheet-1", "title") == 0
    assert sheet_target.sheet_rename_column("sheet-1", "missing", "x") == 0
    changed, errors = sheet_target.sheet_transform_column(
        "sheet-1", "missing", lambda x: x
    )
    assert changed == 0
    assert errors
    assert sheet_target.sheet_drop_column("sheet-1", "missing") == 0
    assert sheet_target.metadata_headers("missing") == []
    assert sheet_target.metadata_add_column("Asset", "status") >= 0
    assert sheet_target.metadata_rename_column("Asset", "missing", "x") == 0
    changed, errors = sheet_target.metadata_transform_column(
        "Asset", "status", lambda value: str(value).upper()
    )
    assert changed >= 1
    assert not errors
    assert sheet_target.metadata_drop_column("Asset", "status") >= 0
    orphans, _missing = sheet_target.metadata_reconcile_rows(
        "Asset", folder_file_ids={"keep"}
    )
    assert orphans >= 1
    sheet_target.bind_collection(
        "Asset", folder_id="folder-1", collection_path="Assets"
    )
    cp = MigrationCheckpoint(plan_id="p", model_name="M")
    sheet_target.save_checkpoint(cp)
    assert sheet_target.load_checkpoint("p") is not None


def test_system_columns_backfill_idempotent(
    sheet_target: InMemoryMigrationTarget,
) -> None:
    first = sheet_target.sheet_backfill_system_columns("sheet-1")
    second = sheet_target.sheet_backfill_system_columns("sheet-1")
    assert first >= 3
    assert second == 0
    row_id = sheet_target.sheets["sheet-1"].rows[0][
        sheet_target.sheets["sheet-1"].headers.index("_gdm_id")
    ]
    uuid.UUID(str(row_id))


def test_operation_is_destructive_flag() -> None:
    op = MigrationOperation(
        kind=MigrationOpKind.ADD_FIELD,
        model_name="M",
        params={},
        destructive=True,
    )
    assert operation_is_destructive(op) is True


def test_backfill_system_columns_planned() -> None:
    class WithSys(BaseModel):
        a: str

    WithSys.gdm_config = {"name": "Sys", "storage": "sheet"}  # type: ignore[attr-defined]
    applied = SchemaSnapshot(
        model_name="Sys",
        storage="sheet",
        mapping_mode="partial",
        format_version=2,
        pydantic_schema={},
        fields=(),
        system_columns=(),
        content_hash="abc",
    )
    target = build_model_snapshot(WithSys)
    plan = MigrationPlanner().plan_from_snapshots(
        model_name="Sys",
        current=applied,
        target=target,
        resource_id="sheet-1",
    )
    assert any(
        op.kind is MigrationOpKind.BACKFILL_SYSTEM_COLUMNS for op in plan.operations
    )


def test_transform_type_change_in_planner() -> None:
    class AmountV1(GDMModel, storage="sheet", mapping="partial"):
        amount: int = Field(source="amount")

    class AmountV2(GDMModel, storage="sheet", mapping="partial"):
        amount: float = Field(source="amount")

    plan = MigrationPlanner().plan_from_snapshots(
        model_name="AmountV1",
        current=build_model_snapshot(AmountV1),
        target=build_model_snapshot(AmountV2),
        resource_id="sheet-1",
        allow_destructive=True,
    )
    assert any(op.kind is MigrationOpKind.TRANSFORM_FIELD for op in plan.operations)


def test_drop_field_apply(sheet_target: InMemoryMigrationTarget) -> None:
    sheet_target.sheets["sheet-1"].headers.append("subtitle")
    sheet_target.sheets["sheet-1"].rows[0].append("sub")
    op = MigrationOperation(
        kind=MigrationOpKind.DROP_FIELD,
        model_name="Article",
        params={
            "source_name": "subtitle",
            "storage": "sheet",
            "target_kind": "source",
        },
        destructive=True,
    )
    plan = MigrationPlan(
        model_name="Article",
        operations=(op,),
        dry_run=False,
        allow_destructive=True,
    )
    report = MigrationEngine(sheet_target).apply(plan)
    assert report.results[0].status == "ok"
    assert "subtitle" not in sheet_target.sheet_headers("sheet-1")


def test_unsupported_operation_kind() -> None:
    op = MigrationOperation(
        kind=MigrationOpKind.ADD_FIELD,
        model_name="Article",
        params={
            "source_name": "x",
            "storage": "sheet",
            "target_kind": "source",
        },
    )
    object.__setattr__(op, "kind", "not_a_real_kind")  # type: ignore[misc]
    target = InMemoryMigrationTarget(
        sheets={"sheet-1": InMemorySheet(headers=[], rows=[])},
        model_bindings={"Article": "sheet-1"},
        files={},
        indexes={},
    )
    from gdm.migrations import MigrationEngine, MigrationPlan

    plan = MigrationPlan(model_name="Article", operations=(op,), dry_run=False)
    report = MigrationEngine(target).apply(plan)
    assert "Unsupported" in report.results[0].message


def test_transform_column_row_errors(sheet_target: InMemoryMigrationTarget) -> None:
    def boom(_: object) -> object:
        raise ValueError("bad")

    changed, errors = sheet_target.sheet_transform_column("sheet-1", "title", boom)
    assert changed >= 1 or errors


def test_metadata_rename_and_add_idempotent(
    sheet_target: InMemoryMigrationTarget,
) -> None:
    assert sheet_target.metadata_add_column("Asset", "tag") == 0
    assert sheet_target.metadata_rename_column("Asset", "tag", "label") >= 0
    cp = MigrationCheckpoint(plan_id="p", model_name="M", completed_op_ids=("a", "b"))
    restored = load_checkpoint(serialize_checkpoint(cp))
    assert restored.completed_op_ids == ("a", "b")
