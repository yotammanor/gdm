"""Deterministic type inference and lightweight GDMModel code generation."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from gdm.explore_types import DriveResource, ResourceKind
from gdm.tooling.mime import MimeCategory, categorize_mime, mime_selector_for_category

_BOOL_TRUE = frozenset({"true", "t", "yes", "y", "1"})
_BOOL_FALSE = frozenset({"false", "f", "no", "n", "0"})
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$"
)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_NON_IDENTIFIER_RE = re.compile(r"[^A-Za-z0-9_]+")

_BUILTIN_DRIVE_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("drive_id", "str", "id"),
    ("name", "str", "name"),
    ("mime_type", "str", "mimeType"),
    ("description", "str | None", "description"),
    ("starred", "bool | None", "starred"),
    ("created_time", "str | None", "createdTime"),
    ("modified_time", "str | None", "modifiedTime"),
    ("web_view_link", "str | None", "webViewLink"),
)


@dataclass(frozen=True, slots=True)
class InferenceEvidence:
    """Evidence supporting an inferred type."""

    kind: str
    detail: str
    weight: float = 1.0


@dataclass(frozen=True, slots=True)
class InferredColumn:
    """Inferred type for a single column or JSON path."""

    source_name: str
    python_type: str
    confidence: float
    nullable: bool
    identifier: str
    target_kind: str = "metadata"
    drive_field: str | None = None
    read_only: bool = False
    evidence: tuple[InferenceEvidence, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class BindingProposal:
    """Suggested binding for an inferred model."""

    storage: str
    resource_hint: str
    collection_path: str | None = None
    folder_id: str | None = None
    mime_patterns: tuple[str, ...] = field(default_factory=tuple)
    mapping_mode: str = "partial"
    primary_key: str | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class InferenceResult:
    """Full inference output for a source."""

    model_name: str
    columns: tuple[InferredColumn, ...]
    binding: BindingProposal
    source_kind: str


def sanitize_identifier(source_name: str, *, used: set[str] | None = None) -> str:
    """Convert a source header/name to a valid Python identifier."""
    cleaned = source_name.strip()
    if _IDENTIFIER_RE.match(cleaned) and not keyword_like(cleaned):
        candidate = cleaned
    else:
        candidate = _NON_IDENTIFIER_RE.sub("_", cleaned)
        candidate = re.sub(r"_+", "_", candidate).strip("_")
        if not candidate or candidate[0].isdigit():
            candidate = f"field_{candidate or 'value'}"
        if keyword_like(candidate):
            candidate = f"{candidate}_"

    if used is not None:
        base = candidate
        suffix = 2
        while candidate in used:
            candidate = f"{base}_{suffix}"
            suffix += 1
        used.add(candidate)
    return candidate


def keyword_like(name: str) -> bool:
    import keyword

    return keyword.iskeyword(name)


def _normalize_scalar(value: Any) -> Any:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped == "":
            return None
        return stripped
    return value


def _classify_value(value: Any) -> tuple[str, InferenceEvidence | None]:
    if value is None:
        return "null", InferenceEvidence("null", "empty/null value", 0.5)
    if isinstance(value, bool):
        return "bool", InferenceEvidence("bool", "native bool", 1.0)
    if isinstance(value, int) and not isinstance(value, bool):
        return "int", InferenceEvidence("int", "native int", 1.0)
    if isinstance(value, float):
        return "float", InferenceEvidence("float", "native float", 1.0)
    if isinstance(value, Decimal):
        return "Decimal", InferenceEvidence("decimal", "Decimal value", 0.95)
    if isinstance(value, datetime):
        return "datetime", InferenceEvidence("datetime", "native datetime", 1.0)
    if isinstance(value, date):
        return "date", InferenceEvidence("date", "native date", 1.0)
    if isinstance(value, list):
        return "list", InferenceEvidence("list", "list value", 0.9)
    if isinstance(value, dict):
        return "dict", InferenceEvidence("dict", "dict value", 0.9)

    text = str(value).strip()
    lower = text.lower()
    if lower in _BOOL_TRUE | _BOOL_FALSE:
        return "bool", InferenceEvidence("bool_string", f"boolean-like '{text}'", 0.85)
    if _ISO_DATETIME_RE.match(text):
        return "datetime", InferenceEvidence("datetime_string", text, 0.8)
    if _ISO_DATE_RE.match(text):
        return "date", InferenceEvidence("date_string", text, 0.8)
    try:
        if "." in text:
            float(text)
            return "float", InferenceEvidence("float_string", text, 0.75)
        int(text)
        return "int", InferenceEvidence("int_string", text, 0.75)
    except ValueError:
        pass
    return "str", InferenceEvidence("string", text[:40], 0.7)


def _merge_types(types: set[str]) -> str:
    non_null = types - {"null"}
    if not non_null:
        return "Any"
    if len(non_null) == 1:
        return next(iter(non_null))
    ordered = [
        "bool",
        "int",
        "float",
        "Decimal",
        "date",
        "datetime",
        "str",
        "list",
        "dict",
    ]
    present = [t for t in ordered if t in non_null]
    rest = sorted(non_null - set(present))
    union_members = present + rest
    if set(union_members) <= {"int", "float"}:
        return "float"
    if len(union_members) == 1:
        return union_members[0]
    return " | ".join(union_members)


def infer_column_from_samples(
    samples: Sequence[Any],
    *,
    source_name: str = "column",
    target_kind: str = "metadata",
    drive_field: str | None = None,
    read_only: bool = False,
) -> InferredColumn:
    """Infer a Python type from sample values for one column."""
    normalized = [_normalize_scalar(v) for v in samples]
    observed_types: set[str] = set()
    evidence: list[InferenceEvidence] = []
    for value in normalized:
        kind, ev = _classify_value(value)
        observed_types.add(kind)
        if ev is not None:
            evidence.append(ev)

    nullable = "null" in observed_types or any(v is None for v in normalized)
    non_null_types = observed_types - {"null"}
    merged = _merge_types(non_null_types or {"Any"})
    if nullable and "Optional" not in merged and "| None" not in merged:
        python_type = f"{merged} | None"
    else:
        python_type = merged

    confidence = _confidence_score(normalized, non_null_types, evidence)
    identifier = sanitize_identifier(source_name)
    return InferredColumn(
        source_name=source_name,
        python_type=python_type,
        confidence=confidence,
        nullable=nullable,
        identifier=identifier,
        target_kind=target_kind,
        drive_field=drive_field,
        read_only=read_only,
        evidence=tuple(evidence[:10]),
    )


def _confidence_score(
    values: Sequence[Any],
    types: set[str],
    evidence: Sequence[InferenceEvidence],
) -> float:
    if not values:
        return 0.0
    non_empty = [v for v in values if v is not None]
    if not non_empty:
        return 0.2
    if len(types) == 1:
        base = 0.95
    elif types <= {"int", "float"}:
        base = 0.85
    elif len(types) <= 2:
        base = 0.65
    else:
        base = 0.45
    coverage = len(non_empty) / len(values)
    weight_bonus = min(0.05 * len(evidence), 0.1)
    return round(min(base * coverage + weight_bonus, 1.0), 3)


def _dominant_mime_patterns(
    resources: Sequence[DriveResource],
) -> tuple[str, ...]:
    counts: dict[MimeCategory, int] = {}
    for resource in resources:
        if resource.kind is ResourceKind.FOLDER:
            continue
        cat = categorize_mime(resource.mime_type)
        if cat in {MimeCategory.SHORTCUT, MimeCategory.FOLDER}:
            continue
        counts[cat] = counts.get(cat, 0) + 1
    if not counts:
        return ()
    dominant = max(counts.values())
    patterns: list[str] = []
    for cat, count in counts.items():
        if count == dominant:
            selector = mime_selector_for_category(cat)
            if selector:
                patterns.append(selector)
    return tuple(patterns)


def infer_from_collection_inventory(
    resources: Sequence[DriveResource],
    *,
    folder_id: str,
    model_name: str = "InferredCollection",
    collection_path: str | None = None,
    metadata_headers: Sequence[str] | None = None,
    metadata_rows: Sequence[Sequence[Any]] | None = None,
    include_extended_drive_fields: bool = False,
) -> InferenceResult:
    """Infer a collection model from Drive metadata without reading file bodies."""
    folder_resources = [
        r
        for r in resources
        if r.parent_id == folder_id and r.kind is not ResourceKind.FOLDER
    ]
    folder = next((r for r in resources if r.id == folder_id), None)
    path_hint = collection_path
    if path_hint is None and folder is not None:
        path_hint = "/".join(folder.path) if folder.path else folder.name

    used: set[str] = set()
    columns: list[InferredColumn] = []
    for identifier, py_type, drive_field in _BUILTIN_DRIVE_FIELDS:
        if drive_field == "id":
            columns.append(
                InferredColumn(
                    source_name=drive_field,
                    python_type=py_type,
                    confidence=1.0,
                    nullable=False,
                    identifier=sanitize_identifier(identifier, used=used),
                    target_kind="drive",
                    drive_field=drive_field,
                    read_only=True,
                    evidence=(InferenceEvidence("drive", "built-in Drive PK", 1.0),),
                )
            )
            continue
        if not include_extended_drive_fields and drive_field not in {
            "name",
            "mimeType",
        }:
            continue
        columns.append(
            InferredColumn(
                source_name=drive_field,
                python_type=py_type,
                confidence=0.95,
                nullable="None" in py_type,
                identifier=sanitize_identifier(identifier, used=used),
                target_kind="drive",
                drive_field=drive_field,
                read_only=drive_field
                in {"mimeType", "createdTime", "modifiedTime", "webViewLink"},
                evidence=(
                    InferenceEvidence("drive", f"Drive field {drive_field}", 0.95),
                ),
            )
        )

    if metadata_headers:
        samples_by_idx: dict[int, list[Any]] = {}
        if metadata_rows:
            for row in metadata_rows:
                for idx, value in enumerate(row):
                    samples_by_idx.setdefault(idx, []).append(value)
        for idx, header in enumerate(metadata_headers):
            if not header or header.startswith("_gdm_"):
                continue
            col = infer_column_from_samples(
                samples_by_idx.get(idx, []),
                source_name=header,
                target_kind="metadata",
            )
            identifier = sanitize_identifier(header, used=used)
            columns.append(
                InferredColumn(
                    source_name=col.source_name,
                    python_type=col.python_type,
                    confidence=col.confidence,
                    nullable=col.nullable,
                    identifier=identifier,
                    target_kind="metadata",
                    evidence=col.evidence,
                )
            )

    mime_patterns = _dominant_mime_patterns(folder_resources)
    binding = BindingProposal(
        storage="collection",
        resource_hint=f"Bind folder ID {folder_id} as a native Drive collection.",
        collection_path=path_hint,
        folder_id=folder_id,
        mime_patterns=mime_patterns,
        mapping_mode="partial",
        primary_key="drive_id",
        notes=(
            "Inferred from Drive list metadata only; no file bodies were read.",
            "Review MIME selectors and metadata columns before adoption.",
        ),
    )
    return InferenceResult(
        model_name=model_name,
        columns=tuple(columns),
        binding=binding,
        source_kind="collection",
    )


def infer_from_sheet_headers(
    headers: Sequence[str],
    rows: Sequence[Sequence[Any]] | None = None,
    *,
    model_name: str = "InferredModel",
) -> InferenceResult:
    """Infer model fields from sheet headers and optional sample rows."""
    columns_by_idx: dict[int, list[Any]] = {}
    if rows:
        for row in rows:
            for idx, value in enumerate(row):
                columns_by_idx.setdefault(idx, []).append(value)

    used: set[str] = set()
    columns: list[InferredColumn] = []
    for idx, header in enumerate(headers):
        if not header or header.startswith("_gdm_"):
            continue
        samples = columns_by_idx.get(idx, [])
        col = infer_column_from_samples(
            samples, source_name=header, target_kind="source"
        )
        identifier = sanitize_identifier(header, used=used)
        columns.append(
            InferredColumn(
                source_name=col.source_name,
                python_type=col.python_type,
                confidence=col.confidence,
                nullable=col.nullable,
                identifier=identifier,
                target_kind="source",
                evidence=col.evidence,
            )
        )

    binding = BindingProposal(
        storage="sheet",
        resource_hint="Bind to an existing worksheet by ID or path.",
        mapping_mode="partial",
        primary_key=_guess_primary_key(columns),
        notes=("Inferred from sheet headers; review types before binding.",),
    )
    return InferenceResult(
        model_name=model_name,
        columns=tuple(columns),
        binding=binding,
        source_kind="sheet",
    )


def _guess_primary_key(columns: Sequence[InferredColumn]) -> str | None:
    for col in columns:
        lowered = col.source_name.lower()
        if lowered in {"id", "uuid", "key", "email"} and col.confidence >= 0.7:
            return col.identifier
    return None


def infer_from_json_object(
    payload: Mapping[str, Any],
    *,
    model_name: str = "InferredDocument",
    storage: str = "file",
    codec: str = "json",
) -> InferenceResult:
    """Infer a model from a JSON object payload (explicit content sampling path)."""
    used: set[str] = set()
    columns: list[InferredColumn] = []
    for key, value in payload.items():
        col = infer_column_from_samples([value], source_name=key, target_kind="source")
        identifier = sanitize_identifier(key, used=used)
        columns.append(
            InferredColumn(
                source_name=col.source_name,
                python_type=col.python_type,
                confidence=col.confidence,
                nullable=col.nullable,
                identifier=identifier,
                target_kind="source",
                evidence=col.evidence,
            )
        )
    binding = BindingProposal(
        storage=storage,
        resource_hint="Bind to a JSON Drive file with explicit codec.",
        mapping_mode="partial",
        mime_patterns=(codec,) if codec else (),
        notes=(f"Inferred from JSON object keys via codec={codec!r}.",),
    )
    return InferenceResult(
        model_name=model_name,
        columns=tuple(columns),
        binding=binding,
        source_kind="json",
    )


def infer_from_json_text(
    raw: str, *, model_name: str = "InferredDocument", storage: str = "file"
) -> InferenceResult:
    """Infer a model from JSON text (explicit content sampling)."""
    payload = json.loads(raw)
    if isinstance(payload, list):
        if not payload:
            return InferenceResult(
                model_name=model_name,
                columns=(),
                binding=BindingProposal(
                    storage=storage, resource_hint="Empty JSON list."
                ),
                source_kind="json",
            )
        first = payload[0]
        if isinstance(first, dict):
            return infer_from_json_object(first, model_name=model_name, storage=storage)
        col = infer_column_from_samples(payload, source_name="value")
        return InferenceResult(
            model_name=model_name,
            columns=(col,),
            binding=BindingProposal(
                storage=storage, resource_hint="JSON list of scalars."
            ),
            source_kind="json",
        )
    if isinstance(payload, dict):
        return infer_from_json_object(payload, model_name=model_name, storage=storage)
    col = infer_column_from_samples([payload], source_name="value")
    return InferenceResult(
        model_name=model_name,
        columns=(col,),
        binding=BindingProposal(storage=storage, resource_hint="JSON scalar document."),
        source_kind="json",
    )


def infer_from_text_lines(
    lines: Iterable[str],
    *,
    model_name: str = "InferredText",
    field_name: str = "body",
) -> InferenceResult:
    """Infer a single-field text body model (explicit content sampling)."""
    samples = [line.rstrip("\n") for line in lines if line.strip()]
    col = infer_column_from_samples(
        samples, source_name=field_name, target_kind="source"
    )
    binding = BindingProposal(
        storage="file",
        resource_hint="Bind to a plain-text Drive file with codec='text'.",
        mapping_mode="partial",
        mime_patterns=("text/plain",),
        notes=("One model field holds the complete UTF-8 file body.",),
    )
    return InferenceResult(
        model_name=model_name,
        columns=(col,),
        binding=binding,
        source_kind="text",
    )


def _field_kwarg(col: InferredColumn) -> str:
    parts: list[str] = []
    if col.target_kind == "drive" and col.drive_field:
        parts.append(f'drive="{col.drive_field}"')
    elif col.target_kind == "metadata":
        parts.append(f'metadata="{col.source_name}"')
    elif col.target_kind == "source":
        parts.append(f'source="{col.source_name}"')
    if col.identifier == "drive_id" and col.drive_field == "id":
        parts.append("primary_key=True")
    if not parts:
        return ""
    return ", " + ", ".join(parts)


def _format_field(col: InferredColumn) -> str:
    kwargs = _field_kwarg(col)
    if col.drive_field == "id":
        default = ' = Field(..., drive="id", primary_key=True)'
    elif kwargs:
        default = f" = Field(...{kwargs})"
    elif col.source_name != col.identifier:
        default = f' = Field(..., source="{col.source_name}")'
    else:
        default = ""
    return f"    {col.identifier}: {col.python_type}{default}"


def _class_header(result: InferenceResult) -> str:
    binding = result.binding
    parts = [
        f'class {result.model_name}(GDMModel, storage="{binding.storage}"',
    ]
    if binding.collection_path:
        parts.append(f', collection="{binding.collection_path}"')
    if binding.mime_patterns:
        if len(binding.mime_patterns) == 1:
            parts.append(f', mime="{binding.mime_patterns[0]}"')
        else:
            joined = ", ".join(f'"{p}"' for p in binding.mime_patterns)
            parts.append(f", mime=({joined})")
    if binding.storage == "file" and result.source_kind in {"json", "text"}:
        codec = "json" if result.source_kind == "json" else "text"
        parts.append(f', codec="{codec}"')
    parts.append(f', mapping="{binding.mapping_mode}"):')
    return "".join(parts)


def generate_model_code(result: InferenceResult) -> str:
    """Generate importable GDMModel source code without external formatters."""
    import_lines = [
        "from __future__ import annotations",
        "",
        "from gdm import GDMModel, Field",
    ]
    if any("datetime" in col.python_type for col in result.columns):
        import_lines.append("from datetime import datetime")
    if any(
        "date" in col.python_type and "datetime" not in col.python_type
        for col in result.columns
    ):
        import_lines.append("from datetime import date")
    if any("Decimal" in col.python_type for col in result.columns):
        import_lines.append("from decimal import Decimal")

    lines = import_lines
    lines.extend(
        [
            "",
            "",
            _class_header(result),
            f'    """Inferred {result.source_kind} model; review before binding."""',
            "",
        ]
    )
    if not result.columns:
        lines.append("    pass")
    else:
        for col in result.columns:
            lines.append(_format_field(col))
    lines.append("")
    return "\n".join(lines)


def propose_binding(result: InferenceResult) -> BindingProposal:
    """Return the binding proposal for an inference result."""
    return result.binding


def exec_generated_model(code: str, *, model_name: str) -> type[Any]:
    """Execute generated model code and return the model class (for tests)."""
    namespace: dict[str, Any] = {}
    exec(code, namespace)
    model = namespace.get(model_name)
    if model is None:
        msg = f"Generated code did not define {model_name!r}."
        raise ValueError(msg)
    return model
