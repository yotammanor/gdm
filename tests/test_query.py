"""Tests for query expressions."""

from __future__ import annotations

import pytest

from gdm.errors import QueryEvaluationError
from gdm.fields import Field
from gdm.model import GDMModel
from gdm.query import select


class Item(GDMModel, storage="file", codec="json"):
    id: str = Field(primary_key=True)
    name: str
    category: str | None = None
    score: int = 0


def test_select_builder() -> None:
    stmt = (
        select(Item)
        .where(Item.category == "books", Item.score >= 10)
        .order_by(Item.name)
        .limit(5)
        .offset(2)
    )
    assert stmt.model is Item
    assert stmt.limit_count == 5
    assert stmt.offset_count == 2
    assert stmt.where_expr is not None
    assert len(stmt.order_by_clauses) == 1


def test_expression_methods() -> None:
    books = Item(id="1", name="Dune", category="books", score=20)
    other = Item(id="2", name="Note", category="notes", score=5)

    from gdm.query import evaluate

    assert evaluate(Item.category == "books", books)
    assert not evaluate(Item.category == "books", other)
    assert evaluate(Item.name.startswith("Du"), books)
    assert evaluate(Item.name.contains("un"), books)
    assert evaluate(Item.name.endswith("ne"), books)
    assert evaluate(Item.category.in_(["books", "notes"]), books)
    assert evaluate(Item.category.is_none(), Item(id="3", name="x"))
    assert evaluate((Item.score >= 10) & (Item.category == "books"), books)
    assert evaluate((Item.score < 10) | (Item.category == "books"), books)
    assert evaluate(~(Item.category == "notes"), books)


def test_exec_local_filtering() -> None:
    from gdm.backends.memory import MemoryBackend
    from gdm.session import Session

    backend = MemoryBackend()
    backend.seed_record(Item, {"id": "1", "name": "Alpha", "category": "a", "score": 1})
    backend.seed_record(Item, {"id": "2", "name": "Beta", "category": "b", "score": 2})

    with Session(backend) as session:
        result = session.exec(
            select(Item).where(Item.category == "b").order_by(Item.name)
        )
        rows = result.all()
        assert len(rows) == 1
        assert rows[0].name == "Beta"


def test_where_requires_expression() -> None:
    with pytest.raises(QueryEvaluationError):
        select(Item).where("not-an-expression")  # type: ignore[arg-type]
