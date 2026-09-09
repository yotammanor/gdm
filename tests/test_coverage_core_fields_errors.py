"""Branch coverage for gdm.fields and gdm.errors."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from gdm.errors import (
    CommitOperation,
    ConflictError,
    PartialCommitError,
    ScanLimitExceeded,
    UnknownSourceFieldError,
)
from gdm.fields import Field, get_gdm_field_metadata


def test_field_rejects_multiple_storage_targets() -> None:
    with pytest.raises(Exception, match="mutually exclusive"):
        Field(source="a", drive="b")


def test_field_json_schema_extra_dict_merge() -> None:
    class WithExtra(BaseModel):
        name: str = Field(json_schema_extra={"description": "label"}, index=True)

    meta = get_gdm_field_metadata(WithExtra.model_fields["name"])
    assert meta.index is True
    extra = WithExtra.model_fields["name"].json_schema_extra
    assert isinstance(extra, dict)
    assert extra["description"] == "label"
    assert "gdm" in extra


def test_field_json_schema_extra_callable_merge() -> None:
    def enrich(schema: dict, handler: object) -> dict:
        del handler
        schema["title"] = "Name"
        return schema

    class WithCallable(BaseModel):
        name: str = Field(json_schema_extra=enrich, unique=True)

    extra = WithCallable.model_fields["name"].json_schema_extra
    assert callable(extra)
    schema: dict = {}
    merged = extra(schema, None)  # type: ignore[operator]
    assert merged["title"] == "Name"
    assert merged["gdm"]["unique"] is True


def test_field_json_schema_extra_unknown_type_still_attaches_metadata() -> None:
    class WithWeirdExtra(BaseModel):
        name: str = Field(json_schema_extra="plain-string", primary_key=True)  # type: ignore[arg-type]

    meta = get_gdm_field_metadata(WithWeirdExtra.model_fields["name"])
    assert meta.primary_key is True


def test_partial_commit_error_str() -> None:
    err = PartialCommitError(
        message="failed halfway",
        completed=[CommitOperation(action="create", model_name="M", locator="loc")],
        pending=[CommitOperation(action="update", model_name="M", locator="loc2")],
    )
    assert str(err) == "failed halfway"


def test_conflict_error_attributes() -> None:
    err = ConflictError(
        "stale",
        locator="loc",
        expected_version="1",
        actual_version="2",
    )
    assert err.locator == "loc"
    assert err.expected_version == "1"
    assert err.actual_version == "2"


def test_scan_limit_exceeded_attributes() -> None:
    err = ScanLimitExceeded("too many", limit=10, observed=11, model_name="Widget")
    assert err.limit == 10
    assert err.observed == 11
    assert err.model_name == "Widget"


def test_unknown_source_field_error_fields_tuple() -> None:
    err = UnknownSourceFieldError("bad field", fields=("extra",))
    assert err.fields == ("extra",)
