"""Tests for migration planning and application."""

from __future__ import annotations

import uuid

import pytest
from pydantic import BaseModel

from gdm.migrations import (
    InMemoryMigrationTarget,
    InMemorySheet,
    MigrationCheckpoint,
    MigrationEngine,
    MigrationOperation,
    MigrationOpKind,
    MigrationPlanner,
    load_checkpoint,
    serialize_checkpoint,
)
from gdm.schema import build_model_snapshot


class V1Model(BaseModel):
    title: str


V1Model.gdm_config = {"name": "Article", "storage": "sheet", "mapping_mode": "partial"}  # type: ignore[attr-defined]


class V2Model(BaseModel):
    title: str
    subtitle: str | None = None


V2Model.gdm_config = {"name": "Article", "storage": "sheet", "mapping_mode": "partial"}  # type: ignore[attr-defined]


@pytest.fixture
def target() -> InMemoryMigrationTarget:
    sheet_id = "sheet-1"
    return InMemoryMigrationTarget(
        sheets={
            sheet_id: InMemorySheet(
                headers=["title"],
                rows=[["Hello"], ["World"]],
            )
        },
        model_bindings={"Article": sheet_id},
        files={},
        indexes={},
    )


def test_plan_additive_field(target: InMemoryMigrationTarget) -> None:
    applied = build_model_snapshot(V1Model)
    current = build_model_snapshot(V2Model)
    planner = MigrationPlanner()
    plan = planner.plan_from_snapshots(
        model_name="Article",
        current=applied,
        target=current,
        resource_id="sheet-1",
    )
    assert plan.dry_run is True
    assert any(op.kind is MigrationOpKind.ADD_FIELD for op in plan.operations)
    assert not plan.has_destructive


def test_destructive_gate_blocks_drop() -> None:
    applied = build_model_snapshot(V2Model)
    current = build_model_snapshot(V1Model)
    planner = MigrationPlanner()
    plan = planner.plan_from_snapshots(
        model_name="Article",
        current=applied,
        target=current,
        resource_id="sheet-1",
        dry_run=False,
        allow_destructive=False,
    )
    assert plan.has_destructive
    assert plan.blocked_reason() is not None


def test_apply_add_column(target: InMemoryMigrationTarget) -> None:
    applied = build_model_snapshot(V1Model)
    current = build_model_snapshot(V2Model)
    planner = MigrationPlanner()
    plan = planner.plan_from_snapshots(
        model_name="Article",
        current=applied,
        target=current,
        resource_id="sheet-1",
        dry_run=False,
    )
    engine = MigrationEngine(target)
    report = engine.apply(plan)
    assert report.succeeded
    headers = target.sheet_headers("sheet-1")
    assert "subtitle" in headers


def test_dry_run_does_not_mutate(target: InMemoryMigrationTarget) -> None:
    applied = build_model_snapshot(V1Model)
    current = build_model_snapshot(V2Model)
    plan = MigrationPlanner().plan_from_snapshots(
        model_name="Article",
        current=applied,
        target=current,
        resource_id="sheet-1",
        dry_run=True,
    )
    report = MigrationEngine(target).apply(plan)
    assert not report.applied
    assert all(r.status == "dry_run" for r in report.results)
    assert target.sheet_headers("sheet-1") == ["title"]


def test_resume_from_checkpoint(target: InMemoryMigrationTarget) -> None:
    op1 = MigrationOperation(
        kind=MigrationOpKind.ADD_FIELD,
        model_name="Article",
        params={
            "source_name": "subtitle",
            "field": "subtitle",
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
    from gdm.migrations import MigrationPlan

    plan = MigrationPlan(
        model_name="Article",
        operations=(op1, op2),
        dry_run=False,
        allow_destructive=True,
    )
    cp = MigrationCheckpoint(
        plan_id="plan-1", model_name="Article", completed_op_ids=("op-1",)
    )
    report = MigrationEngine(target).apply(plan, checkpoint=cp, plan_id="plan-1")
    statuses = [r.status for r in report.results]
    assert statuses[0] == "skipped"
    assert statuses[1] == "ok"


def test_backfill_system_columns(target: InMemoryMigrationTarget) -> None:
    changed = target.sheet_backfill_system_columns("sheet-1")
    assert changed >= 3
    headers = target.sheet_headers("sheet-1")
    assert "_gdm_id" in headers
    row_id = target.sheets["sheet-1"].rows[0][headers.index("_gdm_id")]
    uuid.UUID(str(row_id))


def test_checkpoint_serialization() -> None:
    cp = MigrationCheckpoint(plan_id="p", model_name="M", completed_op_ids=("a",))
    restored = load_checkpoint(serialize_checkpoint(cp))
    assert restored.completed_op_ids == ("a",)


def test_inherently_destructive_move_member_blocked() -> None:
    op = MigrationOperation(
        kind=MigrationOpKind.MOVE_MEMBER,
        model_name="Asset",
        params={"file_id": "f1", "new_parent_id": "dest"},
    )
    from gdm.migrations import MigrationPlan

    plan = MigrationPlan(
        model_name="Asset",
        operations=(op,),
        dry_run=False,
        allow_destructive=False,
    )
    assert plan.has_destructive
    assert plan.blocked_reason() is not None


def test_partial_failure_stops_apply(target: InMemoryMigrationTarget) -> None:
    op = MigrationOperation(
        kind=MigrationOpKind.TRANSFORM_FIELD,
        model_name="Article",
        params={
            "source_name": "missing",
            "from_type": "int",
            "to_type": "float",
            "storage": "sheet",
            "target_kind": "source",
        },
        destructive=True,
        op_id="bad-op",
    )
    from gdm.migrations import MigrationPlan

    plan = MigrationPlan(
        model_name="Article",
        operations=(op,),
        dry_run=False,
        allow_destructive=True,
    )
    report = MigrationEngine(target).apply(plan)
    assert report.partial_failure
