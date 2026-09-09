"""Forward, resumable migrations with dry-run planning and checkpointing."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from gdm.schema import GDMFieldMeta, SchemaSnapshot, build_model_snapshot


class MigrationOpKind(StrEnum):
    """Supported migration operation kinds."""

    ADD_FIELD = "add_field"
    RENAME_FIELD = "rename_field"
    TRANSFORM_FIELD = "transform_field"
    BACKFILL_SYSTEM_COLUMNS = "backfill_system_columns"
    BIND_COLLECTION = "bind_collection"
    BACKFILL_METADATA_COLUMN = "backfill_metadata_column"
    RECONCILE_METADATA_ROWS = "reconcile_metadata_rows"
    RETAG_APP_PROPERTIES = "retag_app_properties"
    MOVE_RETAG = "move_retag"
    MOVE_MEMBER = "move_member"
    CONVERT_MIME = "convert_mime"
    REBUILD_INDEX = "rebuild_index"
    DROP_FIELD = "drop_field"
    UNBIND_COLLECTION = "unbind_collection"


@dataclass(frozen=True, slots=True)
class ReverseOperationSpec:
    """Serializable reverse step for rollback (kind + params + destructive flag)."""

    kind: MigrationOpKind
    params: Mapping[str, Any]
    destructive: bool = False


@dataclass(frozen=True, slots=True)
class MigrationOperation:
    """Single ordered migration step."""

    kind: MigrationOpKind
    model_name: str
    params: Mapping[str, Any]
    destructive: bool = False
    reverse: Mapping[str, Any] | None = None
    op_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def summary(self) -> str:
        label = "metadata-only" if not self.destructive else "destructive"
        return f"[{label}] {self.kind.value} on {self.model_name}: {dict(self.params)}"


@dataclass(frozen=True, slots=True)
class OperationResult:
    """Outcome of applying one migration operation."""

    op_id: str
    kind: MigrationOpKind
    status: str
    message: str
    changed: int = 0
    errors: tuple[str, ...] = field(default_factory=tuple)


@dataclass
class MigrationPlan:
    """Inspect-only migration plan; dry-run by default."""

    model_name: str
    operations: tuple[MigrationOperation, ...]
    from_snapshot: SchemaSnapshot | None = None
    to_snapshot: SchemaSnapshot | None = None
    dry_run: bool = True
    allow_destructive: bool = False

    @property
    def has_destructive(self) -> bool:
        return any(operation_is_destructive(op) for op in self.operations)

    def blocked_reason(self) -> str | None:
        if self.has_destructive and not self.allow_destructive:
            kinds = ", ".join(
                op.kind.value for op in self.operations if operation_is_destructive(op)
            )
            return (
                f"Plan contains destructive operations ({kinds}). "
                "Re-run with allow_destructive=True."
            )
        return None


@dataclass
class MigrationCheckpoint:
    """Persisted progress for resumable migrations."""

    plan_id: str
    model_name: str
    completed_op_ids: tuple[str, ...] = field(default_factory=tuple)
    rolled_back_op_ids: tuple[str, ...] = field(default_factory=tuple)
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def is_done(self, plan: MigrationPlan) -> bool:
        completed = set(self.completed_op_ids)
        return all(op.op_id in completed for op in plan.operations)

    def rollback_is_done(self) -> bool:
        completed = set(self.completed_op_ids)
        rolled = set(self.rolled_back_op_ids)
        return completed <= rolled


@dataclass
class MigrationReport:
    """Result of planning or applying a migration."""

    plan: MigrationPlan
    results: tuple[OperationResult, ...] = field(default_factory=tuple)
    checkpoint: MigrationCheckpoint | None = None
    applied: bool = False
    rolled_back: bool = False
    partial_failure: bool = False

    @property
    def succeeded(self) -> bool:
        return self.applied and not self.partial_failure

    @property
    def rollback_succeeded(self) -> bool:
        return self.rolled_back and not self.partial_failure


@runtime_checkable
class CollectionMigrationTarget(Protocol):
    """Collection metadata-table migration operations."""

    def metadata_headers(self, model_name: str) -> list[str]: ...

    def metadata_add_column(
        self, model_name: str, header: str, default: Any = None
    ) -> int: ...

    def metadata_rename_column(
        self, model_name: str, old_name: str, new_name: str
    ) -> int: ...

    def metadata_drop_column(self, model_name: str, header: str) -> int: ...

    def metadata_transform_column(
        self,
        model_name: str,
        column: str,
        transformer: Callable[[Any], Any],
    ) -> tuple[int, tuple[str, ...]]: ...

    def metadata_reconcile_rows(
        self, model_name: str, *, folder_file_ids: set[str]
    ) -> tuple[int, int]: ...

    def bind_collection(
        self, model_name: str, *, folder_id: str, collection_path: str | None
    ) -> None: ...


@runtime_checkable
class SheetMigrationTarget(Protocol):
    """Sheet-side migration operations."""

    def sheet_headers(self, resource_id: str) -> list[str]: ...

    def sheet_add_column(
        self, resource_id: str, header: str, default: Any = None
    ) -> int: ...

    def sheet_rename_column(
        self, resource_id: str, old_name: str, new_name: str
    ) -> int: ...

    def sheet_transform_column(
        self,
        resource_id: str,
        column: str,
        transformer: Callable[[Any], Any],
    ) -> tuple[int, tuple[str, ...]]: ...

    def sheet_drop_column(self, resource_id: str, header: str) -> int: ...

    def sheet_backfill_system_columns(self, resource_id: str) -> int: ...


@runtime_checkable
class FileMigrationTarget(Protocol):
    """File-side migration operations."""

    def file_move(self, file_id: str, new_parent_id: str) -> None: ...

    def file_retag(
        self,
        file_id: str,
        *,
        gdm_model: str | None = None,
        gdm_schema: str | None = None,
        gdm_project: str | None = None,
    ) -> None: ...


@runtime_checkable
class IndexMigrationTarget(Protocol):
    """Index rebuild hook."""

    def rebuild_index(self, model_name: str, field_name: str) -> int: ...


@runtime_checkable
class MigrationTarget(
    SheetMigrationTarget,
    FileMigrationTarget,
    IndexMigrationTarget,
    CollectionMigrationTarget,
    Protocol,
):
    """Combined migration target surface."""

    def resource_id_for_model(self, model_name: str) -> str | None: ...


TransformFn = Callable[[Any], Any]

INHERENTLY_DESTRUCTIVE_KINDS: frozenset[MigrationOpKind] = frozenset(
    {
        MigrationOpKind.DROP_FIELD,
        MigrationOpKind.TRANSFORM_FIELD,
        MigrationOpKind.MOVE_RETAG,
        MigrationOpKind.MOVE_MEMBER,
        MigrationOpKind.CONVERT_MIME,
    }
)


def operation_is_destructive(op: MigrationOperation) -> bool:
    """Return whether an operation requires explicit destructive approval."""
    return op.destructive or op.kind in INHERENTLY_DESTRUCTIVE_KINDS


INHERENTLY_NON_REVERSIBLE_KINDS: frozenset[MigrationOpKind] = frozenset(
    {
        MigrationOpKind.TRANSFORM_FIELD,
        MigrationOpKind.DROP_FIELD,
        MigrationOpKind.CONVERT_MIME,
        MigrationOpKind.BACKFILL_SYSTEM_COLUMNS,
        MigrationOpKind.RECONCILE_METADATA_ROWS,
    }
)


def _reverse_spec(
    kind: MigrationOpKind,
    params: Mapping[str, Any],
    *,
    destructive: bool = False,
) -> dict[str, Any]:
    """Build a serializable reverse-operation payload."""
    return {
        "kind": kind.value,
        "params": dict(params),
        "destructive": destructive,
    }


def parse_reverse_operation(op: MigrationOperation) -> ReverseOperationSpec | None:
    """Normalize ``MigrationOperation.reverse`` into a reverse spec."""
    if op.reverse is None:
        return None
    rev = op.reverse
    if "kind" not in rev:
        if op.kind is MigrationOpKind.RENAME_FIELD:
            old_name = rev.get("old_name")
            new_name = rev.get("new_name")
            if old_name and new_name:
                return ReverseOperationSpec(
                    kind=MigrationOpKind.RENAME_FIELD,
                    params={"old_name": old_name, "new_name": new_name},
                    destructive=False,
                )
        return None
    try:
        kind = MigrationOpKind(str(rev["kind"]))
    except ValueError:
        return None
    params = dict(rev.get("params", {}))
    destructive = bool(rev.get("destructive", False))
    return ReverseOperationSpec(kind=kind, params=params, destructive=destructive)


def reverse_operation_is_destructive(spec: ReverseOperationSpec) -> bool:
    """Return whether executing a reverse spec requires destructive approval."""
    return spec.destructive or spec.kind in INHERENTLY_DESTRUCTIVE_KINDS


def operation_is_reversible(op: MigrationOperation) -> bool:
    """Return whether a forward operation declares a valid rollback step."""
    if op.kind in INHERENTLY_NON_REVERSIBLE_KINDS:
        return False
    return parse_reverse_operation(op) is not None


def rollback_blocked_reason(
    plan: MigrationPlan,
    apply_checkpoint: MigrationCheckpoint,
    *,
    allow_destructive: bool,
) -> str | None:
    """Return a preflight error when rollback must not write anything."""
    completed_ids = set(apply_checkpoint.completed_op_ids)
    if not completed_ids:
        return "No completed operations to roll back."
    completed_ops = [op for op in plan.operations if op.op_id in completed_ids]
    if not completed_ops:
        return "Checkpoint completed_op_ids do not match this migration plan."
    non_reversible = [op for op in completed_ops if not operation_is_reversible(op)]
    if non_reversible:
        kinds = ", ".join(op.kind.value for op in non_reversible)
        return (
            f"Rollback blocked: completed operations are not reversible ({kinds}). "
            "No rollback writes were performed."
        )
    if not allow_destructive:
        destructive = [
            spec
            for op in completed_ops
            if (spec := parse_reverse_operation(op)) is not None
            and reverse_operation_is_destructive(spec)
        ]
        if destructive:
            kinds = ", ".join(sorted({spec.kind.value for spec in destructive}))
            return (
                f"Rollback contains destructive reverse operations ({kinds}). "
                "Re-run with allow_destructive=True."
            )
    return None


_BOOL_TRUE = frozenset({"true", "t", "yes", "y", "1"})
_BOOL_FALSE = frozenset({"false", "f", "no", "n", "0"})
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$"
)
_SUPPORTED_SCALAR_TOKENS = frozenset(
    {"str", "int", "float", "bool", "date", "datetime"},
)
_SCALAR_COERCE_TARGETS = frozenset({"int", "float", "bool", "date", "datetime"})


def _normalize_type_tokens(python_type: str) -> frozenset[str]:
    """Split a Python type annotation string into non-optional atomic tokens."""
    cleaned = python_type.replace("Optional[", "").replace("]", "")
    cleaned = cleaned.replace("datetime.date", "date")
    cleaned = cleaned.replace("datetime.datetime", "datetime")
    parts = [part.strip() for part in cleaned.split("|")]
    tokens = {part for part in parts if part and part != "None"}
    return frozenset(tokens)


def _coerce_scalar(value: Any, target: str) -> Any:
    if value is None or value == "":
        return None
    if target == "str":
        return str(value)
    if target == "int":
        if isinstance(value, bool):
            return int(value)
        return int(str(value).strip())
    if target == "float":
        if isinstance(value, bool):
            return float(int(value))
        return float(str(value).strip())
    if target == "bool":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in _BOOL_TRUE:
            return True
        if text in _BOOL_FALSE:
            return False
        msg = f"Cannot coerce {value!r} to bool."
        raise ValueError(msg)
    if target == "date":
        if isinstance(value, date) and not isinstance(value, datetime):
            return value
        text = str(value).strip()
        if _ISO_DATE_RE.match(text):
            return date.fromisoformat(text)
        msg = f"Cannot coerce {value!r} to date."
        raise ValueError(msg)
    if target == "datetime":
        if isinstance(value, datetime):
            return value
        text = str(value).strip().replace("Z", "+00:00")
        if _ISO_DATETIME_RE.match(text):
            return datetime.fromisoformat(text)
        msg = f"Cannot coerce {value!r} to datetime."
        raise ValueError(msg)
    msg = f"Unsupported coercion target type {target!r}."
    raise ValueError(msg)


def _coercion_is_supported(
    from_tokens: frozenset[str],
    to_tokens: frozenset[str],
) -> bool:
    if not from_tokens or not to_tokens or from_tokens == to_tokens:
        return False
    if not to_tokens <= _SUPPORTED_SCALAR_TOKENS:
        return False
    if len(to_tokens) != 1:
        return False
    target = next(iter(to_tokens))
    if len(from_tokens) == 1:
        source = next(iter(from_tokens))
        if source not in _SUPPORTED_SCALAR_TOKENS:
            return False
        if source == target:
            return False
        if source == "int" and target == "float":
            return True
        if source == "float" and target == "int":
            return True
        if source in {"int", "float"} and target == "str":
            return True
        if source == "bool" and target in {"int", "str"}:
            return True
        return source == "str" and target in _SCALAR_COERCE_TARGETS
    if not from_tokens <= _SUPPORTED_SCALAR_TOKENS:
        return False
    return (
        ("str" in from_tokens and target in _SCALAR_COERCE_TARGETS)
        or (from_tokens <= {"int", "float"} and to_tokens == {"float"})
        or (from_tokens <= {"int", "float"} and to_tokens == {"int"})
    )


def build_type_coercion_transformer(
    from_type: str,
    to_type: str,
) -> TransformFn | None:
    """Return a deterministic transformer when a safe coercion exists."""
    from_tokens = _normalize_type_tokens(from_type)
    to_tokens = _normalize_type_tokens(to_type)
    if not from_tokens or not to_tokens:
        return None
    if from_tokens == to_tokens:
        return None
    if not _coercion_is_supported(from_tokens, to_tokens):
        return None

    def _transform(value: Any) -> Any:
        if value is None or value == "":
            return None
        if len(to_tokens) == 1:
            target = next(iter(to_tokens))
            if len(from_tokens) == 1:
                source = next(iter(from_tokens))
                if source == target:
                    return value
                if source == "int" and target == "float":
                    return float(value)
                if source == "float" and target == "int":
                    return int(float(value))
                if source in {"int", "float"} and target == "str":
                    return str(value)
                if source == "bool" and target in {"int", "str"}:
                    coerced = _coerce_scalar(value, "bool")
                    return int(coerced) if target == "int" else str(coerced)
                str_targets = _SCALAR_COERCE_TARGETS
                if source == "str" and target in str_targets:
                    return _coerce_scalar(value, target)
            if "str" in from_tokens and target in _SCALAR_COERCE_TARGETS:
                return _coerce_scalar(value, target)
        if from_tokens <= {"int", "float"} and to_tokens == {"float"}:
            return float(value)
        if from_tokens <= {"int", "float"} and to_tokens == {"int"}:
            return int(float(value))
        msg = (
            f"No supported deterministic coercion from {from_type!r} to {to_type!r} "
            f"for value {value!r}."
        )
        raise ValueError(msg)

    return _transform


def _field_params(
    target: SchemaSnapshot,
    meta: GDMFieldMeta,
    **extra: Any,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "storage": target.storage,
        "target_kind": meta.target_kind,
        "source_name": meta.source_name,
    }
    params.update(extra)
    return params


def _stable_op_id(
    kind: MigrationOpKind,
    model_name: str,
    params: Mapping[str, Any],
) -> str:
    """Deterministic operation id so checkpoints survive plan regeneration."""
    payload = json.dumps(
        {"kind": kind.value, "model_name": model_name, "params": dict(params)},
        sort_keys=True,
        default=str,
    )
    return str(uuid.uuid5(uuid.NAMESPACE_OID, payload))


def _requires_source_migration(meta: GDMFieldMeta, storage: str) -> bool:
    """Return whether a field change requires a metadata/sheet migration step."""
    if storage == "sheet":
        return meta.target_kind in {"source", "metadata"}
    if storage in {"collection", "file"}:
        return meta.target_kind == "metadata"
    return meta.target_kind == "source"


def _is_metadata_table_op(op: MigrationOperation) -> bool:
    storage = str(op.params.get("storage", ""))
    target_kind = str(op.params.get("target_kind", ""))
    return storage in {"collection", "file"} and target_kind == "metadata"


def _is_sheet_table_op(op: MigrationOperation) -> bool:
    return str(op.params.get("storage", "")) == "sheet"


def _make_planned_operation(
    kind: MigrationOpKind,
    model_name: str,
    params: Mapping[str, Any],
    *,
    destructive: bool = False,
    reverse: Mapping[str, Any] | None = None,
) -> MigrationOperation:
    return MigrationOperation(
        kind=kind,
        model_name=model_name,
        params=dict(params),
        destructive=destructive,
        reverse=reverse,
        op_id=_stable_op_id(kind, model_name, params),
    )


class MigrationPlanner:
    """Build forward migration plans from schema diffs."""

    def plan_from_snapshots(
        self,
        *,
        model_name: str,
        current: SchemaSnapshot,
        target: SchemaSnapshot,
        resource_id: str,
        dry_run: bool = True,
        allow_destructive: bool = False,
    ) -> MigrationPlan:
        ops = self._diff_to_operations(
            current, target, resource_id=resource_id, model_name=model_name
        )
        return MigrationPlan(
            model_name=model_name,
            operations=tuple(ops),
            from_snapshot=current,
            to_snapshot=target,
            dry_run=dry_run,
            allow_destructive=allow_destructive,
        )

    def plan_from_model(
        self,
        *,
        model: type[Any],
        applied: SchemaSnapshot | None,
        resource_id: str,
        dry_run: bool = True,
        allow_destructive: bool = False,
    ) -> MigrationPlan:
        target = build_model_snapshot(model)
        return self.plan_from_snapshots(
            model_name=target.model_name,
            current=applied or target,
            target=target,
            resource_id=resource_id,
            dry_run=dry_run,
            allow_destructive=allow_destructive,
        )

    def _diff_to_operations(
        self,
        current: SchemaSnapshot,
        target: SchemaSnapshot,
        *,
        resource_id: str,
        model_name: str,
    ) -> list[MigrationOperation]:
        ops: list[MigrationOperation] = []
        cur_by_name = {f.name: f for f in current.fields}
        tgt_by_name = {f.name: f for f in target.fields}
        cur_by_source = {f.source_name: f for f in current.fields}
        tgt_by_source = {f.source_name: f for f in target.fields}

        if target.storage == "sheet" and set(current.system_columns) != set(
            target.system_columns
        ):
            ops.append(
                _make_planned_operation(
                    MigrationOpKind.BACKFILL_SYSTEM_COLUMNS,
                    model_name,
                    {
                        "storage": target.storage,
                        "columns": list(target.system_columns),
                    },
                )
            )

        if (
            target.storage in {"collection", "file"}
            and current.collection_path is None
            and target.collection_path
        ):
            ops.append(
                _make_planned_operation(
                    MigrationOpKind.BIND_COLLECTION,
                    model_name,
                    {
                        "storage": target.storage,
                        "folder_id": resource_id,
                        "collection_path": target.collection_path,
                    },
                    reverse=_reverse_spec(
                        MigrationOpKind.UNBIND_COLLECTION,
                        {
                            "storage": target.storage,
                            "folder_id": resource_id,
                        },
                        destructive=True,
                    ),
                )
            )

        for name in sorted(set(tgt_by_name) - set(cur_by_name)):
            meta = tgt_by_name[name]
            if not _requires_source_migration(meta, target.storage):
                continue
            if meta.target_kind == "metadata" and target.storage in {
                "collection",
                "file",
            }:
                op_kind = MigrationOpKind.BACKFILL_METADATA_COLUMN
            elif target.storage == "sheet":
                op_kind = MigrationOpKind.ADD_FIELD
            else:
                continue
            field_params = _field_params(
                target,
                meta,
                field=name,
                default=meta.default,
            )
            ops.append(
                _make_planned_operation(
                    op_kind,
                    model_name,
                    field_params,
                    reverse=_reverse_spec(
                        MigrationOpKind.DROP_FIELD,
                        _field_params(target, meta, field=name),
                        destructive=True,
                    ),
                )
            )

        for name in sorted(set(cur_by_name) - set(tgt_by_name)):
            meta = cur_by_name[name]
            if not _requires_source_migration(meta, target.storage):
                continue
            if target.storage not in {"sheet", "collection", "file"}:
                continue
            drop_params = _field_params(target, meta)
            ops.append(
                _make_planned_operation(
                    MigrationOpKind.DROP_FIELD,
                    model_name,
                    drop_params,
                    destructive=True,
                )
            )

        for name in sorted(set(cur_by_name) & set(tgt_by_name)):
            cur = cur_by_name[name]
            tgt = tgt_by_name[name]
            if cur.source_name != tgt.source_name:
                if not _requires_source_migration(tgt, target.storage):
                    continue
                if target.storage not in {"sheet", "collection", "file"}:
                    continue
                rename_params = _field_params(
                    target,
                    tgt,
                    field=name,
                    old_name=cur.source_name,
                    new_name=tgt.source_name,
                )
                ops.append(
                    _make_planned_operation(
                        MigrationOpKind.RENAME_FIELD,
                        model_name,
                        rename_params,
                        reverse=_reverse_spec(
                            MigrationOpKind.RENAME_FIELD,
                            {
                                "old_name": tgt.source_name,
                                "new_name": cur.source_name,
                                "field": name,
                                "storage": target.storage,
                                "target_kind": tgt.target_kind,
                            },
                        ),
                    )
                )
            if cur.python_type != tgt.python_type:
                if not _requires_source_migration(tgt, target.storage):
                    continue
                if target.storage not in {"sheet", "collection", "file"}:
                    continue
                transform_params = _field_params(
                    target,
                    tgt,
                    from_type=cur.python_type,
                    to_type=tgt.python_type,
                )
                ops.append(
                    _make_planned_operation(
                        MigrationOpKind.TRANSFORM_FIELD,
                        model_name,
                        transform_params,
                        destructive=True,
                    )
                )

        for source in sorted(set(tgt_by_source) - set(cur_by_source)):
            meta = tgt_by_source[source]
            if meta.index:
                index_params = {
                    "storage": target.storage,
                    "field": meta.name,
                    "source_name": source,
                    "target_kind": meta.target_kind,
                }
                ops.append(
                    _make_planned_operation(
                        MigrationOpKind.REBUILD_INDEX,
                        model_name,
                        index_params,
                        reverse=_reverse_spec(
                            MigrationOpKind.REBUILD_INDEX,
                            index_params,
                        ),
                    )
                )

        return ops


class MigrationEngine:
    """Apply migration plans against a narrow target protocol."""

    def __init__(self, target: MigrationTarget) -> None:
        self.target = target

    def apply(
        self,
        plan: MigrationPlan,
        *,
        checkpoint: MigrationCheckpoint | None = None,
        plan_id: str | None = None,
        on_checkpoint: Callable[[MigrationCheckpoint], None] | None = None,
    ) -> MigrationReport:
        blocked = plan.blocked_reason()
        if blocked:
            return MigrationReport(
                plan=plan,
                results=(
                    OperationResult(
                        op_id="plan",
                        kind=MigrationOpKind.ADD_FIELD,
                        status="blocked",
                        message=blocked,
                    ),
                ),
            )

        if plan.dry_run:
            return self._dry_run_report(plan)

        plan_key = plan_id or str(uuid.uuid4())
        cp = checkpoint or MigrationCheckpoint(
            plan_id=plan_key, model_name=plan.model_name
        )
        completed = set(cp.completed_op_ids)
        results: list[OperationResult] = []
        partial = False

        for op in plan.operations:
            if op.op_id in completed:
                results.append(
                    OperationResult(
                        op_id=op.op_id,
                        kind=op.kind,
                        status="skipped",
                        message="Already completed in checkpoint.",
                    )
                )
                continue
            try:
                result = self._apply_operation(op)
                results.append(result)
                if result.status == "ok":
                    cp.completed_op_ids = (*cp.completed_op_ids, op.op_id)
                    cp.updated_at = datetime.now(UTC).isoformat()
                    if on_checkpoint is not None:
                        on_checkpoint(cp)
                else:
                    partial = True
                    break
            except Exception as exc:
                results.append(
                    OperationResult(
                        op_id=op.op_id,
                        kind=op.kind,
                        status="error",
                        message=str(exc),
                    )
                )
                partial = True
                break

        cp.updated_at = datetime.now(UTC).isoformat()
        return MigrationReport(
            plan=plan,
            results=tuple(results),
            checkpoint=cp,
            applied=True,
            partial_failure=partial,
        )

    def rollback(
        self,
        plan: MigrationPlan,
        apply_checkpoint: MigrationCheckpoint,
        *,
        checkpoint: MigrationCheckpoint | None = None,
        plan_id: str | None = None,
        allow_destructive: bool = False,
        on_checkpoint: Callable[[MigrationCheckpoint], None] | None = None,
    ) -> MigrationReport:
        """Roll back completed forward operations in strict reverse order."""
        blocked = rollback_blocked_reason(
            plan,
            apply_checkpoint,
            allow_destructive=allow_destructive,
        )
        if blocked:
            return MigrationReport(
                plan=plan,
                results=(
                    OperationResult(
                        op_id="rollback",
                        kind=MigrationOpKind.ADD_FIELD,
                        status="blocked",
                        message=blocked,
                    ),
                ),
            )

        if plan.dry_run:
            return self._rollback_dry_run_report(plan, apply_checkpoint)

        plan_key = plan_id or apply_checkpoint.plan_id
        cp = checkpoint or MigrationCheckpoint(
            plan_id=plan_key,
            model_name=plan.model_name,
            completed_op_ids=apply_checkpoint.completed_op_ids,
            rolled_back_op_ids=apply_checkpoint.rolled_back_op_ids,
        )
        rolled_set = set(cp.rolled_back_op_ids)
        op_by_id = {op.op_id: op for op in plan.operations}
        completed_in_order = [
            op_by_id[op_id] for op_id in cp.completed_op_ids if op_id in op_by_id
        ]
        results: list[OperationResult] = []
        partial = False

        for op in reversed(completed_in_order):
            if op.op_id in rolled_set:
                results.append(
                    OperationResult(
                        op_id=op.op_id,
                        kind=op.kind,
                        status="skipped",
                        message="Already rolled back in checkpoint.",
                    )
                )
                continue
            reverse = parse_reverse_operation(op)
            assert reverse is not None
            try:
                result = self._apply_reverse_operation(op, reverse)
                results.append(result)
                if result.status == "ok":
                    cp.rolled_back_op_ids = (*cp.rolled_back_op_ids, op.op_id)
                    cp.updated_at = datetime.now(UTC).isoformat()
                    if on_checkpoint is not None:
                        on_checkpoint(cp)
                else:
                    partial = True
                    break
            except Exception as exc:
                results.append(
                    OperationResult(
                        op_id=op.op_id,
                        kind=op.kind,
                        status="error",
                        message=str(exc),
                    )
                )
                partial = True
                break

        cp.updated_at = datetime.now(UTC).isoformat()
        return MigrationReport(
            plan=plan,
            results=tuple(results),
            checkpoint=cp,
            rolled_back=True,
            partial_failure=partial,
        )

    def _rollback_dry_run_report(
        self,
        plan: MigrationPlan,
        apply_checkpoint: MigrationCheckpoint,
    ) -> MigrationReport:
        rolled_set = set(apply_checkpoint.rolled_back_op_ids)
        op_by_id = {op.op_id: op for op in plan.operations}
        completed_in_order = [
            op_by_id[op_id]
            for op_id in apply_checkpoint.completed_op_ids
            if op_id in op_by_id
        ]
        results: list[OperationResult] = []
        for op in reversed(completed_in_order):
            if op.op_id in rolled_set:
                results.append(
                    OperationResult(
                        op_id=op.op_id,
                        kind=op.kind,
                        status="skipped",
                        message="Already rolled back in checkpoint.",
                    )
                )
                continue
            reverse = parse_reverse_operation(op)
            assert reverse is not None
            results.append(
                OperationResult(
                    op_id=op.op_id,
                    kind=op.kind,
                    status="dry_run",
                    message=(
                        f"Dry-run: would roll back {op.summary()} "
                        f"via {reverse.kind.value} {dict(reverse.params)}"
                    ),
                )
            )
        return MigrationReport(plan=plan, results=tuple(results), rolled_back=False)

    def _apply_reverse_operation(
        self,
        forward_op: MigrationOperation,
        reverse: ReverseOperationSpec,
    ) -> OperationResult:
        merged_params = dict(forward_op.params)
        merged_params.update(reverse.params)
        reverse_op = MigrationOperation(
            kind=reverse.kind,
            model_name=forward_op.model_name,
            params=merged_params,
            destructive=reverse.destructive,
            op_id=forward_op.op_id,
        )
        return self._apply_operation(reverse_op)

    def _dry_run_report(self, plan: MigrationPlan) -> MigrationReport:
        resource_id = self.target.resource_id_for_model(plan.model_name)
        results: list[OperationResult] = []
        for op in plan.operations:
            message = f"Dry-run: would apply {op.summary()}"
            if resource_id is None and op.kind not in {
                MigrationOpKind.MOVE_RETAG,
                MigrationOpKind.REBUILD_INDEX,
                MigrationOpKind.RETAG_APP_PROPERTIES,
                MigrationOpKind.RECONCILE_METADATA_ROWS,
                MigrationOpKind.BIND_COLLECTION,
                MigrationOpKind.MOVE_MEMBER,
                MigrationOpKind.CONVERT_MIME,
            }:
                message += " (no bound resource)"
            results.append(
                OperationResult(
                    op_id=op.op_id,
                    kind=op.kind,
                    status="dry_run",
                    message=message,
                )
            )
        return MigrationReport(plan=plan, results=tuple(results), applied=False)

    def _apply_operation(self, op: MigrationOperation) -> OperationResult:
        missing = _missing_required_params(op)
        if missing:
            return OperationResult(
                op_id=op.op_id,
                kind=op.kind,
                status="error",
                message=(
                    f"Operation {op.kind.value} missing required params: "
                    f"{', '.join(missing)}."
                ),
            )

        resource_id = self.target.resource_id_for_model(op.model_name)
        if (
            resource_id is None
            and op.kind
            not in {
                MigrationOpKind.MOVE_RETAG,
                MigrationOpKind.REBUILD_INDEX,
                MigrationOpKind.RETAG_APP_PROPERTIES,
                MigrationOpKind.RECONCILE_METADATA_ROWS,
                MigrationOpKind.BIND_COLLECTION,
                MigrationOpKind.MOVE_MEMBER,
                MigrationOpKind.CONVERT_MIME,
                MigrationOpKind.BACKFILL_METADATA_COLUMN,
            }
            and not _is_metadata_table_op(op)
        ):
            return OperationResult(
                op_id=op.op_id,
                kind=op.kind,
                status="error",
                message=f"No resource bound for model '{op.model_name}'.",
            )

        if op.kind is MigrationOpKind.ADD_FIELD:
            if _is_metadata_table_op(op):
                return OperationResult(
                    op_id=op.op_id,
                    kind=op.kind,
                    status="error",
                    message=(
                        "ADD_FIELD on collection metadata must use "
                        "BACKFILL_METADATA_COLUMN."
                    ),
                )
            if not _is_sheet_table_op(op):
                return OperationResult(
                    op_id=op.op_id,
                    kind=op.kind,
                    status="error",
                    message=(
                        f"ADD_FIELD is unsupported for storage="
                        f"{op.params.get('storage')!r} "
                        f"target_kind={op.params.get('target_kind')!r}."
                    ),
                )
            assert resource_id is not None
            changed = self.target.sheet_add_column(
                resource_id,
                str(op.params["source_name"]),
                op.params.get("default"),
            )
            return OperationResult(
                op_id=op.op_id,
                kind=op.kind,
                status="ok",
                message=f"Added column '{op.params['source_name']}'.",
                changed=changed,
            )

        if op.kind is MigrationOpKind.RENAME_FIELD:
            if _is_metadata_table_op(op):
                if not callable(getattr(self.target, "metadata_rename_column", None)):
                    return OperationResult(
                        op_id=op.op_id,
                        kind=op.kind,
                        status="error",
                        message="Metadata rename is not supported by this target.",
                    )
                changed = self.target.metadata_rename_column(
                    op.model_name,
                    str(op.params["old_name"]),
                    str(op.params["new_name"]),
                )
                return OperationResult(
                    op_id=op.op_id,
                    kind=op.kind,
                    status="ok",
                    message=(
                        f"Renamed metadata '{op.params['old_name']}' to "
                        f"'{op.params['new_name']}'."
                    ),
                    changed=changed,
                )
            if not _is_sheet_table_op(op):
                return OperationResult(
                    op_id=op.op_id,
                    kind=op.kind,
                    status="error",
                    message=(
                        f"RENAME_FIELD is unsupported for storage="
                        f"{op.params.get('storage')!r} "
                        f"target_kind={op.params.get('target_kind')!r}."
                    ),
                )
            assert resource_id is not None
            changed = self.target.sheet_rename_column(
                resource_id,
                str(op.params["old_name"]),
                str(op.params["new_name"]),
            )
            return OperationResult(
                op_id=op.op_id,
                kind=op.kind,
                status="ok",
                message=(
                    f"Renamed '{op.params['old_name']}' to '{op.params['new_name']}'."
                ),
                changed=changed,
            )

        if op.kind is MigrationOpKind.TRANSFORM_FIELD:
            transformer = build_type_coercion_transformer(
                str(op.params.get("from_type", "")),
                str(op.params.get("to_type", "")),
            )
            if transformer is None:
                return OperationResult(
                    op_id=op.op_id,
                    kind=op.kind,
                    status="error",
                    message=(
                        f"No supported deterministic coercion from "
                        f"{op.params.get('from_type')!r} to "
                        f"{op.params.get('to_type')!r}. "
                        "Provide an explicit transform or adjust the model types."
                    ),
                )
            column = str(op.params["source_name"])
            if _is_metadata_table_op(op):
                metadata_transform = getattr(
                    self.target, "metadata_transform_column", None
                )
                if not callable(metadata_transform):
                    return OperationResult(
                        op_id=op.op_id,
                        kind=op.kind,
                        status="error",
                        message="Metadata transform is not supported by this target.",
                    )
                changed, errors = self.target.metadata_transform_column(
                    op.model_name,
                    column,
                    transformer,
                )
            elif _is_sheet_table_op(op):
                assert resource_id is not None
                changed, errors = self.target.sheet_transform_column(
                    resource_id,
                    column,
                    transformer,
                )
            else:
                return OperationResult(
                    op_id=op.op_id,
                    kind=op.kind,
                    status="error",
                    message=(
                        f"TRANSFORM_FIELD is unsupported for storage="
                        f"{op.params.get('storage')!r}."
                    ),
                )
            if changed == 0 and not errors:
                return OperationResult(
                    op_id=op.op_id,
                    kind=op.kind,
                    status="error",
                    message=(
                        f"Transform for '{column}' made no changes; "
                        "refusing silent success."
                    ),
                )
            status = "ok" if not errors else "warning"
            return OperationResult(
                op_id=op.op_id,
                kind=op.kind,
                status=status,
                message=f"Transformed column '{column}'.",
                changed=changed,
                errors=errors,
            )

        if op.kind is MigrationOpKind.DROP_FIELD:
            column = str(op.params["source_name"])
            if _is_metadata_table_op(op):
                metadata_drop = getattr(self.target, "metadata_drop_column", None)
                if not callable(metadata_drop):
                    return OperationResult(
                        op_id=op.op_id,
                        kind=op.kind,
                        status="error",
                        message=(
                            "Metadata column drop is not supported by this target."
                        ),
                    )
                changed = self.target.metadata_drop_column(op.model_name, column)
                return OperationResult(
                    op_id=op.op_id,
                    kind=op.kind,
                    status="ok",
                    message=f"Dropped metadata column '{column}'.",
                    changed=changed,
                )
            if not _is_sheet_table_op(op):
                return OperationResult(
                    op_id=op.op_id,
                    kind=op.kind,
                    status="error",
                    message=(
                        f"DROP_FIELD is unsupported for storage="
                        f"{op.params.get('storage')!r} "
                        f"target_kind={op.params.get('target_kind')!r}."
                    ),
                )
            assert resource_id is not None
            changed = self.target.sheet_drop_column(resource_id, column)
            return OperationResult(
                op_id=op.op_id,
                kind=op.kind,
                status="ok",
                message=f"Dropped column '{column}'.",
                changed=changed,
            )

        if op.kind is MigrationOpKind.BACKFILL_SYSTEM_COLUMNS:
            if not _is_sheet_table_op(op):
                return OperationResult(
                    op_id=op.op_id,
                    kind=op.kind,
                    status="error",
                    message="System column backfill applies only to sheet storage.",
                )
            assert resource_id is not None
            changed = self.target.sheet_backfill_system_columns(resource_id)
            return OperationResult(
                op_id=op.op_id,
                kind=op.kind,
                status="ok",
                message="Backfilled GDM system columns.",
                changed=changed,
            )

        if op.kind is MigrationOpKind.MOVE_RETAG:
            self.target.file_move(
                str(op.params["file_id"]),
                str(op.params["new_parent_id"]),
            )
            self.target.file_retag(
                str(op.params["file_id"]),
                gdm_model=op.params.get("gdm_model"),
                gdm_schema=op.params.get("gdm_schema"),
                gdm_project=op.params.get("gdm_project"),
            )
            return OperationResult(
                op_id=op.op_id,
                kind=op.kind,
                status="ok",
                message="Moved and retagged file.",
                changed=1,
            )

        if op.kind is MigrationOpKind.REBUILD_INDEX:
            changed = self.target.rebuild_index(
                op.model_name,
                str(op.params["field"]),
            )
            return OperationResult(
                op_id=op.op_id,
                kind=op.kind,
                status="ok",
                message=f"Rebuilt index for '{op.params['field']}' (metadata-only).",
                changed=changed,
            )

        if op.kind is MigrationOpKind.BIND_COLLECTION:
            self.target.bind_collection(
                op.model_name,
                folder_id=str(op.params["folder_id"]),
                collection_path=op.params.get("collection_path"),
            )
            return OperationResult(
                op_id=op.op_id,
                kind=op.kind,
                status="ok",
                message="Bound folder as collection (metadata-only).",
                changed=1,
            )

        if op.kind is MigrationOpKind.UNBIND_COLLECTION:
            unbind = getattr(self.target, "unbind_collection", None)
            if not callable(unbind):
                return OperationResult(
                    op_id=op.op_id,
                    kind=op.kind,
                    status="error",
                    message="Collection unbind is not supported by this target.",
                )
            unbind(op.model_name, folder_id=str(op.params["folder_id"]))
            return OperationResult(
                op_id=op.op_id,
                kind=op.kind,
                status="ok",
                message="Unbound collection (destructive).",
                changed=1,
            )

        if op.kind is MigrationOpKind.BACKFILL_METADATA_COLUMN:
            if not _is_metadata_table_op(op):
                return OperationResult(
                    op_id=op.op_id,
                    kind=op.kind,
                    status="error",
                    message=(
                        "Metadata backfill applies only to collection metadata columns."
                    ),
                )
            changed = self.target.metadata_add_column(
                op.model_name,
                str(op.params["source_name"]),
                op.params.get("default"),
            )
            return OperationResult(
                op_id=op.op_id,
                kind=op.kind,
                status="ok",
                message=f"Backfilled metadata column '{op.params['source_name']}'.",
                changed=changed,
            )

        if op.kind is MigrationOpKind.RECONCILE_METADATA_ROWS:
            orphans, missing = self.target.metadata_reconcile_rows(
                op.model_name,
                folder_file_ids=set(op.params.get("folder_file_ids", [])),
            )
            return OperationResult(
                op_id=op.op_id,
                kind=op.kind,
                status="ok",
                message=(
                    f"Reconciled metadata rows ({orphans} orphan, {missing} missing)."
                ),
                changed=orphans + missing,
            )

        if op.kind is MigrationOpKind.RETAG_APP_PROPERTIES:
            self.target.file_retag(
                str(op.params["file_id"]),
                gdm_model=op.params.get("gdm_model"),
                gdm_schema=op.params.get("gdm_schema"),
                gdm_project=op.params.get("gdm_project"),
            )
            return OperationResult(
                op_id=op.op_id,
                kind=op.kind,
                status="ok",
                message="Retagged routing appProperties (metadata-only).",
                changed=1,
            )

        if op.kind is MigrationOpKind.MOVE_MEMBER:
            self.target.file_move(
                str(op.params["file_id"]),
                str(op.params["new_parent_id"]),
            )
            return OperationResult(
                op_id=op.op_id,
                kind=op.kind,
                status="ok",
                message="Moved collection member (destructive).",
                changed=1,
            )

        if op.kind is MigrationOpKind.CONVERT_MIME:
            return OperationResult(
                op_id=op.op_id,
                kind=op.kind,
                status="error",
                message=(
                    "MIME conversion requires explicit content APIs; not auto-applied."
                ),
            )

        return OperationResult(
            op_id=op.op_id,
            kind=op.kind,
            status="error",
            message=f"Unsupported operation kind: {op.kind}",
        )


_REQUIRED_PARAMS: dict[MigrationOpKind, tuple[str, ...]] = {
    MigrationOpKind.ADD_FIELD: ("source_name",),
    MigrationOpKind.RENAME_FIELD: ("old_name", "new_name"),
    MigrationOpKind.TRANSFORM_FIELD: ("source_name",),
    MigrationOpKind.DROP_FIELD: ("source_name",),
    MigrationOpKind.MOVE_RETAG: ("file_id", "new_parent_id"),
    MigrationOpKind.RETAG_APP_PROPERTIES: ("file_id",),
    MigrationOpKind.MOVE_MEMBER: ("file_id", "new_parent_id"),
    MigrationOpKind.CONVERT_MIME: ("file_id",),
    MigrationOpKind.BIND_COLLECTION: ("folder_id",),
    MigrationOpKind.UNBIND_COLLECTION: ("folder_id",),
    MigrationOpKind.BACKFILL_METADATA_COLUMN: ("source_name",),
    MigrationOpKind.REBUILD_INDEX: ("field",),
}


def _missing_required_params(op: MigrationOperation) -> list[str]:
    required = _REQUIRED_PARAMS.get(op.kind, ())
    return [name for name in required if not op.params.get(name)]


@dataclass
class InMemorySheet:
    """Sheet state for tests."""

    headers: list[str]
    rows: list[list[Any]]


@dataclass
class InMemoryMigrationTarget:
    """Rich in-memory migration target for unit tests."""

    sheets: dict[str, InMemorySheet]
    model_bindings: dict[str, str]
    files: dict[str, dict[str, Any]]
    indexes: dict[tuple[str, str], dict[str, list[str]]]
    metadata_sheets: dict[str, InMemorySheet] = field(default_factory=dict)
    checkpoints: dict[str, MigrationCheckpoint] = field(default_factory=dict)
    write_log: list[str] = field(default_factory=list)

    def resource_id_for_model(self, model_name: str) -> str | None:
        return self.model_bindings.get(model_name)

    def sheet_headers(self, resource_id: str) -> list[str]:
        return list(self.sheets[resource_id].headers)

    def sheet_add_column(
        self, resource_id: str, header: str, default: Any = None
    ) -> int:
        sheet = self.sheets[resource_id]
        if header in sheet.headers:
            return 0
        sheet.headers.append(header)
        idx = len(sheet.headers) - 1
        for row in sheet.rows:
            while len(row) <= idx:
                row.append(default)
            row[idx] = default
        return len(sheet.rows)

    def sheet_rename_column(
        self, resource_id: str, old_name: str, new_name: str
    ) -> int:
        sheet = self.sheets[resource_id]
        if old_name not in sheet.headers:
            return 0
        idx = sheet.headers.index(old_name)
        sheet.headers[idx] = new_name
        return len(sheet.rows)

    def sheet_transform_column(
        self,
        resource_id: str,
        column: str,
        transformer: Callable[[Any], Any],
    ) -> tuple[int, tuple[str, ...]]:
        sheet = self.sheets[resource_id]
        if column not in sheet.headers:
            return 0, (f"Column '{column}' not found.",)
        idx = sheet.headers.index(column)
        errors: list[str] = []
        changed = 0
        for row_idx, row in enumerate(sheet.rows):
            if idx >= len(row):
                continue
            value = row[idx]
            try:
                new_value = transformer(row[idx])
            except Exception as exc:
                errors.append(f"row {row_idx}: {exc}")
                continue
            if new_value != value:
                row[idx] = new_value
                changed += 1
        return changed, tuple(errors)

    def sheet_drop_column(self, resource_id: str, header: str) -> int:
        sheet = self.sheets[resource_id]
        if header not in sheet.headers:
            return 0
        idx = sheet.headers.index(header)
        del sheet.headers[idx]
        for row in sheet.rows:
            if idx < len(row):
                del row[idx]
        return len(sheet.rows)

    def sheet_backfill_system_columns(self, resource_id: str) -> int:
        sheet = self.sheets[resource_id]
        changed = 0
        for col in ("_gdm_id", "_gdm_version", "_gdm_updated_at"):
            if col not in sheet.headers:
                sheet.headers.append(col)
                changed += 1
                for row in sheet.rows:
                    row.append(None)
        for row in sheet.rows:
            id_idx = sheet.headers.index("_gdm_id")
            if id_idx >= len(row) or row[id_idx] in (None, ""):
                row[id_idx] = str(uuid.uuid4())
        return changed

    def file_move(self, file_id: str, new_parent_id: str) -> None:
        meta = self.files.setdefault(file_id, {})
        meta["parent_id"] = new_parent_id

    def file_retag(
        self,
        file_id: str,
        *,
        gdm_model: str | None = None,
        gdm_schema: str | None = None,
        gdm_project: str | None = None,
    ) -> None:
        meta = self.files.setdefault(file_id, {})
        props = meta.setdefault("app_properties", {})
        if gdm_model is not None:
            props["gdm_model"] = gdm_model
        if gdm_schema is not None:
            props["gdm_schema"] = gdm_schema
        if gdm_project is not None:
            props["gdm_project"] = gdm_project

    def rebuild_index(self, model_name: str, field_name: str) -> int:
        key = (model_name, field_name)
        self.indexes[key] = {}
        self.write_log.append(f"index.rebuild:{model_name}.{field_name}")
        return 0

    def metadata_headers(self, model_name: str) -> list[str]:
        sheet = self.metadata_sheets.get(model_name)
        return list(sheet.headers) if sheet else []

    def metadata_add_column(
        self, model_name: str, header: str, default: Any = None
    ) -> int:
        sheet = self.metadata_sheets.setdefault(
            model_name, InMemorySheet(headers=[], rows=[])
        )
        if header in sheet.headers:
            return 0
        sheet.headers.append(header)
        self.write_log.append(f"metadata.add_column:{model_name}.{header}")
        for row in sheet.rows:
            row.append(default)
        return len(sheet.rows)

    def metadata_rename_column(
        self, model_name: str, old_name: str, new_name: str
    ) -> int:
        sheet = self.metadata_sheets.get(model_name)
        if sheet is None or old_name not in sheet.headers:
            return 0
        idx = sheet.headers.index(old_name)
        sheet.headers[idx] = new_name
        self.write_log.append(f"metadata.rename:{model_name}.{old_name}->{new_name}")
        return len(sheet.rows)

    def metadata_drop_column(self, model_name: str, header: str) -> int:
        sheet = self.metadata_sheets.get(model_name)
        if sheet is None or header not in sheet.headers:
            return 0
        idx = sheet.headers.index(header)
        del sheet.headers[idx]
        for row in sheet.rows:
            if idx < len(row):
                del row[idx]
        self.write_log.append(f"metadata.drop:{model_name}.{header}")
        return len(sheet.rows)

    def metadata_transform_column(
        self,
        model_name: str,
        column: str,
        transformer: Callable[[Any], Any],
    ) -> tuple[int, tuple[str, ...]]:
        sheet = self.metadata_sheets.get(model_name)
        if sheet is None or column not in sheet.headers:
            return 0, (f"Column '{column}' not found.",)
        idx = sheet.headers.index(column)
        errors: list[str] = []
        changed = 0
        for row_idx, row in enumerate(sheet.rows):
            if idx >= len(row):
                continue
            value = row[idx]
            try:
                new_value = transformer(value)
            except Exception as exc:
                errors.append(f"row {row_idx}: {exc}")
                continue
            if new_value != value:
                row[idx] = new_value
                changed += 1
        if changed:
            self.write_log.append(f"metadata.transform:{model_name}.{column}")
        return changed, tuple(errors)

    def metadata_reconcile_rows(
        self, model_name: str, *, folder_file_ids: set[str]
    ) -> tuple[int, int]:
        sheet = self.metadata_sheets.get(model_name)
        if sheet is None or "_gdm_file_id" not in sheet.headers:
            return 0, 0
        idx = sheet.headers.index("_gdm_file_id")
        row_ids = {str(row[idx]) for row in sheet.rows if idx < len(row)}
        orphans = len(row_ids - folder_file_ids)
        missing = len(folder_file_ids - row_ids)
        self.write_log.append(f"metadata.reconcile:{model_name}")
        return orphans, missing

    def bind_collection(
        self, model_name: str, *, folder_id: str, collection_path: str | None
    ) -> None:
        self.model_bindings[model_name] = folder_id
        self.write_log.append(f"bind_collection:{model_name}->{folder_id}")
        if collection_path:
            self.files.setdefault(folder_id, {})["collection_path"] = collection_path

    def unbind_collection(self, model_name: str, *, folder_id: str) -> None:
        if self.model_bindings.get(model_name) == folder_id:
            del self.model_bindings[model_name]
        folder_meta = self.files.get(folder_id)
        if isinstance(folder_meta, dict):
            folder_meta.pop("collection_path", None)
        self.write_log.append(f"unbind_collection:{model_name}")

    def save_checkpoint(self, checkpoint: MigrationCheckpoint) -> None:
        self.checkpoints[checkpoint.plan_id] = checkpoint

    def load_checkpoint(self, plan_id: str) -> MigrationCheckpoint | None:
        return self.checkpoints.get(plan_id)


def _serialize_operation(op: MigrationOperation) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "destructive": op.destructive,
        "kind": op.kind.value,
        "model_name": op.model_name,
        "op_id": op.op_id,
        "params": dict(op.params),
    }
    if op.reverse is not None:
        payload["reverse"] = dict(op.reverse)
    return payload


def _deserialize_operation(data: Mapping[str, Any]) -> MigrationOperation:
    return MigrationOperation(
        kind=MigrationOpKind(str(data["kind"])),
        model_name=str(data["model_name"]),
        params=dict(data.get("params", {})),
        destructive=bool(data.get("destructive", False)),
        reverse=data.get("reverse"),
        op_id=str(data.get("op_id", str(uuid.uuid4()))),
    )


def serialize_checkpoint(
    checkpoint: MigrationCheckpoint,
    plan: MigrationPlan | None = None,
) -> str:
    payload: dict[str, Any] = {
        "completed_op_ids": list(checkpoint.completed_op_ids),
        "model_name": checkpoint.model_name,
        "plan_id": checkpoint.plan_id,
        "rolled_back_op_ids": list(checkpoint.rolled_back_op_ids),
        "updated_at": checkpoint.updated_at,
    }
    if plan is not None:
        payload["operations"] = [_serialize_operation(op) for op in plan.operations]
        if plan.from_snapshot is not None:
            from gdm.schema import serialize_snapshot

            payload["from_snapshot"] = serialize_snapshot(plan.from_snapshot)
        payload["dry_run"] = plan.dry_run
        payload["allow_destructive"] = plan.allow_destructive
    return json.dumps(payload, sort_keys=True)


def load_checkpoint(raw: str) -> MigrationCheckpoint:
    data = json.loads(raw)
    return MigrationCheckpoint(
        plan_id=str(data["plan_id"]),
        model_name=str(data["model_name"]),
        completed_op_ids=tuple(data.get("completed_op_ids", [])),
        rolled_back_op_ids=tuple(data.get("rolled_back_op_ids", [])),
        updated_at=str(data.get("updated_at", datetime.now(UTC).isoformat())),
    )


def load_checkpoint_plan(raw: str) -> MigrationPlan | None:
    """Rehydrate a forward migration plan stored alongside a checkpoint."""
    data = json.loads(raw)
    operations_raw = data.get("operations")
    if not operations_raw:
        return None
    from_snapshot = None
    if "from_snapshot" in data:
        from gdm.schema import load_snapshot_json

        from_snapshot = load_snapshot_json(str(data["from_snapshot"]))
    to_snapshot = None
    operations = tuple(_deserialize_operation(item) for item in operations_raw)
    return MigrationPlan(
        model_name=str(data["model_name"]),
        operations=operations,
        from_snapshot=from_snapshot,
        to_snapshot=to_snapshot,
        dry_run=bool(data.get("dry_run", False)),
        allow_destructive=bool(data.get("allow_destructive", False)),
    )
