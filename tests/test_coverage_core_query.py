"""Branch coverage for gdm.query."""

from __future__ import annotations

import pytest

from gdm.errors import QueryEvaluationError
from gdm.fields import Field
from gdm.model import GDMModel
from gdm.query import (
    BooleanExpr,
    BoolOp,
    OrderBy,
    evaluate,
    select,
    sort_key,
)


class Widget(GDMModel, storage="file", codec="json"):
    id: str = Field(primary_key=True)
    label: str
    score: int = 0
    tag: str | None = None


class Other(GDMModel, storage="file", codec="json"):
    id: str = Field(primary_key=True)
    label: str


def _widget(**kwargs: object) -> Widget:
    defaults = {"id": "1", "label": "Alpha", "score": 10, "tag": "x"}
    defaults.update(kwargs)
    return Widget(**defaults)


def test_comparison_operators() -> None:
    item = _widget(score=15, tag="abc")
    assert evaluate(Widget.score < 20, item)
    assert evaluate(Widget.score <= 15, item)
    assert evaluate(Widget.score > 5, item)
    assert evaluate(Widget.score >= 15, item)
    assert evaluate(Widget.tag != "zzz", item)


def test_method_calls_with_none_left() -> None:
    none_tag = _widget(tag=None)
    assert not evaluate(Widget.tag.contains("a"), none_tag)
    assert not evaluate(Widget.tag.startswith("a"), none_tag)
    assert not evaluate(Widget.tag.endswith("a"), none_tag)
    assert evaluate(Widget.tag.is_none(), none_tag)


def test_in_requires_iterable() -> None:
    with pytest.raises(QueryEvaluationError, match="iterable"):
        evaluate(Widget.tag.in_(42), _widget())  # type: ignore[arg-type]


def test_bare_field_ref_invalid() -> None:
    with pytest.raises(QueryEvaluationError, match="Bare field"):
        evaluate(Widget.label, _widget())


def test_wrong_instance_type_for_field() -> None:
    with pytest.raises(QueryEvaluationError, match="Cannot evaluate"):
        evaluate(Widget.label == "x", Other(id="1", label="x"))


def test_not_requires_single_operand() -> None:
    bad = BooleanExpr(BoolOp.NOT, (_widget(), _widget()))  # type: ignore[arg-type]
    with pytest.raises(QueryEvaluationError, match="NOT expects"):
        evaluate(bad, _widget())


def test_unsupported_expression_type() -> None:
    class FakeExpr:
        pass

    with pytest.raises(QueryEvaluationError, match="Unsupported expression"):
        evaluate(FakeExpr(), _widget())  # type: ignore[arg-type]


def test_select_where_empty_raises() -> None:
    with pytest.raises(QueryEvaluationError, match="at least one"):
        select(Widget).where()


def test_select_where_invalid_type_raises() -> None:
    with pytest.raises(QueryEvaluationError, match="Expected a query expression"):
        select(Widget).where("bad")  # type: ignore[arg-type]


def test_select_limit_and_offset_negative() -> None:
    with pytest.raises(QueryEvaluationError, match="non-negative"):
        select(Widget).limit(-1)
    with pytest.raises(QueryEvaluationError, match="non-negative"):
        select(Widget).offset(-1)


def test_select_where_combines_with_existing_filter() -> None:
    stmt = select(Widget).where(Widget.score >= 1).where(Widget.tag == "x")
    assert stmt.where_expr is not None


def test_select_order_by_field_ref_and_order_by() -> None:
    stmt = select(Widget).order_by(Widget.label, OrderBy(Widget.score, descending=True))
    assert len(stmt.order_by_clauses) == 2
    assert stmt.order_by_clauses[1].descending is True


def test_sort_key_descending_numeric_and_other() -> None:
    a = _widget(score=5, label="b")
    b = _widget(id="2", score=10, label="a")
    key_a = sort_key(a, [OrderBy(Widget.score, descending=True)])
    key_b = sort_key(b, [OrderBy(Widget.score, descending=True)])
    assert key_a > key_b
    text_key = sort_key(a, [OrderBy(Widget.label, descending=True)])
    assert text_key[0][1] == "b"


def test_exec_applies_offset_and_limit() -> None:
    from gdm.backends.memory import MemoryBackend
    from gdm.session import Session

    backend = MemoryBackend()
    for index in range(5):
        backend.seed_record(
            Widget,
            {"id": str(index), "label": f"L{index}", "score": index},
        )
    with Session(backend) as session:
        rows = session.exec(
            select(Widget).order_by(Widget.score).offset(1).limit(2),
        ).all()
    assert [row.score for row in rows] == [1, 2]


def test_boolean_composition_operators() -> None:
    item = _widget()
    expr = (Widget.score >= 1) | (Widget.label == "missing")
    assert evaluate(expr, item)
    assert evaluate(~(Widget.label == "missing"), item)
    combined = (Widget.score >= 1) & (Widget.label == "Alpha")
    assert evaluate(combined, item)
