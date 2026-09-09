"""Typed query expression tree and local evaluation."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from gdm.errors import QueryEvaluationError

if TYPE_CHECKING:
    from gdm.model import GDMModel

T = TypeVar("T", bound="GDMModel")


class CompareOp(StrEnum):
    EQ = "eq"
    NE = "ne"
    LT = "lt"
    LE = "le"
    GT = "gt"
    GE = "ge"


class MethodOp(StrEnum):
    CONTAINS = "contains"
    STARTSWITH = "startswith"
    ENDSWITH = "endswith"
    IN = "in"
    IS_NONE = "is_none"


class BoolOp(StrEnum):
    AND = "and"
    OR = "or"
    NOT = "not"


class Expr(ABC):
    """Base class for query expressions."""

    @abstractmethod
    def __and__(self, other: Expr) -> Expr: ...

    @abstractmethod
    def __or__(self, other: Expr) -> Expr: ...

    @abstractmethod
    def __invert__(self) -> Expr: ...


@dataclass(frozen=True, slots=True)
class FieldRef(Expr):
    """Reference to a model field used in query expressions."""

    model: type[Any]
    name: str

    def __eq__(self, other: object) -> Any:
        return Comparison(self, CompareOp.EQ, other)

    def __ne__(self, other: object) -> Any:
        return Comparison(self, CompareOp.NE, other)

    def __lt__(self, other: object) -> Comparison:
        return Comparison(self, CompareOp.LT, other)

    def __le__(self, other: object) -> Comparison:
        return Comparison(self, CompareOp.LE, other)

    def __gt__(self, other: object) -> Comparison:
        return Comparison(self, CompareOp.GT, other)

    def __ge__(self, other: object) -> Comparison:
        return Comparison(self, CompareOp.GE, other)

    def contains(self, other: object) -> MethodCall:
        return MethodCall(self, MethodOp.CONTAINS, other)

    def startswith(self, other: object) -> MethodCall:
        return MethodCall(self, MethodOp.STARTSWITH, other)

    def endswith(self, other: object) -> MethodCall:
        return MethodCall(self, MethodOp.ENDSWITH, other)

    def in_(self, other: Iterable[object]) -> MethodCall:
        return MethodCall(self, MethodOp.IN, other)

    def is_none(self) -> MethodCall:
        return MethodCall(self, MethodOp.IS_NONE, None)

    def __and__(self, other: Expr) -> BooleanExpr:
        return BooleanExpr(BoolOp.AND, (self, _as_expr(other)))

    def __or__(self, other: Expr) -> BooleanExpr:
        return BooleanExpr(BoolOp.OR, (self, _as_expr(other)))

    def __invert__(self) -> BooleanExpr:
        return BooleanExpr(BoolOp.NOT, (self,))


@dataclass(frozen=True, slots=True)
class Comparison(Expr):
    left: FieldRef
    op: CompareOp
    right: object

    def __and__(self, other: Expr) -> BooleanExpr:
        return BooleanExpr(BoolOp.AND, (self, _as_expr(other)))

    def __or__(self, other: Expr) -> BooleanExpr:
        return BooleanExpr(BoolOp.OR, (self, _as_expr(other)))

    def __invert__(self) -> BooleanExpr:
        return BooleanExpr(BoolOp.NOT, (self,))


@dataclass(frozen=True, slots=True)
class MethodCall(Expr):
    left: FieldRef
    op: MethodOp
    right: object

    def __and__(self, other: Expr) -> BooleanExpr:
        return BooleanExpr(BoolOp.AND, (self, _as_expr(other)))

    def __or__(self, other: Expr) -> BooleanExpr:
        return BooleanExpr(BoolOp.OR, (self, _as_expr(other)))

    def __invert__(self) -> BooleanExpr:
        return BooleanExpr(BoolOp.NOT, (self,))


@dataclass(frozen=True, slots=True)
class BooleanExpr(Expr):
    op: BoolOp
    operands: tuple[Expr, ...]

    def __and__(self, other: Expr) -> BooleanExpr:
        return BooleanExpr(BoolOp.AND, (self, _as_expr(other)))

    def __or__(self, other: Expr) -> BooleanExpr:
        return BooleanExpr(BoolOp.OR, (self, _as_expr(other)))

    def __invert__(self) -> BooleanExpr:
        return BooleanExpr(BoolOp.NOT, (self,))


@dataclass(frozen=True, slots=True)
class OrderBy:
    """Ordering clause for a select statement."""

    field: FieldRef
    descending: bool = False


@dataclass
class Select(Generic[T]):
    """Backend-independent select statement."""

    model: type[T]
    where_expr: Expr | None = None
    order_by_clauses: tuple[OrderBy, ...] = ()
    limit_count: int | None = None
    offset_count: int | None = None

    def where(self, *expressions: Expr) -> Select[T]:
        if not expressions:
            raise QueryEvaluationError("where() requires at least one expression")
        for expr in expressions:
            if not isinstance(expr, Expr):
                msg = f"Expected a query expression, got {type(expr)!r}"
                raise QueryEvaluationError(msg)
        combined = expressions[0]
        for expr in expressions[1:]:
            combined = combined & expr
        return Select(
            model=self.model,
            where_expr=combined
            if self.where_expr is None
            else self.where_expr & combined,
            order_by_clauses=self.order_by_clauses,
            limit_count=self.limit_count,
            offset_count=self.offset_count,
        )

    def order_by(self, *clauses: FieldRef | OrderBy) -> Select[T]:
        normalized: list[OrderBy] = []
        for clause in clauses:
            if isinstance(clause, FieldRef):
                normalized.append(OrderBy(clause))
            else:
                normalized.append(clause)
        return Select(
            model=self.model,
            where_expr=self.where_expr,
            order_by_clauses=self.order_by_clauses + tuple(normalized),
            limit_count=self.limit_count,
            offset_count=self.offset_count,
        )

    def limit(self, count: int) -> Select[T]:
        if count < 0:
            raise QueryEvaluationError("limit() must be non-negative")
        return Select(
            model=self.model,
            where_expr=self.where_expr,
            order_by_clauses=self.order_by_clauses,
            limit_count=count,
            offset_count=self.offset_count,
        )

    def offset(self, count: int) -> Select[T]:
        if count < 0:
            raise QueryEvaluationError("offset() must be non-negative")
        return Select(
            model=self.model,
            where_expr=self.where_expr,
            order_by_clauses=self.order_by_clauses,
            limit_count=self.limit_count,
            offset_count=count,
        )


def select(model: type[T]) -> Select[T]:
    """Begin a select statement for ``model``."""
    return Select(model=model)


def _as_expr(value: object) -> Expr:
    if isinstance(value, Expr):
        return value
    msg = f"Expected a query expression, got {type(value)!r}"
    raise QueryEvaluationError(msg)


def _field_value(instance: Any, field_ref: FieldRef) -> Any:
    if not isinstance(instance, field_ref.model):
        msg = (
            f"Cannot evaluate {field_ref.model.__name__}.{field_ref.name} "
            f"against {type(instance).__name__}"
        )
        raise QueryEvaluationError(msg)
    return getattr(instance, field_ref.name)


def evaluate(expr: Expr | None, instance: Any) -> bool:
    """Evaluate ``expr`` against a model instance."""
    if expr is None:
        return True
    if isinstance(expr, FieldRef):
        msg = "Bare field references are not valid filter expressions"
        raise QueryEvaluationError(msg)
    if isinstance(expr, Comparison):
        left = _field_value(instance, expr.left)
        right = expr.right
        if expr.op is CompareOp.EQ:
            return left == right
        if expr.op is CompareOp.NE:
            return left != right
        if expr.op is CompareOp.LT:
            return left < right
        if expr.op is CompareOp.LE:
            return left <= right
        if expr.op is CompareOp.GT:
            return left > right
        if expr.op is CompareOp.GE:
            return left >= right
    if isinstance(expr, MethodCall):
        left = _field_value(instance, expr.left)
        if expr.op is MethodOp.IS_NONE:
            return left is None
        if expr.op is MethodOp.CONTAINS:
            if left is None:
                return False
            return expr.right in left  # type: ignore[operator]
        if expr.op is MethodOp.STARTSWITH:
            if left is None:
                return False
            return str(left).startswith(str(expr.right))
        if expr.op is MethodOp.ENDSWITH:
            if left is None:
                return False
            return str(left).endswith(str(expr.right))
        if expr.op is MethodOp.IN:
            if not isinstance(expr.right, Iterable):
                msg = "in_() requires an iterable of values"
                raise QueryEvaluationError(msg)
            return left in expr.right
    if isinstance(expr, BooleanExpr):
        if expr.op is BoolOp.NOT:
            if len(expr.operands) != 1:
                msg = "NOT expects exactly one operand"
                raise QueryEvaluationError(msg)
            return not evaluate(expr.operands[0], instance)
        if expr.op is BoolOp.AND:
            return all(evaluate(operand, instance) for operand in expr.operands)
        if expr.op is BoolOp.OR:
            return any(evaluate(operand, instance) for operand in expr.operands)
    msg = f"Unsupported expression type: {type(expr)!r}"
    raise QueryEvaluationError(msg)


def sort_key(instance: Any, order_by: Sequence[OrderBy]) -> tuple[Any, ...]:
    """Build a sort key tuple for ``instance``."""
    key: list[Any] = []
    for clause in order_by:
        value = _field_value(instance, clause.field)
        key.append((1, None) if value is None else (0, value))
        if clause.descending:
            key[-1] = (key[-1][0], _reverse_sortable(key[-1][1]))
    return tuple(key)


def _reverse_sortable(value: Any) -> Any:
    if isinstance(value, (int, float)):
        return -value
    return value


@dataclass(frozen=True, slots=True)
class IndexLookup:
    """Single indexed-field equality extracted from a where clause."""

    field_name: str
    value: object


def extract_index_lookup(
    model: type[Any],
    expr: Expr | None,
) -> tuple[IndexLookup | None, Expr | None]:
    """Extract one indexed equality predicate and any remaining filter."""
    if expr is None:
        return None, None
    from gdm.indexes import field_is_indexed

    if isinstance(expr, Comparison) and expr.op is CompareOp.EQ:
        if field_is_indexed(model, expr.left.name):
            return IndexLookup(expr.left.name, expr.right), None
        return None, expr
    if isinstance(expr, BooleanExpr) and expr.op is BoolOp.AND:
        lookup: IndexLookup | None = None
        remaining_parts: list[Expr] = []
        for operand in expr.operands:
            sub_lookup, sub_remaining = extract_index_lookup(model, operand)
            if sub_lookup is not None:
                if lookup is not None:
                    return None, expr
                lookup = sub_lookup
                if sub_remaining is not None:
                    remaining_parts.append(sub_remaining)
            elif sub_remaining is not None:
                remaining_parts.append(sub_remaining)
            else:
                remaining_parts.append(operand)
        if lookup is None:
            return None, expr
        remaining: Expr | None = None
        for part in remaining_parts:
            remaining = part if remaining is None else remaining & part
        return lookup, remaining
    return None, expr
