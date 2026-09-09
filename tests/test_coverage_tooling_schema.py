"""Branch coverage for schema snapshots, drift, and registry."""

from __future__ import annotations

import json

from pydantic import BaseModel

from gdm import Field, GDMModel
from gdm.schema import (
    DriftReport,
    DriftSeverity,
    GDMFieldMeta,
    LiveSourceSchema,
    SchemaRegistry,
    SchemaSnapshot,
    _types_compatible,
    build_live_collection_schema,
    build_live_source_schema,
    build_model_snapshot,
    compute_live_schema_hash,
    compute_schema_hash,
    detect_drift,
    detect_membership_drift,
    load_snapshot_json,
    serialize_snapshot,
    snapshot_from_dict,
)


class Sample(GDMModel, storage="sheet", mapping="partial"):
    name: str = Field(source="Full Name", index=True)
    age: int | None = None


class CollectionImg(
    GDMModel, storage="collection", collection="Assets", mime="image/*"
):
    drive_id: str = Field(drive="id", primary_key=True)
    name: str = Field(drive="name")


class LegacySheet(BaseModel):
    title: str


LegacySheet.gdm_config = {  # type: ignore[attr-defined]
    "name": "Legacy",
    "storage": "sheet",
    "mapping_mode": "strict",
    "mime": ("text/plain",),
}


class TypedModel(GDMModel, storage="sheet", mapping="partial"):
    count: int
    note: str | None = None


def test_drift_report_has_warnings() -> None:
    applied = build_model_snapshot(Sample)
    modified = tuple(
        GDMFieldMeta(
            name=f.name,
            source_name=f"Renamed_{f.name}" if f.name == "name" else f.source_name,
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
            for f in modified
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
        fields=modified,
        system_columns=applied.system_columns,
        content_hash=compute_schema_hash(body),
    )
    report = detect_drift(
        model_snapshot=current, applied_snapshot=applied, live_schema=None
    )
    assert report.has_warnings


def test_model_applied_match_info() -> None:
    snap = build_model_snapshot(Sample)
    report = detect_drift(model_snapshot=snap, applied_snapshot=snap, live_schema=None)
    assert any(d.code == "model_applied_match" for d in report.diagnostics)


def test_storage_and_mime_drift() -> None:
    applied = build_model_snapshot(CollectionImg)
    changed = SchemaSnapshot(
        model_name=applied.model_name,
        storage="file",
        mapping_mode=applied.mapping_mode,
        format_version=applied.format_version,
        pydantic_schema=applied.pydantic_schema,
        fields=applied.fields,
        system_columns=applied.system_columns,
        content_hash="x",
        mime_patterns=("application/pdf",),
        content_capabilities=("download",),
    )
    report = detect_drift(
        model_snapshot=changed, applied_snapshot=applied, live_schema=None
    )
    codes = {d.code for d in report.diagnostics}
    assert "applied_storage_changed" in codes
    assert "applied_mime_patterns_changed" in codes
    assert "applied_content_capabilities_changed" in codes


def test_live_without_model_warning() -> None:
    live = build_live_source_schema(
        resource_id="s",
        resource_kind="sheet",
        headers=["a"],
    )
    applied = build_model_snapshot(Sample)
    report = detect_drift(
        model_snapshot=None, applied_snapshot=applied, live_schema=live
    )
    assert any(d.code == "live_without_model" for d in report.diagnostics)


def test_live_optional_missing_and_type_mismatch() -> None:
    model = build_model_snapshot(Sample)
    live = build_live_source_schema(
        resource_id="s",
        resource_kind="sheet",
        headers=["Full Name"],
        sample_rows=[["Ann"]],
    )
    report = detect_drift(model_snapshot=model, applied_snapshot=None, live_schema=live)
    codes = {d.code for d in report.diagnostics}
    assert "live_missing_optional" in codes

    live_mismatch = build_live_source_schema(
        resource_id="s",
        resource_kind="sheet",
        headers=["Full Name", "age", "extra"],
        sample_rows=[["Ann", "not-int", "x"]],
    )
    report2 = detect_drift(
        model_snapshot=model, applied_snapshot=None, live_schema=live_mismatch
    )
    codes2 = {d.code for d in report2.diagnostics}
    assert "live_type_mismatch" in codes2 or "live_extra_columns" in codes2


def test_types_compatible_branches() -> None:
    assert _types_compatible("str", "Any") is True
    assert _types_compatible("Optional[str]", "str") is True
    assert _types_compatible("int", "float") is True
    assert _types_compatible("str", "datetime") is True
    assert _types_compatible("bool", "str") is False


def test_build_model_snapshot_generics_and_legacy() -> None:
    snap = build_model_snapshot(TypedModel)
    assert "None" in snap.fields[1].python_type
    legacy = build_model_snapshot(LegacySheet)
    assert legacy.model_name == "Legacy"


def test_content_capabilities_image_wildcard() -> None:
    snap = build_model_snapshot(CollectionImg)
    assert "download" in snap.content_capabilities


def test_live_schema_hash_auto() -> None:
    live = LiveSourceSchema(
        resource_id="r",
        resource_kind="sheet",
        fields=(),
    )
    assert live.content_hash
    assert compute_live_schema_hash(live) == live.content_hash


def test_snapshot_from_dict_without_hash() -> None:
    snap = build_model_snapshot(Sample)
    data = json.loads(serialize_snapshot(snap))
    del data["content_hash"]
    restored = snapshot_from_dict(data)
    assert restored.content_hash


def test_schema_registry_roundtrip(tmp_path) -> None:
    registry = SchemaRegistry(str(tmp_path / "schemas"))
    snap = build_model_snapshot(Sample)
    path = registry.save(snap)
    assert path.endswith("Sample.current.json")
    loaded = registry.load("Sample")
    assert loaded is not None
    registry.save(snap, applied=True)
    assert registry.load("Sample", applied=True) is not None
    assert registry.load("Missing") is None


def test_build_live_collection_and_membership() -> None:
    live = build_live_collection_schema(
        folder_id="f",
        metadata_headers=["tag"],
        sample_rows=[["a"]],
        member_count=1,
    )
    assert live.resource_kind == "collection_metadata"
    diags = detect_membership_drift(model_member_ids=["a"], live_member_ids=["b"])
    assert len(diags) == 2


def test_drift_field_added_removed_and_type_changed() -> None:
    v1 = build_model_snapshot(Sample)
    v2_fields = (
        *v1.fields,
        GDMFieldMeta(
            name="extra",
            source_name="extra",
            python_type="str",
            json_schema={},
            required=False,
        ),
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
            for f in v2_fields
        ],
        "format_version": v1.format_version,
        "mapping_mode": v1.mapping_mode,
        "model_name": v1.model_name,
        "pydantic_schema": v1.pydantic_schema,
        "storage": v1.storage,
        "system_columns": list(v1.system_columns),
    }
    v2 = SchemaSnapshot(
        model_name=v1.model_name,
        storage=v1.storage,
        mapping_mode=v1.mapping_mode,
        format_version=v1.format_version,
        pydantic_schema=v1.pydantic_schema,
        fields=v2_fields,
        system_columns=v1.system_columns,
        content_hash=compute_schema_hash(body),
    )
    report = detect_drift(model_snapshot=v2, applied_snapshot=v1, live_schema=None)
    codes = {d.code for d in report.diagnostics}
    assert "applied_field_added" in codes

    type_changed_fields = tuple(
        GDMFieldMeta(
            name=f.name,
            source_name=f.source_name,
            python_type="float" if f.name == "age" else f.python_type,
            json_schema=f.json_schema,
            index=f.index,
            required=f.required,
        )
        for f in v1.fields
    )
    body2 = {
        "fields": [
            {
                "name": f.name,
                "source_name": f.source_name,
                "python_type": f.python_type,
                "json_schema": dict(f.json_schema),
                "index": f.index,
                "required": f.required,
            }
            for f in type_changed_fields
        ],
        "format_version": v1.format_version,
        "mapping_mode": v1.mapping_mode,
        "model_name": v1.model_name,
        "pydantic_schema": v1.pydantic_schema,
        "storage": v1.storage,
        "system_columns": list(v1.system_columns),
    }
    v_type = SchemaSnapshot(
        model_name=v1.model_name,
        storage=v1.storage,
        mapping_mode=v1.mapping_mode,
        format_version=v1.format_version,
        pydantic_schema=v1.pydantic_schema,
        fields=type_changed_fields,
        system_columns=v1.system_columns,
        content_hash=compute_schema_hash(body2),
    )
    report2 = detect_drift(model_snapshot=v_type, applied_snapshot=v1, live_schema=None)
    assert any(d.code == "applied_type_changed" for d in report2.diagnostics)


def test_load_snapshot_json() -> None:
    snap = build_model_snapshot(Sample)
    loaded = load_snapshot_json(serialize_snapshot(snap))
    assert loaded.model_name == "Sample"


def test_file_storage_system_columns_and_generics() -> None:
    class FileModel(GDMModel, storage="file", mapping="partial"):
        body: str

    snap = build_model_snapshot(FileModel)
    assert snap.system_columns == ("_gdm_file_id", "_gdm_version")

    class UnionModel(GDMModel, storage="sheet", mapping="partial"):
        values: list[int]

    union_snap = build_model_snapshot(UnionModel)
    assert "list" in union_snap.fields[0].python_type.lower()

    class LegacySource(GDMModel, storage="sheet", mapping="partial"):
        title: str = Field(source="Title")

    legacy = build_model_snapshot(LegacySource)
    assert legacy.fields[0].source_name == "Title"


def test_live_partial_mapping_preserves_unknown() -> None:
    model = build_model_snapshot(Sample)
    partial = SchemaSnapshot(
        model_name=model.model_name,
        storage=model.storage,
        mapping_mode="partial",
        format_version=model.format_version,
        pydantic_schema=model.pydantic_schema,
        fields=model.fields,
        system_columns=model.system_columns,
        content_hash=model.content_hash,
    )
    live = build_live_source_schema(
        resource_id="s",
        resource_kind="sheet",
        headers=["Full Name", "age", "bonus"],
        sample_rows=[["Ann", "1", "x"]],
    )
    report = detect_drift(
        model_snapshot=partial,
        applied_snapshot=None,
        live_schema=live,
    )
    assert any(d.code == "live_unknown_preserved" for d in report.diagnostics)


def test_empty_drift_report() -> None:
    report: DriftReport = detect_drift(
        model_snapshot=None, applied_snapshot=None, live_schema=None
    )
    assert report.diagnostics[0].severity is DriftSeverity.INFO
