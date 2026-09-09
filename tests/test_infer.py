"""Tests for inference and codegen."""

from __future__ import annotations

import json

from gdm.infer import (
    generate_model_code,
    infer_column_from_samples,
    infer_from_json_text,
    infer_from_sheet_headers,
    infer_from_text_lines,
    sanitize_identifier,
)


def test_weird_headers_sanitize_identifiers() -> None:
    assert sanitize_identifier("Full Name") == "Full_Name"
    assert sanitize_identifier("2024 Revenue") == "field_2024_Revenue"
    used: set[str] = set()
    first = sanitize_identifier("type", used=used)
    second = sanitize_identifier("type", used=used)
    assert first != second


def test_mixed_values_infer_union() -> None:
    col = infer_column_from_samples(["1", "2.5", "3"], source_name="amount")
    assert "float" in col.python_type or "|" in col.python_type


def test_nullable_column() -> None:
    col = infer_column_from_samples(["", None, "x"], source_name="note")
    assert col.nullable is True
    assert "None" in col.python_type


def test_bool_string_inference() -> None:
    col = infer_column_from_samples(["yes", "no", "true"], source_name="active")
    assert col.python_type == "bool"


def test_sheet_headers_weird_and_mixed() -> None:
    headers = ["User ID", "Amount ($)", "Active?", "Notes"]
    rows = [
        ["1", "10", "yes", "a"],
        ["2", "20.5", "no", ""],
        ["3", None, "true", "b"],
    ]
    result = infer_from_sheet_headers(headers, rows, model_name="Users")
    names = {c.identifier for c in result.columns}
    assert "User_ID" in names or "User ID" in names
    assert result.binding.storage == "sheet"


def test_json_inference() -> None:
    payload = {"title": "Hello", "count": 3, "published": "2024-01-01"}
    result = infer_from_json_text(json.dumps(payload), model_name="Doc")
    assert len(result.columns) == 3
    code = generate_model_code(result)
    assert "class Doc(GDMModel," in code
    assert "gdm_config" not in code
    assert "from gdm import GDMModel" in code


def test_text_lines_inference() -> None:
    result = infer_from_text_lines(["alpha", "beta", ""], model_name="Lines")
    assert len(result.columns) == 1
    assert result.source_kind == "text"


def test_generate_model_code_source_name_field() -> None:
    result = infer_from_sheet_headers(["Full Name"], [["Ann"]], model_name="Person")
    code = generate_model_code(result)
    assert 'source="Full Name"' in code
    assert "gdm_config" not in code


def test_confidence_empty_samples() -> None:
    col = infer_column_from_samples([], source_name="empty")
    assert col.confidence == 0.0
