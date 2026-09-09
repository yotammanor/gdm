"""Canonical schema snapshots, hashing, and three-way drift detection."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar, Protocol, runtime_checkable

SCHEMA_FORMAT_VERSION = 2


class DriftSeverity(StrEnum):
    """Severity level for schema drift diagnostics."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class DriftCategory(StrEnum):
    """High-level drift categories for collection models."""

    MEMBERSHIP = "membership"
    DRIVE_METADATA = "drive_metadata"
    METADATA_TABLE = "metadata_table"
    CONTENT_CAPABILITY = "content_capability"
    MODEL = "model"


@dataclass(frozen=True, slots=True)
class GDMFieldMeta:
    """GDM field metadata merged into canonical schema snapshots."""

    name: str
    source_name: str
    python_type: str
    json_schema: Mapping[str, Any]
    target_kind: str = "source"
    drive_field: str | None = None
    read_only: bool = False
    index: bool = False
    unique: bool = False
    primary_key: bool = False
    link: str | None = None
    alias: str | None = None
    default: Any = None
    required: bool = True


@dataclass(frozen=True, slots=True)
class SchemaSnapshot:
    """Deterministic snapshot of a model or live source schema."""

    model_name: str
    storage: str
    mapping_mode: str
    format_version: int
    pydantic_schema: Mapping[str, Any]
    fields: tuple[GDMFieldMeta, ...]
    system_columns: tuple[str, ...]
    content_hash: str
    collection_path: str | None = None
    mime_patterns: tuple[str, ...] = ()
    content_capabilities: tuple[str, ...] = ()
    read_only_drive_fields: tuple[str, ...] = ()

    def to_canonical_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable canonical representation."""
        return {
            "collection_path": self.collection_path,
            "content_capabilities": list(self.content_capabilities),
            "content_hash": self.content_hash,
            "fields": [_field_meta_to_dict(f) for f in self.fields],
            "format_version": self.format_version,
            "mapping_mode": self.mapping_mode,
            "mime_patterns": list(self.mime_patterns),
            "model_name": self.model_name,
            "pydantic_schema": self.pydantic_schema,
            "read_only_drive_fields": list(self.read_only_drive_fields),
            "storage": self.storage,
            "system_columns": list(self.system_columns),
        }


@dataclass(frozen=True, slots=True)
class LiveSourceField:
    """Field discovered from a live Drive or Sheet source."""

    source_name: str
    inferred_type: str
    sample_values: tuple[str, ...] = ()
    nullable: bool = False


@dataclass(frozen=True, slots=True)
class LiveSourceSchema:
    """Schema representation inferred from live source data."""

    resource_id: str
    resource_kind: str
    fields: tuple[LiveSourceField, ...]
    extra_columns: tuple[str, ...] = ()
    row_count: int | None = None
    content_hash: str = ""

    def __post_init__(self) -> None:
        if not self.content_hash:
            object.__setattr__(self, "content_hash", compute_live_schema_hash(self))


@dataclass(frozen=True, slots=True)
class DriftDiagnostic:
    """Actionable drift diagnostic with severity."""

    code: str
    message: str
    severity: DriftSeverity
    field: str | None = None
    action: str | None = None
    category: DriftCategory = DriftCategory.MODEL


@dataclass(frozen=True, slots=True)
class DriftReport:
    """Three-way drift among model, last-applied, and live schemas."""

    model_snapshot: SchemaSnapshot | None
    applied_snapshot: SchemaSnapshot | None
    live_schema: LiveSourceSchema | None
    diagnostics: tuple[DriftDiagnostic, ...] = field(default_factory=tuple)

    @property
    def has_errors(self) -> bool:
        return any(d.severity is DriftSeverity.ERROR for d in self.diagnostics)

    @property
    def has_warnings(self) -> bool:
        return any(d.severity is DriftSeverity.WARNING for d in self.diagnostics)


@runtime_checkable
class GDMModelProtocol(Protocol):
    """Protocol expected from ``gdm.model.GDMModel``."""

    @classmethod
    def model_json_schema(cls) -> dict[str, Any]: ...

    model_fields: ClassVar[Mapping[str, Any]]

    @classmethod
    def gdm_model_name(cls) -> str: ...

    @classmethod
    def gdm_storage(cls) -> str | None: ...

    @classmethod
    def gdm_mapping(cls) -> str: ...

    @classmethod
    def gdm_collection_path(cls) -> str | None: ...


def _canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)


def compute_schema_hash(payload: Mapping[str, Any]) -> str:
    """Compute a stable SHA-256 hash of canonical JSON."""
    digest = hashlib.sha256(_canonical_json(payload).encode("utf-8"))
    return digest.hexdigest()


def compute_live_schema_hash(live: LiveSourceSchema) -> str:
    """Hash a live source schema without its precomputed hash field."""
    payload = {
        "extra_columns": list(live.extra_columns),
        "fields": [
            {
                "inferred_type": f.inferred_type,
                "nullable": f.nullable,
                "source_name": f.source_name,
            }
            for f in live.fields
        ],
        "resource_id": live.resource_id,
        "resource_kind": live.resource_kind,
        "row_count": live.row_count,
    }
    return compute_schema_hash(payload)


def _field_meta_to_dict(meta: GDMFieldMeta) -> dict[str, Any]:
    return {
        "alias": meta.alias,
        "default": meta.default,
        "drive_field": meta.drive_field,
        "index": meta.index,
        "json_schema": dict(meta.json_schema),
        "link": meta.link,
        "name": meta.name,
        "primary_key": meta.primary_key,
        "python_type": meta.python_type,
        "read_only": meta.read_only,
        "required": meta.required,
        "source_name": meta.source_name,
        "target_kind": meta.target_kind,
        "unique": meta.unique,
    }


def _python_type_name(annotation: Any) -> str:
    if annotation is None:
        return "None"
    if isinstance(annotation, str):
        return annotation
    origin = getattr(annotation, "__origin__", None)
    args = getattr(annotation, "__args__", ())
    name = getattr(annotation, "__name__", None) or str(annotation)
    if origin is not None:
        arg_names = ", ".join(_python_type_name(a) for a in args)
        origin_name = getattr(origin, "__name__", str(origin))
        return f"{origin_name}[{arg_names}]"
    return name


def _gdm_extra_dict(field_info: Any) -> dict[str, Any]:
    extra = getattr(field_info, "json_schema_extra", None) or {}
    if isinstance(extra, dict):
        raw = extra.get("gdm")
        return raw if isinstance(raw, dict) else extra
    return {}


def _extract_gdm_field_meta(model: type[GDMModelProtocol]) -> tuple[GDMFieldMeta, ...]:
    """Extract GDM field metadata from a model class."""
    from gdm.collection.constants import READ_ONLY_DRIVE_FIELDS
    from gdm.fields import field_storage_target, get_gdm_field_metadata
    from gdm.fields import source_name as resolve_source_name

    schema = model.model_json_schema()
    properties = schema.get("properties", {})
    required = set(schema.get("required", []))
    fields: list[GDMFieldMeta] = []
    for name, field_info in model.model_fields.items():
        json_prop = properties.get(name, {})
        annotation = getattr(field_info, "annotation", Any)
        gdm_meta = get_gdm_field_metadata(field_info)
        legacy = _gdm_extra_dict(field_info)
        source = legacy.get("source_name") or legacy.get("source")
        if source is None and gdm_meta.source is not None:
            source = gdm_meta.source
        if source is None:
            source = resolve_source_name(name, field_info)
        storage = model.gdm_storage() if hasattr(model, "gdm_storage") else None
        target_kind, target_name = field_storage_target(
            name, field_info, storage=storage
        )
        drive_field = gdm_meta.drive
        read_only = target_kind == "drive" and target_name in READ_ONLY_DRIVE_FIELDS
        fields.append(
            GDMFieldMeta(
                name=name,
                source_name=str(source),
                python_type=_python_type_name(annotation),
                json_schema=json_prop if isinstance(json_prop, dict) else {},
                target_kind=target_kind,
                drive_field=drive_field,
                read_only=read_only,
                index=bool(gdm_meta.index or legacy.get("index", False)),
                unique=bool(gdm_meta.unique or legacy.get("unique", False)),
                primary_key=bool(
                    gdm_meta.primary_key or legacy.get("primary_key", False)
                ),
                link=legacy.get("link") or gdm_meta.foreign_key,
                alias=getattr(field_info, "alias", None),
                default=getattr(field_info, "default", None),
                required=name in required,
            )
        )
    return tuple(sorted(fields, key=lambda f: f.name))


def _model_config(
    model: type[GDMModelProtocol],
) -> tuple[str, str, str, str | None, tuple[str, ...]]:
    """Return (model_name, storage, mapping_mode, collection_path, mime_patterns)."""
    if hasattr(model, "gdm_model_name"):
        storage = model.gdm_storage()
        mapping = model.gdm_mapping()
        collection_path = (
            model.gdm_collection_path()
            if hasattr(model, "gdm_collection_path")
            else None
        )
        mime_patterns = tuple(getattr(model, "__gdm_mime_patterns__", ()))
        return (
            str(model.gdm_model_name()),
            str(storage or "sheet"),
            str(mapping),
            collection_path,
            tuple(mime_patterns),
        )

    gdm_cfg = getattr(model, "gdm_config", None) or {}
    if isinstance(gdm_cfg, dict):
        return (
            str(gdm_cfg.get("name", model.__name__)),
            str(gdm_cfg.get("storage", "sheet")),
            str(gdm_cfg.get("mapping_mode", "partial")),
            gdm_cfg.get("collection"),
            tuple(
                gdm_cfg.get("mime", ())
                if isinstance(gdm_cfg.get("mime"), tuple)
                else ()
            ),
        )
    return model.__name__, "sheet", "partial", None, ()


def _content_capabilities_for_storage(
    storage: str,
    mime_patterns: Sequence[str],
) -> tuple[str, ...]:
    from gdm.tooling.doctor import content_capabilities_for_mime

    if storage not in {"collection", "file"}:
        return ()
    caps: set[str] = set()
    for pattern in mime_patterns:
        if pattern.endswith("/*"):
            caps.update(("download", "upload"))
            continue
        caps.update(content_capabilities_for_mime(pattern))
    if not caps and storage == "collection":
        caps.update(("download", "upload", "export"))
    return tuple(sorted(caps))


def build_model_snapshot(model: type[GDMModelProtocol]) -> SchemaSnapshot:
    """Build a canonical, deterministic schema snapshot for a GDM model."""
    from gdm.collection.constants import READ_ONLY_DRIVE_FIELDS

    model_name, storage, mapping_mode, collection_path, mime_patterns = _model_config(
        model
    )
    pydantic_schema = model.model_json_schema()
    fields = _extract_gdm_field_meta(model)
    system_columns = _system_columns_for_storage(storage)
    read_only_drive = tuple(
        sorted(
            f.drive_field
            for f in fields
            if f.target_kind == "drive" and f.drive_field in READ_ONLY_DRIVE_FIELDS
        )
    )
    content_caps = _content_capabilities_for_storage(storage, mime_patterns)
    body = {
        "collection_path": collection_path,
        "content_capabilities": list(content_caps),
        "fields": [_field_meta_to_dict(f) for f in fields],
        "format_version": SCHEMA_FORMAT_VERSION,
        "mapping_mode": mapping_mode,
        "mime_patterns": list(mime_patterns),
        "model_name": model_name,
        "pydantic_schema": pydantic_schema,
        "read_only_drive_fields": list(read_only_drive),
        "storage": storage,
        "system_columns": list(system_columns),
    }
    content_hash = compute_schema_hash(body)
    return SchemaSnapshot(
        model_name=model_name,
        storage=storage,
        mapping_mode=mapping_mode,
        format_version=SCHEMA_FORMAT_VERSION,
        pydantic_schema=pydantic_schema,
        fields=fields,
        system_columns=system_columns,
        content_hash=content_hash,
        collection_path=collection_path,
        mime_patterns=mime_patterns,
        content_capabilities=content_caps,
        read_only_drive_fields=read_only_drive,
    )


def _system_columns_for_storage(storage: str) -> tuple[str, ...]:
    if storage == "sheet":
        return ("_gdm_id", "_gdm_version", "_gdm_updated_at")
    if storage in {"collection", "file"}:
        return ("_gdm_file_id", "_gdm_version")
    return ()


def snapshot_from_dict(data: Mapping[str, Any]) -> SchemaSnapshot:
    """Reconstruct a snapshot from persisted JSON."""
    fields = tuple(
        GDMFieldMeta(
            name=str(item["name"]),
            source_name=str(item.get("source_name", item["name"])),
            python_type=str(item.get("python_type", "Any")),
            json_schema=item.get("json_schema", {}),
            target_kind=str(item.get("target_kind", "source")),
            drive_field=item.get("drive_field"),
            read_only=bool(item.get("read_only", False)),
            index=bool(item.get("index", False)),
            unique=bool(item.get("unique", False)),
            primary_key=bool(item.get("primary_key", False)),
            link=item.get("link"),
            alias=item.get("alias"),
            default=item.get("default"),
            required=bool(item.get("required", True)),
        )
        for item in data.get("fields", [])
    )
    body = {
        "collection_path": data.get("collection_path"),
        "content_capabilities": list(data.get("content_capabilities", [])),
        "fields": [_field_meta_to_dict(f) for f in fields],
        "format_version": int(data.get("format_version", SCHEMA_FORMAT_VERSION)),
        "mapping_mode": str(data.get("mapping_mode", "partial")),
        "mime_patterns": list(data.get("mime_patterns", [])),
        "model_name": str(data["model_name"]),
        "pydantic_schema": data.get("pydantic_schema", {}),
        "read_only_drive_fields": list(data.get("read_only_drive_fields", [])),
        "storage": str(data.get("storage", "sheet")),
        "system_columns": list(data.get("system_columns", [])),
    }
    stored_hash = data.get("content_hash")
    content_hash = str(stored_hash) if stored_hash else compute_schema_hash(body)
    return SchemaSnapshot(
        model_name=str(data["model_name"]),
        storage=str(data.get("storage", "sheet")),
        mapping_mode=str(data.get("mapping_mode", "partial")),
        format_version=int(data.get("format_version", SCHEMA_FORMAT_VERSION)),
        pydantic_schema=data.get("pydantic_schema", {}),
        fields=fields,
        system_columns=tuple(data.get("system_columns", [])),
        content_hash=content_hash,
        collection_path=data.get("collection_path"),
        mime_patterns=tuple(data.get("mime_patterns", [])),
        content_capabilities=tuple(data.get("content_capabilities", [])),
        read_only_drive_fields=tuple(data.get("read_only_drive_fields", [])),
    )


def build_live_source_schema(
    *,
    resource_id: str,
    resource_kind: str,
    headers: Sequence[str],
    sample_rows: Sequence[Sequence[Any]] | None = None,
    row_count: int | None = None,
) -> LiveSourceSchema:
    """Build a live source schema from sheet headers and optional samples."""
    from gdm.infer import infer_column_from_samples

    fields: list[LiveSourceField] = []
    samples_by_col: dict[int, list[Any]] = {}
    if sample_rows:
        for row in sample_rows:
            for idx, value in enumerate(row):
                samples_by_col.setdefault(idx, []).append(value)

    for idx, header in enumerate(headers):
        if header.startswith("_gdm_"):
            continue
        samples = samples_by_col.get(idx, [])
        inferred = infer_column_from_samples(samples, source_name=header)
        fields.append(
            LiveSourceField(
                source_name=header,
                inferred_type=inferred.python_type,
                sample_values=tuple(str(v) for v in samples[:5] if v not in (None, "")),
                nullable=inferred.nullable,
            )
        )
    declared = {f.source_name for f in fields}
    extra = tuple(h for h in headers if h not in declared and not h.startswith("_gdm_"))
    return LiveSourceSchema(
        resource_id=resource_id,
        resource_kind=resource_kind,
        fields=tuple(fields),
        extra_columns=extra,
        row_count=row_count,
    )


def build_live_collection_schema(
    *,
    folder_id: str,
    metadata_headers: Sequence[str],
    sample_rows: Sequence[Sequence[Any]] | None = None,
    member_count: int | None = None,
) -> LiveSourceSchema:
    """Build a live metadata-table schema for a collection folder."""
    return build_live_source_schema(
        resource_id=folder_id,
        resource_kind="collection_metadata",
        headers=list(metadata_headers),
        sample_rows=sample_rows,
        row_count=member_count,
    )


def detect_membership_drift(
    *,
    model_member_ids: Sequence[str],
    live_member_ids: Sequence[str],
) -> list[DriftDiagnostic]:
    """Detect folder membership drift between model index and live folder."""
    model_set = set(model_member_ids)
    live_set = set(live_member_ids)
    diagnostics: list[DriftDiagnostic] = []
    for file_id in sorted(model_set - live_set):
        diagnostics.append(
            DriftDiagnostic(
                code="membership_missing_live",
                message=f"Model member {file_id!r} is not in the live folder.",
                severity=DriftSeverity.WARNING,
                category=DriftCategory.MEMBERSHIP,
                action="Reconcile membership or remove stale metadata row.",
            )
        )
    for file_id in sorted(live_set - model_set):
        diagnostics.append(
            DriftDiagnostic(
                code="membership_added_live",
                message=f"Live folder member {file_id!r} is not in the model index.",
                severity=DriftSeverity.INFO,
                category=DriftCategory.MEMBERSHIP,
                action="Adopt or backfill metadata for the new member.",
            )
        )
    return diagnostics


def detect_drift(
    *,
    model_snapshot: SchemaSnapshot | None,
    applied_snapshot: SchemaSnapshot | None,
    live_schema: LiveSourceSchema | None,
) -> DriftReport:
    """Compare model, last-applied, and live schemas; emit diagnostics."""
    diagnostics: list[DriftDiagnostic] = []

    if model_snapshot and applied_snapshot:
        if model_snapshot.content_hash != applied_snapshot.content_hash:
            diagnostics.extend(
                _compare_snapshots(model_snapshot, applied_snapshot, prefix="applied")
            )
        else:
            diagnostics.append(
                DriftDiagnostic(
                    code="model_applied_match",
                    message="Model schema matches last-applied snapshot.",
                    severity=DriftSeverity.INFO,
                )
            )

    if model_snapshot and live_schema:
        diagnostics.extend(
            _compare_model_to_live(model_snapshot, live_schema),
        )

    if applied_snapshot and live_schema and model_snapshot is None:
        diagnostics.append(
            DriftDiagnostic(
                code="live_without_model",
                message="Live source present but no model snapshot supplied.",
                severity=DriftSeverity.WARNING,
                action="Run inference or bind an explicit model.",
            )
        )

    if not diagnostics:
        diagnostics.append(
            DriftDiagnostic(
                code="no_schemas",
                message="No schema inputs provided for drift detection.",
                severity=DriftSeverity.INFO,
            )
        )

    return DriftReport(
        model_snapshot=model_snapshot,
        applied_snapshot=applied_snapshot,
        live_schema=live_schema,
        diagnostics=tuple(diagnostics),
    )


def _compare_snapshots(
    current: SchemaSnapshot,
    previous: SchemaSnapshot,
    *,
    prefix: str,
) -> list[DriftDiagnostic]:
    diagnostics: list[DriftDiagnostic] = []
    current_fields = {f.name: f for f in current.fields}
    previous_fields = {f.name: f for f in previous.fields}

    for name in sorted(set(previous_fields) - set(current_fields)):
        diagnostics.append(
            DriftDiagnostic(
                code=f"{prefix}_field_removed",
                message=f"Field '{name}' exists in {prefix} snapshot but not in model.",
                severity=DriftSeverity.ERROR,
                field=name,
                action="Add a migration or restore the field in the model.",
            )
        )

    for name in sorted(set(current_fields) - set(previous_fields)):
        diagnostics.append(
            DriftDiagnostic(
                code=f"{prefix}_field_added",
                message=f"Field '{name}' is new in the model.",
                severity=DriftSeverity.INFO,
                field=name,
                action="Plan an additive migration to backfill or expose the column.",
            )
        )

    for name in sorted(set(current_fields) & set(previous_fields)):
        cur = current_fields[name]
        prev = previous_fields[name]
        if cur.source_name != prev.source_name:
            diagnostics.append(
                DriftDiagnostic(
                    code=f"{prefix}_source_renamed",
                    message=(
                        f"Field '{name}' source name changed "
                        f"from '{prev.source_name}' to '{cur.source_name}'."
                    ),
                    severity=DriftSeverity.WARNING,
                    field=name,
                    action="Add a rename migration with aliases for existing data.",
                    category=(
                        DriftCategory.METADATA_TABLE
                        if cur.target_kind == "metadata"
                        else DriftCategory.DRIVE_METADATA
                    ),
                )
            )
        if cur.python_type != prev.python_type:
            diagnostics.append(
                DriftDiagnostic(
                    code=f"{prefix}_type_changed",
                    message=(
                        f"Field '{name}' type changed from "
                        f"'{prev.python_type}' to '{cur.python_type}'."
                    ),
                    severity=DriftSeverity.ERROR,
                    field=name,
                    action="Add a transform migration or mark as destructive.",
                )
            )

    if current.storage != previous.storage:
        diagnostics.append(
            DriftDiagnostic(
                code=f"{prefix}_storage_changed",
                message=(
                    f"Storage changed from '{previous.storage}' to '{current.storage}'."
                ),
                severity=DriftSeverity.ERROR,
                action="Storage changes require explicit destructive migration.",
                category=DriftCategory.MODEL,
            )
        )

    if current.mime_patterns != previous.mime_patterns:
        diagnostics.append(
            DriftDiagnostic(
                code=f"{prefix}_mime_patterns_changed",
                message=(
                    f"MIME selectors changed from {previous.mime_patterns} "
                    f"to {current.mime_patterns}."
                ),
                severity=DriftSeverity.WARNING,
                category=DriftCategory.CONTENT_CAPABILITY,
                action=(
                    "Review content capability impact; no implicit content migration."
                ),
            )
        )

    if current.content_capabilities != previous.content_capabilities:
        diagnostics.append(
            DriftDiagnostic(
                code=f"{prefix}_content_capabilities_changed",
                message="Native content capabilities changed in the model snapshot.",
                severity=DriftSeverity.INFO,
                category=DriftCategory.CONTENT_CAPABILITY,
            )
        )

    return diagnostics


def _compare_model_to_live(
    model: SchemaSnapshot,
    live: LiveSourceSchema,
) -> list[DriftDiagnostic]:
    diagnostics: list[DriftDiagnostic] = []
    model_by_source = {f.source_name: f for f in model.fields}
    live_by_source = {f.source_name: f for f in live.fields}

    for source_name in sorted(set(model_by_source) - set(live_by_source)):
        field_meta = model_by_source[source_name]
        if field_meta.required:
            diagnostics.append(
                DriftDiagnostic(
                    code="live_missing_required",
                    message=(
                        f"Required model column '{source_name}' "
                        "is missing from the live source."
                    ),
                    severity=DriftSeverity.ERROR,
                    field=field_meta.name,
                    action="Add the column to the sheet or relax model requirements.",
                )
            )
        else:
            diagnostics.append(
                DriftDiagnostic(
                    code="live_missing_optional",
                    message=f"Optional model column '{source_name}' is absent live.",
                    severity=DriftSeverity.WARNING,
                    field=field_meta.name,
                )
            )

    for source_name in sorted(set(live_by_source) - set(model_by_source)):
        if model.mapping_mode == "strict":
            diagnostics.append(
                DriftDiagnostic(
                    code="live_unknown_strict",
                    message=(
                        f"Live column '{source_name}' is not declared on the model."
                    ),
                    severity=DriftSeverity.ERROR,
                    field=source_name,
                    action="Add the field to the model or switch mapping mode.",
                )
            )
        else:
            diagnostics.append(
                DriftDiagnostic(
                    code="live_unknown_preserved",
                    message=(
                        f"Live column '{source_name}' will be preserved "
                        f"under '{model.mapping_mode}' mapping."
                    ),
                    severity=DriftSeverity.INFO,
                    field=source_name,
                )
            )

    for source_name in sorted(set(model_by_source) & set(live_by_source)):
        model_field = model_by_source[source_name]
        live_field = live_by_source[source_name]
        if not _types_compatible(model_field.python_type, live_field.inferred_type):
            diagnostics.append(
                DriftDiagnostic(
                    code="live_type_mismatch",
                    message=(
                        f"Column '{source_name}' model type "
                        f"'{model_field.python_type}' may not match live "
                        f"inference '{live_field.inferred_type}'."
                    ),
                    severity=DriftSeverity.WARNING,
                    field=model_field.name,
                    action="Inspect sample values and add a transform migration.",
                )
            )

    if live.extra_columns:
        diagnostics.append(
            DriftDiagnostic(
                code="live_extra_columns",
                message=(
                    f"Live source has extra columns: {', '.join(live.extra_columns)}."
                ),
                severity=DriftSeverity.INFO,
            )
        )

    return diagnostics


_TYPE_COMPAT_RE = re.compile(r"Optional\[(.+)\]|Union\[.+\]")


def _types_compatible(model_type: str, live_type: str) -> bool:
    if model_type == live_type:
        return True
    if live_type == "Any" or model_type == "Any":
        return True
    if "Optional" in model_type and live_type in model_type:
        return True
    if model_type in {"int", "float"} and live_type in {"int", "float"}:
        return True
    return model_type == "str" and live_type in {"str", "datetime", "date"}


def serialize_snapshot(snapshot: SchemaSnapshot) -> str:
    """Serialize a snapshot to deterministic JSON."""
    return _canonical_json(snapshot.to_canonical_dict())


def load_snapshot_json(raw: str) -> SchemaSnapshot:
    """Load a snapshot from JSON text."""
    return snapshot_from_dict(json.loads(raw))


class SchemaRegistry:
    """Read/write schema snapshots under a project schemas directory."""

    def __init__(self, schemas_dir: str) -> None:
        self.schemas_dir = schemas_dir

    def path_for(self, model_name: str, *, applied: bool = False) -> str:
        suffix = "applied" if applied else "current"
        safe = re.sub(r"[^\w.-]", "_", model_name)
        return f"{self.schemas_dir}/{safe}.{suffix}.json"

    def save(self, snapshot: SchemaSnapshot, *, applied: bool = False) -> str:
        import os

        os.makedirs(self.schemas_dir, exist_ok=True)
        path = self.path_for(snapshot.model_name, applied=applied)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(serialize_snapshot(snapshot))
            handle.write("\n")
        return path

    def load(self, model_name: str, *, applied: bool = False) -> SchemaSnapshot | None:
        import os

        path = self.path_for(model_name, applied=applied)
        if not os.path.isfile(path):
            return None
        with open(path, encoding="utf-8") as handle:
            return load_snapshot_json(handle.read())
