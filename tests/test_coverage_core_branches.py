"""Additional branch coverage to reach >=95% on assigned core modules."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from googleapiclient.errors import HttpError

from gdm.auth import build_drive_service, build_sheets_service
from gdm.errors import NotFoundError, PermissionDeniedError
from gdm.fields import Field
from gdm.model import GDMModel
from gdm.query import (
    BooleanExpr,
    BoolOp,
    CompareOp,
    Comparison,
    FieldRef,
    MethodCall,
    MethodOp,
)
from gdm.retry import execute_with_retry, normalize_http_error


class Note(GDMModel, storage="file", codec="json"):
    id: str = Field(primary_key=True)
    body: str
    tag: str | None = None


class Coll(GDMModel, storage="collection"):
    id: str = Field(primary_key=True)
    title: str = Field(metadata="title")


def test_field_ref_boolean_composition_operators() -> None:
    left = FieldRef(Note, "tag")
    right = Comparison(FieldRef(Note, "body"), CompareOp.EQ, "x")
    assert isinstance(left & right, BooleanExpr)
    assert isinstance(left | right, BooleanExpr)
    assert isinstance(~left, BooleanExpr)


def test_comparison_and_method_call_boolean_operators() -> None:
    comp = Comparison(FieldRef(Note, "body"), CompareOp.EQ, "a")
    call = MethodCall(FieldRef(Note, "tag"), MethodOp.IS_NONE, None)
    assert isinstance(comp & call, BooleanExpr)
    assert isinstance(comp | call, BooleanExpr)
    assert isinstance(~comp, BooleanExpr)
    assert isinstance(call & comp, BooleanExpr)
    assert isinstance(call | comp, BooleanExpr)
    assert isinstance(~call, BooleanExpr)


def test_boolean_expr_chained_operators() -> None:
    inner = BooleanExpr(BoolOp.AND, (FieldRef(Note, "body"),))
    outer = inner & Comparison(FieldRef(Note, "id"), CompareOp.EQ, "1")
    assert isinstance(outer, BooleanExpr)
    assert isinstance(inner | outer, BooleanExpr)
    assert isinstance(~inner, BooleanExpr)


def test_as_expr_rejects_non_expression() -> None:
    from gdm.query import _as_expr

    with pytest.raises(Exception, match="Expected a query expression"):
        _as_expr(42)


def test_evaluate_comparison_ge_branch() -> None:
    from gdm.query import evaluate

    item = Note(id="1", body="a")
    assert evaluate(Comparison(FieldRef(Note, "id"), CompareOp.GE, "1"), item)


def test_normalize_http_error_raises_mapped_exceptions() -> None:
    resp = MagicMock(status=404)
    err = HttpError(resp, b"missing")
    assert isinstance(normalize_http_error(err), NotFoundError)


def test_execute_with_retry_reraises_normalized_auth_errors() -> None:
    from gdm.retry import RetryPolicy

    resp = MagicMock(status=401)

    def fail() -> None:
        raise HttpError(resp, b"unauthorized")

    policy = RetryPolicy(max_attempts=3, initial_delay=0.0, max_delay=0.0, jitter=0.0)
    with pytest.raises(PermissionDeniedError):
        execute_with_retry(fail, policy=policy)


def test_build_services_unwrapped() -> None:
    creds = MagicMock()
    with patch("gdm.auth.build") as build:
        build.return_value = MagicMock()
        assert build_drive_service(creds, wrap=False) is build.return_value
        assert build_sheets_service(creds, wrap=False) is build.return_value


def test_model_assign_primary_key_collection_pk_field() -> None:
    item = Coll(id="local-key", title="hello")
    assert item.gdm_assign_primary_key() == "local-key"


def test_model_content_handle_lazy_load() -> None:
    from gdm.collection.backend import CollectionBackend
    from tests.fakes import FakeDrive

    item = Coll(id="1", title="T")
    inventory_item = MagicMock()
    backend = CollectionBackend(FakeDrive(), project_id="p")
    handle = MagicMock()
    with patch.object(backend, "content_handle", return_value=handle):
        item.gdm_bind_collection(
            locator=MagicMock(),
            drive_version="1",
            drive_fingerprint="fp",
            metadata_version="1",
            metadata_fingerprint="mfp",
            inventory_item=inventory_item,
            collection_backend=backend,
            persisted=True,
        )
        assert item.content is handle
        assert item.content is handle


def test_model_gdm_codec_and_mapping_helpers() -> None:
    assert Note.gdm_codec() == "json"
    assert Note.gdm_mapping() == "partial"
    assert Note.gdm_collection_path() is None


def test_model_from_source_python_field_name_passthrough() -> None:
    row = Note.gdm_from_source({"id": "1", "body": "text"})
    assert row.id == "1"
    assert row.body == "text"


def test_field_level_multiple_targets_rejected() -> None:
    from gdm.errors import InvalidModelConfigError

    with pytest.raises(InvalidModelConfigError, match="mutually exclusive"):
        Field(source="n", property="p")
