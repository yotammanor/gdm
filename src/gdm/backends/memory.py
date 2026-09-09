"""In-memory backend for tests and local development."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from gdm.backends.base import (
    BackendCapabilities,
    DeleteResult,
    ModelSchemaInfo,
    RecordLocator,
    StoredRecord,
    WriteResult,
)
from gdm.errors import ConflictError, GDMError
from gdm.fields import field_storage_target, source_name
from gdm.indexes import IndexRuntime, compute_source_fingerprint
from gdm.model import (
    SYSTEM_ID_FIELD,
    SYSTEM_UPDATED_AT_FIELD,
    SYSTEM_VERSION_FIELD,
    GDMModel,
    utc_now_iso,
)


@dataclass
class MemoryResource:
    """One logical Drive file or worksheet."""

    storage: str
    resource_id: str
    records: dict[str, StoredRecord] = field(default_factory=dict)


@dataclass
class OperationLogEntry:
    """One recorded backend operation."""

    action: str
    model_name: str
    locator: RecordLocator
    detail: str = ""


class MemoryBackend:
    """Backend that simulates file and sheet storage in memory."""

    def __init__(
        self,
        *,
        index_runtime: IndexRuntime | None = None,
    ) -> None:
        self._resources: dict[tuple[str, str], MemoryResource] = {}
        self.operation_log: list[OperationLogEntry] = []
        self.scan_call_count = 0
        self.index_runtime = index_runtime

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            supports_scan=True,
            supports_metadata_pushdown=False,
            supports_sidecar_index=self.index_runtime is not None,
            supports_batch_write=True,
            supports_conditional_write=True,
        )

    def inspect_schema(self, model: type[GDMModel]) -> ModelSchemaInfo:
        storage = model.gdm_storage()
        if storage is None:
            msg = f"Model {model.__name__} has no storage configured"
            raise GDMError(msg)
        return ModelSchemaInfo(
            model_name=model.gdm_model_name(),
            storage=storage,
            source=model.__gdm_source__,
            mapping=model.gdm_mapping(),
            schema_version=model.__gdm_schema_version__,
            field_sources=tuple(model.field_source_map().items()),
        )

    def ensure_resource(self, model: type[GDMModel]) -> MemoryResource:
        storage = model.gdm_storage()
        if storage is None:
            msg = f"Model {model.__name__} has no storage configured"
            raise GDMError(msg)
        if model.uses_legacy_file_codec():
            resource_storage = "file"
        elif model.is_collection_model():
            resource_storage = "collection"
        else:
            resource_storage = storage
        resource_id = model.__gdm_source__ or model.gdm_model_name()
        key = (resource_storage, resource_id)
        if key not in self._resources:
            self._resources[key] = MemoryResource(
                storage=resource_storage, resource_id=resource_id
            )
        return self._resources[key]

    def seed_record(
        self,
        model: type[GDMModel],
        values: dict[str, Any],
        *,
        record_key: str | None = None,
        version: str = "1",
        unknown: dict[str, Any] | None = None,
    ) -> StoredRecord:
        """Insert an initial record for tests without going through Session."""
        resource = self.ensure_resource(model)
        key = record_key or self._derive_record_key(model, values)
        unknown_values = dict(unknown or {})
        payload = dict(values)
        if model.gdm_storage() == "sheet":
            payload.setdefault(SYSTEM_ID_FIELD, key)
            payload.setdefault(SYSTEM_VERSION_FIELD, version)
            payload.setdefault(SYSTEM_UPDATED_AT_FIELD, utc_now_iso())
        fingerprint = self._fingerprint(payload, unknown_values)
        storage = model.gdm_storage()
        assert storage is not None
        locator = RecordLocator(
            storage=storage,
            resource_id=resource.resource_id,
            record_key=key,
        )
        stored = StoredRecord(
            locator=locator,
            values=payload,
            unknown=unknown_values,
            version=version,
            fingerprint=fingerprint,
        )
        resource.records[key] = stored
        return stored

    def read(self, model: type[GDMModel], locator: RecordLocator) -> StoredRecord:
        resource = self._get_resource(locator)
        try:
            return _clone_record(resource.records[locator.record_key])
        except KeyError as exc:
            msg = f"Record {locator.record_key!r} not found"
            raise GDMError(msg) from exc

    def read_by_record_key(
        self,
        model: type[GDMModel],
        record_key: str,
    ) -> StoredRecord:
        """Load one record by primary key without scanning."""
        resource = self.ensure_resource(model)
        try:
            return _clone_record(resource.records[record_key])
        except KeyError as exc:
            msg = f"Record {record_key!r} not found"
            raise GDMError(msg) from exc

    def scan(
        self,
        model: type[GDMModel],
        *,
        limit: int | None = None,
    ) -> list[StoredRecord]:
        self.scan_call_count += 1
        resource = self.ensure_resource(model)
        records = [_clone_record(record) for record in resource.records.values()]
        records.sort(key=lambda item: item.locator.record_key)
        if limit is not None:
            return records[:limit]
        return records

    def compute_index_fingerprints(
        self,
        model: type[GDMModel],
    ) -> tuple[str, Any]:
        from gdm.indexes import IndexFingerprints

        resource = self.ensure_resource(model)
        versions = [int(record.version or "0") for record in resource.records.values()]
        aggregate_version = max(versions) if versions else 0
        fingerprint = compute_source_fingerprint(
            resource_id=resource.resource_id,
            row_count=len(resource.records),
            version=aggregate_version,
        )
        if model.is_collection_model():
            fps = IndexFingerprints(
                drive_fingerprint=fingerprint,
                metadata_fingerprint=self._metadata_table_fingerprint(resource),
            )
            return fps.combined(), fps
        return fingerprint, None

    @staticmethod
    def _metadata_table_fingerprint(resource: MemoryResource) -> str:
        payload = {
            record.locator.record_key: (
                record.metadata_fingerprint or record.fingerprint
            )
            for record in resource.records.values()
        }
        encoded = json.dumps(payload, sort_keys=True, default=str).encode()
        return hashlib.sha256(encoded).hexdigest()

    def write(
        self,
        model: type[GDMModel],
        record: StoredRecord,
        *,
        expected_version: str | None,
        create: bool = False,
    ) -> WriteResult:
        resource = (
            self.ensure_resource(model)
            if create
            else self._get_resource(record.locator)
        )
        existing = resource.records.get(record.locator.record_key)
        if create and existing is not None:
            msg = f"Record {record.locator.record_key!r} already exists"
            raise GDMError(msg)
        if not create and existing is None:
            msg = f"Record {record.locator.record_key!r} does not exist"
            raise GDMError(msg)
        if (
            expected_version is not None
            and existing is not None
            and existing.version != expected_version
        ):
            raise ConflictError(
                "Source version changed since last read",
                locator=record.locator,
                expected_version=expected_version,
                actual_version=existing.version,
            )
        next_version = (
            "1" if existing is None else str(int(existing.version or "0") + 1)
        )
        payload = dict(record.values)
        unknown = dict(record.unknown)
        if model.gdm_storage() == "sheet":
            payload[SYSTEM_VERSION_FIELD] = next_version
            payload[SYSTEM_UPDATED_AT_FIELD] = utc_now_iso()
            payload.setdefault(SYSTEM_ID_FIELD, record.locator.record_key)
        fingerprint = self._fingerprint(payload, unknown)
        stored = StoredRecord(
            locator=record.locator,
            values=payload,
            unknown=unknown,
            version=next_version,
            fingerprint=fingerprint,
        )
        resource.records[record.locator.record_key] = stored
        result = WriteResult(
            locator=record.locator,
            version=next_version,
            fingerprint=fingerprint,
            created=existing is None,
        )
        self.operation_log.append(
            OperationLogEntry(
                action="create" if existing is None else "update",
                model_name=model.gdm_model_name(),
                locator=record.locator,
            )
        )
        return result

    def delete(
        self,
        model: type[GDMModel],
        locator: RecordLocator,
        *,
        expected_version: str | None,
    ) -> DeleteResult:
        resource = self._get_resource(locator)
        existing = resource.records.get(locator.record_key)
        if existing is None:
            msg = f"Record {locator.record_key!r} does not exist"
            raise GDMError(msg)
        if expected_version is not None and existing.version != expected_version:
            raise ConflictError(
                "Source version changed since last read",
                locator=locator,
                expected_version=expected_version,
                actual_version=existing.version,
            )
        del resource.records[locator.record_key]
        self.operation_log.append(
            OperationLogEntry(
                action="delete",
                model_name=model.gdm_model_name(),
                locator=locator,
            )
        )
        return DeleteResult(locator=locator)

    def mutate_external(
        self,
        model: type[GDMModel],
        record_key: str,
        *,
        values: dict[str, Any] | None = None,
        unknown: dict[str, Any] | None = None,
        bump_version: bool = True,
    ) -> StoredRecord:
        """Simulate a direct human edit outside GDM."""
        resource = self.ensure_resource(model)
        existing = resource.records[record_key]
        payload = dict(existing.values)
        extra = dict(existing.unknown)
        if values:
            payload.update(values)
        if unknown:
            extra.update(unknown)
        version = existing.version or "1"
        if bump_version:
            version = str(int(version) + 1)
        fingerprint = self._fingerprint(payload, extra)
        stored = StoredRecord(
            locator=existing.locator,
            values=payload,
            unknown=extra,
            version=version,
            fingerprint=fingerprint,
        )
        resource.records[record_key] = stored
        return stored

    def _get_resource(self, locator: RecordLocator) -> MemoryResource:
        key = (locator.storage, locator.resource_id)
        try:
            return self._resources[key]
        except KeyError as exc:
            msg = f"Resource {locator.resource_id!r} not found"
            raise GDMError(msg) from exc

    def _derive_record_key(
        self,
        model: type[GDMModel],
        values: dict[str, Any],
    ) -> str:
        pk_field = model.primary_key_field()
        if pk_field is not None:
            source = source_name(pk_field, model.model_fields[pk_field])
            if source in values and values[source] is not None:
                return str(values[source])
            if pk_field in values and values[pk_field] is not None:
                return str(values[pk_field])
        if SYSTEM_ID_FIELD in values:
            return str(values[SYSTEM_ID_FIELD])
        return str(uuid.uuid4())

    @staticmethod
    def _fingerprint(values: dict[str, Any], unknown: dict[str, Any]) -> str:
        payload = {"values": values, "unknown": unknown}
        encoded = json.dumps(payload, sort_keys=True, default=str).encode()
        return hashlib.sha256(encoded).hexdigest()


def build_stored_record(model: type[GDMModel], instance: GDMModel) -> StoredRecord:
    """Build a ``StoredRecord`` from a model instance."""
    storage = model.gdm_storage()
    if storage is None:
        msg = f"Model {model.__name__} has no storage configured"
        raise GDMError(msg)
    record_key = instance.gdm_record_key()
    locator = instance.gdm_locator
    if locator is None:
        resource_id = model.__gdm_source__ or model.gdm_model_name()
        storage_kind = "file" if model.uses_legacy_file_codec() else storage
        locator = RecordLocator(
            storage=storage_kind,
            resource_id=resource_id,
            record_key=record_key,
        )
    values = instance.gdm_to_source(include_unknown=False)
    unknown = dict(instance._gdm_state.unknown_fields)
    state = instance._gdm_state
    if model.is_collection_model():
        for name, info in model.model_fields.items():
            target_kind, _ = field_storage_target(name, info, storage=storage)
            if target_kind in {"drive", "metadata", "property", "app_property"}:
                values[name] = getattr(instance, name)
    if storage == "sheet":
        values.setdefault(SYSTEM_ID_FIELD, record_key)
        if state.source_version is not None:
            values.setdefault(SYSTEM_VERSION_FIELD, state.source_version)
    return StoredRecord(
        locator=locator,
        values=values,
        unknown=unknown,
        version=state.source_version,
        fingerprint=state.source_fingerprint,
        drive_version=state.drive_version,
        drive_fingerprint=state.drive_fingerprint,
        metadata_version=state.metadata_version,
        metadata_fingerprint=state.metadata_fingerprint,
    )


def materialize(
    model: type[GDMModel],
    stored: StoredRecord,
    *,
    collection_backend: Any | None = None,
) -> GDMModel:
    """Hydrate a model instance from a stored record."""
    combined = dict(stored.values)
    instance = model.gdm_from_source(combined, unknown=stored.unknown)
    if stored.locator.storage == "collection":
        instance.gdm_bind_collection(
            locator=stored.locator,
            drive_version=stored.drive_version,
            drive_fingerprint=stored.drive_fingerprint,
            metadata_version=stored.metadata_version,
            metadata_fingerprint=stored.metadata_fingerprint,
            unknown_fields=stored.unknown,
            collection_backend=collection_backend,
            persisted=True,
        )
    else:
        instance.gdm_bind(
            locator=stored.locator,
            version=stored.version,
            fingerprint=stored.fingerprint,
            unknown_fields=stored.unknown,
            persisted=True,
        )
    return instance


def _clone_record(record: StoredRecord) -> StoredRecord:
    return StoredRecord(
        locator=record.locator,
        values=dict(record.values),
        unknown=dict(record.unknown),
        version=record.version,
        fingerprint=record.fingerprint,
        drive_version=record.drive_version,
        drive_fingerprint=record.drive_fingerprint,
        metadata_version=record.metadata_version,
        metadata_fingerprint=record.metadata_fingerprint,
    )
