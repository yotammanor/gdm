"""Branch coverage for gdm.model configuration and binding."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gdm.errors import (
    InvalidForeignKeyError,
    InvalidModelConfigError,
    UnknownSourceFieldError,
)
from gdm.fields import Field, field_storage_target, get_gdm_field_metadata
from gdm.model import DriveObject, DriveObjectMixin, GDMModel, utc_now_iso


def test_table_conflicts_with_non_sheet_storage() -> None:
    with pytest.raises(InvalidModelConfigError, match="table=True"):

        class Bad(GDMModel, storage="file", table=True):
            name: str


def test_storage_file_alias_resolves_to_collection() -> None:
    class Legacy(GDMModel, storage="file"):
        name: str

    assert Legacy.gdm_storage() == "collection"


def test_mime_patterns_from_string_and_tuple() -> None:
    class One(GDMModel, storage="collection", mime="text/plain"):
        name: str

    class Many(GDMModel, storage="collection", mime=("a/*", "b/*")):
        name: str

    assert One.__gdm_mime_patterns__ == ("text/plain",)
    assert Many.__gdm_mime_patterns__ == ("a/*", "b/*")


def test_subclass_inherits_mime_patterns() -> None:
    class Base(GDMModel, storage="collection", mime="image/*"):
        name: str

    class Child(Base):
        extra: str | None = None

    class Narrowed(Base, mime="image/png"):
        pass

    assert Child.__gdm_mime_patterns__ == ("image/*",)
    assert Narrowed.__gdm_mime_patterns__ == ("image/png",)


def test_subclass_inherits_storage_codec_and_mapping() -> None:
    class Base(GDMModel, storage="collection", mapping="permissive"):
        drive_id: str | None = Field(default=None, drive="id")

    class Child(Base):
        extra: str | None = Field(default=None, metadata="extra")

    assert Child.gdm_storage() == "collection"
    assert Child.is_collection_model()
    assert Child.gdm_mapping() == "permissive"

    class Doc(GDMModel, storage="file", codec="json"):
        id: str = Field(primary_key=True)
        title: str

    class DocChild(Doc):
        pass

    assert DocChild.gdm_storage() == "file"
    assert DocChild.gdm_codec() == "json"


def test_subclass_may_override_inherited_storage() -> None:
    class Base(GDMModel, storage="file", codec="json"):
        id: str = Field(primary_key=True)
        title: str

    class Row(Base, storage="sheet"):
        pass

    assert Row.gdm_storage() == "sheet"
    assert Row.gdm_codec() is None


def test_resource_identity_is_not_inherited() -> None:
    class Base(DriveObject, collection="Assets"):
        pass

    class Child(Base):
        pass

    assert Base.gdm_collection_path() == "Assets"
    assert Child.gdm_collection_path() is None
    assert Child.gdm_storage() == "collection"


def test_inherited_mixin_fields_survive_deeper_subclassing() -> None:
    """Regression: pydantic read class attrs through the FieldRef metaclass hook."""
    from gdm.collection.collection import collection_model_with_mime

    class Mixin(GDMModel, DriveObjectMixin, storage="collection"):
        pass

    class Nested(Mixin):
        pass

    for model in (Nested, collection_model_with_mime(Mixin, "image/*")):
        info = model.model_fields["name"]
        assert get_gdm_field_metadata(info).drive == "name"
        assert info.default is None


def test_drive_object_base_needs_no_storage_keyword() -> None:
    class Asset(DriveObject, collection="Assets"):
        campaign: str | None = Field(default=None, metadata="campaign")

    assert Asset.gdm_storage() == "collection"
    assert Asset.is_collection_model()
    assert Asset.gdm_collection_path() == "Assets"
    assert {"drive_id", "name", "mime_type", "campaign"} <= set(Asset.model_fields)
    targets = {
        name: field_storage_target(name, info, storage="collection")[0]
        for name, info in Asset.model_fields.items()
    }
    assert targets["name"] == "drive"
    assert targets["campaign"] == "metadata"


def test_invalid_foreign_key_empty_segments() -> None:
    with pytest.raises(InvalidForeignKeyError):

        class Bad(GDMModel, storage="sheet"):
            ref: str = Field(foreign_key=".id")


def test_multiple_storage_targets_on_field_rejected() -> None:
    with pytest.raises(InvalidModelConfigError):

        class Bad(GDMModel, storage="sheet"):
            name: str = Field(source="n", metadata="m")


def test_collection_model_allows_drive_metadata_combo_at_class_level() -> None:
    class Asset(GDMModel, storage="collection"):
        drive_id: str | None = Field(default=None, drive="id")
        title: str = Field(metadata="title")

    assert Asset.model_fields["drive_id"] is not None


def test_primary_key_falls_back_to_drive_id_field() -> None:
    class Doc(GDMModel, storage="collection"):
        drive_id: str | None = Field(default=None, drive="id")
        title: str

    assert Doc.primary_key_field() == "drive_id"


def test_gdm_extra_only_in_permissive_mode() -> None:
    class Partial(GDMModel, storage="sheet", mapping="partial"):
        title: str

    partial = Partial(title="x")
    partial._gdm_state.unknown_fields = {"legacy": 1}
    assert partial.gdm_extra == {}

    class Permissive(GDMModel, storage="sheet", mapping="permissive"):
        title: str

    perm = Permissive(title="x")
    perm._gdm_state.unknown_fields = {"legacy": 1}
    assert perm.gdm_extra == {"legacy": 1}


def test_gdm_from_source_strict_and_permissive() -> None:
    class Row(GDMModel, storage="sheet", mapping="strict"):
        title: str

    with pytest.raises(UnknownSourceFieldError):
        Row.gdm_from_source({"unknown_col": "x"})

    class Loose(GDMModel, storage="sheet", mapping="permissive"):
        title: str

    inst = Loose.gdm_from_source({"title": "ok", "extra": "keep"})
    assert inst.title == "ok"
    assert inst._gdm_state.unknown_fields["extra"] == "keep"


def test_gdm_assign_primary_key_collection_with_locator() -> None:
    from gdm.backends.base import RecordLocator

    class Item(GDMModel, storage="collection"):
        id: str = Field(primary_key=True)
        title: str

    item = Item(id="local", title="t")
    item.gdm_bind_collection(
        locator=RecordLocator(
            storage="collection", resource_id="folder", record_key="rk"
        ),
        drive_version="1",
        drive_fingerprint="fp",
        metadata_version="1",
        metadata_fingerprint="mfp",
        persisted=True,
    )
    assert item.gdm_assign_primary_key() == "rk"


def test_gdm_assign_primary_key_without_field_uses_unknown_system_id() -> None:
    class NoPk(GDMModel, storage="sheet"):
        title: str

    from gdm.model import SYSTEM_ID_FIELD

    first = NoPk(title="a")
    key = first.gdm_assign_primary_key()
    second = NoPk(title="b")
    second._gdm_state.unknown_fields[SYSTEM_ID_FIELD] = key
    assert second.gdm_assign_primary_key() == key


def test_gdm_assign_primary_key_generates_on_model_field() -> None:
    class WithPk(GDMModel, storage="sheet"):
        id: str | None = Field(default=None, primary_key=True)
        title: str

    row = WithPk(title="x")
    generated = row.gdm_assign_primary_key()
    assert row.id == generated


def test_content_handle_requires_inventory_and_backend() -> None:
    class Asset(GDMModel, storage="collection"):
        id: str = Field(primary_key=True)
        title: str

    bare = Asset(id="1", title="x")
    with pytest.raises(InvalidModelConfigError, match="materialized collection"):
        _ = bare.content

    asset = Asset(id="1", title="x")
    asset.gdm_bind_collection(
        locator=MagicMock(),
        drive_version="1",
        drive_fingerprint="fp",
        metadata_version="1",
        metadata_fingerprint="mfp",
        inventory_item=MagicMock(),
        collection_backend=MagicMock(),
        persisted=True,
    )
    with pytest.raises(InvalidModelConfigError, match="Collection backend"):
        _ = asset.content


def test_gdm_record_key_uses_locator_when_persisted() -> None:
    from gdm.backends.base import RecordLocator

    class Tag(GDMModel, storage="file", codec="json"):
        id: str = Field(primary_key=True)
        name: str

    tag = Tag(id="local", name="n")
    tag.gdm_bind(
        locator=RecordLocator(
            storage="file", resource_id="folder", record_key="stored"
        ),
        version="1",
        fingerprint="fp",
        persisted=True,
    )
    assert tag.gdm_record_key() == "stored"


def test_field_source_map_skips_drive_only_fields() -> None:
    class Mixed(GDMModel, DriveObjectMixin, storage="collection"):
        id: str = Field(primary_key=True)
        title: str = Field(metadata="title")

    mapping = Mixed.field_source_map()
    assert "drive_id" not in mapping
    assert mapping["title"] == "title"


def test_field_storage_target_defaults() -> None:
    class SheetRow(GDMModel, storage="sheet"):
        title: str

    kind, name = field_storage_target(
        "title", SheetRow.model_fields["title"], storage="sheet"
    )
    assert kind == "source"
    assert name == "title"

    class Coll(GDMModel, storage="collection"):
        title: str

    kind, name = field_storage_target(
        "title", Coll.model_fields["title"], storage="collection"
    )
    assert kind == "metadata"
    assert name == "title"


def test_uses_legacy_file_codec() -> None:
    class Encoded(GDMModel, storage="file", codec="json"):
        id: str = Field(primary_key=True)

    assert Encoded.uses_legacy_file_codec() is True
    assert Encoded.is_collection_model() is False


def test_gdm_to_source_includes_unknown_when_requested() -> None:
    class Row(GDMModel, storage="sheet", mapping="partial"):
        title: str

    row = Row(title="hello")
    row._gdm_state.unknown_fields = {"legacy": "v"}
    payload = row.gdm_to_source(include_unknown=True)
    assert payload["legacy"] == "v"


def test_utc_now_iso_format() -> None:
    stamp = utc_now_iso()
    assert stamp.endswith(("+00:00", "Z")) or "+00:00" in stamp


def test_custom_model_name_kwarg() -> None:
    class Internal(GDMModel, storage="sheet", __gdm_model_name__="PublicName"):
        title: str

    assert Internal.gdm_model_name() == "PublicName"


def test_get_gdm_field_metadata_defaults() -> None:
    class Plain(GDMModel, storage="sheet"):
        title: str

    meta = get_gdm_field_metadata(Plain.model_fields["title"])
    assert meta.primary_key is False
    assert meta.source is None
