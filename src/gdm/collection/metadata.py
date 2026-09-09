"""Collection metadata table protocol and implementations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from gdm.collection.constants import (
    METADATA_ID_COLUMN,
    METADATA_SIDECAR_FORMAT_VERSION,
    METADATA_VERSION_COLUMN,
)
from gdm.errors import ConflictError, GDMError, NotFoundError


@dataclass(frozen=True, slots=True)
class CollectionMetadataRow:
    """One metadata-table row keyed by Drive file ID."""

    file_id: str
    values: dict[str, Any]
    version: str | None = None
    fingerprint: str | None = None
    row_index: int | None = None


@runtime_checkable
class CollectionMetadataStore(Protocol):
    """Protocol for collection metadata keyed by Drive file ID."""

    def list_rows(
        self,
        *,
        file_ids: set[str] | None = None,
        limit: int | None = None,
    ) -> list[CollectionMetadataRow]: ...

    def get_row(self, file_id: str) -> CollectionMetadataRow | None: ...

    def upsert_row(
        self,
        file_id: str,
        values: dict[str, Any],
        *,
        expected_version: str | None = None,
        expected_fingerprint: str | None = None,
    ) -> CollectionMetadataRow: ...

    def delete_row(self, file_id: str) -> None: ...

    def reconcile(
        self,
        *,
        folder_file_ids: set[str],
    ) -> tuple[list[str], list[str]]:
        """Return (orphan_row_ids, missing_row_ids)."""
        ...


def metadata_fingerprint(values: dict[str, Any]) -> str:
    """Stable fingerprint for metadata row content."""
    encoded = json.dumps(values, sort_keys=True, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def sidecar_fingerprint(rows: dict[str, dict[str, Any]]) -> str:
    """Stable fingerprint for an entire metadata sidecar body."""
    return metadata_fingerprint(rows)


def _row_payload(values: dict[str, Any]) -> dict[str, Any]:
    """Return user-facing metadata values without system columns."""
    return {
        key: value
        for key, value in values.items()
        if key not in {METADATA_ID_COLUMN, METADATA_VERSION_COLUMN}
    }


def _row_version(values: dict[str, Any], *, fallback: str = "1") -> str:
    raw = values.get(METADATA_VERSION_COLUMN)
    return str(raw) if raw is not None else fallback


@dataclass
class InMemoryCollectionMetadataStore:
    """In-memory metadata store for tests."""

    rows: dict[str, CollectionMetadataRow] = field(default_factory=dict)
    unknown_columns: set[str] = field(default_factory=set)

    def list_rows(
        self,
        *,
        file_ids: set[str] | None = None,
        limit: int | None = None,
    ) -> list[CollectionMetadataRow]:
        items = list(self.rows.values())
        if file_ids is not None:
            items = [row for row in items if row.file_id in file_ids]
        items.sort(key=lambda row: row.file_id)
        if limit is not None:
            return items[:limit]
        return items

    def get_row(self, file_id: str) -> CollectionMetadataRow | None:
        return self.rows.get(file_id)

    def upsert_row(
        self,
        file_id: str,
        values: dict[str, Any],
        *,
        expected_version: str | None = None,
        expected_fingerprint: str | None = None,
    ) -> CollectionMetadataRow:
        existing = self.rows.get(file_id)
        if existing is not None:
            if expected_version is not None and existing.version != expected_version:
                msg = (
                    f"Metadata conflict for {file_id}: expected version "
                    f"{expected_version}, found {existing.version}."
                )
                raise ConflictError(msg)
            if (
                expected_fingerprint is not None
                and existing.fingerprint != expected_fingerprint
            ):
                msg = (
                    f"Metadata conflict for {file_id}: expected fingerprint "
                    f"{expected_fingerprint}, found {existing.fingerprint}."
                )
                raise ConflictError(msg)
            merged = dict(existing.values)
            merged.update(values)
            next_version = str(int(existing.version or "0") + 1)
        else:
            merged = dict(values)
            next_version = "1"
        self.unknown_columns.update(
            key for key in merged if key not in values and key != METADATA_ID_COLUMN
        )
        fingerprint = metadata_fingerprint(merged)
        row = CollectionMetadataRow(
            file_id=file_id,
            values=merged,
            version=next_version,
            fingerprint=fingerprint,
        )
        self.rows[file_id] = row
        return row

    def delete_row(self, file_id: str) -> None:
        self.rows.pop(file_id, None)

    def reconcile(
        self,
        *,
        folder_file_ids: set[str],
    ) -> tuple[list[str], list[str]]:
        row_ids = set(self.rows)
        orphans = sorted(row_ids - folder_file_ids)
        missing = sorted(folder_file_ids - row_ids)
        return orphans, missing


@dataclass
class JsonCollectionMetadataStore:
    """JSON sidecar metadata store."""

    path_data: dict[str, dict[str, Any]] = field(default_factory=dict)
    versions: dict[str, str] = field(default_factory=dict)

    def list_rows(
        self,
        *,
        file_ids: set[str] | None = None,
        limit: int | None = None,
    ) -> list[CollectionMetadataRow]:
        items = [
            CollectionMetadataRow(
                file_id=fid,
                values=dict(values),
                version=self.versions.get(fid, "1"),
                fingerprint=metadata_fingerprint(values),
            )
            for fid, values in self.path_data.items()
        ]
        if file_ids is not None:
            items = [row for row in items if row.file_id in file_ids]
        items.sort(key=lambda row: row.file_id)
        if limit is not None:
            return items[:limit]
        return items

    def get_row(self, file_id: str) -> CollectionMetadataRow | None:
        values = self.path_data.get(file_id)
        if values is None:
            return None
        return CollectionMetadataRow(
            file_id=file_id,
            values=dict(values),
            version=self.versions.get(file_id, "1"),
            fingerprint=metadata_fingerprint(values),
        )

    def upsert_row(
        self,
        file_id: str,
        values: dict[str, Any],
        *,
        expected_version: str | None = None,
        expected_fingerprint: str | None = None,
    ) -> CollectionMetadataRow:
        existing = self.get_row(file_id)
        if existing is not None:
            if expected_version is not None and existing.version != expected_version:
                raise ConflictError(
                    f"Metadata conflict for {file_id}: expected version "
                    f"{expected_version}, found {existing.version}."
                )
            if (
                expected_fingerprint is not None
                and existing.fingerprint != expected_fingerprint
            ):
                raise ConflictError(
                    f"Metadata conflict for {file_id}: expected fingerprint "
                    f"{expected_fingerprint}, found {existing.fingerprint}."
                )
            merged = dict(existing.values)
            merged.update(values)
            next_version = str(int(existing.version or "0") + 1)
        else:
            merged = dict(values)
            next_version = "1"
        self.path_data[file_id] = merged
        self.versions[file_id] = next_version
        fingerprint = metadata_fingerprint(merged)
        return CollectionMetadataRow(
            file_id=file_id,
            values=merged,
            version=next_version,
            fingerprint=fingerprint,
        )

    def delete_row(self, file_id: str) -> None:
        self.path_data.pop(file_id, None)
        self.versions.pop(file_id, None)

    def reconcile(
        self,
        *,
        folder_file_ids: set[str],
    ) -> tuple[list[str], list[str]]:
        row_ids = set(self.path_data)
        return sorted(row_ids - folder_file_ids), sorted(folder_file_ids - row_ids)


class DriveJsonCollectionMetadataStore:
    """Drive-backed JSON sidecar metadata store."""

    def __init__(
        self,
        drive: Any,
        *,
        file_id: str,
    ) -> None:
        self._drive = drive
        self._file_id = file_id
        self._rows: dict[str, dict[str, Any]] = {}
        self._loaded = False
        self._loaded_sidecar_version: str | None = None

    def _reader(self) -> Any:
        reader = getattr(self._drive, "read_file_bytes", None)
        if reader is None:
            msg = "Drive service wrapper must provide read_file_bytes for metadata IO."
            raise GDMError(msg)
        return reader

    def _writer(self) -> Any:
        writer = getattr(self._drive, "write_file_bytes", None)
        if writer is None:
            msg = "Drive service wrapper must provide write_file_bytes for metadata IO."
            raise GDMError(msg)
        return writer

    def _load(self) -> None:
        if self._loaded:
            return
        raw = self._reader()(self._file_id)
        if not raw or raw.strip() in {b"", b"{}"}:
            self._rows = {}
            self._loaded_sidecar_version = "0"
        else:
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                msg = f"Expected JSON object in metadata sidecar {self._file_id}."
                raise GDMError(msg)
            rows = data.get("rows", {})
            if not isinstance(rows, dict):
                msg = f"Expected rows object in metadata sidecar {self._file_id}."
                raise GDMError(msg)
            self._rows = {
                str(file_id): dict(values)
                for file_id, values in rows.items()
                if isinstance(values, dict)
            }
            self._loaded_sidecar_version = str(data.get("sidecar_version", "0"))
        self._loaded = True

    def _persist(self) -> None:
        self._load()
        raw = self._reader()(self._file_id)
        if raw and raw.strip() not in {b"", b"{}"}:
            current = json.loads(raw.decode("utf-8"))
            if not isinstance(current, dict):
                msg = f"Expected JSON object in metadata sidecar {self._file_id}."
                raise GDMError(msg)
            current_version = str(current.get("sidecar_version", "0"))
            if (
                self._loaded_sidecar_version is not None
                and current_version != self._loaded_sidecar_version
            ):
                msg = (
                    f"Metadata sidecar conflict for {self._file_id}: expected "
                    f"sidecar version {self._loaded_sidecar_version}, "
                    f"found {current_version}."
                )
                raise ConflictError(msg)
        next_version = str(int(self._loaded_sidecar_version or "0") + 1)
        payload = {
            "format_version": METADATA_SIDECAR_FORMAT_VERSION,
            "sidecar_version": next_version,
            "rows": self._rows,
        }
        encoded = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        self._writer()(self._file_id, encoded)
        self._loaded_sidecar_version = next_version

    def _materialize_row(
        self, file_id: str, values: dict[str, Any]
    ) -> CollectionMetadataRow:
        payload = _row_payload(values)
        return CollectionMetadataRow(
            file_id=file_id,
            values=payload,
            version=_row_version(values),
            fingerprint=metadata_fingerprint(payload),
        )

    def list_rows(
        self,
        *,
        file_ids: set[str] | None = None,
        limit: int | None = None,
    ) -> list[CollectionMetadataRow]:
        self._load()
        items = [
            self._materialize_row(file_id, dict(values))
            for file_id, values in self._rows.items()
        ]
        if file_ids is not None:
            items = [row for row in items if row.file_id in file_ids]
        items.sort(key=lambda row: row.file_id)
        if limit is not None:
            return items[:limit]
        return items

    def get_row(self, file_id: str) -> CollectionMetadataRow | None:
        self._load()
        values = self._rows.get(file_id)
        if values is None:
            return None
        return self._materialize_row(file_id, dict(values))

    def upsert_row(
        self,
        file_id: str,
        values: dict[str, Any],
        *,
        expected_version: str | None = None,
        expected_fingerprint: str | None = None,
    ) -> CollectionMetadataRow:
        self._load()
        existing = self.get_row(file_id)
        if existing is not None:
            if expected_version is not None and existing.version != expected_version:
                msg = (
                    f"Metadata conflict for {file_id}: expected version "
                    f"{expected_version}, found {existing.version}."
                )
                raise ConflictError(msg)
            if (
                expected_fingerprint is not None
                and existing.fingerprint != expected_fingerprint
            ):
                msg = (
                    f"Metadata conflict for {file_id}: expected fingerprint "
                    f"{expected_fingerprint}, found {existing.fingerprint}."
                )
                raise ConflictError(msg)
            merged = dict(self._rows[file_id])
            merged.update(values)
            next_version = str(int(existing.version or "0") + 1)
        else:
            merged = dict(values)
            next_version = "1"
        merged[METADATA_VERSION_COLUMN] = next_version
        self._rows[file_id] = merged
        self._persist()
        return self._materialize_row(file_id, merged)

    def delete_row(self, file_id: str) -> None:
        self._load()
        if file_id not in self._rows:
            raise NotFoundError(f"Metadata row for {file_id} not found.")
        del self._rows[file_id]
        self._persist()

    def reconcile(
        self,
        *,
        folder_file_ids: set[str],
    ) -> tuple[list[str], list[str]]:
        self._load()
        row_ids = set(self._rows)
        return sorted(row_ids - folder_file_ids), sorted(folder_file_ids - row_ids)

    def migration_headers(self) -> list[str]:
        """Return the logical column set used by JSON metadata migrations."""
        self._load()
        columns = {METADATA_ID_COLUMN, METADATA_VERSION_COLUMN}
        for values in self._rows.values():
            columns.update(values)
        return sorted(columns)

    def migration_add_column(self, header: str, default: Any = None) -> int:
        """Add a key to existing JSON metadata rows."""
        self._load()
        changed = 0
        for values in self._rows.values():
            if header in values:
                continue
            values[header] = default
            self._bump_row_version(values)
            changed += 1
        if changed:
            self._persist()
        return changed

    def migration_rename_column(self, old_name: str, new_name: str) -> int:
        """Rename one key across JSON metadata rows."""
        self._load()
        if old_name == new_name:
            return 0
        if new_name != old_name and new_name in self.migration_headers():
            return 0
        changed = 0
        for values in self._rows.values():
            if old_name not in values:
                continue
            values[new_name] = values.pop(old_name)
            self._bump_row_version(values)
            changed += 1
        if changed:
            self._persist()
        return changed

    def migration_drop_column(self, header: str) -> int:
        """Drop one key from JSON metadata rows."""
        self._load()
        changed = 0
        for values in self._rows.values():
            if header not in values:
                continue
            del values[header]
            self._bump_row_version(values)
            changed += 1
        if changed:
            self._persist()
        return changed

    def migration_transform_column(
        self,
        column: str,
        transformer: Callable[[Any], Any],
    ) -> tuple[int, tuple[str, ...]]:
        """Transform a JSON metadata key and report row-specific failures."""
        self._load()
        errors: list[str] = []
        changed = 0
        for file_id, values in self._rows.items():
            if column not in values:
                continue
            current = values[column]
            try:
                transformed = transformer(current)
            except Exception as exc:
                errors.append(f"row {file_id}: {exc}")
                continue
            if transformed == current:
                continue
            values[column] = transformed
            self._bump_row_version(values)
            changed += 1
        if changed:
            self._persist()
        return changed, tuple(errors)

    @staticmethod
    def _bump_row_version(values: dict[str, Any]) -> None:
        current = _row_version(values)
        values[METADATA_VERSION_COLUMN] = str(int(current or "0") + 1)


class SheetsCollectionMetadataStore:
    """Google Sheets metadata table keyed by Drive file ID."""

    def __init__(
        self,
        sheets: Any,
        *,
        spreadsheet_id: str,
        worksheet: str = "metadata",
    ) -> None:
        self._sheets = sheets
        self.spreadsheet_id = spreadsheet_id
        self.worksheet = worksheet
        self._header: list[str] | None = None
        self._rows: dict[str, tuple[int, dict[str, Any], str | None]] = {}
        self._worksheet_ready = False

    def _range(self, cell_range: str) -> str:
        return f"{self.worksheet}!{cell_range}"

    def _ensure_worksheet(self) -> None:
        if self._worksheet_ready:
            return
        get_spreadsheet = getattr(self._sheets, "get", None)
        batch_update = getattr(self._sheets, "batchUpdate", None)
        if not callable(get_spreadsheet) or not callable(batch_update):
            # Narrow values-only fakes and custom adapters may provision the
            # worksheet themselves.
            self._worksheet_ready = True
            return
        response = get_spreadsheet(
            spreadsheetId=self.spreadsheet_id,
            fields="sheets.properties",
        )
        sheets = response.get("sheets", [])
        titles = {
            str(sheet.get("properties", {}).get("title", ""))
            for sheet in sheets
            if isinstance(sheet, dict)
        }
        if self.worksheet not in titles:
            batch_update(
                spreadsheetId=self.spreadsheet_id,
                body={
                    "requests": [
                        {
                            "addSheet": {
                                "properties": {
                                    "title": self.worksheet,
                                }
                            }
                        }
                    ]
                },
            )
        self._worksheet_ready = True

    def _load(self) -> None:
        self._ensure_worksheet()
        response = self._sheets.values().get(
            spreadsheetId=self.spreadsheet_id,
            range=self._range("1:10000"),
        )
        values = response.get("values", [])
        if not values:
            self._header = [METADATA_ID_COLUMN, METADATA_VERSION_COLUMN]
            self._rows = {}
            return
        header = [str(col) for col in values[0]]
        self._header = header
        id_index = (
            header.index(METADATA_ID_COLUMN) if METADATA_ID_COLUMN in header else 0
        )
        version_index = (
            header.index(METADATA_VERSION_COLUMN)
            if METADATA_VERSION_COLUMN in header
            else None
        )
        rows: dict[str, tuple[int, dict[str, Any], str | None]] = {}
        for row_index, raw in enumerate(values[1:], start=2):
            if not raw:
                continue
            file_id = str(raw[id_index]) if id_index < len(raw) else ""
            if not file_id:
                continue
            row_values: dict[str, Any] = {}
            for col_index, col_name in enumerate(header):
                if col_index < len(raw):
                    row_values[col_name] = raw[col_index]
            version = (
                str(raw[version_index])
                if version_index is not None and version_index < len(raw)
                else None
            )
            rows[file_id] = (row_index, row_values, version)
        self._rows = rows

    def _ensure_header(self, columns: set[str]) -> list[str]:
        if self._header is None:
            self._load()
        assert self._header is not None
        header = list(self._header)
        for col in sorted(columns):
            if col not in header:
                header.append(col)
        if header != self._header:
            self._sheets.values().update(
                spreadsheetId=self.spreadsheet_id,
                range=self._range("1:1"),
                body={"values": [header]},
            )
            self._header = header
        return header

    def list_rows(
        self,
        *,
        file_ids: set[str] | None = None,
        limit: int | None = None,
    ) -> list[CollectionMetadataRow]:
        if self._header is None:
            self._load()
        items: list[CollectionMetadataRow] = []
        for file_id, (row_index, values, version) in self._rows.items():
            if file_ids is not None and file_id not in file_ids:
                continue
            payload = {
                key: value
                for key, value in values.items()
                if key not in {METADATA_ID_COLUMN, METADATA_VERSION_COLUMN}
            }
            items.append(
                CollectionMetadataRow(
                    file_id=file_id,
                    values=payload,
                    version=version,
                    fingerprint=metadata_fingerprint(payload),
                    row_index=row_index,
                )
            )
        items.sort(key=lambda row: row.file_id)
        if limit is not None:
            return items[:limit]
        return items

    def get_row(self, file_id: str) -> CollectionMetadataRow | None:
        if self._header is None:
            self._load()
        entry = self._rows.get(file_id)
        if entry is None:
            return None
        row_index, values, version = entry
        payload = {
            key: value
            for key, value in values.items()
            if key not in {METADATA_ID_COLUMN, METADATA_VERSION_COLUMN}
        }
        return CollectionMetadataRow(
            file_id=file_id,
            values=payload,
            version=version,
            fingerprint=metadata_fingerprint(payload),
            row_index=row_index,
        )

    def upsert_row(
        self,
        file_id: str,
        values: dict[str, Any],
        *,
        expected_version: str | None = None,
        expected_fingerprint: str | None = None,
    ) -> CollectionMetadataRow:
        if self._header is None:
            self._load()
        existing = self.get_row(file_id)
        if existing is not None:
            if expected_version is not None and existing.version != expected_version:
                raise ConflictError(
                    f"Metadata conflict for {file_id}: expected version "
                    f"{expected_version}, found {existing.version}."
                )
            if (
                expected_fingerprint is not None
                and existing.fingerprint != expected_fingerprint
            ):
                raise ConflictError(
                    f"Metadata conflict for {file_id}: expected fingerprint "
                    f"{expected_fingerprint}, found {existing.fingerprint}."
                )
            merged = dict(existing.values)
            merged.update(values)
            next_version = str(int(existing.version or "0") + 1)
            row_index = existing.row_index
        else:
            merged = dict(values)
            next_version = "1"
            row_index = None
        header = self._ensure_header(
            set(merged) | {METADATA_ID_COLUMN, METADATA_VERSION_COLUMN}
        )
        row_values = [""] * len(header)
        for col_index, col_name in enumerate(header):
            if col_name == METADATA_ID_COLUMN:
                row_values[col_index] = file_id
            elif col_name == METADATA_VERSION_COLUMN:
                row_values[col_index] = next_version
            elif col_name in merged:
                row_values[col_index] = merged[col_name]
        if row_index is None:
            self._sheets.values().append(
                spreadsheetId=self.spreadsheet_id,
                range=self._range("A:A"),
                body={"values": [row_values]},
            )
            self._load()
            refreshed = self.get_row(file_id)
            if refreshed is None:
                msg = f"Failed to append metadata row for {file_id}."
                raise GDMError(msg)
            return refreshed
        self._sheets.values().update(
            spreadsheetId=self.spreadsheet_id,
            range=self._range(f"{row_index}:{row_index}"),
            body={"values": [row_values]},
        )
        self._rows[file_id] = (
            row_index,
            dict(zip(header, row_values, strict=True)),
            next_version,
        )
        fingerprint = metadata_fingerprint(merged)
        return CollectionMetadataRow(
            file_id=file_id,
            values=merged,
            version=next_version,
            fingerprint=fingerprint,
            row_index=row_index,
        )

    def delete_row(self, file_id: str) -> None:
        if self._header is None:
            self._load()
        entry = self._rows.pop(file_id, None)
        if entry is None:
            raise NotFoundError(f"Metadata row for {file_id} not found.")
        row_index = entry[0]
        self._sheets.values().clear(
            spreadsheetId=self.spreadsheet_id,
            range=self._range(f"{row_index}:{row_index}"),
        )

    def reconcile(
        self,
        *,
        folder_file_ids: set[str],
    ) -> tuple[list[str], list[str]]:
        if self._header is None:
            self._load()
        row_ids = set(self._rows)
        return sorted(row_ids - folder_file_ids), sorted(folder_file_ids - row_ids)
