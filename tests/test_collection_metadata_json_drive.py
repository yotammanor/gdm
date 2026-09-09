"""Drive-backed JSON collection metadata store tests."""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest

from gdm.backends.drive import DriveBackend
from gdm.backends.registry import RegistryBackend
from gdm.backends.sheets import SheetsBackend
from gdm.client import GDMClient
from gdm.collection.constants import FOLDER_MIME, METADATA_JSON_FILENAME
from gdm.collection.metadata import (
    DriveJsonCollectionMetadataStore,
    JsonCollectionMetadataStore,
)
from gdm.errors import ConflictError
from gdm.fields import Field
from gdm.model import GDMModel
from gdm.project import Project, RegistryEntry, ResourceKind
from gdm.session import Session
from tests.collection_fakes import CollectionMemoryDriveService, seed_mixed_collection
from tests.fakes import FakeSheetsService


class JsonAsset(GDMModel, storage="collection", mapping="partial"):
    drive_id: str = Field(drive="id")
    name: str = Field(drive="name")
    campaign: str | None = Field(default=None, metadata="campaign")


def _json_backend_setup(*, metadata_backend: str = "json") -> dict[str, Any]:
    drive_service = CollectionMemoryDriveService()
    root_id = drive_service.files.seed(
        file_id="root-json",
        name="Root",
        mime_type=FOLDER_MIME,
        parents=[],
    )
    project = Project(
        project_id=uuid.uuid4(),
        root_folder_id=root_id,
        drive=drive_service,
    )
    project.initialize()
    folder_id = drive_service.files.seed(
        name="Assets",
        mime_type=FOLDER_MIME,
        parents=[root_id],
    )
    ids = seed_mixed_collection(drive_service.files, folder_id)
    drive = DriveBackend(drive_service, project_id=str(project.project_id))
    sheets = SheetsBackend(FakeSheetsService([["_gdm_file_id", "_gdm_version"]]))
    backend = RegistryBackend(
        project=project,
        drive=drive,
        sheets=sheets,
        default_metadata_backend=metadata_backend,
    )
    backend.bind_model(
        JsonAsset,
        RegistryEntry(
            model_name=JsonAsset.gdm_model_name(),
            resource_id=folder_id,
            kind=ResourceKind.FOLDER,
            extra={"metadata_backend": metadata_backend},
        ),
    )
    client = GDMClient(
        project=project,
        drive=drive,
        sheets=sheets,
        registry_backend=backend,
        drive_service=drive_service,  # type: ignore[arg-type]
    )
    return {
        "drive_service": drive_service,
        "project": project,
        "backend": backend,
        "client": client,
        "folder_id": folder_id,
        "ids": ids,
    }


def test_drive_json_metadata_roundtrip_and_reopen() -> None:
    setup = _json_backend_setup()
    coll = setup["client"].collection(JsonAsset)
    doc_id = setup["ids"]["doc"]
    doc = coll.get(doc_id)
    with Session(setup["backend"]) as session:
        doc.campaign = "launch"
        session.add(doc)
        session.commit()

    file_id = setup["project"].ensure_collection_json_metadata_binding(
        JsonAsset.gdm_model_name()
    )
    raw = setup["drive_service"].read_file_bytes(file_id)
    payload = json.loads(raw.decode())
    assert payload["rows"][doc_id]["campaign"] == "launch"
    assert int(payload["sidecar_version"]) >= 1

    reopened = DriveJsonCollectionMetadataStore(
        setup["drive_service"],
        file_id=file_id,
    )
    row = reopened.get_row(doc_id)
    assert row is not None
    assert row.values["campaign"] == "launch"

    setup["backend"]._metadata_stores.pop(JsonAsset.gdm_model_name(), None)
    reloaded = setup["client"].collection(JsonAsset)
    again = reloaded.get(doc_id)
    assert again.campaign == "launch"


def test_drive_json_sidecar_conflict() -> None:
    setup = _json_backend_setup()
    file_id = setup["project"].ensure_collection_json_metadata_binding(
        JsonAsset.gdm_model_name()
    )
    store_a = DriveJsonCollectionMetadataStore(setup["drive_service"], file_id=file_id)
    store_b = DriveJsonCollectionMetadataStore(setup["drive_service"], file_id=file_id)
    store_a.upsert_row("f1", {"tag": "a"})
    store_b.upsert_row("f2", {"tag": "b"})
    with pytest.raises(ConflictError, match="sidecar conflict"):
        store_a.upsert_row("f3", {"tag": "c"})


def test_json_backend_never_calls_sheets() -> None:
    setup = _json_backend_setup()
    sheets = setup["backend"].sheets.service
    spreadsheets = MagicMock(wraps=sheets.spreadsheets())
    sheets.spreadsheets = MagicMock(return_value=spreadsheets)
    coll = setup["client"].collection(JsonAsset)
    doc = coll.get(setup["ids"]["doc"])
    with Session(setup["backend"]) as session:
        doc.campaign = "no-sheets"
        session.add(doc)
        session.commit()
    spreadsheets.values.assert_not_called()
    spreadsheets.get.assert_not_called()
    spreadsheets.batchUpdate.assert_not_called()


def test_json_metadata_list_does_not_download_member_content() -> None:
    setup = _json_backend_setup()
    drive_files = setup["drive_service"].files
    before = [call for call in drive_files.calls if call[0] == "get_media"]
    coll = setup["client"].collection(JsonAsset)
    rows = coll.list()
    after = [call for call in drive_files.calls if call[0] == "get_media"]
    assert len(rows) >= 1
    assert after == before


def test_in_memory_json_store_remains_backwards_compatible() -> None:
    store = JsonCollectionMetadataStore()
    row = store.upsert_row("x", {"a": 1, "legacy": "keep"})
    updated = store.upsert_row("x", {"b": 2})
    assert updated.values["legacy"] == "keep"
    assert row.version == "1"


def test_json_sidecar_file_created_under_collections_area() -> None:
    setup = _json_backend_setup()
    setup["client"].collection(JsonAsset)
    collections_root = setup["project"].collections_folder_id()
    listed = setup["drive_service"].files.list(
        q=f"'{collections_root}' in parents and trashed = false"
    )
    model_folder = next(
        item for item in listed["files"] if item["name"] == JsonAsset.gdm_model_name()
    )
    metadata_files = setup["drive_service"].files.list(
        q=(
            f"'{model_folder['id']}' in parents and name = "
            f"'{METADATA_JSON_FILENAME}' and trashed = false"
        )
    )
    assert len(metadata_files["files"]) == 1
