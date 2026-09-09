# ruff: noqa: N803, A002, N802
"""Tests for gdm.backends.sheets."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from googleapiclient.errors import HttpError

from gdm.auth import (
    MappingMode,
    NotFoundError,
    PermissionDeniedError,
    UnsupportedOperationError,
)
from gdm.backends.sheets import (
    SheetsBackend,
    a1_range,
    quote_sheet_title,
)
from gdm.errors import ConflictError


class FakeValues:
    def __init__(self, table: list[list[Any]]) -> None:
        self.table = table
        self.batch_data: list[dict[str, Any]] = []

    def get(
        self,
        *,
        spreadsheetId: str,
        range: str,
        majorDimension: str = "ROWS",
        valueRenderOption: str | None = None,
    ) -> dict[str, Any]:
        del spreadsheetId, majorDimension, valueRenderOption
        if range.endswith("1:1"):
            return {"values": [self.table[0]]} if self.table else {"values": []}
        if ":ZZ" in range:
            return {"values": list(self.table)}
        if ":" in range.split("!")[1]:
            start = int(range.split("!")[1].split(":")[0]) - 1
            return {"values": self.table[start:]}

    def append(
        self,
        *,
        spreadsheetId: str,
        range: str,
        body: dict[str, Any],
        valueInputOption: str = "USER_ENTERED",
        insertDataOption: str = "INSERT_ROWS",
    ) -> dict[str, Any]:
        del spreadsheetId, range, valueInputOption, insertDataOption
        self.table.append(body["values"][0])
        return {"updates": {"updatedRows": 1}}

    def update(
        self,
        *,
        spreadsheetId: str,
        range: str,
        body: dict[str, Any],
        valueInputOption: str = "USER_ENTERED",
    ) -> dict[str, Any]:
        del spreadsheetId, valueInputOption
        row = int(range.split("!")[1].split(":")[0])
        if row > len(self.table):
            self.table.append(body["values"][0])
        else:
            self.table[row - 1] = body["values"][0]
        return {"updatedRows": 1}

    def batchUpdate(
        self, *, spreadsheetId: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        del spreadsheetId
        self.batch_data = body["data"]
        for item in body["data"]:
            row = int(item["range"].split("!")[1].split(":")[0])
            self.table[row - 1] = item["values"][0]
        return {"totalUpdatedRows": len(body["data"])}


class FakeSpreadsheets:
    def __init__(self, values: FakeValues) -> None:
        self._values = values

    def values(self) -> FakeValues:
        return self._values

    def get(self, *, spreadsheetId: str, fields: str | None = None) -> dict[str, Any]:
        del spreadsheetId, fields
        return {"sheets": [{"properties": {"sheetId": 0, "title": "Sheet1"}}]}

    def batchUpdate(
        self, *, spreadsheetId: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        del spreadsheetId, body
        return {}


class FakeSheetsService:
    def __init__(self, table: list[list[Any]]) -> None:
        self._values = FakeValues(table)
        self._spreadsheets = FakeSpreadsheets(self._values)

    def spreadsheets(self) -> FakeSpreadsheets:
        return self._spreadsheets


@pytest.fixture
def backend() -> SheetsBackend:
    table = [
        ["name", "score", "_gdm_id", "_gdm_version", "_gdm_updated_at"],
        ["alice", "10", "id-1", "1", "2024-01-01T00:00:00+00:00"],
        ["bob", "20", "id-2", "2", "2024-01-02T00:00:00+00:00"],
    ]
    return SheetsBackend(FakeSheetsService(table))


def test_quote_sheet_title_escapes() -> None:
    assert quote_sheet_title("Bob's Sheet") == "'Bob\\'s Sheet'"
    assert a1_range("Bob's Sheet", "A1:B2") == "'Bob\\'s Sheet'!A1:B2"


def test_get_headers(backend: SheetsBackend) -> None:
    schema = backend.get_headers("ss", "Sheet1")
    assert schema.headers[0] == "name"
    assert schema.header_index["_gdm_id"] == 2


def test_initialize_headers_only_mutates_empty_sheet() -> None:
    service = FakeSheetsService([])
    backend = SheetsBackend(service)
    initialized = backend.initialize_headers("ss", "Sheet1", ["id", "name", "id"])
    assert initialized.headers == ["id", "name"]
    assert service._values.table == [["id", "name"]]

    unchanged = backend.initialize_headers("ss", "Sheet1", ["replacement"])
    assert unchanged.headers == ["id", "name"]
    assert service._values.table == [["id", "name"]]


def test_read_rows_partial(backend: SheetsBackend) -> None:
    page = backend.read_rows("ss", "Sheet1", declared_fields={"name", "score"})
    assert len(page.rows) == 2
    assert page.rows[0].values["name"] == "alice"


def test_read_rows_strict_rejects_unknown(backend: SheetsBackend) -> None:
    with pytest.raises(UnsupportedOperationError, match="Unknown columns"):
        backend.read_rows(
            "ss",
            "Sheet1",
            mapping_mode=MappingMode.STRICT,
            declared_fields={"name"},
        )


def test_find_row_by_primary_key(backend: SheetsBackend) -> None:
    row = backend.find_row_by_primary_key("ss", "Sheet1", primary_key="id-2")
    assert row.values["name"] == "bob"


def test_find_row_missing(backend: SheetsBackend) -> None:
    with pytest.raises(NotFoundError):
        backend.find_row_by_primary_key("ss", "Sheet1", primary_key="missing")


def test_update_row_conflict(backend: SheetsBackend) -> None:
    row = backend.find_row_by_primary_key("ss", "Sheet1", primary_key="id-1")
    with pytest.raises(ConflictError):
        backend.update_row(
            "ss",
            "Sheet1",
            row_index=row.row_index,
            values={"score": "99"},
            expected_version=99,
        )


def test_update_row_bumps_version(backend: SheetsBackend) -> None:
    row = backend.find_row_by_primary_key("ss", "Sheet1", primary_key="id-1")
    updated = backend.update_row(
        "ss",
        "Sheet1",
        row_index=row.row_index,
        values={"score": "11"},
        expected_version=1,
        expected_fingerprint=row.fingerprint,
    )
    assert updated.version == 2
    assert updated.values["score"] == "11"


def test_append_row(backend: SheetsBackend) -> None:
    appended = backend.append_row(
        "ss",
        "Sheet1",
        values={"name": "carol", "score": "30"},
        record_id="id-3",
    )
    assert appended.values["_gdm_id"] == "id-3"
    assert appended.version == 1


def test_batch_update_rows(backend: SheetsBackend) -> None:
    results = backend.batch_update_rows(
        "ss",
        "Sheet1",
        [(2, {"score": "15"}), (3, {"score": "25"})],
    )
    assert len(results) == 2
    assert results[0].version == 2


def test_delete_row(backend: SheetsBackend) -> None:
    backend.delete_row("ss", "Sheet1", row_index=2, sheet_id=0)
    # no exception == success with fake batchUpdate


def test_permission_error(backend: SheetsBackend) -> None:
    resp = MagicMock(status=403)
    err = HttpError(resp, b"denied")

    def _raise(**kwargs: object) -> dict[str, object]:
        del kwargs
        raise err

    backend.service._values.get = _raise  # type: ignore[method-assign]
    with pytest.raises(PermissionDeniedError):
        backend.get_headers("ss", "Sheet1")
