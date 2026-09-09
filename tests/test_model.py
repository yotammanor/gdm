"""Tests for GDMModel."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from gdm.errors import UnknownSourceFieldError
from gdm.fields import Field
from gdm.model import GDMModel
from gdm.query import Comparison


class Task(GDMModel, storage="sheet", source="tasks-sheet", mapping="partial"):
    id: str = Field(primary_key=True, source="task_id")
    status: str
    title: str


class LooseTask(GDMModel, storage="sheet", mapping="permissive"):
    id: str = Field(primary_key=True)
    status: str


class StrictTask(GDMModel, storage="sheet", mapping="strict"):
    id: str = Field(primary_key=True)
    status: str


def test_class_level_field_expressions() -> None:
    expr = Task.status == "todo"
    assert isinstance(expr, Comparison)
    assert expr.left.name == "status"
    assert expr.right == "todo"


def test_instance_field_access_unaffected() -> None:
    task = Task(id="1", status="todo", title="Write tests")
    assert task.status == "todo"
    assert task.title == "Write tests"


def test_binding_state_not_in_model_dump() -> None:
    task = Task(id="1", status="todo", title="Write tests")
    task.gdm_bind(
        locator=object(),
        version="3",
        fingerprint="abc",
        unknown_fields={"legacy": "keep"},
    )
    dumped = task.model_dump()
    assert "legacy" not in dumped
    assert dumped == {"id": "1", "status": "todo", "title": "Write tests"}


def test_partial_mapping_preserves_unknown_without_exposing() -> None:
    task = Task.gdm_from_source(
        {"task_id": "1", "status": "todo", "title": "A", "legacy": "x"},
    )
    assert task.gdm_extra == {}
    assert task._gdm_state.unknown_fields["legacy"] == "x"


def test_permissive_mapping_exposes_unknown() -> None:
    task = LooseTask.gdm_from_source(
        {"id": "1", "status": "todo", "legacy": "x"},
    )
    assert task.gdm_extra["legacy"] == "x"


def test_strict_mapping_rejects_unknown() -> None:
    with pytest.raises(UnknownSourceFieldError):
        StrictTask.gdm_from_source({"id": "1", "status": "todo", "legacy": "x"})


def test_to_source_roundtrip_preserves_unknown() -> None:
    task = LooseTask.gdm_from_source({"id": "1", "status": "todo", "legacy": "x"})
    payload = task.gdm_to_source()
    assert payload["legacy"] == "x"
    assert payload["status"] == "todo"


def test_model_class_metadata() -> None:
    assert Task.gdm_model_name() == "Task"
    assert Task.gdm_storage() == "sheet"
    assert Task.gdm_mapping() == "partial"
    assert Task.primary_key_field() == "id"
    assert Task.field_source_map()["id"] == "task_id"


def test_primary_key_cannot_be_null() -> None:
    with pytest.raises(ValidationError):
        Task(id=None, status="todo", title="bad")  # type: ignore[arg-type]
