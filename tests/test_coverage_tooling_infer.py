"""Branch coverage for inference and code generation."""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal

import pytest

from gdm import Field, GDMModel
from gdm.explore_types import DriveResource, ResourceKind
from gdm.infer import (
    exec_generated_model,
    generate_model_code,
    infer_column_from_samples,
    infer_from_collection_inventory,
    infer_from_json_object,
    infer_from_json_text,
    infer_from_sheet_headers,
    infer_from_text_lines,
    keyword_like,
    propose_binding,
    sanitize_identifier,
)


def test_sanitize_identifier_keyword_and_numeric() -> None:
    assert keyword_like("class") is True
    assert sanitize_identifier("2024 Revenue") == "field_2024_Revenue"
    assert sanitize_identifier("class") == "class_"
    used: set[str] = {"x"}
    assert sanitize_identifier("x", used=used) == "x_2"


def test_classify_scalar_types() -> None:
    assert (
        infer_column_from_samples([True, False], source_name="b").python_type == "bool"
    )
    assert infer_column_from_samples([1, 2], source_name="i").python_type == "int"
    assert infer_column_from_samples([1.5], source_name="f").python_type == "float"
    col = infer_column_from_samples([Decimal("1.5")], source_name="d")
    assert "Decimal" in col.python_type
    col = infer_column_from_samples([datetime(2024, 1, 1)], source_name="dt")
    assert "datetime" in col.python_type
    col = infer_column_from_samples([date(2024, 1, 1)], source_name="d")
    assert "date" in col.python_type
    col = infer_column_from_samples([[1]], source_name="lst")
    assert "list" in col.python_type
    col = infer_column_from_samples([{"a": 1}], source_name="obj")
    assert "dict" in col.python_type
    col = infer_column_from_samples(["2024-01-01T12:00:00Z"], source_name="ts")
    assert "datetime" in col.python_type
    col = infer_column_from_samples(["2024-01-01"], source_name="d")
    assert "date" in col.python_type
    col = infer_column_from_samples(["not-a-number"], source_name="s")
    assert "str" in col.python_type


def test_merge_types_union_and_empty() -> None:
    col = infer_column_from_samples(["a", 1, True], source_name="mixed")
    assert "|" in col.python_type
    empty = infer_column_from_samples([], source_name="empty")
    assert empty.confidence == 0.0
    all_null = infer_column_from_samples([None, ""], source_name="nulls")
    assert all_null.confidence == 0.2


def test_infer_collection_metadata_and_extended_drive() -> None:
    resources = (
        DriveResource(
            "folder",
            "Assets",
            "application/vnd.google-apps.folder",
            ResourceKind.FOLDER,
            "root",
            path=("Assets",),
        ),
        DriveResource(
            "f1",
            "photo.png",
            "image/png",
            ResourceKind.FILE,
            "folder",
        ),
        DriveResource(
            "f2",
            "doc.pdf",
            "application/pdf",
            ResourceKind.FILE,
            "folder",
        ),
    )
    result = infer_from_collection_inventory(
        resources,
        folder_id="folder",
        model_name="Asset",
        metadata_headers=["tag", "_gdm_id", "status"],
        metadata_rows=[["launch", "x", "ok"]],
        include_extended_drive_fields=True,
    )
    drive_fields = {c.drive_field for c in result.columns if c.drive_field}
    assert "description" in drive_fields
    assert any(c.identifier == "tag" for c in result.columns)
    assert len(result.binding.mime_patterns) >= 1


def test_infer_json_branches() -> None:
    empty_list = infer_from_json_text("[]", model_name="Empty")
    assert empty_list.columns == ()
    list_of_dicts = infer_from_json_text(
        json.dumps([{"a": 1}, {"a": 2}]), model_name="Rows"
    )
    assert any(c.identifier == "a" for c in list_of_dicts.columns)
    list_scalar = infer_from_json_text(json.dumps([1, 2, 3]), model_name="Nums")
    assert list_scalar.columns[0].identifier == "value"
    scalar = infer_from_json_text('"hello"', model_name="Scalar")
    assert scalar.columns[0].python_type == "str"
    obj = infer_from_json_object({"title": "Hi"}, model_name="Doc", storage="file")
    assert obj.binding.storage == "file"


def test_generate_model_code_imports_and_compile_roundtrip() -> None:
    result = infer_from_sheet_headers(
        ["Full Name", "Amount", "When"],
        [["Ann", "1.5", "2024-01-01T00:00:00Z"]],
        model_name="Person",
    )
    code = generate_model_code(result)
    assert 'source="Full Name"' in code
    assert "source_name=" not in code
    assert "from datetime import datetime" in code

    result_decimal = infer_from_json_object(
        {"cost": Decimal("9.99"), "day": date(2024, 1, 1)},
        model_name="Ledger",
    )
    code_decimal = generate_model_code(result_decimal)
    assert "from decimal import Decimal" in code_decimal
    assert "from datetime import date" in code_decimal

    compiled = exec_generated_model(code, model_name="Person")
    assert issubclass(compiled, GDMModel)


def test_generate_model_code_collection_header_variants() -> None:
    resources = (
        DriveResource(
            "f",
            "Mix",
            "application/vnd.google-apps.folder",
            ResourceKind.FOLDER,
            None,
        ),
        DriveResource("a", "a.png", "image/png", ResourceKind.FILE, "f"),
        DriveResource("b", "b.jpg", "image/jpeg", ResourceKind.FILE, "f"),
    )
    result = infer_from_collection_inventory(resources, folder_id="f", model_name="Mix")
    code = generate_model_code(result)
    assert 'drive="id"' in code
    assert "primary_key=True" in code

    text_result = infer_from_text_lines(["line"], model_name="Lines")
    text_code = generate_model_code(text_result)
    assert 'codec="text"' in text_code

    json_result = infer_from_json_text('{"x": 1}', model_name="JsonDoc")
    json_code = generate_model_code(json_result)
    assert 'codec="json"' in json_code

    empty = infer_from_json_text("[]", model_name="EmptyModel")
    empty_code = generate_model_code(empty)
    assert "pass" in empty_code


def test_propose_binding_and_guess_primary_key() -> None:
    result = infer_from_sheet_headers(
        ["email", "name"],
        [["a@x.com", "Ann"]],
        model_name="User",
    )
    binding = propose_binding(result)
    assert binding.primary_key == "email"


def test_infer_sheet_skips_gdm_headers() -> None:
    result = infer_from_sheet_headers(["_gdm_id", "title"], [["x", "Hi"]])
    assert len(result.columns) == 1


def test_infer_collection_no_dominant_mime() -> None:
    resources = (
        DriveResource(
            "f",
            "Empty",
            "application/vnd.google-apps.folder",
            ResourceKind.FOLDER,
            None,
        ),
    )
    result = infer_from_collection_inventory(resources, folder_id="f")
    assert result.binding.mime_patterns == ()


def test_exec_generated_model_missing_raises() -> None:
    with pytest.raises(ValueError, match="did not define"):
        exec_generated_model("x = 1", model_name="Missing")


class _PlainModel(GDMModel, storage="sheet", mapping="partial"):
    name: str = Field(source="name")


def test_generated_code_with_explicit_source_field() -> None:
    from gdm.infer import BindingProposal, InferenceResult, InferredColumn

    col = InferredColumn(
        source_name="name",
        python_type="str",
        confidence=1.0,
        nullable=False,
        identifier="name",
        target_kind="source",
    )
    result = InferenceResult(
        model_name="Plain",
        columns=(col,),
        binding=BindingProposal(storage="sheet", resource_hint="hint"),
        source_kind="sheet",
    )
    code = generate_model_code(result)
    assert 'source="name"' in code
    assert "source_name=" not in code
