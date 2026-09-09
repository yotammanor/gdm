"""Branch coverage for collection metadata stores."""
# ruff: noqa: N803, A002, F841

from __future__ import annotations

from typing import Any

import pytest

from gdm.collection.constants import METADATA_ID_COLUMN, METADATA_VERSION_COLUMN
from gdm.collection.metadata import (
    InMemoryCollectionMetadataStore,
    JsonCollectionMetadataStore,
    SheetsCollectionMetadataStore,
    metadata_fingerprint,
)
from gdm.errors import ConflictError, GDMError, NotFoundError
from tests.fakes import FakeSheetsService


def test_in_memory_list_filters_and_limit() -> None:
    store = InMemoryCollectionMetadataStore()
    store.upsert_row("a", {"x": 1})
    store.upsert_row("b", {"x": 2})
    store.upsert_row("c", {"x": 3})
    filtered = store.list_rows(file_ids={"a", "c"})
    assert [row.file_id for row in filtered] == ["a", "c"]
    limited = store.list_rows(limit=2)
    assert len(limited) == 2


def test_in_memory_fingerprint_conflict_and_unknown_columns() -> None:
    store = InMemoryCollectionMetadataStore()
    row = store.upsert_row("f1", {"campaign": "a", "legacy": "keep"})
    with pytest.raises(ConflictError, match="fingerprint"):
        store.upsert_row(
            "f1",
            {"campaign": "b"},
            expected_fingerprint="wrong",
        )
    updated = store.upsert_row("f1", {"extra": "v"})
    assert updated.values["legacy"] == "keep"
    assert "legacy" in store.unknown_columns


def test_json_store_version_conflict_and_filters() -> None:
    store = JsonCollectionMetadataStore()
    row = store.upsert_row("j1", {"tag": "one"})
    with pytest.raises(ConflictError, match="version"):
        store.upsert_row("j1", {"tag": "two"}, expected_version="0")
    rows = store.list_rows(file_ids={"j1"}, limit=1)
    assert len(rows) == 1
    assert rows[0].fingerprint == metadata_fingerprint({"tag": "one"})
    store.delete_row("j1")
    orphans, missing = store.reconcile(folder_file_ids=set())
    assert orphans == []
    assert missing == []


def test_sheets_store_empty_sheet_gets_default_header() -> None:
    table = [[METADATA_ID_COLUMN, METADATA_VERSION_COLUMN]]
    service = FakeSheetsService(table)
    store = SheetsCollectionMetadataStore(
        service.spreadsheets(),
        spreadsheet_id="meta-1",
        worksheet="metadata",
    )
    rows = store.list_rows()
    assert rows == []
    row = store.upsert_row("new-1", {"status": "draft"})
    assert row.file_id == "new-1"
    assert row.values["status"] == "draft"


def test_sheets_store_missing_standard_headers() -> None:
    table = [["custom_id", "label"], ["file-x", "Launch"]]
    service = FakeSheetsService(table)
    store = SheetsCollectionMetadataStore(
        service.spreadsheets(),
        spreadsheet_id="meta-2",
    )
    store._load()
    row = store.get_row("file-x")
    assert row is not None
    assert row.values["label"] == "Launch"
    assert row.file_id == "file-x"


def test_sheets_store_skips_blank_rows_and_ids() -> None:
    table = [
        [METADATA_ID_COLUMN, METADATA_VERSION_COLUMN, "note"],
        [],
        ["", "1", "empty-id"],
        ["valid", "1", "ok"],
    ]
    service = FakeSheetsService(table)
    store = SheetsCollectionMetadataStore(
        service.spreadsheets(),
        spreadsheet_id="meta-3",
    )
    rows = store.list_rows()
    assert len(rows) == 1
    assert rows[0].file_id == "valid"


def test_sheets_store_header_expansion_on_upsert() -> None:
    table = [[METADATA_ID_COLUMN, METADATA_VERSION_COLUMN]]
    service = FakeSheetsService(table)
    store = SheetsCollectionMetadataStore(
        service.spreadsheets(),
        spreadsheet_id="meta-4",
    )
    store.upsert_row("f1", {"alpha": "1", "beta": "2"})
    updated = store.upsert_row("f1", {"gamma": "3"}, expected_version="1")
    assert updated.version == "2"
    assert updated.values["gamma"] == "3"
    assert store.get_row("f1") is not None


def test_sheets_store_update_existing_row_path() -> None:
    table = [
        [METADATA_ID_COLUMN, METADATA_VERSION_COLUMN, "status"],
        ["existing", "1", "open"],
    ]
    service = FakeSheetsService(table)
    store = SheetsCollectionMetadataStore(
        service.spreadsheets(),
        spreadsheet_id="meta-5",
    )
    row = store.upsert_row("existing", {"status": "closed"})
    assert row.row_index == 2
    assert row.version == "2"


def test_sheets_store_version_and_fingerprint_conflicts() -> None:
    table = [
        [METADATA_ID_COLUMN, METADATA_VERSION_COLUMN, "v"],
        ["conflict", "3", "x"],
    ]
    service = FakeSheetsService(table)
    store = SheetsCollectionMetadataStore(
        service.spreadsheets(),
        spreadsheet_id="meta-6",
    )
    with pytest.raises(ConflictError, match="version"):
        store.upsert_row("conflict", {"v": "y"}, expected_version="1")
    existing = store.get_row("conflict")
    assert existing is not None
    with pytest.raises(ConflictError, match="fingerprint"):
        store.upsert_row(
            "conflict",
            {"v": "y"},
            expected_fingerprint="bad-fp",
        )


def test_sheets_store_delete_and_reconcile() -> None:
    table = [
        [METADATA_ID_COLUMN, METADATA_VERSION_COLUMN],
        ["gone", "1"],
        ["stay", "1"],
    ]
    service = FakeSheetsService(table)
    store = SheetsCollectionMetadataStore(
        service.spreadsheets(),
        spreadsheet_id="meta-7",
    )
    store.delete_row("gone")
    with pytest.raises(NotFoundError):
        store.delete_row("gone")
    orphans, missing = store.reconcile(folder_file_ids={"stay", "new"})
    assert orphans == []
    assert missing == ["new"]


def test_sheets_store_append_failure_raises_gdm_error() -> None:
    class BrokenValues:
        def get(
            self,
            *,
            spreadsheetId: str,
            range: str,
            majorDimension: str = "ROWS",
            valueRenderOption: str | None = None,
        ) -> dict[str, Any]:
            del spreadsheetId, majorDimension, valueRenderOption
            if "1:10000" in range:
                return {"values": [[METADATA_ID_COLUMN, METADATA_VERSION_COLUMN]]}
            return {"values": []}

        def append(self, **kwargs: Any) -> dict[str, Any]:
            del kwargs
            return {"updates": {"updatedRows": 1}}

        def update(self, **kwargs: Any) -> dict[str, Any]:
            del kwargs
            return {}

    class BrokenSpreadsheets:
        def values(self) -> BrokenValues:
            return BrokenValues()

    store = SheetsCollectionMetadataStore(
        BrokenSpreadsheets(),
        spreadsheet_id="broken",
    )
    with pytest.raises(GDMError, match="Failed to append"):
        store.upsert_row("missing-after-append", {"k": "v"})
