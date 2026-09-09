"""Unit tests for collection metadata stores."""

from __future__ import annotations

import pytest

from gdm.collection.metadata import (
    InMemoryCollectionMetadataStore,
    JsonCollectionMetadataStore,
    metadata_fingerprint,
)
from gdm.errors import ConflictError, NotFoundError


def test_in_memory_metadata_upsert_and_conflict() -> None:
    store = InMemoryCollectionMetadataStore()
    row = store.upsert_row("f1", {"campaign": "launch"})
    assert row.version == "1"
    updated = store.upsert_row(
        "f1",
        {"approved": True},
        expected_version="1",
        expected_fingerprint=row.fingerprint,
    )
    assert updated.version == "2"
    assert updated.values["campaign"] == "launch"
    with pytest.raises(ConflictError):
        store.upsert_row("f1", {"campaign": "x"}, expected_version="1")


def test_json_metadata_store_roundtrip() -> None:
    store = JsonCollectionMetadataStore()
    store.upsert_row("f1", {"tag": "a"})
    rows = store.list_rows()
    assert len(rows) == 1
    assert store.get_row("f1") is not None
    store.delete_row("f1")
    assert store.get_row("f1") is None


def test_reconcile_orphans_and_missing() -> None:
    store = InMemoryCollectionMetadataStore()
    store.upsert_row("orphan", {"x": 1})
    orphans, missing = store.reconcile(folder_file_ids={"live"})
    assert orphans == ["orphan"]
    assert missing == ["live"]


def test_metadata_fingerprint_stable() -> None:
    fp1 = metadata_fingerprint({"a": 1, "b": 2})
    fp2 = metadata_fingerprint({"b": 2, "a": 1})
    assert fp1 == fp2


def test_in_memory_delete_missing_is_noop() -> None:
    store = InMemoryCollectionMetadataStore()
    store.delete_row("missing")


def test_json_fingerprint_conflict() -> None:
    store = JsonCollectionMetadataStore()
    store.upsert_row("f1", {"a": 1})
    with pytest.raises(ConflictError):
        store.upsert_row("f1", {"a": 2}, expected_fingerprint="wrong")


def test_sheets_metadata_store_with_fake() -> None:
    from gdm.collection.metadata import SheetsCollectionMetadataStore
    from tests.fakes import FakeSheetsService

    table = [["_gdm_file_id", "_gdm_version", "campaign"]]
    service = FakeSheetsService(table)
    store = SheetsCollectionMetadataStore(
        service.spreadsheets(),
        spreadsheet_id="sheet-1",
        worksheet="metadata",
    )
    row = store.upsert_row("doc-1", {"campaign": "launch"})
    assert row.file_id == "doc-1"
    loaded = store.get_row("doc-1")
    assert loaded is not None
    assert loaded.values["campaign"] == "launch"
    orphans, missing = store.reconcile(folder_file_ids={"doc-1", "doc-2"})
    assert orphans == []
    assert missing == ["doc-2"]
    store.delete_row("doc-1")
    with pytest.raises(NotFoundError):
        store.delete_row("doc-1")
