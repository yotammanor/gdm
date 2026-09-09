"""Direct unit tests for gdm.codecs encode/decode helpers."""

from __future__ import annotations

import pytest

from gdm.codecs import (
    BYTES_MIME,
    JSON_MIME,
    TEXT_MIME,
    YAML_MIME,
    codec_body_field_names,
    codec_mime_type,
    decode_body,
    encode_body,
    load_mapping_payload,
    validate_codec_model,
)
from gdm.errors import (
    InvalidModelConfigError,
    MissingOptionalDependencyError,
    UnknownSourceFieldError,
    UnsupportedOperationError,
)
from gdm.fields import Field
from gdm.model import GDMModel


class JsonDoc(GDMModel, storage="file", codec="json", mapping="permissive"):
    id: str = Field(primary_key=True)
    title: str
    body: str


class JsonStrict(GDMModel, storage="file", codec="json", mapping="strict"):
    id: str = Field(primary_key=True)
    title: str


class JsonPartial(GDMModel, storage="file", codec="json", mapping="partial"):
    id: str = Field(primary_key=True)
    title: str = Field(source="Title")


class TextNote(GDMModel, storage="file", codec="text", mapping="partial"):
    id: str = Field(primary_key=True)
    text: str


class BinaryAsset(GDMModel, storage="file", codec="bytes", mime="application/pdf"):
    id: str = Field(primary_key=True)
    data: bytes


class BinaryDefaultMime(GDMModel, storage="file", codec="bytes"):
    id: str = Field(primary_key=True)
    data: bytes


class YamlCfg(GDMModel, storage="file", codec="yaml", mapping="permissive"):
    id: str = Field(primary_key=True)
    name: str


class SheetRow(GDMModel, storage="sheet", mapping="partial"):
    id: str = Field(primary_key=True)
    title: str


class MixedBodyFields(GDMModel, storage="file", codec="json", mapping="partial"):
    id: str = Field(primary_key=True)
    drive_id: str = Field(drive="id")
    tag: str | None = Field(default=None, metadata="tag")
    title: str


def test_codec_body_field_names_excludes_non_body_targets() -> None:
    names = codec_body_field_names(MixedBodyFields)
    assert names == ["title"]


def test_validate_codec_model_no_codec_is_noop() -> None:
    validate_codec_model(SheetRow)


def test_validate_codec_model_text_requires_str_annotation() -> None:
    with pytest.raises(InvalidModelConfigError, match="typed str"):

        class BadText(GDMModel, storage="file", codec="text"):
            id: str = Field(primary_key=True)
            note: int


def test_validate_codec_model_accepts_optional_str_annotation() -> None:
    class OptionalText(GDMModel, storage="file", codec="text"):
        id: str = Field(primary_key=True)
        note: str | None

    validate_codec_model(OptionalText)


def test_validate_codec_model_accepts_union_str_annotation() -> None:
    class UnionText(GDMModel, storage="file", codec="text"):
        id: str = Field(primary_key=True)
        note: str | None

    validate_codec_model(UnionText)


def test_codec_mime_types() -> None:
    assert codec_mime_type(JsonDoc) == JSON_MIME
    assert codec_mime_type(TextNote) == TEXT_MIME
    assert codec_mime_type(BinaryAsset) == "application/pdf"
    assert codec_mime_type(BinaryDefaultMime) == BYTES_MIME
    pytest.importorskip("yaml")
    assert codec_mime_type(YamlCfg) == YAML_MIME


def test_codec_mime_type_unsupported_raises() -> None:
    class NoCodec(GDMModel, storage="sheet"):
        id: str = Field(primary_key=True)

    with pytest.raises(UnsupportedOperationError, match="Unsupported codec"):
        codec_mime_type(NoCodec)


def test_encode_json_preserves_unknown_and_existing_payload() -> None:
    raw = encode_body(
        JsonDoc,
        {"title": "T", "body": "B"},
        {"extra": 1},
        preserve_unknown=True,
        existing_payload={"legacy": "keep", "title": "old"},
    )
    payload = __import__("json").loads(raw.decode())
    assert payload["title"] == "T"
    assert payload["body"] == "B"
    assert payload["extra"] == 1
    assert payload["legacy"] == "keep"


def test_encode_json_without_preserve_unknown() -> None:
    raw = encode_body(
        JsonDoc,
        {"title": "T", "body": "B"},
        {"extra": 1},
        preserve_unknown=False,
    )
    payload = __import__("json").loads(raw.decode())
    assert payload == {"title": "T", "body": "B"}


def test_encode_yaml_roundtrip() -> None:
    pytest.importorskip("yaml")
    raw = encode_body(YamlCfg, {"name": "prod"}, {"extra": True})
    mapped, unknown = decode_body(YamlCfg, raw)
    assert mapped["name"] == "prod"
    assert unknown["extra"] is True


def test_encode_text_and_bytes_bodies() -> None:
    text_raw = encode_body(TextNote, {"text": "hello"}, {})
    assert text_raw == b"hello"
    bytes_raw = encode_body(BinaryAsset, {"data": b"\x00\x01"}, {})
    assert bytes_raw == b"\x00\x01"


def test_encode_text_missing_field_raises() -> None:
    with pytest.raises(UnsupportedOperationError, match="missing from record"):
        encode_body(TextNote, {}, {})


def test_encode_bytes_missing_or_invalid_raises() -> None:
    with pytest.raises(UnsupportedOperationError, match="missing from record"):
        encode_body(BinaryAsset, {}, {})
    with pytest.raises(UnsupportedOperationError, match="must be bytes"):
        encode_body(BinaryAsset, {"data": "not-bytes"}, {})


def test_encode_unsupported_codec_raises() -> None:
    with pytest.raises(UnsupportedOperationError, match="Unsupported codec"):
        encode_body(SheetRow, {"title": "x"}, {})


def test_decode_json_non_object_raises() -> None:
    with pytest.raises(UnsupportedOperationError, match="expects an object"):
        decode_body(JsonDoc, b"[1, 2, 3]")


def test_decode_yaml_non_mapping_raises() -> None:
    pytest.importorskip("yaml")
    with pytest.raises(UnsupportedOperationError, match="expects a mapping"):
        decode_body(YamlCfg, b"- item\n")


def test_decode_unsupported_codec_raises() -> None:
    with pytest.raises(UnsupportedOperationError, match="Unsupported codec"):
        decode_body(SheetRow, b"{}")


def test_split_payload_strict_unknown_raises() -> None:
    with pytest.raises(UnknownSourceFieldError, match="Unknown source field"):
        decode_body(JsonStrict, b'{"title": "x", "unexpected": 1}')


def test_split_payload_partial_keeps_unknown() -> None:
    mapped, unknown = decode_body(JsonPartial, b'{"Title": "x", "extra": 1}')
    assert mapped["title"] == "x"
    assert unknown["extra"] == 1


def test_split_payload_maps_source_names() -> None:
    mapped, unknown = decode_body(JsonPartial, b'{"Title": "mapped"}')
    assert mapped["title"] == "mapped"
    assert unknown == {}


def test_load_mapping_payload_json_and_yaml() -> None:
    assert load_mapping_payload(JsonDoc, b'{"a": 1}') == {"a": 1}
    pytest.importorskip("yaml")
    assert load_mapping_payload(YamlCfg, b"name: prod\n") == {"name": "prod"}


def test_load_mapping_payload_wrong_codec_or_shape() -> None:
    with pytest.raises(UnsupportedOperationError, match="requires json or yaml"):
        load_mapping_payload(TextNote, b"hello")
    with pytest.raises(UnsupportedOperationError, match="expects a mapping"):
        load_mapping_payload(JsonDoc, b"[]")


def test_yaml_import_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real_import = builtins.__import__

    def blocked_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "yaml":
            raise ImportError("blocked")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    with pytest.raises(MissingOptionalDependencyError, match="yaml"):
        encode_body(YamlCfg, {"name": "x"}, {})
