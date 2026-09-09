"""Google-backed migration target for production apply."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gdm.backends.sheets import (
    COL_GDM_ID,
    COL_GDM_UPDATED_AT,
    COL_GDM_VERSION,
    SYSTEM_COLUMNS,
    SheetsBackend,
    SheetSchema,
    a1_range,
)
from gdm.client import GDMClient
from gdm.collection.constants import (
    FOLDER_MIME,
    METADATA_BACKEND_JSON,
    METADATA_ID_COLUMN,
)
from gdm.collection.inventory import collection_inventory_fingerprint
from gdm.collection.metadata import (
    DriveJsonCollectionMetadataStore,
    SheetsCollectionMetadataStore,
)
from gdm.errors import GDMError, NotFoundError
from gdm.indexes import (
    IndexFingerprints,
    IndexMaintainer,
    IndexStore,
    StaleIndexError,
    compute_collection_fingerprints,
    compute_source_fingerprint,
)
from gdm.migrations import MigrationCheckpoint, load_checkpoint, serialize_checkpoint
from gdm.project import REGISTRY_FILENAME, RegistryEntry, ResourceKind

logger = logging.getLogger(__name__)


def build_production_migration_target(
    client: GDMClient,
    *,
    bindings: Mapping[str, str] | None = None,
    indexes_dir: str,
    checkpoint_dir: Path | None = None,
    schema_field_lookup: Callable[[str, str], str | None] | None = None,
    schema_field_target_lookup: (
        Callable[[str, str], tuple[str, str] | None] | None
    ) = None,
) -> ProductionMigrationTarget:
    """Construct a migration target from an open :class:`GDMClient`."""
    return ProductionMigrationTarget(
        client=client,
        bindings=dict(bindings or {}),
        index_store=IndexStore(indexes_dir),
        checkpoint_dir=checkpoint_dir,
        schema_field_lookup=schema_field_lookup,
        schema_field_target_lookup=schema_field_target_lookup,
    )


@dataclass
class ProductionMigrationTarget:
    """Drive/Sheets-backed migration target using project registry bindings."""

    client: GDMClient
    bindings: dict[str, str] = field(default_factory=dict)
    index_store: IndexStore = field(default_factory=lambda: IndexStore(".gdm/indexes"))
    checkpoint_dir: Path | None = None
    schema_field_lookup: Callable[[str, str], str | None] | None = None
    schema_field_target_lookup: Callable[[str, str], tuple[str, str] | None] | None = (
        None
    )
    _metadata_stores: dict[str, Any] = field(default_factory=dict, repr=False)
    _write_log: list[str] = field(default_factory=list, repr=False)

    @property
    def sheets(self) -> SheetsBackend:
        return self.client.sheets

    @property
    def project(self) -> Any:
        return self.client.project

    def resource_id_for_model(self, model_name: str) -> str | None:
        if model_name in self.bindings:
            return self.bindings[model_name]
        entry = self.project.registry.get(model_name)
        return entry.resource_id if entry is not None else None

    def _registry_entry(self, model_name: str) -> RegistryEntry:
        entry = self.project.registry.get(model_name)
        if entry is None:
            msg = f"Model {model_name!r} is not registered in the project registry."
            raise NotFoundError(msg)
        return entry

    def _sheet_title_for(self, model_name: str, resource_id: str) -> str:
        entry = self.project.registry.get(model_name)
        if entry is not None:
            worksheet = entry.extra.get("worksheet")
            if worksheet:
                return str(worksheet)
        return "Sheet1"

    def _metadata_store(self, model_name: str) -> Any:
        if model_name in self._metadata_stores:
            return self._metadata_stores[model_name]
        default_backend = getattr(
            self.client.backend,
            "default_metadata_backend",
            "sheets",
        )
        backend = self.project.metadata_backend_for(
            model_name,
            default=default_backend,
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

    def sheet_headers(self, resource_id: str) -> list[str]:
        model_name = self._model_for_resource(resource_id)
        title = self._sheet_title_for(model_name, resource_id)
        return list(self.sheets.get_headers(resource_id, title).headers)

    def sheet_add_column(
        self, resource_id: str, header: str, default: Any = None
    ) -> int:
        model_name = self._model_for_resource(resource_id)
        title = self._sheet_title_for(model_name, resource_id)
        self.sheets.ensure_worksheet(resource_id, title)
        schema = self.sheets.get_headers(resource_id, title)
        if header in schema.headers:
            return 0
        table = self.sheets._read_sheet_table(resource_id, title)
        if not table:
            table = [[header]]
        else:
            table[0] = [*list(table[0]), header]
            for row_idx in range(1, len(table)):
                row = list(table[row_idx])
                while len(row) < len(table[0]):
                    row.append(default)
                table[row_idx] = row
        self._write_table(resource_id, title, table)
        self._write_log.append(f"sheet.add_column:{resource_id}.{header}")
        return max(len(table) - 1, 0)

    def sheet_rename_column(
        self, resource_id: str, old_name: str, new_name: str
    ) -> int:
        model_name = self._model_for_resource(resource_id)
        title = self._sheet_title_for(model_name, resource_id)
        schema = self.sheets.get_headers(resource_id, title)
        if old_name not in schema.headers:
            return 0
        if new_name in schema.headers and new_name != old_name:
            return 0
        idx = schema.headers.index(old_name)
        headers = list(schema.headers)
        headers[idx] = new_name
        self.sheets.values.update(
            spreadsheetId=resource_id,
            range=a1_range(title, "1:1"),
            body={"values": [headers]},
        )
        self._write_log.append(f"sheet.rename:{resource_id}.{old_name}->{new_name}")
        table = self.sheets._read_sheet_table(resource_id, title)
        return max(len(table) - 1, 0)

    def sheet_transform_column(
        self,
        resource_id: str,
        column: str,
        transformer: Callable[[Any], Any],
    ) -> tuple[int, tuple[str, ...]]:
        model_name = self._model_for_resource(resource_id)
        title = self._sheet_title_for(model_name, resource_id)
        schema = self.sheets.get_headers(resource_id, title)
        if column not in schema.header_index:
            return 0, (f"Column '{column}' not found.",)
        col_idx = schema.header_index[column]
        table = self.sheets._read_sheet_table(resource_id, title)
        if len(table) < 2:
            return 0, ()
        errors: list[str] = []
        changed = 0
        updates: list[tuple[int, dict[str, Any]]] = []
        for row_index, raw in enumerate(table[1:], start=2):
            value = raw[col_idx] if col_idx < len(raw) else None
            try:
                new_value = transformer(value)
            except Exception as exc:
                errors.append(f"row {row_index}: {exc}")
                continue
            if new_value != value:
                updates.append((row_index, {column: new_value}))
                changed += 1
        if updates:
            self.sheets.batch_update_rows(resource_id, title, updates, schema=schema)
            self._write_log.append(f"sheet.transform:{resource_id}.{column}")
        return changed, tuple(errors)

    def sheet_drop_column(self, resource_id: str, header: str) -> int:
        model_name = self._model_for_resource(resource_id)
        title = self._sheet_title_for(model_name, resource_id)
        schema = self.sheets.get_headers(resource_id, title)
        if header not in schema.headers:
            return 0
        col_idx = schema.header_index[header]
        table = self.sheets._read_sheet_table(resource_id, title)
        if not table:
            return 0
        new_table = []
        for row in table:
            row_list = list(row)
            if col_idx < len(row_list):
                del row_list[col_idx]
            new_table.append(row_list)
        self._write_table(resource_id, title, new_table)
        self._write_log.append(f"sheet.drop:{resource_id}.{header}")
        return max(len(new_table) - 1, 0)

    def sheet_backfill_system_columns(self, resource_id: str) -> int:
        model_name = self._model_for_resource(resource_id)
        title = self._sheet_title_for(model_name, resource_id)
        schema = self.sheets.get_headers(resource_id, title)
        headers = list(schema.headers)
        changed = 0
        for col in SYSTEM_COLUMNS:
            if col not in headers:
                headers.append(col)
                changed += 1
        if changed:
            self.sheets.values.update(
                spreadsheetId=resource_id,
                range=a1_range(title, "1:1"),
                body={"values": [headers]},
            )
        schema = SheetSchema.from_header_row(headers)
        table = self.sheets._read_sheet_table(resource_id, title)
        updates: list[tuple[int, dict[str, Any]]] = []
        for row_index, raw in enumerate(table[1:], start=2):
            values = self.sheets._row_to_dict(schema, raw)
            patch: dict[str, Any] = {}
            if COL_GDM_ID in schema.header_index:
                current_id = values.get(COL_GDM_ID)
                if current_id in (None, ""):
                    patch[COL_GDM_ID] = str(uuid.uuid4())
            if COL_GDM_VERSION in schema.header_index and not values.get(
                COL_GDM_VERSION
            ):
                patch[COL_GDM_VERSION] = 1
            if COL_GDM_UPDATED_AT in schema.header_index and not values.get(
                COL_GDM_UPDATED_AT
            ):
                patch[COL_GDM_UPDATED_AT] = datetime.now(UTC).isoformat()
            if patch:
                updates.append((row_index, patch))
        if updates:
            self.sheets.batch_update_rows(resource_id, title, updates, schema=schema)
            self._write_log.append(f"sheet.backfill_system:{resource_id}")
        return changed

    def file_move(self, file_id: str, new_parent_id: str) -> None:
        meta = self.client.drive.get_metadata(file_id)
        remove = [p for p in meta.parents if p != new_parent_id]
        if new_parent_id in meta.parents and not remove:
            return
        kwargs: dict[str, Any] = {
            "fileId": file_id,
            "supportsAllDrives": self.client.drive.supports_all_drives,
            "fields": "id,parents",
        }
        if new_parent_id not in meta.parents:
            kwargs["addParents"] = new_parent_id
        if remove:
            kwargs["removeParents"] = ",".join(remove)
        if "addParents" in kwargs or "removeParents" in kwargs:
            self.client.drive.service.files.update(**kwargs)
            self._write_log.append(f"file.move:{file_id}->{new_parent_id}")

    def file_retag(
        self,
        file_id: str,
        *,
        gdm_model: str | None = None,
        gdm_schema: str | None = None,
        gdm_project: str | None = None,
    ) -> None:
        meta = self.client.drive.get_metadata(file_id)
        props = dict(meta.app_properties)
        updated = False
        project_id = gdm_project or str(self.client.project.project_id)
        if props.get("gdm_project") != project_id:
            props["gdm_project"] = project_id
            updated = True
        if gdm_model is not None and props.get("gdm_model") != gdm_model:
            props["gdm_model"] = gdm_model
            updated = True
        if gdm_schema is not None and props.get("gdm_schema") != gdm_schema:
            props["gdm_schema"] = gdm_schema
            updated = True
        if not updated:
            return
        self.client.drive.service.files.update(
            fileId=file_id,
            body={"appProperties": props},
            supportsAllDrives=self.client.drive.supports_all_drives,
            fields="id,appProperties",
        )
        self._write_log.append(f"file.retag:{file_id}")

    def rebuild_index(
        self,
        model_name: str,
        field_name: str,
        *,
        unique: bool = False,
    ) -> int:
        entry = self._registry_entry(model_name)
        target_kind = "source"
        source_name = field_name
        if self.schema_field_target_lookup is not None:
            resolved_target = self.schema_field_target_lookup(model_name, field_name)
            if resolved_target is not None:
                target_kind, source_name = resolved_target
        if self.schema_field_lookup is not None:
            resolved = self.schema_field_lookup(model_name, field_name)
            if resolved:
                source_name = resolved
        before_records = self._collect_index_records(
            entry,
            source_name,
            target_kind=target_kind,
        )
        before_fingerprint, _ = self._index_fingerprint(
            entry,
            before_records,
        )
        records = self._collect_index_records(
            entry,
            source_name,
            target_kind=target_kind,
        )
        fingerprint, fingerprints = self._index_fingerprint(entry, records)
        if before_fingerprint != fingerprint:
            raise StaleIndexError(
                f"{model_name} changed while rebuilding index {field_name!r}; "
                "retry the rebuild."
            )
        maintainer = IndexMaintainer(self.index_store)
        index = maintainer.rebuild_from_records(
            model_name=model_name,
            field_name=field_name,
            unique=unique,
            records=[
                (record_id, {field_name: payload.get(source_name)})
                for record_id, payload in records
                if payload.get(source_name) is not None
            ],
            source_fingerprint=fingerprint,
            fingerprints=fingerprints,
            locator_kind=(
                "drive_file_id" if entry.kind is ResourceKind.FOLDER else "primary_key"
            ),
        )
        self._write_log.append(f"index.rebuild:{model_name}.{field_name}")
        return sum(len(ids) for ids in index.entries.values())

    def index_fingerprint(self, model_name: str) -> str:
        """Return the live fingerprint used to validate a sidecar index."""
        entry = self._registry_entry(model_name)
        records = self._collect_index_records(entry, "", target_kind="source")
        fingerprint, _ = self._index_fingerprint(entry, records)
        return fingerprint

    def metadata_headers(self, model_name: str) -> list[str]:
        store = self._metadata_store(model_name)
        if isinstance(store, DriveJsonCollectionMetadataStore):
            return store.migration_headers()
        if store._header is None:
            store._load()
        return list(store._header or [])

    def metadata_add_column(
        self, model_name: str, header: str, default: Any = None
    ) -> int:
        store = self._metadata_store(model_name)
        if isinstance(store, DriveJsonCollectionMetadataStore):
            changed = store.migration_add_column(header, default)
            self._write_log.append(f"metadata.add_column:{model_name}.{header}")
            return changed
        if store._header is None:
            store._load()
        assert store._header is not None
        if header in store._header:
            return 0
        columns = store._ensure_header(set(store._header) | {header})
        changed_rows = 0
        for file_id, (row_index, values, version) in list(store._rows.items()):
            if header in values:
                continue
            merged = dict(values)
            merged[header] = default
            row_values = _metadata_row_values(
                columns,
                merged,
                file_id,
                str(version or "1"),
            )
            store._sheets.values().update(
                spreadsheetId=store.spreadsheet_id,
                range=store._range(f"{row_index}:{row_index}"),
                body={"values": [row_values]},
            )
            store._rows[file_id] = (
                row_index,
                dict(zip(columns, row_values, strict=True)),
                version,
            )
            changed_rows += 1
        self._write_log.append(f"metadata.add_column:{model_name}.{header}")
        return changed_rows

    def metadata_rename_column(
        self, model_name: str, old_name: str, new_name: str
    ) -> int:
        store = self._metadata_store(model_name)
        if isinstance(store, DriveJsonCollectionMetadataStore):
            changed = store.migration_rename_column(old_name, new_name)
            if changed:
                self._write_log.append(
                    f"metadata.rename:{model_name}.{old_name}->{new_name}"
                )
            return changed
        if store._header is None:
            store._load()
        assert store._header is not None
        if old_name not in store._header:
            return 0
        if new_name in store._header:
            return 0
        idx = store._header.index(old_name)
        header = list(store._header)
        header[idx] = new_name
        store._sheets.values().update(
            spreadsheetId=store.spreadsheet_id,
            range=store._range("1:1"),
            body={"values": [header]},
        )
        store._header = header
        for file_id, (row_index, values, version) in store._rows.items():
            if old_name in values:
                values[new_name] = values.pop(old_name)
                store._rows[file_id] = (row_index, values, version)
        self._write_log.append(f"metadata.rename:{model_name}.{old_name}->{new_name}")
        return len(store._rows)

    def metadata_drop_column(self, model_name: str, header: str) -> int:
        from gdm.collection.constants import METADATA_ID_COLUMN, METADATA_VERSION_COLUMN

        if header in {METADATA_ID_COLUMN, METADATA_VERSION_COLUMN}:
            msg = f"Refusing to drop required metadata column {header!r}."
            raise GDMError(msg)
        store = self._metadata_store(model_name)
        if isinstance(store, DriveJsonCollectionMetadataStore):
            changed = store.migration_drop_column(header)
            if changed:
                self._write_log.append(f"metadata.drop:{model_name}.{header}")
            return changed
        if store._header is None:
            store._load()
        assert store._header is not None
        if header not in store._header:
            return 0
        header_row = [col for col in store._header if col != header]
        store._sheets.values().update(
            spreadsheetId=store.spreadsheet_id,
            range=store._range("1:1"),
            body={"values": [header_row]},
        )
        store._header = header_row
        for file_id, (row_index, values, version) in list(store._rows.items()):
            if header not in values:
                continue
            merged = dict(values)
            del merged[header]
            row_values = _metadata_row_values(
                header_row,
                merged,
                file_id,
                str(version or "1"),
            )
            store._sheets.values().update(
                spreadsheetId=store.spreadsheet_id,
                range=store._range(f"{row_index}:{row_index}"),
                body={"values": [row_values]},
            )
            store._rows[file_id] = (
                row_index,
                dict(zip(header_row, row_values, strict=True)),
                version,
            )
        self._write_log.append(f"metadata.drop:{model_name}.{header}")
        return len(store._rows)

    def metadata_transform_column(
        self,
        model_name: str,
        column: str,
        transformer: Callable[[Any], Any],
    ) -> tuple[int, tuple[str, ...]]:
        store = self._metadata_store(model_name)
        if isinstance(store, DriveJsonCollectionMetadataStore):
            changed, errors = store.migration_transform_column(column, transformer)
            if changed:
                self._write_log.append(f"metadata.transform:{model_name}.{column}")
            return changed, errors
        if store._header is None:
            store._load()
        assert store._header is not None
        if column not in store._header:
            return 0, (f"Column '{column}' not found.",)
        errors: list[str] = []
        changed = 0
        for file_id, (row_index, values, version) in list(store._rows.items()):
            if column not in values:
                continue
            current = values[column]
            try:
                new_value = transformer(current)
            except Exception as exc:
                errors.append(f"row {row_index}: {exc}")
                continue
            if new_value == current:
                continue
            merged = dict(values)
            merged[column] = new_value
            row_values = _metadata_row_values(
                store._header,
                merged,
                file_id,
                str(version or "1"),
            )
            store._sheets.values().update(
                spreadsheetId=store.spreadsheet_id,
                range=store._range(f"{row_index}:{row_index}"),
                body={"values": [row_values]},
            )
            store._rows[file_id] = (
                row_index,
                dict(zip(store._header, row_values, strict=True)),
                version,
            )
            changed += 1
        if changed:
            self._write_log.append(f"metadata.transform:{model_name}.{column}")
        return changed, tuple(errors)

    def metadata_reconcile_rows(
        self, model_name: str, *, folder_file_ids: set[str]
    ) -> tuple[int, int]:
        store = self._metadata_store(model_name)
        orphans, missing = store.reconcile(folder_file_ids=folder_file_ids)
        self._write_log.append(f"metadata.reconcile:{model_name}")
        return len(orphans), len(missing)

    def bind_collection(
        self,
        model_name: str,
        *,
        folder_id: str,
        collection_path: str | None,
    ) -> None:
        metadata = self.project.assert_resource_in_project_scope(folder_id)
        if str(metadata.get("mimeType", "")) != FOLDER_MIME:
            raise GDMError(f"Resource {folder_id!r} is not a Drive folder.")
        existing = self.project.registry.get(model_name)
        if (
            existing is not None
            and existing.resource_id == folder_id
            and existing.kind is ResourceKind.FOLDER
        ):
            extra = dict(existing.extra)
            if collection_path and extra.get("collection_path") != collection_path:
                extra["collection_path"] = collection_path
                self.project.register_resource(
                    RegistryEntry(
                        model_name=model_name,
                        resource_id=folder_id,
                        kind=ResourceKind.FOLDER,
                        extra=extra,
                    )
                )
                self._write_log.append(f"bind_collection:{model_name}->{folder_id}")
            return
        extra: dict[str, Any] = {}
        if collection_path:
            extra["collection_path"] = collection_path
        self.project.register_resource(
            RegistryEntry(
                model_name=model_name,
                resource_id=folder_id,
                kind=ResourceKind.FOLDER,
                extra=extra,
            )
        )
        self.bindings[model_name] = folder_id
        self._write_log.append(f"bind_collection:{model_name}->{folder_id}")

    def unbind_collection(self, model_name: str, *, folder_id: str) -> None:
        entry = self.project.registry.get(model_name)
        if entry is None or entry.resource_id != folder_id:
            return
        del self.project.registry.entries[model_name]
        self.bindings.pop(model_name, None)
        if self.project.gdm_folder_id is not None:
            self.project._write_json_file(
                REGISTRY_FILENAME,
                self.project.registry.to_dict(),
                parent_id=self.project.gdm_folder_id,
            )
        self._write_log.append(f"unbind_collection:{model_name}")

    def save_checkpoint(self, checkpoint: MigrationCheckpoint) -> None:
        if self.checkpoint_dir is None:
            return
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        path = self.checkpoint_dir / f"{checkpoint.model_name}.checkpoint.json"
        path.write_text(serialize_checkpoint(checkpoint) + "\n", encoding="utf-8")

    def load_checkpoint(self, model_name: str) -> MigrationCheckpoint | None:
        if self.checkpoint_dir is None:
            return None
        path = self.checkpoint_dir / f"{model_name}.checkpoint.json"
        if not path.is_file():
            return None
        return load_checkpoint(path.read_text(encoding="utf-8"))

    def _model_for_resource(self, resource_id: str) -> str:
        for model_name, bound_id in self.bindings.items():
            if bound_id == resource_id:
                return model_name
        for model_name, entry in self.project.registry.entries.items():
            if entry.resource_id == resource_id:
                return model_name
        msg = f"No model binding found for resource {resource_id!r}."
        raise GDMError(msg)

    def _write_table(
        self, spreadsheet_id: str, sheet_title: str, table: list[list[Any]]
    ) -> None:
        if not table:
            return
        self.sheets.values.update(
            spreadsheetId=spreadsheet_id,
            range=a1_range(sheet_title, "1:1"),
            body={"values": [table[0]]},
        )
        if len(table) > 1:
            end_row = len(table)
            self.sheets.values.update(
                spreadsheetId=spreadsheet_id,
                range=a1_range(sheet_title, f"2:{end_row}"),
                body={"values": table[1:]},
            )

    def _collect_index_records(
        self,
        entry: RegistryEntry,
        source_name: str,
        *,
        target_kind: str,
    ) -> list[tuple[str, dict[str, Any]]]:
        if entry.kind in {ResourceKind.SPREADSHEET, ResourceKind.WORKSHEET}:
            sheet_title = str(entry.extra.get("worksheet") or "Sheet1")
            schema = self.sheets.get_headers(entry.resource_id, sheet_title)
            records: list[tuple[str, dict[str, Any]]] = []
            start = 2
            while True:
                page = self.sheets.read_rows(
                    entry.resource_id,
                    sheet_title,
                    start_row=start,
                    max_rows=500,
                    primary_key_column=COL_GDM_ID
                    if COL_GDM_ID in schema.header_index
                    else None,
                )
                for row in page.rows:
                    record_id = str(
                        row.values.get(COL_GDM_ID) or f"row:{row.row_index}"
                    )
                    records.append((record_id, dict(row.values)))
                if not page.truncated or page.next_start_row is None:
                    break
                start = page.next_start_row
            return records
        if entry.kind is ResourceKind.FOLDER:
            store = self._metadata_store(entry.model_name)
            rows = store.list_rows()
            metadata_by_id = {row.file_id: dict(row.values) for row in rows}
            if target_kind in {"metadata", "source"}:
                return [
                    (
                        row.file_id,
                        {source_name: row.values.get(source_name), **row.values},
                    )
                    for row in rows
                ]

            inventory = self.client.backend._collection.inventory(
                entry.resource_id
            ).scan(scan_limit=self.client.backend.scan_limit)
            records: list[tuple[str, dict[str, Any]]] = []
            for item in inventory:
                values = metadata_by_id.get(item.file_id, {})
                if target_kind == "drive":
                    value = self.client.backend._collection._drive_field_value(
                        item,
                        source_name,
                    )
                elif target_kind == "property":
                    value = item.properties.get(source_name)
                elif target_kind == "app_property":
                    value = item.app_properties.get(source_name)
                else:
                    value = values.get(source_name)
                records.append(
                    (
                        item.file_id,
                        {**values, source_name: value},
                    )
                )
            return records
        msg = f"Unsupported registry kind for index rebuild: {entry.kind.value}."
        raise GDMError(msg)

    def _index_fingerprint(
        self,
        entry: RegistryEntry,
        records: list[tuple[str, dict[str, Any]]],
    ) -> tuple[str, IndexFingerprints | None]:
        if entry.kind is ResourceKind.FOLDER:
            store = self._metadata_store(entry.model_name)
            metadata_payload = {
                row.file_id: row.fingerprint for row in store.list_rows()
            }
            metadata_fingerprint = hashlib.sha256(
                json.dumps(
                    metadata_payload,
                    sort_keys=True,
                    default=str,
                ).encode()
            ).hexdigest()
            inventory = self.client.backend._collection.inventory(
                entry.resource_id
            ).scan(scan_limit=self.client.backend.scan_limit)
            drive_version = collection_inventory_fingerprint(
                entry.resource_id,
                inventory,
            )
            fingerprints = compute_collection_fingerprints(
                folder_id=entry.resource_id,
                drive_version=drive_version,
                metadata_table_fingerprint=metadata_fingerprint,
            )
            return fingerprints.combined(), fingerprints

        try:
            drive_metadata = self.client.drive.get_metadata(entry.resource_id)
        except NotFoundError:
            drive_metadata = None
        if drive_metadata is not None and drive_metadata.version is not None:
            return (
                compute_source_fingerprint(
                    resource_id=entry.resource_id,
                    version=drive_metadata.version,
                ),
                None,
            )

        versions: list[int] = []
        for _record_id, values in records:
            raw_version = values.get(COL_GDM_VERSION)
            try:
                versions.append(int(raw_version or 0))
            except (TypeError, ValueError):
                versions.append(0)
        return (
            compute_source_fingerprint(
                resource_id=entry.resource_id,
                row_count=len(records),
                version=max(versions) if versions else 0,
            ),
            None,
        )


def _metadata_row_values(
    header: list[str],
    merged: dict[str, Any],
    file_id: str,
    next_version: str,
) -> list[Any]:
    from gdm.collection.constants import METADATA_VERSION_COLUMN

    row_values: list[Any] = [""] * len(header)
    for col_index, col_name in enumerate(header):
        if col_name == METADATA_ID_COLUMN:
            row_values[col_index] = file_id
        elif col_name == METADATA_VERSION_COLUMN:
            row_values[col_index] = next_version
        elif col_name in merged:
            row_values[col_index] = merged[col_name]
    return row_values
