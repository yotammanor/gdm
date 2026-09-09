"""Project-scoped backend implementing the :class:`Backend` protocol."""

from __future__ import annotations

from typing import Any

from gdm.backends.base import (
    BackendCapabilities,
    DeleteResult,
    ModelSchemaInfo,
    RecordLocator,
    StoredRecord,
    WriteResult,
)
from gdm.backends.drive import DriveBackend
from gdm.backends.memory import build_stored_record
from gdm.backends.sheets import (
    COL_GDM_ID,
    COL_GDM_VERSION,
    SYSTEM_COLUMNS,
    SheetsBackend,
)
from gdm.codecs import codec_mime_type, decode_body, encode_body, load_mapping_payload
from gdm.collection.backend import (
    CollectionBackend,
    collection_drive_fingerprint,
)
from gdm.collection.constants import FOLDER_MIME, METADATA_BACKEND_JSON, SHEET_MIME
from gdm.collection.inventory import collection_inventory_fingerprint
from gdm.collection.metadata import (
    DriveJsonCollectionMetadataStore,
    SheetsCollectionMetadataStore,
)
from gdm.errors import (
    ConflictError,
    GDMError,
    MappingMode,
    NotFoundError,
    ScanLimitExceeded,
    UnsupportedOperationError,
)
from gdm.fields import source_name
from gdm.indexes import (
    IndexFingerprints,
    IndexRuntime,
    IndexStore,
    StalePolicy,
    compute_collection_fingerprints,
    compute_source_fingerprint,
)
from gdm.model import SYSTEM_ID_FIELD, SYSTEM_VERSION_FIELD, GDMModel
from gdm.project import Project, RegistryEntry, ResourceKind


class RegistryBackend:
    """Route model CRUD through Drive and Sheets using the project registry."""

    def __init__(
        self,
        *,
        project: Project,
        drive: DriveBackend,
        sheets: SheetsBackend,
        scan_limit: int = 1000,
        collection_backend: CollectionBackend | None = None,
        metadata_stores: dict[str, Any] | None = None,
        index_store: IndexStore | None = None,
        stale_policy: StalePolicy = StalePolicy.FALLBACK,
        default_metadata_backend: str = "sheets",
    ) -> None:
        self.project = project
        self.drive = drive
        self.sheets = sheets
        self.scan_limit = scan_limit
        self.default_metadata_backend = default_metadata_backend
        self._row_index_cache: dict[tuple[str, str], int] = {}
        self._collection = collection_backend or CollectionBackend(
            drive,
            project_id=str(project.project_id),
            adoption_mode=project.adoption_mode,
            resource_scope_validator=project.assert_resource_in_project_scope,
        )
        self._metadata_stores: dict[str, Any] = dict(metadata_stores or {})
        self._index_store = index_store
        self._stale_policy = stale_policy
        self.index_runtime = (
            IndexRuntime(store=index_store, stale_policy=stale_policy)
            if index_store is not None
            else None
        )

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            supports_scan=True,
            supports_metadata_pushdown=False,
            supports_sidecar_index=self.index_runtime is not None,
            supports_batch_write=False,
            supports_conditional_write=True,
        )

    def configure_index_store(
        self,
        store: IndexStore,
        *,
        stale_policy: StalePolicy | None = None,
    ) -> None:
        """Attach or replace the optional sidecar index runtime."""
        self._index_store = store
        policy = stale_policy if stale_policy is not None else self._stale_policy
        self._stale_policy = policy
        self.index_runtime = IndexRuntime(store=store, stale_policy=policy)

    def rebuild_index(self, model: type[GDMModel], field_name: str) -> int:
        """Rebuild one registered model field index from the live backend."""
        if self.index_runtime is None:
            msg = "Index store is not configured on this backend."
            raise GDMError(msg)
        index = self.index_runtime.rebuild_field_index(model, field_name, self)
        return sum(len(ids) for ids in index.entries.values())

    def rebuild_model_indexes(self, model: type[GDMModel]) -> dict[str, int]:
        """Rebuild all indexed fields for ``model``."""
        if self.index_runtime is None:
            msg = "Index store is not configured on this backend."
            raise GDMError(msg)
        return self.index_runtime.rebuild_model_indexes(model, self)

    def compute_index_fingerprints(
        self,
        model: type[GDMModel],
    ) -> tuple[str, IndexFingerprints | None]:
        """Return live combined and component fingerprints for ``model``."""
        entry = self._registry_entry(model)
        if model.is_collection_model():
            folder_id = self._collection_folder_id(model)
            metadata_store = self._metadata_store_for(model)
            metadata_fp = self._metadata_table_fingerprint(metadata_store)
            inventory = self._collection.inventory(folder_id).scan(
                scan_limit=self.scan_limit
            )
            drive_version = collection_inventory_fingerprint(
                folder_id,
                inventory,
            )
            fps = compute_collection_fingerprints(
                folder_id=folder_id,
                drive_version=drive_version,
                metadata_table_fingerprint=metadata_fp,
            )
            return fps.combined(), fps
        if model.gdm_storage() == "sheet":
            spreadsheet_id, _sheet_title = self._sheet_binding(model)
            try:
                metadata = self.drive.get_metadata(spreadsheet_id)
            except NotFoundError:
                metadata = None
            if metadata is not None and metadata.version is not None:
                fingerprint = compute_source_fingerprint(
                    resource_id=spreadsheet_id,
                    version=metadata.version,
                )
                return fingerprint, None
            records = self.scan(model, limit=self.scan_limit + 1)
            if len(records) > self.scan_limit:
                raise ScanLimitExceeded(
                    f"Sheet fingerprint scan for {model.gdm_model_name()} "
                    f"exceeded limit {self.scan_limit}.",
                    limit=self.scan_limit,
                    observed=len(records),
                    model_name=model.gdm_model_name(),
                )
            versions = [int(record.version or "0") for record in records]
            fingerprint = compute_source_fingerprint(
                resource_id=spreadsheet_id,
                row_count=len(records),
                version=max(versions) if versions else 0,
            )
            return fingerprint, None
        fingerprint = compute_source_fingerprint(
            resource_id=entry.resource_id,
            row_count=len(self.scan(model, limit=self.scan_limit)),
        )
        return fingerprint, None

    @staticmethod
    def _metadata_table_fingerprint(metadata_store: Any) -> str:
        rows = metadata_store.list_rows()
        payload = {row.file_id: row.fingerprint for row in rows}
        import hashlib
        import json

        encoded = json.dumps(payload, sort_keys=True, default=str).encode()
        return hashlib.sha256(encoded).hexdigest()

    def inspect_schema(self, model: type[GDMModel]) -> ModelSchemaInfo:
        storage = model.gdm_storage()
        if storage is None:
            msg = f"Model {model.__name__} has no storage configured"
            raise GDMError(msg)
        entry = self._registry_entry(model)
        source = model.__gdm_source__ or entry.extra.get("worksheet")
        return ModelSchemaInfo(
            model_name=model.gdm_model_name(),
            storage=storage,
            source=source,
            mapping=model.gdm_mapping(),
            schema_version=model.__gdm_schema_version__,
            field_sources=tuple(model.field_source_map().items()),
        )

    def read(self, model: type[GDMModel], locator: RecordLocator) -> StoredRecord:
        if locator.storage == "collection":
            return self._read_collection(model, locator)
        if locator.storage == "file":
            return self._read_file(model, locator)
        if locator.storage == "sheet":
            return self._read_sheet(model, locator)
        msg = f"Unsupported storage {locator.storage!r}"
        raise UnsupportedOperationError(msg)

    def read_by_record_key(
        self,
        model: type[GDMModel],
        record_key: str,
    ) -> StoredRecord:
        """Load one record by logical primary key without scanning."""
        if model.is_collection_model():
            folder_id = self._collection_folder_id(model)
            return self._read_collection(
                model,
                RecordLocator(
                    storage="collection",
                    resource_id=folder_id,
                    record_key=record_key,
                ),
            )
        if model.gdm_storage() == "sheet":
            spreadsheet_id, _sheet_title = self._sheet_binding(model)
            return self._read_sheet(
                model,
                RecordLocator(
                    storage="sheet",
                    resource_id=spreadsheet_id,
                    record_key=record_key,
                ),
            )
        if model.uses_legacy_file_codec():
            parent_id = self._file_parent_id(model)
            page = self.drive.scan_by_model(
                model_name=model.gdm_model_name(),
                parent_id=parent_id,
                scan_limit=self.scan_limit,
            )
            for meta in page.items:
                key = str(meta.app_properties.get("gdm_id") or meta.file_id)
                if key == record_key:
                    locator = RecordLocator(
                        storage="file",
                        resource_id=meta.file_id,
                        record_key=key,
                    )
                    return self._read_file(model, locator)
            msg = f"Record {record_key!r} not found for {model.gdm_model_name()}"
            raise NotFoundError(msg)
        msg = f"Model {model.__name__} has no storage configured"
        raise GDMError(msg)

    def scan(
        self,
        model: type[GDMModel],
        *,
        limit: int | None = None,
    ) -> list[StoredRecord]:
        effective_limit = limit if limit is not None else self.scan_limit
        if model.is_collection_model():
            return self._scan_collection(model, limit=effective_limit)
        if model.uses_legacy_file_codec():
            return self._scan_files(model, limit=effective_limit)
        if model.gdm_storage() == "sheet":
            return self._scan_sheet(model, limit=effective_limit)
        msg = f"Model {model.__name__} has no storage configured"
        raise GDMError(msg)

    def write(
        self,
        model: type[GDMModel],
        record: StoredRecord,
        *,
        expected_version: str | None,
        create: bool = False,
    ) -> WriteResult:
        if record.locator.storage == "collection":
            return self._write_collection(
                model, record, expected_version=expected_version, create=create
            )
        if record.locator.storage == "file":
            return self._write_file(
                model, record, expected_version=expected_version, create=create
            )
        if record.locator.storage == "sheet":
            return self._write_sheet(
                model, record, expected_version=expected_version, create=create
            )
        msg = f"Unsupported storage {record.locator.storage!r}"
        raise UnsupportedOperationError(msg)

    def delete(
        self,
        model: type[GDMModel],
        locator: RecordLocator,
        *,
        expected_version: str | None,
    ) -> DeleteResult:
        if locator.storage == "collection":
            item = self._collection.inventory(locator.resource_id).get(
                locator.record_key
            )
            self._collection.assert_resource_belongs_to_project(item)
            if (
                expected_version is not None
                and item.version is not None
                and item.version != expected_version
            ):
                msg = (
                    f"Drive conflict for file {locator.record_key}: expected version "
                    f"{expected_version}, found {item.version}."
                )
                raise ConflictError(
                    msg,
                    locator=locator,
                    expected_version=expected_version,
                    actual_version=item.version,
                )
            self._collection.trash(locator.record_key)
            return DeleteResult(locator=locator)
        if locator.storage == "file":
            self.drive.delete(
                locator.resource_id,
                expected_version=expected_version,
            )
            return DeleteResult(locator=locator)
        if locator.storage == "sheet":
            spreadsheet_id, sheet_title = self._sheet_binding(model)
            pk_col = self._primary_key_column(model)
            row = self.sheets.find_row_by_primary_key(
                spreadsheet_id,
                sheet_title,
                primary_key=locator.record_key,
                primary_key_column=pk_col,
                scan_limit=self.scan_limit,
            )
            actual_version = str(row.version) if row.version is not None else None
            if expected_version is not None and actual_version != expected_version:
                msg = (
                    f"Conflict deleting row {row.row_index}: version mismatch "
                    f"(expected {expected_version}, found {actual_version})."
                )
                raise ConflictError(
                    msg,
                    locator=locator,
                    expected_version=expected_version,
                    actual_version=actual_version,
                )
            self.sheets.delete_row(
                spreadsheet_id,
                sheet_title,
                row_index=row.row_index,
            )
            cache_key = (spreadsheet_id, locator.record_key)
            self._row_index_cache.pop(cache_key, None)
            return DeleteResult(locator=locator)
        msg = f"Unsupported storage {locator.storage!r}"
        raise UnsupportedOperationError(msg)

    def bind_model(
        self,
        model: type[GDMModel],
        entry: RegistryEntry,
    ) -> None:
        """Register a model binding in the project registry."""
        self.project.register_resource(entry)
        model.__gdm_source__ = entry.extra.get("worksheet") or model.__gdm_source__

    def bind_collection(
        self,
        model: type[GDMModel],
        *,
        folder_id: str | None = None,
        path: str | None = None,
    ) -> RegistryEntry:
        """Bind a collection model to a Drive folder by ID or discovery path.

        Exactly one of ``folder_id`` or ``path`` must be supplied unless the
        model declares ``collection=`` and neither is given (uses the hint).

        Persists the immutable folder ID in the project registry. Path hints are
        stored for documentation only; subsequent reads and writes use the ID.
        Mutates the remote registry on success.
        """
        if folder_id is not None and path is not None:
            msg = "Provide folder_id or path, not both."
            raise ValueError(msg)
        collection_path: str | None = path
        if folder_id is None and path is None:
            collection_path = model.gdm_collection_path()
            if collection_path is None:
                msg = (
                    f"Model {model.gdm_model_name()!r} has no collection path hint "
                    "and no folder_id/path was supplied."
                )
                raise GDMError(msg)
            folder_id = self.project.resolve_path(collection_path)
        elif path is not None:
            folder_id = self.project.resolve_path(path)
        assert folder_id is not None
        metadata = self.project.assert_resource_in_project_scope(folder_id)
        mime_type = str(metadata.get("mimeType", ""))
        if mime_type != FOLDER_MIME:
            msg = f"Resource {folder_id!r} is not a folder (mimeType={mime_type!r})."
            raise GDMError(msg)
        extra: dict[str, Any] = {}
        if collection_path is None:
            collection_path = model.gdm_collection_path()
        if collection_path:
            extra["collection_path"] = collection_path
        backend = self.project.metadata_backend_for(
            model.gdm_model_name(),
            default=self.default_metadata_backend,
        )
        if backend == METADATA_BACKEND_JSON:
            extra["metadata_backend"] = METADATA_BACKEND_JSON
        elif self.default_metadata_backend != "sheets":
            extra["metadata_backend"] = backend
        entry = RegistryEntry(
            model_name=model.gdm_model_name(),
            resource_id=folder_id,
            kind=ResourceKind.FOLDER,
            extra=extra,
        )
        self.bind_model(model, entry)
        return entry

    def _registry_entry(self, model: type[GDMModel]) -> RegistryEntry:
        try:
            return self.project.get_registered(model.gdm_model_name())
        except NotFoundError as exc:
            msg = (
                f"Model {model.gdm_model_name()!r} is not bound in project "
                f"{self.project.project_id}."
            )
            raise NotFoundError(msg) from exc

    def collection(self, model: type[GDMModel]) -> Any:
        """Return a typed :class:`DriveCollection` for ``model``."""
        from gdm.collection.collection import DriveCollection

        folder_id = self._collection_folder_id(model)
        metadata_store = self._metadata_store_for(model)
        return DriveCollection(
            model=model,
            folder_id=folder_id,
            backend=self._collection,
            metadata_store=metadata_store,
            scan_limit=self.scan_limit,
        )

    def _metadata_store_for(self, model: type[GDMModel]) -> Any:
        model_name = model.gdm_model_name()
        if model_name in self._metadata_stores:
            return self._metadata_stores[model_name]
        backend = self.project.metadata_backend_for(
            model_name,
            default=self.default_metadata_backend,
        )
        if backend == METADATA_BACKEND_JSON:
            file_id = self.project.ensure_collection_json_metadata_binding(model_name)
            store = DriveJsonCollectionMetadataStore(
                self.project.drive,
                file_id=file_id,
            )
        else:
            sheet_id, worksheet = self.project.ensure_collection_metadata_binding(
                model_name
            )
            store = SheetsCollectionMetadataStore(
                self.sheets.service.spreadsheets(),
                spreadsheet_id=sheet_id,
                worksheet=worksheet,
            )
        self._metadata_stores[model_name] = store
        return store

    def register_metadata_store(self, model_name: str, store: Any) -> None:
        """Register a test or custom metadata store for ``model_name``."""
        self._metadata_stores[model_name] = store

    def _collection_folder_id(self, model: type[GDMModel]) -> str:
        entry = self._registry_entry(model)
        if entry.kind is ResourceKind.FOLDER:
            folder_id = entry.resource_id
        elif entry.parent_id is not None:
            folder_id = entry.parent_id
        else:
            folder_id = entry.resource_id
        self._assert_resource_type(folder_id, FOLDER_MIME, "Drive folder")
        return folder_id

    def _scan_collection(
        self,
        model: type[GDMModel],
        *,
        limit: int,
    ) -> list[StoredRecord]:
        folder_id = self._collection_folder_id(model)
        inventory = self._collection.inventory(folder_id)
        items = inventory.scan(
            scan_limit=limit + 1,
            include_folders=False,
            mime_patterns=model.__gdm_mime_patterns__ or None,
        )
        if len(items) > limit:
            raise ScanLimitExceeded(
                f"Collection scan for {model.gdm_model_name()} exceeded limit {limit}.",
                limit=limit,
                observed=len(items),
                model_name=model.gdm_model_name(),
            )
        metadata_store = self._metadata_store_for(model)
        records: list[StoredRecord] = []
        for item in items:
            if not self._collection.matches_model(model, item):
                continue
            row = metadata_store.get_row(item.file_id)
            metadata_values = dict(row.values) if row else {}
            mapped, unknown = self._collection.materialize_values(
                model, item, metadata_values
            )
            locator = RecordLocator(
                storage="collection",
                resource_id=folder_id,
                record_key=item.file_id,
            )
            records.append(
                StoredRecord(
                    locator=locator,
                    values=mapped,
                    unknown=unknown,
                    version=item.version,
                    fingerprint=collection_drive_fingerprint(item),
                    drive_version=item.version,
                    drive_fingerprint=collection_drive_fingerprint(item),
                    metadata_version=row.version if row else None,
                    metadata_fingerprint=row.fingerprint if row else None,
                )
            )
        records.sort(key=lambda item: item.locator.record_key)
        return records

    def _read_collection(
        self,
        model: type[GDMModel],
        locator: RecordLocator,
    ) -> StoredRecord:
        item = self._collection.inventory(locator.resource_id).get(locator.record_key)
        metadata_store = self._metadata_store_for(model)
        row = metadata_store.get_row(item.file_id)
        metadata_values = dict(row.values) if row else {}
        mapped, unknown = self._collection.materialize_values(
            model, item, metadata_values
        )
        return StoredRecord(
            locator=locator,
            values=mapped,
            unknown=unknown,
            version=item.version,
            fingerprint=collection_drive_fingerprint(item),
            drive_version=item.version,
            drive_fingerprint=collection_drive_fingerprint(item),
            metadata_version=row.version if row else None,
            metadata_fingerprint=row.fingerprint if row else None,
        )

    def _write_collection(
        self,
        model: type[GDMModel],
        record: StoredRecord,
        *,
        expected_version: str | None,
        create: bool,
    ) -> WriteResult:
        del create
        file_id = record.locator.record_key
        folder_id = record.locator.resource_id
        item = self._collection.inventory(folder_id).get(file_id)
        self._collection.assert_resource_belongs_to_project(item)
        if (
            expected_version is not None
            and item.version is not None
            and item.version != expected_version
        ):
            from gdm.errors import ConflictError

            msg = (
                f"Drive conflict for file {file_id}: expected version "
                f"{expected_version}, found {item.version}."
            )
            raise ConflictError(msg)
        instance = model.gdm_from_source(record.values, unknown=record.unknown)
        metadata_store = self._metadata_store_for(model)
        instance.gdm_bind_collection(
            locator=record.locator,
            drive_version=item.version,
            drive_fingerprint=collection_drive_fingerprint(item),
            metadata_version=record.metadata_version,
            metadata_fingerprint=record.metadata_fingerprint,
            unknown_fields=record.unknown,
            metadata_store=metadata_store,
            inventory_item=item,
            collection_backend=self._collection,
            persisted=True,
        )
        drive_body, metadata_values, properties, app_properties = (
            self._collection.build_write_payloads(model, instance)
        )
        routing = self._collection.routing_app_properties(model)
        merged_app = self._collection.merge_property_patch(item.app_properties, {})
        merged_app.update(app_properties)
        merged_app.update(routing)
        merged_props = self._collection.merge_property_patch(
            item.properties, properties
        )
        self._collection.validate_drive_properties(merged_props, kind="properties")
        self._collection.validate_drive_properties(merged_app, kind="appProperties")
        needs_drive_patch = (
            drive_body
            or properties
            or app_properties
            or merged_app != item.app_properties
        )
        if needs_drive_patch:
            body: dict[str, Any] = dict(drive_body)
            if merged_props != item.properties:
                body["properties"] = merged_props
            if merged_app != item.app_properties:
                body["appProperties"] = merged_app

            def _update() -> dict[str, Any]:
                return self.drive.service.files.update(
                    fileId=file_id,
                    body=body,
                    fields=self._collection.DEFAULT_FIELDS,
                    supportsAllDrives=self.drive.supports_all_drives,
                )

            from gdm.collection.inventory import inventory_item_from_file

            item = inventory_item_from_file(self.drive._call(_update, retryable=False))
        row = metadata_store.upsert_row(
            file_id,
            metadata_values,
            expected_version=record.metadata_version,
            expected_fingerprint=record.metadata_fingerprint,
        )
        locator = RecordLocator(
            storage="collection",
            resource_id=folder_id,
            record_key=file_id,
        )
        return WriteResult(
            locator=locator,
            version=str(item.version or "1"),
            fingerprint=collection_drive_fingerprint(item),
            metadata_version=row.version,
            metadata_fingerprint=row.fingerprint,
            created=False,
        )

    def _file_parent_id(self, model: type[GDMModel]) -> str:
        entry = self._registry_entry(model)
        if entry.kind is ResourceKind.FOLDER:
            parent_id = entry.resource_id
        elif entry.parent_id is not None:
            parent_id = entry.parent_id
        else:
            parent_id = entry.resource_id
        self._assert_resource_type(parent_id, FOLDER_MIME, "Drive folder")
        return parent_id

    def _sheet_binding(self, model: type[GDMModel]) -> tuple[str, str]:
        entry = self._registry_entry(model)
        if entry.kind not in {ResourceKind.SPREADSHEET, ResourceKind.WORKSHEET}:
            msg = f"Registry entry for {model.gdm_model_name()!r} is not a sheet."
            raise UnsupportedOperationError(msg)
        spreadsheet_id = entry.resource_id
        self._assert_resource_type(spreadsheet_id, SHEET_MIME, "Google Sheet")
        sheet_title = (
            entry.extra.get("worksheet")
            or model.__gdm_source__
            or model.gdm_model_name()
        )
        if not sheet_title:
            msg = f"No worksheet title configured for {model.gdm_model_name()!r}."
            raise GDMError(msg)
        return spreadsheet_id, str(sheet_title)

    def _assert_resource_type(
        self,
        resource_id: str,
        expected_mime: str,
        label: str,
    ) -> None:
        metadata = self.project.assert_resource_in_project_scope(resource_id)
        mime_type = str(metadata.get("mimeType", ""))
        if mime_type != expected_mime:
            msg = (
                f"Registry resource {resource_id!r} is not a {label} "
                f"(mimeType={mime_type!r})."
            )
            raise GDMError(msg)

    def _mapping_mode(self, model: type[GDMModel]) -> MappingMode:
        return MappingMode(model.gdm_mapping())

    def _declared_fields(self, model: type[GDMModel]) -> set[str]:
        fields = {source_name(name, info) for name, info in model.model_fields.items()}
        fields.update(set(model.model_fields))
        return fields

    def _primary_key_column(self, model: type[GDMModel]) -> str | None:
        pk_field = model.primary_key_field()
        if pk_field is None:
            return COL_GDM_ID
        return source_name(pk_field, model.model_fields[pk_field])

    def _scan_files(
        self,
        model: type[GDMModel],
        *,
        limit: int,
    ) -> list[StoredRecord]:
        if not model.uses_legacy_file_codec():
            msg = (
                f"Model {model.gdm_model_name()!r} uses collection storage; "
                "scan via folder inventory, not appProperties."
            )
            raise UnsupportedOperationError(msg)
        parent_id = self._file_parent_id(model)
        page = self.drive.scan_by_model(
            model_name=model.gdm_model_name(),
            parent_id=parent_id,
            scan_limit=limit + 1,
        )
        if page.truncated or len(page.items) > limit:
            raise ScanLimitExceeded(
                f"File scan for {model.gdm_model_name()} exceeded limit {limit}.",
                limit=limit,
                observed=len(page.items),
                model_name=model.gdm_model_name(),
            )
        records: list[StoredRecord] = []
        for meta in page.items:
            self.project.assert_resource_belongs_to_project(
                resource_id=meta.file_id,
                app_properties=meta.app_properties,
            )
            record_key = meta.app_properties.get("gdm_id") or meta.file_id
            values, unknown = self._read_file_values(
                model, meta.file_id, record_key=str(record_key)
            )
            locator = RecordLocator(
                storage="file",
                resource_id=meta.file_id,
                record_key=str(record_key),
            )
            records.append(
                StoredRecord(
                    locator=locator,
                    values=values,
                    unknown=unknown,
                    version=meta.version,
                    fingerprint=self.drive.fingerprint(meta),
                )
            )
        records.sort(key=lambda item: item.locator.record_key)
        return records

    def _scan_sheet(
        self,
        model: type[GDMModel],
        *,
        limit: int,
    ) -> list[StoredRecord]:
        spreadsheet_id, sheet_title = self._sheet_binding(model)
        pk_col = self._primary_key_column(model)
        records: list[StoredRecord] = []
        start_row = 2
        while len(records) <= limit:
            page = self.sheets.read_rows(
                spreadsheet_id,
                sheet_title,
                mapping_mode=self._mapping_mode(model),
                declared_fields=self._declared_fields(model),
                start_row=start_row,
                max_rows=min(500, limit + 1 - len(records)),
                primary_key_column=pk_col,
            )
            for row in page.rows:
                record_key = str(row.values.get(pk_col or COL_GDM_ID) or row.row_index)
                cache_key = (spreadsheet_id, record_key)
                self._row_index_cache[cache_key] = row.row_index
                values, unknown = self._split_sheet_values(model, row.values)
                locator = RecordLocator(
                    storage="sheet",
                    resource_id=spreadsheet_id,
                    record_key=record_key,
                )
                records.append(
                    StoredRecord(
                        locator=locator,
                        values=values,
                        unknown=unknown,
                        version=str(row.version) if row.version is not None else None,
                        fingerprint=row.fingerprint,
                    )
                )
                if len(records) > limit:
                    raise ScanLimitExceeded(
                        f"Sheet scan for {model.gdm_model_name()} exceeded "
                        f"limit {limit}.",
                        limit=limit,
                        observed=len(records),
                        model_name=model.gdm_model_name(),
                    )
            if not page.truncated or page.next_start_row is None:
                break
            start_row = page.next_start_row
        records.sort(key=lambda item: item.locator.record_key)
        return records

    def _read_file(
        self,
        model: type[GDMModel],
        locator: RecordLocator,
    ) -> StoredRecord:
        meta = self.drive.get_metadata(locator.resource_id)
        self.project.assert_resource_belongs_to_project(
            resource_id=meta.file_id,
            app_properties=meta.app_properties,
        )
        record_key = meta.app_properties.get("gdm_id") or meta.file_id
        values, unknown = self._read_file_values(
            model, locator.resource_id, record_key=str(record_key)
        )
        return StoredRecord(
            locator=locator,
            values=values,
            unknown=unknown,
            version=meta.version,
            fingerprint=self.drive.fingerprint(meta),
        )

    def _read_sheet(
        self,
        model: type[GDMModel],
        locator: RecordLocator,
    ) -> StoredRecord:
        spreadsheet_id, sheet_title = self._sheet_binding(model)
        pk_col = self._primary_key_column(model)
        row = self.sheets.find_row_by_primary_key(
            spreadsheet_id,
            sheet_title,
            primary_key=locator.record_key,
            primary_key_column=pk_col,
            scan_limit=self.scan_limit,
        )
        cache_key = (spreadsheet_id, locator.record_key)
        self._row_index_cache[cache_key] = row.row_index
        values, unknown = self._split_sheet_values(model, row.values)
        return StoredRecord(
            locator=locator,
            values=values,
            unknown=unknown,
            version=str(row.version) if row.version is not None else None,
            fingerprint=row.fingerprint,
        )

    def _read_file_values(
        self,
        model: type[GDMModel],
        file_id: str,
        *,
        record_key: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        codec = model.gdm_codec() or "json"
        if codec == "json":
            payload = self.drive.read_json(file_id)
            values = self._mapped_values(model, payload)
            unknown = self._unknown_from_payload(model, payload)
        else:
            raw = self.drive.read_file_body(file_id)
            values, unknown = decode_body(model, raw)
        pk_field = model.primary_key_field()
        if pk_field is not None and pk_field not in values and record_key is not None:
            values[pk_field] = record_key
        return values, unknown

    def _write_file(
        self,
        model: type[GDMModel],
        record: StoredRecord,
        *,
        expected_version: str | None,
        create: bool,
    ) -> WriteResult:
        parent_id = self._file_parent_id(model)
        codec = model.gdm_codec() or "json"
        preserve_unknown = model.gdm_mapping() != "strict"
        file_id = None if create else record.locator.resource_id

        if codec == "json":
            payload = dict(record.values)
            payload.update(record.unknown)
            meta = self.drive.write_json(
                parent_id=parent_id,
                model_name=model.gdm_model_name(),
                record_id=record.locator.record_key,
                payload=payload,
                file_id=file_id,
                schema_version=model.__gdm_schema_version__,
                expected_version=expected_version,
                expected_fingerprint=record.fingerprint,
                preserve_unknown=preserve_unknown,
            )
        else:
            existing_payload: dict[str, Any] | None = None
            if file_id is not None and codec == "yaml" and preserve_unknown:
                raw = self.drive.read_file_body(file_id)
                existing_payload = load_mapping_payload(model, raw)
            body = encode_body(
                model,
                record.values,
                record.unknown,
                preserve_unknown=preserve_unknown,
                existing_payload=existing_payload,
            )
            meta = self.drive.write_file_body(
                parent_id=parent_id,
                body=body,
                mime_type=codec_mime_type(model),
                model_name=model.gdm_model_name(),
                record_id=record.locator.record_key,
                file_id=file_id,
                schema_version=model.__gdm_schema_version__,
                expected_version=expected_version,
                expected_fingerprint=record.fingerprint,
            )
        locator = RecordLocator(
            storage="file",
            resource_id=meta.file_id,
            record_key=record.locator.record_key,
        )
        return WriteResult(
            locator=locator,
            version=str(meta.version or "1"),
            fingerprint=self.drive.fingerprint(meta),
            created=create,
        )

    def _write_sheet(
        self,
        model: type[GDMModel],
        record: StoredRecord,
        *,
        expected_version: str | None,
        create: bool,
    ) -> WriteResult:
        spreadsheet_id, sheet_title = self._sheet_binding(model)
        self.sheets.ensure_worksheet(spreadsheet_id, sheet_title)
        values = dict(record.values)
        values.update(record.unknown)
        pk_col = self._primary_key_column(model)
        if pk_col:
            values.setdefault(pk_col, record.locator.record_key)
        if create:
            headers = [
                source_name(name, info) for name, info in model.model_fields.items()
            ]
            headers.extend(column for column in SYSTEM_COLUMNS if column not in headers)
            schema = self.sheets.initialize_headers(
                spreadsheet_id,
                sheet_title,
                headers,
            )
            row = self.sheets.append_row(
                spreadsheet_id,
                sheet_title,
                values=values,
                schema=schema,
                record_id=record.locator.record_key,
            )
        else:
            row_index = self._resolve_sheet_row_index(
                spreadsheet_id, sheet_title, model, record.locator.record_key
            )
            expected = int(expected_version) if expected_version is not None else None
            row = self.sheets.update_row(
                spreadsheet_id,
                sheet_title,
                row_index=row_index,
                values=values,
                expected_version=expected,
                expected_fingerprint=record.fingerprint,
                preserve_unknown=model.gdm_mapping() != "strict",
            )
        cache_key = (spreadsheet_id, record.locator.record_key)
        self._row_index_cache[cache_key] = row.row_index
        locator = RecordLocator(
            storage="sheet",
            resource_id=spreadsheet_id,
            record_key=record.locator.record_key,
        )
        return WriteResult(
            locator=locator,
            version=str(row.version or "1"),
            fingerprint=row.fingerprint,
            created=create,
        )

    def _resolve_sheet_row_index(
        self,
        spreadsheet_id: str,
        sheet_title: str,
        model: type[GDMModel],
        record_key: str,
    ) -> int:
        cache_key = (spreadsheet_id, record_key)
        cached = self._row_index_cache.get(cache_key)
        if cached is not None:
            return cached
        pk_col = self._primary_key_column(model)
        row = self.sheets.find_row_by_primary_key(
            spreadsheet_id,
            sheet_title,
            primary_key=record_key,
            primary_key_column=pk_col,
            scan_limit=self.scan_limit,
        )
        self._row_index_cache[cache_key] = row.row_index
        return row.row_index

    @staticmethod
    def _mapped_values(
        model: type[GDMModel],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        source_to_field = model.source_field_map()
        mapped: dict[str, Any] = {}
        for key, raw in payload.items():
            if key in source_to_field:
                mapped[source_to_field[key]] = raw
            elif key in model.model_fields:
                mapped[key] = raw
        return mapped

    def _unknown_from_payload(
        self,
        model: type[GDMModel],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if model.gdm_mapping() == "strict":
            return {}
        declared = self._declared_fields(model)
        system = {SYSTEM_ID_FIELD, SYSTEM_VERSION_FIELD, COL_GDM_ID, COL_GDM_VERSION}
        return {
            key: value
            for key, value in payload.items()
            if key not in declared and key not in system
        }

    def _split_sheet_values(
        self,
        model: type[GDMModel],
        values: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        source_to_field = model.source_field_map()
        mapped: dict[str, Any] = {}
        unknown: dict[str, Any] = {}
        system = {COL_GDM_ID, COL_GDM_VERSION, "_gdm_updated_at"}
        for key, raw in values.items():
            if key in source_to_field:
                mapped[source_to_field[key]] = raw
            elif key in model.model_fields:
                mapped[key] = raw
            elif key in system or model.gdm_mapping() == "strict":
                continue
            else:
                unknown[key] = raw
        return mapped, unknown


def build_stored_record_for_registry(
    model: type[GDMModel],
    instance: GDMModel,
    *,
    project: Project,
) -> StoredRecord:
    """Build a stored record using registry resource identifiers."""
    del project
    stored = build_stored_record(model, instance)
    storage = model.gdm_storage()
    if storage == "file" and stored.locator.resource_id == (
        model.__gdm_source__ or model.gdm_model_name()
    ):
        parent_hint = model.__gdm_source__ or model.gdm_model_name()
        if stored.locator.resource_id == parent_hint:
            file_id = (
                stored.locator.resource_id
                if instance.gdm_locator is not None
                and instance.gdm_locator.resource_id != parent_hint
                else parent_hint
            )
            stored = StoredRecord(
                locator=RecordLocator(
                    storage="file",
                    resource_id=file_id,
                    record_key=stored.locator.record_key,
                ),
                values=stored.values,
                unknown=stored.unknown,
                version=stored.version,
                fingerprint=stored.fingerprint,
            )
    return stored
