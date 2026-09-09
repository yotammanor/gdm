"""Structured file codec encode/decode helpers for legacy file models."""

from __future__ import annotations

import json
from typing import Any

from gdm.errors import (
    InvalidModelConfigError,
    MissingOptionalDependencyError,
    UnsupportedOperationError,
)
from gdm.fields import get_gdm_field_metadata, source_name
from gdm.model import GDMModel

BYTES_MIME = "application/octet-stream"
TEXT_MIME = "text/plain"
JSON_MIME = "application/json"
YAML_MIME = "application/x-yaml"


def _yaml_module() -> Any:
    try:
        import yaml
    except ImportError as exc:
        raise MissingOptionalDependencyError(
            "YAML codec requires PyYAML; "
            "install with: pip install 'google-drive-model[yaml]'",
            extra="yaml",
        ) from exc
    return yaml


def codec_body_field_names(model: type[GDMModel]) -> list[str]:
    """Return field names serialized into the file body (text/bytes codecs)."""
    pk_field = model.primary_key_field()
    names: list[str] = []
    for name, info in model.model_fields.items():
        metadata = get_gdm_field_metadata(info)
        if (
            metadata.drive
            or metadata.metadata
            or metadata.property
            or metadata.app_property
        ):
            continue
        if pk_field is not None and name == pk_field:
            continue
        names.append(name)
    return names


def _annotation_is(annotation: Any, expected: type) -> bool:
    import types
    import typing

    if annotation is expected:
        return True
    if isinstance(annotation, str):
        normalized = annotation.replace(" ", "")
        if normalized == expected.__name__:
            return True
        if normalized in {
            f"{expected.__name__}|None",
            f"None|{expected.__name__}",
            f"typing.Optional[{expected.__name__}]",
        }:
            return True
    origin = typing.get_origin(annotation)
    if origin in {typing.Union, types.UnionType}:
        args = [arg for arg in typing.get_args(annotation) if arg is not type(None)]
        return len(args) == 1 and _annotation_is(args[0], expected)
    return False


def validate_codec_model(model: type[GDMModel]) -> None:
    """Validate codec-specific model shape at class definition time."""
    codec = model.gdm_codec()
    if codec is None:
        return
    if codec in {"text", "bytes"}:
        body_fields = codec_body_field_names(model)
        if len(body_fields) != 1:
            msg = (
                f"Model {model.__name__} with codec={codec!r} requires exactly one "
                f"body field (excluding primary key and drive/metadata targets); "
                f"found {len(body_fields)}: {body_fields}."
            )
            raise InvalidModelConfigError(msg)
        field_name = body_fields[0]
        info = model.model_fields[field_name]
        if codec == "text" and not _annotation_is(info.annotation, str):
            msg = (
                f"Model {model.__name__}.{field_name} must be typed str "
                f"for codec='text'; got {info.annotation!r}."
            )
            raise InvalidModelConfigError(msg)
        if codec == "bytes" and not _annotation_is(info.annotation, bytes):
            msg = (
                f"Model {model.__name__}.{field_name} must be typed bytes for "
                f"codec='bytes'; got {info.annotation!r}."
            )
            raise InvalidModelConfigError(msg)


def codec_mime_type(model: type[GDMModel]) -> str:
    """Return the Drive MIME type for a codec model's file body."""
    codec = model.gdm_codec()
    if codec == "json":
        return JSON_MIME
    if codec == "text":
        return TEXT_MIME
    if codec == "bytes":
        patterns = model.__gdm_mime_patterns__
        if patterns:
            return patterns[0]
        return BYTES_MIME
    if codec == "yaml":
        return YAML_MIME
    msg = f"Unsupported codec {codec!r}."
    raise UnsupportedOperationError(msg)


def encode_body(
    model: type[GDMModel],
    values: dict[str, Any],
    unknown: dict[str, Any],
    *,
    preserve_unknown: bool = True,
    existing_payload: dict[str, Any] | None = None,
) -> bytes:
    """Serialize mapped record values to a file body."""
    codec = model.gdm_codec()
    if codec == "json":
        payload = dict(values)
        if preserve_unknown:
            payload.update(unknown)
        if existing_payload is not None and preserve_unknown:
            payload = {**existing_payload, **payload}
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if codec == "yaml":
        yaml = _yaml_module()
        payload = dict(values)
        if preserve_unknown:
            payload.update(unknown)
        if existing_payload is not None and preserve_unknown:
            payload = {**existing_payload, **payload}
        text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
        return text.encode("utf-8")
    if codec == "text":
        field_name = codec_body_field_names(model)[0]
        key = source_name(field_name, model.model_fields[field_name])
        raw = values.get(key, values.get(field_name))
        if raw is None:
            msg = f"Text codec body field {field_name!r} is missing from record values."
            raise UnsupportedOperationError(msg)
        return str(raw).encode("utf-8")
    if codec == "bytes":
        field_name = codec_body_field_names(model)[0]
        key = source_name(field_name, model.model_fields[field_name])
        raw = values.get(key, values.get(field_name))
        if raw is None:
            msg = (
                f"Bytes codec body field {field_name!r} is missing from record values."
            )
            raise UnsupportedOperationError(msg)
        if isinstance(raw, bytes):
            return raw
        msg = f"Bytes codec field {field_name!r} must be bytes, got {type(raw)!r}."
        raise UnsupportedOperationError(msg)
    msg = f"Unsupported codec {codec!r}."
    raise UnsupportedOperationError(msg)


def decode_body(
    model: type[GDMModel],
    raw: bytes,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Deserialize a file body into mapped values and unknown fields."""
    codec = model.gdm_codec()
    if codec == "json":
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            msg = "JSON codec expects an object at the file root."
            raise UnsupportedOperationError(msg)
        return _split_payload(model, payload)
    if codec == "yaml":
        yaml = _yaml_module()
        payload = yaml.safe_load(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            msg = "YAML codec expects a mapping at the file root."
            raise UnsupportedOperationError(msg)
        return _split_payload(model, payload)
    if codec == "text":
        field_name = codec_body_field_names(model)[0]
        return {field_name: raw.decode("utf-8")}, {}
    if codec == "bytes":
        field_name = codec_body_field_names(model)[0]
        return {field_name: raw}, {}
    msg = f"Unsupported codec {codec!r}."
    raise UnsupportedOperationError(msg)


def _split_payload(
    model: type[GDMModel],
    payload: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split a mapping payload into mapped values and unknown fields."""
    mapping = model.gdm_mapping()
    source_to_field = model.source_field_map()
    declared = set(model.model_fields)
    declared_sources = set(source_to_field) | declared
    mapped: dict[str, Any] = {}
    unknown: dict[str, Any] = {}
    for key, value in payload.items():
        if key in source_to_field:
            mapped[source_to_field[key]] = value
        elif key in declared:
            mapped[key] = value
        elif mapping == "strict":
            from gdm.errors import UnknownSourceFieldError

            raise UnknownSourceFieldError(
                f"Unknown source field {key!r} for {model.__name__}",
                fields=(key,),
            )
        elif mapping in {"partial", "permissive"} and key not in declared_sources:
            unknown[key] = value
    return mapped, unknown


def load_mapping_payload(model: type[GDMModel], raw: bytes) -> dict[str, Any]:
    """Load a mapping-root payload from raw file bytes (JSON/YAML codecs)."""
    codec = model.gdm_codec()
    if codec == "yaml":
        yaml = _yaml_module()
        payload = yaml.safe_load(raw.decode("utf-8"))
    elif codec == "json":
        payload = json.loads(raw.decode("utf-8"))
    else:
        msg = f"load_mapping_payload requires json or yaml codec, got {codec!r}."
        raise UnsupportedOperationError(msg)
    if not isinstance(payload, dict):
        msg = f"{codec} codec expects a mapping at the file root."
        raise UnsupportedOperationError(msg)
    return payload
