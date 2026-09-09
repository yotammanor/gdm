"""Tests for collection-centered Drive folder storage."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from gdm.backends.drive import DriveBackend
from gdm.backends.registry import RegistryBackend
from gdm.backends.sheets import SheetsBackend
from gdm.collection.constants import DOC_MIME, FOLDER_MIME
from gdm.collection.metadata import (
    CollectionMetadataRow,
    InMemoryCollectionMetadataStore,
)
from gdm.errors import (
    AmbiguousResourceError,
    ConflictError,
    InvalidModelConfigError,
    NotFoundError,
    PartialCommitError,
    UnsupportedOperationError,
)
from gdm.fields import Field
from gdm.model import GDMModel
from gdm.project import Project, RegistryEntry, ResourceKind
from gdm.session import Session
from tests.collection_fakes import (
    CollectionMemoryDriveService,
    FakeDocsService,
    FakeSlidesService,
    seed_mixed_collection,
)
from tests.fakes import FakeSheetsService


class Asset(GDMModel, storage="collection", mapping="partial"):
    drive_id: str = Field(drive="id")
    name: str = Field(drive="name")
    mime_type: str = Field(drive="mimeType")
    campaign: str | None = Field(default=None, metadata="campaign")
    approved: bool = Field(default=False, metadata="approved")


class ImageAsset(GDMModel, storage="collection", mime="image/*", mapping="partial"):
    drive_id: str = Field(drive="id")
    name: str = Field(drive="name")
    mime_type: str = Field(drive="mimeType")


class JsonNote(GDMModel, storage="file", codec="json", mapping="partial"):
    id: str | None = Field(default=None, primary_key=True)
    title: str


@pytest.fixture
def collection_setup() -> dict[str, Any]:
    drive_service = CollectionMemoryDriveService()
    root_id = drive_service.files.seed(
        file_id="root-folder",
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
    collection_folder = drive_service.files.seed(
        name="Campaigns",
        mime_type=FOLDER_MIME,
        parents=[root_id],
    )
    ids = seed_mixed_collection(drive_service.files, collection_folder)
    metadata = InMemoryCollectionMetadataStore(
        rows={
            ids["doc"]: CollectionMetadataRow(
                file_id=ids["doc"],
                values={"campaign": "launch", "approved": True},
                version="1",
                fingerprint="fp-doc",
            )
        }
    )
    drive = DriveBackend(
        drive_service,
        project_id=str(project.project_id),
    )
    sheets = SheetsBackend(FakeSheetsService([["_gdm_file_id", "_gdm_version"]]))
    backend = RegistryBackend(
        project=project,
        drive=drive,
        sheets=sheets,
        scan_limit=100,
        collection_backend=__import__(
            "gdm.collection.backend", fromlist=["CollectionBackend"]
        ).CollectionBackend(
            drive,
            project_id=str(project.project_id),
            documents_service=FakeDocsService(),
            slides_service=FakeSlidesService(),
        ),
        metadata_stores={Asset.gdm_model_name(): metadata},
    )
    backend.bind_model(
        Asset,
        RegistryEntry(
            model_name=Asset.gdm_model_name(),
            resource_id=collection_folder,
            kind=ResourceKind.FOLDER,
        ),
    )
    backend.bind_model(
        ImageAsset,
        RegistryEntry(
            model_name=ImageAsset.gdm_model_name(),
            resource_id=collection_folder,
            kind=ResourceKind.FOLDER,
        ),
    )
    return {
        "backend": backend,
        "project": project,
        "folder_id": collection_folder,
        "ids": ids,
        "drive_service": drive_service,
        "metadata": metadata,
    }


def test_storage_file_alias_is_collection() -> None:
    class Alias(GDMModel, storage="file"):
        name: str

    assert Alias.gdm_storage() == "collection"
    assert not Alias.uses_legacy_file_codec()


def test_codec_preserves_legacy_file_storage() -> None:
    assert JsonNote.gdm_storage() == "file"
    assert JsonNote.uses_legacy_file_codec()


def test_field_targets_mutually_exclusive() -> None:
    with pytest.raises(InvalidModelConfigError, match="mutually exclusive"):
        Field(source="x", drive="name")


def test_collection_field_defaults_to_metadata_target() -> None:
    class Sample(GDMModel, storage="collection"):
        tag: str = Field(default="")

    from gdm.fields import field_storage_target

    kind, name = field_storage_target(
        "tag", Sample.model_fields["tag"], storage="collection"
    )
    assert kind == "metadata"
    assert name == "tag"


def test_metadata_scan_does_not_download_content(
    collection_setup: dict[str, Any],
) -> None:
    backend = collection_setup["backend"]
    drive_service = collection_setup["drive_service"]
    drive_service.files.calls.clear()
    records = backend.scan(Asset)
    assert len(records) >= 5
    content_calls = [
        call for call in drive_service.files.calls if call[0] in {"get_media", "export"}
    ]
    assert content_calls == []
    doc = next(
        r for r in records if r.locator.record_key == collection_setup["ids"]["doc"]
    )
    assert doc.values["campaign"] == "launch"


def test_untagged_files_discoverable_without_app_properties(
    collection_setup: dict[str, Any],
) -> None:
    backend = collection_setup["backend"]
    records = backend.scan(Asset)
    file_ids = {record.locator.record_key for record in records}
    assert collection_setup["ids"]["image"] in file_ids
    assert collection_setup["ids"]["pdf"] in file_ids


def test_find_by_name_returns_all_duplicates(collection_setup: dict[str, Any]) -> None:
    coll = collection_setup["backend"].collection(Asset)
    matches = coll.find_by_name("duplicate")
    assert len(matches) == 2


def test_one_by_name_raises_ambiguous(collection_setup: dict[str, Any]) -> None:
    coll = collection_setup["backend"].collection(Asset)
    with pytest.raises(AmbiguousResourceError, match="duplicate"):
        coll.one_by_name("duplicate")


def test_mime_selector_filters_typed_list(collection_setup: dict[str, Any]) -> None:
    coll = collection_setup["backend"].collection(ImageAsset)
    images = coll.list()
    assert len(images) == 1
    assert images[0].mime_type == "image/png"


def test_inventory_includes_unmodeled_siblings(
    collection_setup: dict[str, Any],
) -> None:
    coll = collection_setup["backend"].collection(Asset)
    inventory = coll.inventory()
    names = {item.name for item in inventory}
    assert "notes.txt" in names


def test_adopt_in_place_without_move(collection_setup: dict[str, Any]) -> None:
    backend = collection_setup["backend"]
    folder_id = collection_setup["folder_id"]
    file_id = collection_setup["ids"]["doc"]
    item = backend._collection.adopt(folder_id=folder_id, file_id=file_id)
    assert item.file_id == file_id


def test_adopt_outside_requires_explicit_move_or_shortcut(
    collection_setup: dict[str, Any],
) -> None:
    backend = collection_setup["backend"]
    outside = collection_setup["drive_service"].files.seed(
        name="Outside",
        mime_type=DOC_MIME,
        parents=[collection_setup["project"].root_folder_id],
    )
    with pytest.raises(UnsupportedOperationError, match="move=True or shortcut=True"):
        backend._collection.adopt(
            folder_id=collection_setup["folder_id"],
            file_id=outside,
        )


def test_move_and_shortcut_semantics(collection_setup: dict[str, Any]) -> None:
    backend = collection_setup["backend"]
    folder_id = collection_setup["folder_id"]
    outside = collection_setup["drive_service"].files.seed(
        name="External",
        mime_type=DOC_MIME,
        parents=[collection_setup["project"].root_folder_id],
    )
    moved = backend._collection.adopt(folder_id=folder_id, file_id=outside, move=True)
    assert folder_id in moved.parents
    outside2 = collection_setup["drive_service"].files.seed(
        name="Another External",
        mime_type=DOC_MIME,
        parents=[collection_setup["project"].root_folder_id],
    )
    shortcut = backend._collection.adopt(
        folder_id=folder_id,
        file_id=outside2,
        shortcut=True,
    )
    assert shortcut.is_shortcut
    assert shortcut.shortcut_target_id == outside2


def test_detach_moves_without_deleting(collection_setup: dict[str, Any]) -> None:
    backend = collection_setup["backend"]
    coll = backend.collection(Asset)
    file_id = collection_setup["ids"]["image"]
    dest = collection_setup["drive_service"].files.seed(
        name="Archive",
        mime_type=FOLDER_MIME,
        parents=[collection_setup["project"].root_folder_id],
    )
    coll.detach(file_id, to_folder_id=dest)
    assert file_id in collection_setup["drive_service"].files.records
    assert dest in collection_setup["drive_service"].files.records[file_id]["parents"]


def test_trash_vs_permanent_delete(collection_setup: dict[str, Any]) -> None:
    backend = collection_setup["backend"]
    coll = backend.collection(Asset)
    temp_id = collection_setup["drive_service"].files.seed(
        name="temp",
        mime_type=DOC_MIME,
        parents=[collection_setup["folder_id"]],
    )
    coll.trash(temp_id)
    assert collection_setup["drive_service"].files.records[temp_id]["trashed"] is True
    coll.delete_permanently(temp_id)
    assert temp_id not in collection_setup["drive_service"].files.records


def test_session_delete_trashes_collection_object(
    collection_setup: dict[str, Any],
) -> None:
    backend = collection_setup["backend"]
    file_id = collection_setup["ids"]["pdf"]
    target = backend.collection(Asset).get(file_id)
    with Session(backend) as session:
        session.delete(target)
        session.commit()
    assert collection_setup["drive_service"].files.records[file_id]["trashed"] is True


def test_metadata_conflict_detected(collection_setup: dict[str, Any]) -> None:
    metadata: InMemoryCollectionMetadataStore = collection_setup["metadata"]
    file_id = collection_setup["ids"]["doc"]
    # Simulate external edit bumping version without matching fingerprint expectation.
    metadata.rows[file_id] = CollectionMetadataRow(
        file_id=file_id,
        values={"campaign": "edited-externally", "approved": True},
        version="2",
        fingerprint="changed",
    )
    backend = collection_setup["backend"]
    record = backend.read(
        Asset,
        __import__("gdm.backends.base", fromlist=["RecordLocator"]).RecordLocator(
            storage="collection",
            resource_id=collection_setup["folder_id"],
            record_key=file_id,
        ),
    )
    record.metadata_version = "1"
    record.metadata_fingerprint = "fp-doc"
    with pytest.raises(ConflictError):
        backend.write(Asset, record, expected_version=record.version)


def test_metadata_orphan_reconcile(collection_setup: dict[str, Any]) -> None:
    metadata: InMemoryCollectionMetadataStore = collection_setup["metadata"]
    metadata.upsert_row("orphan-id", {"campaign": "ghost"})
    coll = collection_setup["backend"].collection(Asset)
    orphans, missing = coll.reconcile_metadata()
    assert "orphan-id" in orphans
    assert (
        collection_setup["ids"]["slide"] in missing
        or collection_setup["ids"]["slide"] not in metadata.rows
    )


def test_native_doc_content_handle(collection_setup: dict[str, Any]) -> None:
    coll = collection_setup["backend"].collection(Asset)
    handle = coll.content(collection_setup["ids"]["doc"])
    docs = handle.google_doc()
    doc = docs.get()
    assert doc["documentId"] == collection_setup["ids"]["doc"]
    docs.batch_update([{"insertText": {"location": {"index": 1}, "text": "Hi"}}])


def test_native_slides_content_handle(collection_setup: dict[str, Any]) -> None:
    coll = collection_setup["backend"].collection(Asset)
    handle = coll.content(collection_setup["ids"]["slide"])
    slides = handle.google_slides()
    presentation = slides.get()
    assert presentation["presentationId"] == collection_setup["ids"]["slide"]


def test_binary_download_and_upload(collection_setup: dict[str, Any]) -> None:
    coll = collection_setup["backend"].collection(Asset)
    image_id = collection_setup["ids"]["image"]
    collection_setup["drive_service"].files.content[image_id] = b"\x89PNG"
    handle = coll.content(image_id)
    assert handle.download() == b"\x89PNG"
    handle.upload(b"new-bytes", mime_type="image/png")
    assert collection_setup["drive_service"].files.content[image_id] == b"new-bytes"


def test_native_workspace_rejects_byte_upload(collection_setup: dict[str, Any]) -> None:
    coll = collection_setup["backend"].collection(Asset)
    handle = coll.content(collection_setup["ids"]["doc"])
    with pytest.raises(UnsupportedOperationError, match="cannot be replaced"):
        handle.upload(b"x", mime_type="text/plain")


def test_export_portable_view(collection_setup: dict[str, Any]) -> None:
    coll = collection_setup["backend"].collection(Asset)
    handle = coll.content(collection_setup["ids"]["doc"])
    exported = handle.export("application/pdf")
    assert exported == b"exported-native"


def test_nested_recursion_explicit(collection_setup: dict[str, Any]) -> None:
    coll = collection_setup["backend"].collection(Asset)
    nested_items = coll.iter_recursive()
    nested_ids = {item.drive_id for item in nested_items}
    assert collection_setup["ids"]["nested_doc"] in nested_ids


def test_session_collection_crud_metadata_only(
    collection_setup: dict[str, Any],
) -> None:
    backend = collection_setup["backend"]
    drive_service = collection_setup["drive_service"]
    drive_service.files.calls.clear()
    target = backend.collection(Asset).get(collection_setup["ids"]["doc"])
    with Session(backend) as session:
        target.campaign = "updated"
        session.add(target)
        session.commit()
    media_calls = [
        c for c in drive_service.files.calls if c[0] in {"get_media", "export"}
    ]
    assert media_calls == []


def test_shortcut_identity_preserved(collection_setup: dict[str, Any]) -> None:
    coll = collection_setup["backend"].collection(Asset)
    inventory = coll.inventory()
    shortcut = next(
        i for i in inventory if i.file_id == collection_setup["ids"]["shortcut"]
    )
    assert shortcut.is_shortcut
    assert shortcut.shortcut_target_id == collection_setup["ids"]["doc"]


def test_read_only_drive_field_rejects_write(collection_setup: dict[str, Any]) -> None:
    target = (
        collection_setup["backend"]
        .collection(Asset)
        .get(collection_setup["ids"]["doc"])
    )
    target.mime_type = "application/pdf"
    with Session(collection_setup["backend"]) as session:
        session.add(target)
        with pytest.raises(PartialCommitError) as excinfo:
            session.commit()
    assert isinstance(excinfo.value.cause, UnsupportedOperationError)
    assert "read-only" in str(excinfo.value.cause)


def test_get_by_file_id(collection_setup: dict[str, Any]) -> None:
    coll = collection_setup["backend"].collection(Asset)
    item = coll.get(collection_setup["ids"]["doc"])
    assert item.campaign == "launch"


def test_get_missing_raises(collection_setup: dict[str, Any]) -> None:
    coll = collection_setup["backend"].collection(Asset)
    with pytest.raises(NotFoundError):
        coll.get("missing-id")


def test_create_native_and_upload(collection_setup: dict[str, Any]) -> None:
    coll = collection_setup["backend"].collection(Asset)
    created = coll.create_native("New Doc", kind="document")
    assert created.mime_type == DOC_MIME
    uploaded = coll.upload("photo.jpg", b"jpeg", mime_type="image/jpeg")
    assert uploaded.mime_type == "image/jpeg"


def test_unsupported_native_kind(collection_setup: dict[str, Any]) -> None:
    coll = collection_setup["backend"].collection(Asset)
    with pytest.raises(UnsupportedOperationError, match="Unsupported native kind"):
        coll.create_native("Bad", kind="not-a-type")


class RoutedAsset(GDMModel, storage="collection", schema_version="3"):
    drive_id: str = Field(drive="id")
    name: str = Field(drive="name")
    flag: str | None = Field(default=None, app_property="flag")


def test_app_property_and_name_update(collection_setup: dict[str, Any]) -> None:
    backend = collection_setup["backend"]
    folder_id = collection_setup["folder_id"]
    backend.bind_model(
        RoutedAsset,
        RegistryEntry(
            model_name=RoutedAsset.gdm_model_name(),
            resource_id=folder_id,
            kind=ResourceKind.FOLDER,
        ),
    )
    backend.register_metadata_store(
        RoutedAsset.gdm_model_name(),
        InMemoryCollectionMetadataStore(),
    )
    coll = backend.collection(RoutedAsset)
    doc = coll.create_native("Routed", kind="document")
    doc.flag = "yes"
    with Session(backend) as session:
        session.add(doc)
        session.commit()
    props = collection_setup["drive_service"].files.records[doc.drive_id][
        "appProperties"
    ]
    assert props.get("gdm_schema") == "3"


def test_where_mime_subcollection_and_move(collection_setup: dict[str, Any]) -> None:
    coll = collection_setup["backend"].collection(Asset)
    images = coll.where_mime("image/*")
    assert "image/*" in images.mime_patterns
    nested_id = collection_setup["ids"]["nested"]
    sub = coll.subcollection(nested_id)
    assert sub.folder_id == nested_id
