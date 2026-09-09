"""Branch coverage for migration type coercion and engine edge paths."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from gdm.migrations import (
    InMemoryMigrationTarget,
    InMemorySheet,
    MigrationCheckpoint,
    MigrationEngine,
    MigrationOperation,
    MigrationOpKind,
    MigrationPlan,
    build_type_coercion_transformer,
)


def test_coerce_scalar_branches() -> None:
    transform_bool = build_type_coercion_transformer("str", "bool")
    assert transform_bool is not None
    assert transform_bool("yes") is True
    assert transform_bool("no") is False
    with pytest.raises(ValueError, match="Cannot coerce"):
        transform_bool("maybe")

    transform_date = build_type_coercion_transformer("str", "date")
    assert transform_date is not None
    assert transform_date("2024-06-01") == date(2024, 6, 1)
    with pytest.raises(ValueError, match="Cannot coerce"):
        transform_date("not-a-date")

    transform_dt = build_type_coercion_transformer("str", "datetime")
    assert transform_dt is not None
    parsed = transform_dt("2024-06-01T12:00:00Z")
    assert isinstance(parsed, datetime)

    transform_int = build_type_coercion_transformer("bool", "int")
    assert transform_int is not None
    assert transform_int("true") == 1

    transform_str = build_type_coercion_transformer("int", "str")
    assert transform_str is not None
    assert transform_str(42) == "42"


def test_coercion_same_type_and_empty_tokens_return_none() -> None:
    assert build_type_coercion_transformer("str", "str") is None
    assert build_type_coercion_transformer("", "int") is None


def test_transform_field_metadata_path() -> None:
    target = InMemoryMigrationTarget(
        sheets={},
        model_bindings={},
        files={},
        indexes={},
        metadata_sheets={
            "Asset": InMemorySheet(
                headers=["_gdm_file_id", "score"],
                rows=[["file-a", "1"]],
            )
        },
    )
    op = MigrationOperation(
        kind=MigrationOpKind.TRANSFORM_FIELD,
        model_name="Asset",
        params={
            "source_name": "score",
            "storage": "collection",
            "target_kind": "metadata",
            "from_type": "str",
            "to_type": "int",
        },
        destructive=True,
    )
    plan = MigrationPlan(
        model_name="Asset",
        operations=(op,),
        dry_run=False,
        allow_destructive=True,
    )
    report = MigrationEngine(target).apply(plan)
    assert report.results[0].status == "ok"


def test_transform_field_no_changes_is_error() -> None:
    target = InMemoryMigrationTarget(
        sheets={
            "sheet-1": InMemorySheet(headers=["amount"], rows=[]),
        },
        model_bindings={"Score": "sheet-1"},
        files={},
        indexes={},
    )
    op = MigrationOperation(
        kind=MigrationOpKind.TRANSFORM_FIELD,
        model_name="Score",
        params={
            "source_name": "amount",
            "storage": "sheet",
            "target_kind": "source",
            "from_type": "str",
            "to_type": "int",
        },
    )
    plan = MigrationPlan(
        model_name="Score",
        operations=(op,),
        dry_run=False,
        allow_destructive=True,
    )
    report = MigrationEngine(target).apply(plan)
    assert report.results[0].status == "error"
    assert "no changes" in report.results[0].message.lower()


def test_rollback_dry_run_skips_already_rolled_back() -> None:
    target = InMemoryMigrationTarget(
        sheets={"sheet-1": InMemorySheet(headers=["title"], rows=[["x"]])},
        model_bindings={"Article": "sheet-1"},
        files={},
        indexes={},
    )
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
            "params": {"source_name": "subtitle"},
            "destructive": True,
        },
        op_id="op-1",
    )
    plan = MigrationPlan(
        model_name="Article",
        operations=(op,),
        dry_run=True,
        allow_destructive=True,
    )
    cp = MigrationCheckpoint(
        plan_id="p",
        model_name="Article",
        completed_op_ids=("op-1",),
        rolled_back_op_ids=("op-1",),
    )
    report = MigrationEngine(target).rollback(plan, cp, allow_destructive=True)
    assert report.results[0].status == "skipped"


def test_add_field_wrong_storage_errors() -> None:
    target = InMemoryMigrationTarget(
        sheets={},
        model_bindings={"Asset": "folder-1"},
        files={},
        indexes={},
    )
    op = MigrationOperation(
        kind=MigrationOpKind.ADD_FIELD,
        model_name="Asset",
        params={
            "source_name": "tag",
            "storage": "collection",
            "target_kind": "metadata",
        },
    )
    plan = MigrationPlan(model_name="Asset", operations=(op,), dry_run=False)
    report = MigrationEngine(target).apply(plan)
    assert report.results[0].status == "error"
    assert "BACKFILL_METADATA_COLUMN" in report.results[0].message
