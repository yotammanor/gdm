"""Google Sheets v4 backend for tabular model storage."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from googleapiclient.errors import HttpError

from gdm.errors import (
    AmbiguousResourceError,
    ConflictError,
    MappingMode,
    NotFoundError,
    UnsupportedOperationError,
)
from gdm.retry import RetryPolicy, execute_with_retry, normalize_http_error

logger = logging.getLogger(__name__)

COL_GDM_ID = "_gdm_id"
COL_GDM_VERSION = "_gdm_version"
COL_GDM_UPDATED_AT = "_gdm_updated_at"
SYSTEM_COLUMNS = (COL_GDM_ID, COL_GDM_VERSION, COL_GDM_UPDATED_AT)

_A1_ESCAPE_RE = re.compile(r"([\\'])")

SHEET_ID_PATTERN = re.compile(r"^\d+$")


def quote_sheet_title(title: str) -> str:
    """Quote a worksheet title for A1 notation."""
    escaped = _A1_ESCAPE_RE.sub(r"\\\1", title)
    return f"'{escaped}'"


def a1_range(sheet_title: str, cell_range: str) -> str:
    return f"{quote_sheet_title(sheet_title)}!{cell_range}"


@runtime_checkable
class SheetsValuesClient(Protocol):
    def get(
        self,
        *,
        spreadsheetId: str,
        range: str,
        majorDimension: str = "ROWS",
        valueRenderOption: str | None = None,
    ) -> dict[str, Any]: ...

    def update(
        self,
        *,
        spreadsheetId: str,
        range: str,
        body: dict[str, Any],
        valueInputOption: str = "USER_ENTERED",
    ) -> dict[str, Any]: ...

    def append(
        self,
        *,
        spreadsheetId: str,
        range: str,
        body: dict[str, Any],
        valueInputOption: str = "USER_ENTERED",
        insertDataOption: str = "INSERT_ROWS",
    ) -> dict[str, Any]: ...

    def batchUpdate(
        self,
        *,
        spreadsheetId: str,
        body: dict[str, Any],
    ) -> dict[str, Any]: ...

    def batchGet(
        self,
        *,
        spreadsheetId: str,
        ranges: list[str],
        majorDimension: str = "ROWS",
    ) -> dict[str, Any]: ...

    def clear(
        self,
        *,
        spreadsheetId: str,
        range: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...


@runtime_checkable
class SheetsSpreadsheetsClient(Protocol):
    def get(
        self,
        *,
        spreadsheetId: str,
        fields: str | None = None,
    ) -> dict[str, Any]: ...

    def batchUpdate(
        self,
        *,
        spreadsheetId: str,
        body: dict[str, Any],
    ) -> dict[str, Any]: ...


@runtime_checkable
class SheetsServiceLike(Protocol):
    spreadsheets: SheetsSpreadsheetsClient
    # Discovery client nests values under spreadsheets().values()


@dataclass
class SheetRow:
    row_index: int
    values: dict[str, Any]
    fingerprint: str
    version: int | None = None


@dataclass
class SheetSchema:
    headers: list[str]
    header_index: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_header_row(cls, header: list[Any]) -> SheetSchema:
        headers = [str(h) for h in header]
        return cls(headers=headers, header_index={h: i for i, h in enumerate(headers)})


@dataclass
class SheetScanPage:
    rows: list[SheetRow]
    next_start_row: int | None = None
    truncated: bool = False


class SheetsBackend:
    """Direct Sheets v4 adapter with mapping modes and optimistic concurrency."""

    def __init__(
        self,
        service: Any,
        *,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self.service = service
        self.retry_policy = retry_policy or RetryPolicy()

    @property
    def values(self) -> SheetsValuesClient:
        return self.service.spreadsheets().values()

    def _call(
        self,
        func: Callable[[], Any],
        *,
        retryable: bool = True,
    ) -> Any:
        if getattr(self.service, "_gdm_executes_with_retry", False):
            return func()
        return execute_with_retry(
            func,
            policy=self.retry_policy,
            retryable=retryable,
        )

    def _normalize_http_error(self, exc: HttpError) -> Exception:
        return normalize_http_error(exc)

    def ensure_worksheet(self, spreadsheet_id: str, sheet_title: str) -> None:
        """Create a worksheet tab when missing; never rename or delete existing tabs."""

        def _get() -> dict[str, Any]:
            try:
                return self.service.spreadsheets().get(
                    spreadsheetId=spreadsheet_id,
                    fields="sheets.properties.title",
                )
            except HttpError as exc:
                raise self._normalize_http_error(exc) from exc

        meta = self._call(_get)
        titles = {
            str(sheet.get("properties", {}).get("title", ""))
            for sheet in meta.get("sheets", [])
            if isinstance(sheet, dict)
        }
        if sheet_title in titles:
            return

        def _add() -> dict[str, Any]:
            try:
                return self.service.spreadsheets().batchUpdate(
                    spreadsheetId=spreadsheet_id,
                    body={
                        "requests": [
                            {
                                "addSheet": {
                                    "properties": {"title": sheet_title},
                                }
                            }
                        ]
                    },
                )
            except HttpError as exc:
                raise self._normalize_http_error(exc) from exc

        self._call(_add, retryable=False)

    def get_headers(
        self,
        spreadsheet_id: str,
        sheet_title: str,
    ) -> SheetSchema:
        self.ensure_worksheet(spreadsheet_id, sheet_title)
        range_ = a1_range(sheet_title, "1:1")

        def _get() -> dict[str, Any]:
            try:
                return self.values.get(spreadsheetId=spreadsheet_id, range=range_)
            except HttpError as exc:
                raise self._normalize_http_error(exc) from exc

        response = self._call(_get)
        rows = response.get("values", [])
        if not rows:
            return SheetSchema(headers=[], header_index={})
        return SheetSchema.from_header_row(rows[0])

    def initialize_headers(
        self,
        spreadsheet_id: str,
        sheet_title: str,
        headers: list[str],
    ) -> SheetSchema:
        """Initialize an empty worksheet without changing existing headers."""
        schema = self.get_headers(spreadsheet_id, sheet_title)
        if schema.headers:
            return schema
        ordered = list(dict.fromkeys(header for header in headers if header))
        if not ordered:
            return schema
        range_ = a1_range(sheet_title, "1:1")

        def _update() -> dict[str, Any]:
            try:
                return self.values.update(
                    spreadsheetId=spreadsheet_id,
                    range=range_,
                    body={"values": [ordered]},
                )
            except HttpError as exc:
                raise self._normalize_http_error(exc) from exc

        self._call(_update, retryable=False)
        return SheetSchema.from_header_row(ordered)

    def read_rows(
        self,
        spreadsheet_id: str,
        sheet_title: str,
        *,
        mapping_mode: MappingMode = MappingMode.PARTIAL,
        declared_fields: set[str] | None = None,
        start_row: int = 2,
        max_rows: int = 1000,
        primary_key_column: str | None = None,
    ) -> SheetScanPage:
        end_row = start_row + max_rows - 1
        range_ = a1_range(sheet_title, f"{start_row}:{end_row}")

        def _get() -> dict[str, Any]:
            try:
                return self.values.get(spreadsheetId=spreadsheet_id, range=range_)
            except HttpError as exc:
                raise self._normalize_http_error(exc) from exc

        response = self._call(_get)
        schema = self.get_headers(spreadsheet_id, sheet_title)
        raw_rows = response.get("values", [])
        declared = declared_fields or set(schema.headers)
        pk_col = primary_key_column or (
            COL_GDM_ID if COL_GDM_ID in schema.header_index else None
        )

        rows: list[SheetRow] = []
        for offset, raw in enumerate(raw_rows):
            row_index = start_row + offset
            values = self._row_to_dict(schema, raw)
            self._validate_mapping(values, schema, mapping_mode, declared)
            fingerprint = self.row_fingerprint(schema, raw)
            version = self._parse_int(values.get(COL_GDM_VERSION))
            if pk_col and not values.get(pk_col):
                continue
            rows.append(
                SheetRow(
                    row_index=row_index,
                    values=values,
                    fingerprint=fingerprint,
                    version=version,
                )
            )

        truncated = len(raw_rows) >= max_rows
        next_start = start_row + len(raw_rows) if truncated else None
        return SheetScanPage(rows=rows, next_start_row=next_start, truncated=truncated)

    def append_row(
        self,
        spreadsheet_id: str,
        sheet_title: str,
        *,
        values: dict[str, Any],
        schema: SheetSchema | None = None,
        record_id: str | None = None,
        initial_version: int = 1,
    ) -> SheetRow:
        schema = schema or self.get_headers(spreadsheet_id, sheet_title)
        row_values = dict(values)
        if record_id is not None:
            row_values.setdefault(COL_GDM_ID, record_id)
        row_values.setdefault(COL_GDM_VERSION, initial_version)
        row_values[COL_GDM_UPDATED_AT] = datetime.now(tz=UTC).isoformat()
        row = self._dict_to_row(schema, row_values)
        range_ = a1_range(sheet_title, "A1")

        def _append() -> dict[str, Any]:
            try:
                return self.values.append(
                    spreadsheetId=spreadsheet_id,
                    range=range_,
                    body={"values": [row]},
                    valueInputOption="USER_ENTERED",
                    insertDataOption="INSERT_ROWS",
                )
            except HttpError as exc:
                raise self._normalize_http_error(exc) from exc

        response = self._call(_append, retryable=False)
        updated = response.get("updates", {})
        row_index = int(updated.get("updatedRows", 1))  # approximate; tests mock exact
        table = self._read_sheet_table(spreadsheet_id, sheet_title)
        row_index = len(table)
        fingerprint = self.row_fingerprint(schema, row)
        return SheetRow(
            row_index=row_index,
            values=row_values,
            fingerprint=fingerprint,
            version=initial_version,
        )

    def update_row(
        self,
        spreadsheet_id: str,
        sheet_title: str,
        *,
        row_index: int,
        values: dict[str, Any],
        schema: SheetSchema | None = None,
        expected_fingerprint: str | None = None,
        expected_version: int | None = None,
        preserve_unknown: bool = True,
    ) -> SheetRow:
        schema = schema or self.get_headers(spreadsheet_id, sheet_title)
        existing = self._read_row_at(spreadsheet_id, sheet_title, schema, row_index)
        if (
            expected_fingerprint is not None
            and existing.fingerprint != expected_fingerprint
        ):
            msg = (
                f"Conflict updating row {row_index}: fingerprint mismatch "
                f"(expected {expected_fingerprint}, found {existing.fingerprint})."
            )
            raise ConflictError(msg)
        if expected_version is not None and existing.version != expected_version:
            msg = (
                f"Conflict updating row {row_index}: version mismatch "
                f"(expected {expected_version}, found {existing.version})."
            )
            raise ConflictError(msg)

        merged = dict(existing.values) if preserve_unknown else {}
        merged.update(values)
        new_version = (existing.version or 0) + 1
        merged[COL_GDM_VERSION] = new_version
        merged[COL_GDM_UPDATED_AT] = datetime.now(tz=UTC).isoformat()
        row = self._dict_to_row(schema, merged)
        range_ = a1_range(sheet_title, f"{row_index}:{row_index}")

        def _update() -> dict[str, Any]:
            try:
                return self.values.update(
                    spreadsheetId=spreadsheet_id,
                    range=range_,
                    body={"values": [row]},
                )
            except HttpError as exc:
                raise self._normalize_http_error(exc) from exc

        self._call(_update, retryable=False)
        fingerprint = self.row_fingerprint(schema, row)
        return SheetRow(
            row_index=row_index,
            values=merged,
            fingerprint=fingerprint,
            version=new_version,
        )

    def delete_row(
        self,
        spreadsheet_id: str,
        sheet_title: str,
        *,
        row_index: int,
        sheet_id: int | None = None,
    ) -> None:
        sheet_id = sheet_id or self._resolve_sheet_id(spreadsheet_id, sheet_title)

        def _delete() -> dict[str, Any]:
            body = {
                "requests": [
                    {
                        "deleteDimension": {
                            "range": {
                                "sheetId": sheet_id,
                                "dimension": "ROWS",
                                "startIndex": row_index - 1,
                                "endIndex": row_index,
                            }
                        }
                    }
                ]
            }
            try:
                return self.service.spreadsheets().batchUpdate(
                    spreadsheetId=spreadsheet_id,
                    body=body,
                )
            except HttpError as exc:
                raise self._normalize_http_error(exc) from exc

        self._call(_delete, retryable=False)

    def find_row_by_primary_key(
        self,
        spreadsheet_id: str,
        sheet_title: str,
        *,
        primary_key: str,
        primary_key_column: str | None = None,
        scan_limit: int = 5000,
    ) -> SheetRow:
        schema = self.get_headers(spreadsheet_id, sheet_title)
        pk_col = primary_key_column or (
            COL_GDM_ID if COL_GDM_ID in schema.header_index else None
        )
        if pk_col is None:
            msg = "No primary key column configured."
            raise UnsupportedOperationError(msg)

        start = 2
        while start <= scan_limit + 1:
            page = self.read_rows(
                spreadsheet_id,
                sheet_title,
                start_row=start,
                max_rows=min(500, scan_limit),
                primary_key_column=pk_col,
            )
            for row in page.rows:
                if str(row.values.get(pk_col)) == primary_key:
                    return row
            if not page.truncated or page.next_start_row is None:
                break
            start = page.next_start_row
        msg = f"No row with {pk_col}={primary_key!r}."
        raise NotFoundError(msg)

    def batch_update_rows(
        self,
        spreadsheet_id: str,
        sheet_title: str,
        updates: list[tuple[int, dict[str, Any]]],
        *,
        schema: SheetSchema | None = None,
    ) -> list[SheetRow]:
        schema = schema or self.get_headers(spreadsheet_id, sheet_title)
        data: list[dict[str, Any]] = []
        results: list[SheetRow] = []
        for row_index, values in updates:
            existing = self._read_row_at(spreadsheet_id, sheet_title, schema, row_index)
            merged = {**existing.values, **values}
            merged[COL_GDM_VERSION] = (existing.version or 0) + 1
            merged[COL_GDM_UPDATED_AT] = datetime.now(tz=UTC).isoformat()
            row = self._dict_to_row(schema, merged)
            data.append(
                {
                    "range": a1_range(sheet_title, f"{row_index}:{row_index}"),
                    "values": [row],
                }
            )
            results.append(
                SheetRow(
                    row_index=row_index,
                    values=merged,
                    fingerprint=self.row_fingerprint(schema, row),
                    version=int(merged[COL_GDM_VERSION]),
                )
            )

        def _batch() -> dict[str, Any]:
            try:
                return self.values.batchUpdate(
                    spreadsheetId=spreadsheet_id,
                    body={"valueInputOption": "USER_ENTERED", "data": data},
                )
            except HttpError as exc:
                raise self._normalize_http_error(exc) from exc

        self._call(_batch, retryable=False)
        return results

    @staticmethod
    def row_fingerprint(schema: SheetSchema, row_values: list[Any]) -> str:
        canonical = json.dumps(
            {
                h: row_values[i] if i < len(row_values) else None
                for i, h in enumerate(schema.headers)
            },
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _read_sheet_table(
        self,
        spreadsheet_id: str,
        sheet_title: str,
    ) -> list[list[Any]]:
        range_ = a1_range(sheet_title, "A:ZZ")

        def _get() -> dict[str, Any]:
            try:
                return self.values.get(spreadsheetId=spreadsheet_id, range=range_)
            except HttpError as exc:
                raise self._normalize_http_error(exc) from exc

        response = self._call(_get)
        return response.get("values", [])

    def _read_row_at(
        self,
        spreadsheet_id: str,
        sheet_title: str,
        schema: SheetSchema,
        row_index: int,
    ) -> SheetRow:
        table = self._read_sheet_table(spreadsheet_id, sheet_title)
        if row_index < 1 or row_index > len(table):
            msg = f"Row {row_index} out of range."
            raise NotFoundError(msg)
        raw = table[row_index - 1]
        values = self._row_to_dict(schema, raw)
        return SheetRow(
            row_index=row_index,
            values=values,
            fingerprint=self.row_fingerprint(schema, raw),
            version=self._parse_int(values.get(COL_GDM_VERSION)),
        )

    def _resolve_sheet_id(self, spreadsheet_id: str, sheet_title: str) -> int:
        def _get() -> dict[str, Any]:
            try:
                return self.service.spreadsheets().get(
                    spreadsheetId=spreadsheet_id,
                    fields="sheets(properties(sheetId,title))",
                )
            except HttpError as exc:
                raise self._normalize_http_error(exc) from exc

        meta = self._call(_get)
        matches = [
            s["properties"]["sheetId"]
            for s in meta.get("sheets", [])
            if s.get("properties", {}).get("title") == sheet_title
        ]
        if not matches:
            msg = f"Worksheet {sheet_title!r} not found."
            raise NotFoundError(msg)
        if len(matches) > 1:
            msg = f"Ambiguous worksheet title {sheet_title!r}."
            raise AmbiguousResourceError(msg)
        return int(matches[0])

    @staticmethod
    def _row_to_dict(schema: SheetSchema, raw: list[Any]) -> dict[str, Any]:
        return {
            header: raw[idx] if idx < len(raw) else None
            for header, idx in schema.header_index.items()
        }

    @staticmethod
    def _dict_to_row(schema: SheetSchema, values: dict[str, Any]) -> list[Any]:
        row: list[Any] = [None] * len(schema.headers)
        for header, idx in schema.header_index.items():
            if header in values:
                row[idx] = values[header]
        return row

    @staticmethod
    def _parse_int(value: Any) -> int | None:
        if value is None or value == "":
            return None
        return int(value)

    @staticmethod
    def _validate_mapping(
        values: dict[str, Any],
        schema: SheetSchema,
        mapping_mode: MappingMode,
        declared_fields: set[str],
    ) -> None:
        unknown_headers = set(schema.headers) - declared_fields - set(SYSTEM_COLUMNS)
        if mapping_mode == MappingMode.STRICT and unknown_headers:
            msg = f"Unknown columns in strict mode: {sorted(unknown_headers)}"
            raise UnsupportedOperationError(msg)
        if mapping_mode == MappingMode.STRICT:
            extra_keys = set(values) - declared_fields - set(SYSTEM_COLUMNS)
            if extra_keys:
                msg = f"Unknown values in strict mode: {sorted(extra_keys)}"
                raise UnsupportedOperationError(msg)
