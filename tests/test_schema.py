"""Tests for schema snapshots and drift detection."""

from __future__ import annotations

import json

from gdm import Field, GDMModel
from gdm.schema import (
    DriftCategory,
    DriftSeverity,
    GDMFieldMeta,
    SchemaSnapshot,
    build_live_collection_schema,
    build_live_source_schema,
    build_model_snapshot,
    compute_schema_hash,
    detect_drift,
    detect_membership_drift,
    load_snapshot_json,
    serialize_snapshot,
    snapshot_from_dict,
)


class SampleModel(GDMModel, storage="sheet", mapping="partial"):
    name: str = Field(source="Full Name", index=True)
    age: int | None = None
    email: str


class CollectionModel(
    GDMModel, storage="collection", collection="Assets", mime="image/*"
):
    drive_id: str = Field(drive="id", primary_key=True)
    name: str = Field(drive="name")
    tag: str | None = Field(default=None, metadata="tag")


def test_build_model_snapshot_deterministic_hash() -> None:
    first = build_model_snapshot(SampleModel)
    second = build_model_snapshot(SampleModel)
    assert first.content_hash == second.content_hash
    assert first.model_name == "SampleModel"
    assert first.storage == "sheet"
    assert any(f.name == "name" and f.index for f in first.fields)


def test_snapshot_roundtrip_json() -> None:
    snapshot = build_model_snapshot(SampleModel)
    raw = serialize_snapshot(snapshot)
    loaded = load_snapshot_json(raw)
    assert loaded.content_hash == snapshot.content_hash
    assert loaded.fields[0].source_name == snapshot.fields[0].source_name


def test_compute_schema_hash_stable() -> None:
    payload = {"a": 1, "b": 2}
    assert compute_schema_hash(payload) == compute_schema_hash({"b": 2, "a": 1})


def test_detect_drift_model_vs_applied_rename() -> None:
    applied = build_model_snapshot(SampleModel)
    modified_fields = tuple(
        GDMFieldMeta(
            name=f.name,
            source_name="Renamed" if f.name == "name" else f.source_name,
            python_type=f.python_type,
            json_schema=f.json_schema,
            index=f.index,
            required=f.required,
        )
        for f in applied.fields
    )
    body = {
        "fields": [
            {
                "name": f.name,
                "source_name": f.source_name,
                "python_type": f.python_type,
                "json_schema": dict(f.json_schema),
                "index": f.index,
                "required": f.required,
            }
            for f in modified_fields
        ],
        "format_version": applied.format_version,
        "mapping_mode": applied.mapping_mode,
        "model_name": applied.model_name,
        "pydantic_schema": applied.pydantic_schema,
        "storage": applied.storage,
        "system_columns": list(applied.system_columns),
    }
    current = SchemaSnapshot(
        model_name=applied.model_name,
        storage=applied.storage,
        mapping_mode=applied.mapping_mode,
        format_version=applied.format_version,
        pydantic_schema=applied.pydantic_schema,
        fields=modified_fields,
        system_columns=applied.system_columns,
        content_hash=compute_schema_hash(body),
    )
    report = detect_drift(
        model_snapshot=current,
        applied_snapshot=applied,
        live_schema=None,
    )
    codes = {d.code for d in report.diagnostics}
    assert "applied_source_renamed" in codes


def test_detect_drift_live_missing_required() -> None:
    model = build_model_snapshot(SampleModel)
    live = build_live_source_schema(
        resource_id="sheet1",
        resource_kind="sheet",
        headers=["email"],
        sample_rows=[["a@example.com"]],
    )
    report = detect_drift(model_snapshot=model, applied_snapshot=None, live_schema=live)
    assert report.has_errors
    assert any(d.code == "live_missing_required" for d in report.diagnostics)


def test_detect_drift_strict_unknown_live_column() -> None:
    strict_model = build_model_snapshot(SampleModel)
    strict = SchemaSnapshot(
        model_name=strict_model.model_name,
        storage=strict_model.storage,
        mapping_mode="strict",
        format_version=strict_model.format_version,
        pydantic_schema=strict_model.pydantic_schema,
        fields=strict_model.fields,
        system_columns=strict_model.system_columns,
        content_hash=strict_model.content_hash,
    )
    live = build_live_source_schema(
        resource_id="sheet1",
        resource_kind="sheet",
        headers=["name", "email", "extra_col"],
        sample_rows=[["Ann", "a@example.com", "x"]],
    )
    report = detect_drift(
        model_snapshot=strict, applied_snapshot=None, live_schema=live
    )
    assert any(d.code == "live_unknown_strict" for d in report.diagnostics)


def test_snapshot_from_dict() -> None:
    data = json.loads(serialize_snapshot(build_model_snapshot(SampleModel)))
    restored = snapshot_from_dict(data)
    assert restored.model_name == "SampleModel"


def test_collection_snapshot_includes_binding_and_capabilities() -> None:
    snapshot = build_model_snapshot(CollectionModel)
    assert snapshot.storage == "collection"
    assert snapshot.collection_path == "Assets"
    assert snapshot.mime_patterns == ("image/*",)
    assert snapshot.content_capabilities
    drive_field = next(f for f in snapshot.fields if f.name == "drive_id")
    assert drive_field.target_kind == "drive"
    assert drive_field.read_only is True


def test_membership_drift_detection() -> None:
    diagnostics = detect_membership_drift(
        model_member_ids=["a", "b"],
        live_member_ids=["a", "c"],
    )
    codes = {d.code for d in diagnostics}
    assert "membership_missing_live" in codes
    assert "membership_added_live" in codes
    assert all(d.category is DriftCategory.MEMBERSHIP for d in diagnostics)


def test_build_live_collection_schema() -> None:
    live = build_live_collection_schema(
        folder_id="folder1",
        metadata_headers=["tag", "approved"],
        sample_rows=[["launch", "yes"]],
    )
    assert live.resource_kind == "collection_metadata"


def test_drift_severity_levels() -> None:
    report = detect_drift(model_snapshot=None, applied_snapshot=None, live_schema=None)
    assert report.diagnostics[0].severity is DriftSeverity.INFO
