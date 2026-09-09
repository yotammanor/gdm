"""GDM field metadata and ``Field()`` helper."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, cast

from pydantic.fields import Field as PydanticField
from pydantic.fields import FieldInfo

from gdm.errors import InvalidModelConfigError

GDM_METADATA_KEY = "gdm"


@dataclass(frozen=True, slots=True)
class GDMFieldMetadata:
    """Introspectable GDM field metadata."""

    primary_key: bool = False
    source: str | None = None
    drive: str | None = None
    metadata: str | None = None
    property: str | None = None
    app_property: str | None = None
    index: bool = False
    unique: bool = False
    foreign_key: str | None = None
    drive_link: bool = False


def _validate_field_metadata(metadata: GDMFieldMetadata) -> None:
    targets = [
        metadata.source,
        metadata.drive,
        metadata.metadata,
        metadata.property,
        metadata.app_property,
    ]
    set_targets = [target for target in targets if target is not None]
    if len(set_targets) > 1:
        msg = (
            "Field storage targets (source, drive, metadata, property, "
            "app_property) are mutually exclusive."
        )
        raise InvalidModelConfigError(msg)


def Field(  # noqa: N802
    default: Any = ...,
    *,
    default_factory: Any = None,
    primary_key: bool = False,
    source: str | None = None,
    drive: str | None = None,
    metadata: str | None = None,
    property: str | None = None,
    app_property: str | None = None,
    index: bool = False,
    unique: bool = False,
    foreign_key: str | None = None,
    drive_link: bool = False,
    **kwargs: Any,
) -> Any:
    """Create a Pydantic field with attached GDM metadata."""
    field_metadata = GDMFieldMetadata(
        primary_key=primary_key,
        source=source,
        drive=drive,
        metadata=metadata,
        property=property,
        app_property=app_property,
        index=index,
        unique=unique,
        foreign_key=foreign_key,
        drive_link=drive_link,
    )
    _validate_field_metadata(field_metadata)
    json_schema_extra = kwargs.pop("json_schema_extra", None)
    if json_schema_extra is None:
        extra: dict[str, Any] = {GDM_METADATA_KEY: asdict(field_metadata)}
    elif callable(json_schema_extra):
        original = json_schema_extra

        def merged(schema: dict[str, Any], handler: Any) -> dict[str, Any]:
            result = original(schema, handler)
            result.setdefault(GDM_METADATA_KEY, asdict(field_metadata))
            return result

        extra = merged  # type: ignore[assignment]
    elif isinstance(json_schema_extra, dict):
        extra = {**json_schema_extra, GDM_METADATA_KEY: asdict(field_metadata)}
    else:
        extra = {GDM_METADATA_KEY: asdict(field_metadata)}

    schema_extra = cast(Any, extra)
    if default_factory is not None:
        return PydanticField(
            default_factory=default_factory,
            json_schema_extra=schema_extra,
            **kwargs,
        )
    if default is ...:
        return PydanticField(json_schema_extra=schema_extra, **kwargs)
    return PydanticField(default, json_schema_extra=schema_extra, **kwargs)


def get_gdm_field_metadata(field_info: FieldInfo) -> GDMFieldMetadata:
    """Return GDM metadata for a Pydantic ``FieldInfo``."""
    extra = field_info.json_schema_extra
    raw: dict[str, Any] | None = None
    if isinstance(extra, dict):
        raw = extra.get(GDM_METADATA_KEY)
    if raw is None:
        return GDMFieldMetadata()
    return GDMFieldMetadata(
        primary_key=bool(raw.get("primary_key", False)),
        source=raw.get("source"),
        drive=raw.get("drive"),
        metadata=raw.get("metadata"),
        property=raw.get("property"),
        app_property=raw.get("app_property"),
        index=bool(raw.get("index", False)),
        unique=bool(raw.get("unique", False)),
        foreign_key=raw.get("foreign_key"),
        drive_link=bool(raw.get("drive_link", False)),
    )


def field_storage_target(
    field_name: str,
    field_info: FieldInfo,
    *,
    storage: str | None,
) -> tuple[str, str]:
    """Return ``(target_kind, source_name)`` for a model field."""
    metadata = get_gdm_field_metadata(field_info)
    if metadata.source is not None:
        return "source", metadata.source
    if metadata.drive is not None:
        return "drive", metadata.drive
    if metadata.metadata is not None:
        return "metadata", metadata.metadata
    if metadata.property is not None:
        return "property", metadata.property
    if metadata.app_property is not None:
        return "app_property", metadata.app_property
    if storage in {"collection", "file"}:
        return "metadata", field_name
    return "source", field_name


def source_name(field_name: str, field_info: FieldInfo) -> str:
    """Return the source-side name for a model field."""
    metadata = get_gdm_field_metadata(field_info)
    if metadata.source is not None:
        return metadata.source
    if metadata.metadata is not None:
        return metadata.metadata
    if metadata.drive is not None:
        return metadata.drive
    if metadata.property is not None:
        return metadata.property
    if metadata.app_property is not None:
        return metadata.app_property
    return field_name
