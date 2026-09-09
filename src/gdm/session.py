"""Session and result objects."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Generic, TypeVar, cast

from gdm.backends.base import Backend, RecordLocator, StorageKind
from gdm.backends.memory import build_stored_record, materialize
from gdm.errors import (
    CommitOperation,
    ConflictError,
    GDMError,
    PartialCommitError,
    ReadOnlySessionError,
    ResultError,
    ScanLimitExceeded,
)
from gdm.indexes import (
    GdmIndexError,
    IndexRuntime,
    record_payload_for_index,
    validate_unique_constraints,
)
from gdm.model import GDMModel
from gdm.query import Select, evaluate, extract_index_lookup, sort_key

T = TypeVar("T", bound=GDMModel)


@dataclass
class _InstanceState:
    instance: GDMModel
    persisted: bool
    deleted: bool = False
    snapshot_version: str | None = None


class Result(Generic[T]):
    """Query execution result."""

    def __init__(self, items: list[T]) -> None:
        self._items = items

    def all(self) -> list[T]:
        """Return all rows."""
        return list(self._items)

    def first(self) -> T | None:
        """Return the first row or ``None``."""
        if not self._items:
            return None
        return self._items[0]

    def one(self) -> T:
        """Return exactly one row."""
        if len(self._items) != 1:
            msg = f"Expected one result, found {len(self._items)}"
            raise ResultError(msg)
        return self._items[0]

    def one_or_none(self) -> T | None:
        """Return one row or ``None`` when empty."""
        if not self._items:
            return None
        if len(self._items) > 1:
            msg = f"Expected at most one result, found {len(self._items)}"
            raise ResultError(msg)
        return self._items[0]


class Session:
    """Unit-of-work session with explicit non-transaction commit semantics."""

    def __init__(
        self,
        backend: Backend,
        *,
        read_only: bool = False,
        scan_limit: int | None = None,
        index_runtime: IndexRuntime | None = None,
    ) -> None:
        self._backend = backend
        self._read_only = read_only
        self._scan_limit = scan_limit
        runtime = index_runtime or getattr(backend, "index_runtime", None)
        self._index_runtime = runtime
        self._tracked: dict[int, _InstanceState] = {}

    @property
    def read_only(self) -> bool:
        """Return whether mutating operations are disabled."""
        return self._read_only

    @property
    def backend(self) -> Backend:
        """Return the bound backend."""
        return self._backend

    def __enter__(self) -> Session:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if exc_type is None:
            return
        self.rollback()

    def add(self, instance: GDMModel) -> None:
        """Track a new or updated instance for commit."""
        self._ensure_writable()
        instance.gdm_assign_primary_key()
        self._track(instance, persisted=instance.gdm_is_persisted)

    def add_all(self, instances: Iterable[GDMModel]) -> None:
        """Track multiple instances."""
        for instance in instances:
            self.add(instance)

    def delete(self, instance: GDMModel) -> None:
        """Mark an instance for deletion on commit."""
        self._ensure_writable()
        state = self._track(instance, persisted=True)
        state.deleted = True

    def refresh(self, instance: GDMModel) -> None:
        """Reload an instance from the backend."""
        locator = instance.gdm_locator
        if locator is None:
            msg = "Cannot refresh an instance without a record locator"
            raise GDMError(msg)
        model = instance.__class__
        stored = self._backend.read(model, locator)
        if model.is_collection_model():
            collection_backend = getattr(self._backend, "_collection", None)
            inventory_item = None
            if collection_backend is not None:
                inventory_item = collection_backend.inventory(locator.resource_id).get(
                    locator.record_key
                )
            mapped = dict(stored.values)
            refreshed = model.model_validate(mapped)
            refreshed.gdm_bind_collection(
                locator=stored.locator,
                drive_version=stored.drive_version,
                drive_fingerprint=stored.drive_fingerprint,
                metadata_version=stored.metadata_version,
                metadata_fingerprint=stored.metadata_fingerprint,
                unknown_fields=stored.unknown,
                metadata_store=instance._gdm_state.metadata_store,
                inventory_item=inventory_item or instance._gdm_state.inventory_item,
                collection_backend=collection_backend,
                persisted=True,
            )
            instance.__dict__.update(refreshed.__dict__)
            instance._gdm_state = refreshed._gdm_state
            snapshot_version = stored.drive_version
        else:
            updated = materialize(model, stored)
            instance.__dict__.update(updated.__dict__)
            instance.gdm_bind(
                locator=stored.locator,
                version=stored.version,
                fingerprint=stored.fingerprint,
                unknown_fields=stored.unknown,
                persisted=True,
            )
            snapshot_version = stored.version
        tracked = self._tracked.get(id(instance))
        if tracked is not None:
            tracked.snapshot_version = snapshot_version
            tracked.persisted = True
            tracked.deleted = False

    def get(self, model: type[GDMModel], key: str) -> GDMModel | None:
        """Load one instance by primary key."""
        limit = self._effective_scan_limit()
        records = self._backend.scan(model, limit=limit)
        for stored in records:
            instance = self._materialize_record(model, stored)
            if instance.gdm_record_key() == str(key):
                self._track(instance, persisted=True)
                return instance
        if len(records) >= limit:
            raise ScanLimitExceeded(
                f"Scan limit {limit} reached while searching for {key!r}.",
                limit=limit,
                observed=len(records),
                model_name=model.gdm_model_name(),
            )
        return None

    def prefetch(
        self,
        model: type[GDMModel],
        keys: Iterable[str],
    ) -> dict[str, GDMModel]:
        """Batch-load related records by primary key without server-side joins."""
        unique_keys = list(dict.fromkeys(str(key) for key in keys if key))
        if not unique_keys:
            return {}
        found: dict[str, GDMModel] = {}
        remaining = set(unique_keys)
        limit = self._effective_scan_limit()
        records = self._backend.scan(model, limit=limit)
        for stored in records:
            instance = self._materialize_record(model, stored)
            key = instance.gdm_record_key()
            if key in remaining:
                self._track(instance, persisted=True)
                found[key] = instance
                remaining.remove(key)
                if not remaining:
                    break
        if remaining and len(records) >= limit:
            raise ScanLimitExceeded(
                f"Prefetch scan for {model.gdm_model_name()} exceeded limit {limit}.",
                limit=limit,
                observed=len(records),
                model_name=model.gdm_model_name(),
            )
        return found

    def exec(self, statement: Select[T]) -> Result[T]:
        """Execute a select statement locally against the backend."""
        model = statement.model
        instances = self._execute_select(model, statement)
        for instance in instances:
            self._track(instance, persisted=True)
        return Result(instances)

    def _execute_select(self, model: type[GDMModel], statement: Select[T]) -> list[T]:
        if self._index_runtime is not None and statement.where_expr is not None:
            lookup, remaining = extract_index_lookup(model, statement.where_expr)
            if lookup is not None:
                indexed = self._index_runtime.lookup_record_ids(
                    model,
                    lookup.field_name,
                    lookup.value,
                    self._backend,
                )
                if indexed is not None:
                    return self._materialize_indexed_results(
                        model,
                        statement,
                        record_ids=indexed,
                        filter_expr=remaining,
                    )
        return self._execute_scan_select(model, statement)

    def _execute_scan_select(
        self,
        model: type[GDMModel],
        statement: Select[T],
    ) -> list[T]:
        limit = self._effective_scan_limit()
        records = self._backend.scan(model, limit=limit)
        instances = [
            cast(T, self._materialize_record(model, record)) for record in records
        ]
        return self._finalize_select_instances(instances, statement)

    def _materialize_indexed_results(
        self,
        model: type[GDMModel],
        statement: Select[T],
        *,
        record_ids: tuple[str, ...],
        filter_expr: Any | None,
    ) -> list[T]:
        read_by_key = getattr(self._backend, "read_by_record_key", None)
        instances: list[T] = []
        for record_id in record_ids:
            try:
                if callable(read_by_key):
                    stored = read_by_key(model, record_id)
                else:
                    stored = self._backend.read(
                        model,
                        RecordLocator(
                            storage=self._storage_kind(model),
                            resource_id=self._resource_id_for_model(model),
                            record_key=record_id,
                        ),
                    )
            except GDMError:
                continue
            instances.append(cast(T, self._materialize_record(model, stored)))
        return self._finalize_select_instances(
            instances,
            statement,
            filter_expr=filter_expr or statement.where_expr,
        )

    def _finalize_select_instances(
        self,
        instances: list[T],
        statement: Select[T],
        *,
        filter_expr: Any | None = None,
    ) -> list[T]:
        expr = filter_expr if filter_expr is not None else statement.where_expr
        if expr is not None:
            instances = [instance for instance in instances if evaluate(expr, instance)]
        if statement.order_by_clauses:
            instances.sort(key=lambda item: sort_key(item, statement.order_by_clauses))
        if statement.offset_count is not None:
            instances = instances[statement.offset_count :]
        if statement.limit_count is not None:
            instances = instances[: statement.limit_count]
        return instances

    @staticmethod
    def _storage_kind(model: type[GDMModel]) -> StorageKind:
        storage = model.gdm_storage()
        if storage == "collection":
            return "collection"
        if model.uses_legacy_file_codec():
            return "file"
        if storage == "sheet":
            return "sheet"
        return "file"

    def _resource_id_for_model(self, model: type[GDMModel]) -> str:
        locator_fn = getattr(self._backend, "_collection_folder_id", None)
        if model.is_collection_model() and callable(locator_fn):
            return str(locator_fn(model))
        resource = getattr(self._backend, "ensure_resource", None)
        if callable(resource):
            return resource(model).resource_id
        return model.__gdm_source__ or model.gdm_model_name()

    def commit(self) -> None:
        """Validate and apply pending changes in deterministic order."""
        self._ensure_writable()
        operations = self._pending_operations()
        self._validate_unique_constraints(operations)
        completed: list[CommitOperation] = []
        for operation in operations:
            try:
                state = self._state_for_operation(operation)
                old_payload = self._indexed_payload_for_state(state)
                if operation.action == "delete":
                    self._commit_delete(operation)
                    self._maintain_indexes_after_delete(state, old_payload)
                else:
                    self._commit_write(operation)
                    self._maintain_indexes_after_write(state, old_payload)
                completed.append(operation)
            except ConflictError as exc:
                if completed:
                    pending = [op for op in operations if op not in completed]
                    msg = (
                        "Commit failed after applying "
                        f"{len(completed)} operation(s); "
                        f"{len(pending)} operation(s) remain pending"
                    )
                    raise PartialCommitError(
                        message=msg,
                        completed=completed,
                        pending=pending,
                        cause=exc,
                    ) from exc
                raise
            except GdmIndexError:
                raise
            except Exception as exc:
                pending = [op for op in operations if op not in completed]
                msg = (
                    "Commit failed after applying "
                    f"{len(completed)} operation(s); "
                    f"{len(pending)} operation(s) remain pending"
                )
                raise PartialCommitError(
                    message=msg,
                    completed=completed,
                    pending=pending,
                    cause=exc,
                ) from exc
        self._tracked.clear()

    def rollback(self) -> None:
        """Discard uncommitted local changes."""
        for state in self._tracked.values():
            state.instance.gdm_clear_binding()
        self._tracked.clear()

    def _ensure_writable(self) -> None:
        if self._read_only:
            raise ReadOnlySessionError("Session is read-only")

    def _collection_backend(self) -> Any | None:
        return getattr(self._backend, "_collection", None)

    def _materialize_record(self, model: type[GDMModel], stored: Any) -> GDMModel:
        backend = self._collection_backend() if model.is_collection_model() else None
        return materialize(model, stored, collection_backend=backend)

    def _effective_scan_limit(self) -> int:
        if self._scan_limit is not None:
            return self._scan_limit
        scan_limit = getattr(self._backend, "scan_limit", None)
        if isinstance(scan_limit, int):
            return scan_limit
        return 1000

    def _track(self, instance: GDMModel, *, persisted: bool) -> _InstanceState:
        key = id(instance)
        existing = self._tracked.get(key)
        if existing is None:
            state = _InstanceState(
                instance=instance,
                persisted=persisted,
                snapshot_version=instance._gdm_state.source_version,
            )
            self._tracked[key] = state
            return state
        existing.persisted = existing.persisted or persisted
        if existing.snapshot_version is None:
            existing.snapshot_version = instance._gdm_state.source_version
        return existing

    def _pending_operations(self) -> list[CommitOperation]:
        deletes: list[CommitOperation] = []
        creates: list[CommitOperation] = []
        updates: list[CommitOperation] = []
        for state in self._tracked.values():
            model = state.instance.__class__
            locator = state.instance.gdm_locator
            if state.deleted:
                if locator is None:
                    continue
                deletes.append(
                    CommitOperation(
                        action="delete",
                        model_name=model.gdm_model_name(),
                        locator=locator,
                    )
                )
                continue
            if state.persisted and locator is not None:
                updates.append(
                    CommitOperation(
                        action="update",
                        model_name=model.gdm_model_name(),
                        locator=locator,
                    )
                )
            else:
                stored = build_stored_record(model, state.instance)
                creates.append(
                    CommitOperation(
                        action="create",
                        model_name=model.gdm_model_name(),
                        locator=stored.locator,
                    )
                )

        def sort_key(op: CommitOperation) -> tuple[str, str, str]:
            locator = op.locator
            if isinstance(locator, RecordLocator):
                return (op.model_name, op.action, locator.record_key)
            return (op.model_name, op.action, "")

        ordered = sorted(deletes, key=sort_key)
        ordered.extend(sorted(creates, key=sort_key))
        ordered.extend(sorted(updates, key=sort_key))
        return ordered

    def _commit_write(self, operation: CommitOperation) -> None:
        state = self._state_for_operation(operation)
        instance = state.instance
        model = instance.__class__
        stored = build_stored_record(model, instance)
        result = self._backend.write(
            model,
            stored,
            expected_version=state.snapshot_version,
            create=operation.action == "create",
        )
        if model.is_collection_model():
            collection_backend = self._collection_backend()
            inventory_item = state.instance._gdm_state.inventory_item
            if collection_backend is not None and isinstance(
                result.locator, RecordLocator
            ):
                inventory_item = collection_backend.inventory(
                    result.locator.resource_id
                ).get(result.locator.record_key)
            instance.gdm_bind_collection(
                locator=result.locator,
                drive_version=result.version,
                drive_fingerprint=result.fingerprint,
                metadata_version=result.metadata_version,
                metadata_fingerprint=result.metadata_fingerprint,
                unknown_fields=stored.unknown,
                metadata_store=state.instance._gdm_state.metadata_store,
                inventory_item=inventory_item,
                collection_backend=collection_backend,
                persisted=True,
            )
        else:
            instance.gdm_bind(
                locator=result.locator,
                version=result.version,
                fingerprint=result.fingerprint,
                unknown_fields=stored.unknown,
                persisted=True,
            )
        state.snapshot_version = result.version

    def _commit_delete(self, operation: CommitOperation) -> None:
        state = self._state_for_operation(operation)
        instance = state.instance
        model = instance.__class__
        locator = operation.locator
        if not isinstance(locator, RecordLocator):
            msg = "Delete operation requires a record locator"
            raise GDMError(msg)
        self._backend.delete(
            model,
            locator,
            expected_version=state.snapshot_version,
        )
        instance.gdm_clear_binding()

    def _state_for_operation(self, operation: CommitOperation) -> _InstanceState:
        for state in self._tracked.values():
            if state.instance.__class__.gdm_model_name() != operation.model_name:
                continue
            locator = state.instance.gdm_locator
            if not isinstance(operation.locator, RecordLocator):
                continue
            if locator is None and operation.action == "create":
                if state.instance.gdm_record_key() == operation.locator.record_key:
                    return state
                continue
            if (
                locator is not None
                and locator.record_key == operation.locator.record_key
            ):
                return state
        msg = f"No tracked instance for operation {operation.action}"
        raise GDMError(msg)

    def _validate_unique_constraints(self, operations: list[CommitOperation]) -> None:
        grouped: dict[type[GDMModel], list[tuple[str, Any | None, dict[str, Any]]]] = {}
        deleted_keys: dict[type[GDMModel], set[str]] = {}
        for state in self._tracked.values():
            model = state.instance.__class__
            if state.deleted:
                deleted_keys.setdefault(model, set()).add(
                    state.instance.gdm_record_key()
                )
        for state in self._tracked.values():
            if state.deleted:
                continue
            model = state.instance.__class__
            record_id = state.instance.gdm_record_key()
            old_payload = self._indexed_payload_for_state(state)
            new_payload = self._indexed_payload_from_instance(state.instance)
            grouped.setdefault(model, []).append((record_id, old_payload, new_payload))
        for model, pending in grouped.items():
            filtered = [
                item
                for item in pending
                if item[0] not in deleted_keys.get(model, set())
            ]
            validate_unique_constraints(
                model=model,
                pending=filtered,
                backend=self._backend,
            )

    def _indexed_payload_for_state(
        self, state: _InstanceState
    ) -> dict[str, Any] | None:
        instance = state.instance
        model = instance.__class__
        if not state.persisted or instance.gdm_locator is None:
            return None
        stored = self._backend.read(model, instance.gdm_locator)
        return record_payload_for_index(model, stored)

    @staticmethod
    def _indexed_payload_from_instance(instance: GDMModel) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for name in instance.__class__.model_fields:
            payload[name] = getattr(instance, name)
        return payload

    def _maintain_indexes_after_write(
        self,
        state: _InstanceState,
        old_payload: dict[str, Any] | None,
    ) -> None:
        if self._index_runtime is None:
            return
        instance = state.instance
        model = instance.__class__
        self._index_runtime.maintain_after_write(
            model=model,
            record_id=instance.gdm_record_key(),
            old_payload=old_payload,
            new_payload=self._indexed_payload_from_instance(instance),
            backend=self._backend,
        )

    def _maintain_indexes_after_delete(
        self,
        state: _InstanceState,
        old_payload: dict[str, Any] | None,
    ) -> None:
        if self._index_runtime is None or old_payload is None:
            return
        instance = state.instance
        model = instance.__class__
        self._index_runtime.maintain_after_delete(
            model=model,
            record_id=instance.gdm_record_key(),
            old_payload=old_payload,
            backend=self._backend,
        )
