"""Branch coverage for collection backend, inventory, and DriveCollection."""
# ruff: noqa: PT012, RUF059

from __future__ import annotations

import uuid
from typing import Any

import pytest

from gdm.backends.drive import DriveBackend
from gdm.backends.registry import RegistryBackend
from gdm.backends.sheets import SheetsBackend
from gdm.collection.backend import (
    CollectionBackend,
    collection_drive_fingerprint,
    collection_record_key,
)
from gdm.collection.collection import DriveCollection, collection_model_with_mime
from gdm.collection.constants import (
    DOC_MIME,
    FOLDER_MIME,
    MAX_DRIVE_PROPERTIES_PER_FILE,
)
from gdm.collection.inventory import (
    DriveInventory,
    DriveInventoryItem,
    drive_fingerprint,
    inventory_item_from_file,
)
from gdm.errors import (
    AmbiguousResourceError,
    InvalidDrivePropertyError,
    InvalidModelConfigError,
    NotFoundError,
    ScanLimitExceeded,
    UnsupportedOperationError,
)
from gdm.fields import Field
from gdm.model import GDMModel
from gdm.project import Project, RegistryEntry, ResourceKind
from tests.collection_fakes import CollectionMemoryDriveService, seed_mixed_collection
from tests.fakes import FakeSheetsService


class Asset(GDMModel, storage="collection", mapping="partial"):
    drive_id: str = Field(drive="id")
    name: str = Field(drive="name")
    mime_type: str = Field(drive="mimeType")
    campaign: str | None = Field(default=None, metadata="campaign")
    legacy: str | None = Field(default=None, source="legacy_source")


class StrictAsset(GDMModel, storage="collection", mapping="strict"):
    drive_id: str = Field(drive="id")
    name: str = Field(drive="name")
    tag: str | None = Field(default=None, metadata="tag")


class ReadOnlyAsset(GDMModel, storage="collection"):
    drive_id: str = Field(drive="id")
    name: str = Field(drive="name")
    created_time: str | None = Field(default=None, drive="createdTime")


class DocOnly(
    GDMModel, storage="collection", mime="application/vnd.google-apps.document"
):
    drive_id: str = Field(drive="id")
    name: str = Field(drive="name")


@pytest.fixture
def setup() -> dict[str, Any]:
    drive_service = CollectionMemoryDriveService()
    root = drive_service.files.seed(
        file_id="root",
        name="Root",
        mime_type=FOLDER_MIME,
        parents=[],
    )
    project = Project(project_id=uuid.uuid4(), root_folder_id=root, drive=drive_service)
    project.initialize()
    folder = drive_service.files.seed(
        name="Assets",
        mime_type=FOLDER_MIME,
        parents=[root],
    )
    ids = seed_mixed_collection(drive_service.files, folder)
    drive = DriveBackend(drive_service, project_id=str(project.project_id))
    backend = CollectionBackend(drive, project_id=str(project.project_id))
    return {
        "backend": backend,
        "drive": drive,
        "folder": folder,
        "ids": ids,
        "drive_service": drive_service,
        "project": project,
    }


def test_collection_backend_materialize_and_property_helpers(
    setup: dict[str, Any],
) -> None:
    backend: CollectionBackend = setup["backend"]
    item = backend.inventory(setup["folder"]).get(setup["ids"]["doc"])
    mapped, unknown = backend.materialize_values(
        Asset,
        item,
        {"campaign": "x", "legacy_source": "old", "extra_meta": "free"},
    )
    assert mapped["campaign"] == "x"
    assert mapped["legacy"] == "old"
    assert unknown["extra_meta"] == "free"
    assert backend.serialize_property_value(True) == "true"
    assert backend.serialize_property_value(False) == "false"
    with pytest.raises(InvalidDrivePropertyError):
        backend.serialize_property_value(None)
    with pytest.raises(InvalidDrivePropertyError):
        backend.validate_drive_properties(
            {f"k{i}": "v" for i in range(MAX_DRIVE_PROPERTIES_PER_FILE + 1)},
            kind="properties",
        )
    with pytest.raises(InvalidDrivePropertyError):
        backend.validate_drive_properties({"x" * 200: "v"}, kind="properties")
    with pytest.raises(InvalidDrivePropertyError):
        backend.validate_drive_properties({"k": "v" * 200}, kind="properties")
    merged = backend.merge_property_patch({"a": "1"}, {"b": "2"})
    assert merged == {"a": "1", "b": "2"}


def test_collection_backend_write_payloads_and_routing(setup: dict[str, Any]) -> None:
    backend: CollectionBackend = setup["backend"]
    coll = DriveCollection(
        model=Asset,
        folder_id=setup["folder"],
        backend=backend,
        metadata_store=__import__(
            "gdm.collection.metadata", fromlist=["InMemoryCollectionMetadataStore"]
        ).InMemoryCollectionMetadataStore(),
    )
    doc = coll.get(setup["ids"]["doc"])
    drive_body, metadata, props, app_props = backend.build_write_payloads(Asset, doc)
    assert "name" not in drive_body
    doc.campaign = "next"
    _, metadata, _, _ = backend.build_write_payloads(Asset, doc)
    assert metadata["campaign"] == "next"
    routing = backend.routing_app_properties(Asset)
    assert routing["gdm_model"] == Asset.gdm_model_name()
    with pytest.raises(UnsupportedOperationError, match="read-only"):
        ro_coll = DriveCollection(
            model=ReadOnlyAsset,
            folder_id=setup["folder"],
            backend=backend,
            metadata_store=__import__(
                "gdm.collection.metadata", fromlist=["InMemoryCollectionMetadataStore"]
            ).InMemoryCollectionMetadataStore(),
        )
        ro = ro_coll.get(setup["ids"]["doc"])
        ro.created_time = "changed"
        backend.build_write_payloads(ReadOnlyAsset, ro)
    with pytest.raises(UnsupportedOperationError, match="Unsupported drive field"):
        backend._drive_field_value(
            inventory_item_from_file(
                {"id": "x", "name": "x", "mimeType": DOC_MIME, "parents": []}
            ),
            "unsupportedField",
        )


def test_collection_backend_native_adopt_detach_paths(setup: dict[str, Any]) -> None:
    backend: CollectionBackend = setup["backend"]
    folder = setup["folder"]
    external = setup["drive_service"].files.seed(
        name="External",
        mime_type=DOC_MIME,
        parents=[setup["project"].root_folder_id],
    )
    with pytest.raises(UnsupportedOperationError, match="at most one"):
        backend.adopt(folder_id=folder, file_id=external, move=True, shortcut=True)
    with pytest.raises(UnsupportedOperationError, match="move=True or shortcut=True"):
        backend.adopt(folder_id=folder, file_id=external)
    moved = backend.adopt(folder_id=folder, file_id=external, move=True)
    assert folder in moved.parents
    shortcut = setup["drive_service"].files.seed(
        name="Outside",
        mime_type=DOC_MIME,
        parents=[setup["project"].root_folder_id],
    )
    linked = backend.adopt(folder_id=folder, file_id=shortcut, shortcut=True)
    assert linked.is_shortcut
    already = backend.adopt(folder_id=folder, file_id=setup["ids"]["doc"])
    assert already.file_id == setup["ids"]["doc"]
    dest = setup["drive_service"].files.seed(
        name="Dest",
        mime_type=FOLDER_MIME,
        parents=[setup["project"].root_folder_id],
    )
    detached = backend.detach(
        file_id=setup["ids"]["image"],
        collection_folder_id=folder,
        to_folder_id=dest,
    )
    assert dest in detached.parents
    with pytest.raises(UnsupportedOperationError, match="Unsupported native kind"):
        backend.create_native(folder_id=folder, name="n", kind="not-a-kind")
    backend.trash(setup["ids"]["unmodeled"])
    backend.delete_permanently(setup["ids"]["unmodeled"])


def test_collection_backend_matches_and_fingerprints(setup: dict[str, Any]) -> None:
    backend: CollectionBackend = setup["backend"]
    item = backend.inventory(setup["folder"]).get(setup["ids"]["doc"])
    assert backend.matches_model(Asset, item) is True
    assert collection_record_key(item) == item.file_id
    assert collection_drive_fingerprint(item) == drive_fingerprint(item)
    no_pattern = type(
        "NoPattern",
        (GDMModel,),
        {
            "__gdm_storage__": "collection",
            "__gdm_mime_patterns__": (),
        },
    )
    assert backend.matches_model(no_pattern, item) is True


def test_inventory_pagination_scan_and_errors(setup: dict[str, Any]) -> None:
    files = setup["drive_service"].files
    folder = setup["folder"]
    for index in range(105):
        files.seed(name=f"p-{index}", mime_type=DOC_MIME, parents=[folder])
    inv = DriveInventory(files, folder_id=folder)
    page = inv.list_all_children(page_size=50)
    assert len(page.items) == 50
    assert page.next_page_token is not None
    with pytest.raises(ScanLimitExceeded):
        inv.scan(scan_limit=2, include_folders=False)
    filtered = inv.scan(
        scan_limit=200,
        mime_patterns=("image/*",),
    )
    assert all(item.mime_type.startswith("image/") for item in filtered)
    version_item = DriveInventoryItem(
        file_id="v1",
        name="v",
        mime_type="text/plain",
        version="9",
    )
    assert drive_fingerprint(version_item) == "version:9"
    assert (
        drive_fingerprint(
            DriveInventoryItem(file_id="only-id", name="n", mime_type="text/plain")
        )
        == "only-id"
    )
    outside = files.seed(name="Outside", mime_type=DOC_MIME, parents=[folder])
    files.records[outside]["parents"] = [setup["project"].root_folder_id]
    with pytest.raises(NotFoundError, match="not a direct child"):
        inv.get(outside)
    named = inv.find_by_name("Brief", mime_type=DOC_MIME)
    assert named


def test_drive_collection_helpers_and_strict_metadata(setup: dict[str, Any]) -> None:
    from gdm.collection.metadata import InMemoryCollectionMetadataStore

    backend: CollectionBackend = setup["backend"]
    store = InMemoryCollectionMetadataStore()
    coll = DriveCollection(
        model=StrictAsset,
        folder_id=setup["folder"],
        backend=backend,
        metadata_store=store,
    )
    with pytest.raises(InvalidModelConfigError, match="Unknown metadata keys"):
        coll.create_native("S", kind="document", metadata={"unknown_key": "x"})
    filtered_model = collection_model_with_mime(DocOnly, "application/pdf")
    assert "application/pdf" in filtered_model.__gdm_mime_patterns__
    assert filtered_model.gdm_storage() == "collection"
    doc_only = DriveCollection(
        model=DocOnly,
        folder_id=setup["folder"],
        backend=backend,
        metadata_store=store,
    )
    with pytest.raises(NotFoundError, match="does not match"):
        doc_only.get(setup["ids"]["image"])
    with pytest.raises(NotFoundError, match="No modeled file"):
        coll.one_by_name("missing-name")
    for _ in range(3):
        setup["drive_service"].files.seed(
            name="dup-name",
            mime_type=DOC_MIME,
            parents=[setup["folder"]],
        )
    with pytest.raises(AmbiguousResourceError):
        coll.one_by_name("dup-name")
    orphans, missing = coll.reconcile_metadata()
    assert isinstance(orphans, list)
    assert isinstance(missing, list)
    adopted = coll.adopt(
        setup["drive_service"].files.seed(
            name="Adopted",
            mime_type=DOC_MIME,
            parents=[setup["project"].root_folder_id],
        ),
        move=True,
    )
    assert adopted.name == "Adopted"
    coll.trash(adopted.drive_id)
    nested = coll.iter_recursive()
    assert any(item.name == "Inner" for item in nested)


def test_drive_collection_scan_limit_in_list(setup: dict[str, Any]) -> None:
    from gdm.collection.metadata import InMemoryCollectionMetadataStore

    files = setup["drive_service"].files
    folder = setup["folder"]
    for index in range(5):
        files.seed(name=f"limit-{index}", mime_type=DOC_MIME, parents=[folder])
    coll = DriveCollection(
        model=Asset,
        folder_id=folder,
        backend=setup["backend"],
        metadata_store=InMemoryCollectionMetadataStore(),
        scan_limit=2,
    )
    with pytest.raises(ScanLimitExceeded):
        coll.list()


def test_registry_collection_folder_parent_resolution(setup: dict[str, Any]) -> None:
    project = setup["project"]
    drive = setup["drive"]
    sheets = SheetsBackend(FakeSheetsService([["_gdm_file_id", "_gdm_version"]]))
    registry = RegistryBackend(project=project, drive=drive, sheets=sheets)
    child_file = setup["drive_service"].files.seed(
        name="ChildDoc",
        mime_type=DOC_MIME,
        parents=[setup["folder"]],
    )
    registry.bind_model(
        DocOnly,
        RegistryEntry(
            model_name=DocOnly.gdm_model_name(),
            resource_id=child_file,
            kind=ResourceKind.FILE,
            parent_id=setup["folder"],
        ),
    )
    folder_id = registry._collection_folder_id(DocOnly)
    assert folder_id == setup["folder"]
