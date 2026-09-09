"""Tests for GDM field metadata."""

from __future__ import annotations

from pydantic import BaseModel

from gdm.fields import Field, get_gdm_field_metadata, source_name


class Sample(BaseModel):
    id: int = Field(primary_key=True, source="ID")
    title: str = Field(index=True, unique=True)
    owner_id: str | None = Field(default=None, foreign_key="User.id")
    folder: str | None = Field(default=None, drive_link=True)


def test_field_wraps_pydantic_metadata() -> None:
    metadata = get_gdm_field_metadata(Sample.model_fields["id"])
    assert metadata.primary_key is True
    assert metadata.source == "ID"
    assert get_gdm_field_metadata(Sample.model_fields["title"]).index is True
    assert get_gdm_field_metadata(Sample.model_fields["title"]).unique is True
    assert (
        get_gdm_field_metadata(Sample.model_fields["owner_id"]).foreign_key == "User.id"
    )
    assert get_gdm_field_metadata(Sample.model_fields["folder"]).drive_link is True


def test_source_name_falls_back_to_python_name() -> None:
    assert source_name("title", Sample.model_fields["title"]) == "title"
    assert source_name("id", Sample.model_fields["id"]) == "ID"


def test_field_default_factory() -> None:
    class WithDefault(BaseModel):
        tags: list[str] = Field(default_factory=list, index=True)

    instance = WithDefault()
    assert instance.tags == []
    assert get_gdm_field_metadata(WithDefault.model_fields["tags"]).index is True
