"""Regression tests for collection/native-content runtime hardening."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from gdm.backends.drive import DriveBackend
from gdm.backends.registry import RegistryBackend
from gdm.backends.sheets import SheetsBackend
from gdm.client import GDMClient
from gdm.collection.constants import DOC_MIME, FOLDER_MIME
from gdm.collection.metadata import (
    CollectionMetadataRow,
    InMemoryCollectionMetadataStore,
    SheetsCollectionMetadataStore,
)
from gdm.errors import (
    AmbiguousResourceError,
    ConflictError,
    ForeignProjectError,
    InvalidDrivePropertyError,
    InvalidModelConfigError,
    PartialCommitError,
)
from gdm.fields import Field
from gdm.google_api import ExecutingDocsService, wrap_docs_service
from gdm.model import DriveObjectMixin, GDMModel
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


class RequiredMeta(GDMModel, storage="collection"):
    drive_id: str = Field(drive="id")
    name: str = Field(drive="name")
    status: str = Field(metadata="status")


class PropertyAsset(GDMModel, storage="collection"):
    drive_id: str = Field(drive="id")
    name: str = Field(drive="name")
    public_tag: str | None = Field(default=None, property="public_tag")
    route_flag: str | None = Field(default=None, app_property="route_flag")


class MixinAsset(GDMModel, DriveObjectMixin, storage="collection"):
    campaign: str | None = Field(default=None, metadata="campaign")


class DiscoveryRequest:
    """Minimal discovery ``HttpRequest`` stand-in."""

    def __init__(self, payload: Any, *, on_execute: Any | None = None) -> None:
        self._payload = payload
        self._on_execute = on_execute
        self.execute_calls = 0

    def execute(self) -> Any:
        self.execute_calls += 1
        if self._on_execute is not None:
            self._on_execute()
        return self._payload


class DiscoveryShapedDriveFiles:
    """Drive files fake returning discovery requests with ``.execute``."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.execute_calls = 0

    def list(self, **kwargs: Any) -> DiscoveryRequest:
        def _on_execute() -> None:
            self.execute_calls += 1

        return DiscoveryRequest(self._inner.list(**kwargs), on_execute=_on_execute)

    def get(self, **kwargs: Any) -> DiscoveryRequest:
        def _on_execute() -> None:
            self.execute_calls += 1

        return DiscoveryRequest(self._inner.get(**kwargs), on_execute=_on_execute)

    def create(self, **kwargs: Any) -> DiscoveryRequest:
        def _on_execute() -> None:
            self.execute_calls += 1

        return DiscoveryRequest(self._inner.create(**kwargs), on_execute=_on_execute)

    def update(self, **kwargs: Any) -> DiscoveryRequest:
        def _on_execute() -> None:
            self.execute_calls += 1

        return DiscoveryRequest(self._inner.update(**kwargs), on_execute=_on_execute)

    def export(self, **kwargs: Any) -> DiscoveryRequest:
        def _on_execute() -> None:
            self.execute_calls += 1

        return DiscoveryRequest(self._inner.export(**kwargs), on_execute=_on_execute)

    def get_media(self, **kwargs: Any) -> DiscoveryRequest:
        def _on_execute() -> None:
            self.execute_calls += 1

        return DiscoveryRequest(self._inner.get_media(**kwargs), on_execute=_on_execute)


@pytest.fixture
def backend_setup() -> dict[str, Any]:
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
    folder_id = drive_service.files.seed(
        name="Assets",
        mime_type=FOLDER_MIME,
        parents=[root_id],
    )
    ids = seed_mixed_collection(drive_service.files, folder_id)
    metadata = InMemoryCollectionMetadataStore(
        rows={
            ids["doc"]: CollectionMetadataRow(
                file_id=ids["doc"],
                values={"campaign": "launch"},
                version="1",
                fingerprint="fp-doc",
            )
        }
    )
    drive = DriveBackend(drive_service, project_id=str(project.project_id))
    sheets = SheetsBackend(FakeSheetsService([["_gdm_file_id", "_gdm_version"]]))
    backend = RegistryBackend(
        project=project,
        drive=drive,
        sheets=sheets,
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
            resource_id=folder_id,
            kind=ResourceKind.FOLDER,
        ),
    )
    backend.bind_model(
        PropertyAsset,
        RegistryEntry(
            model_name=PropertyAsset.gdm_model_name(),
            resource_id=folder_id,
            kind=ResourceKind.FOLDER,
        ),
    )
    backend.bind_model(
        RequiredMeta,
        RegistryEntry(
            model_name=RequiredMeta.gdm_model_name(),
            resource_id=folder_id,
            kind=ResourceKind.FOLDER,
        ),
    )
    backend.register_metadata_store(
        PropertyAsset.gdm_model_name(), InMemoryCollectionMetadataStore()
    )
    backend.register_metadata_store(
        RequiredMeta.gdm_model_name(), InMemoryCollectionMetadataStore()
    )
    return {
        "backend": backend,
        "folder_id": folder_id,
        "ids": ids,
        "drive_service": drive_service,
        "metadata": metadata,
        "project": project,
        "drive": drive,
        "sheets": sheets,
    }


def test_materialized_object_content_lazy_handle(backend_setup: dict[str, Any]) -> None:
    coll = backend_setup["backend"].collection(Asset)
    doc = coll.get(backend_setup["ids"]["doc"])
    backend_setup["drive_service"].files.calls.clear()
    payload = doc.content.google_doc().get()
    assert payload["documentId"] == backend_setup["ids"]["doc"]
    media_calls = [
        c
        for c in backend_setup["drive_service"].files.calls
        if c[0] in {"get_media", "export"}
    ]
    assert media_calls == []


def test_collection_foreign_objects_require_adoption_mode(
    backend_setup: dict[str, Any],
) -> None:
    file_id = backend_setup["ids"]["image"]
    backend_setup["drive_service"].files.records[file_id]["appProperties"] = {
        "gdm_project": str(uuid.uuid4())
    }
    coll = backend_setup["backend"].collection(Asset)
    with pytest.raises(ForeignProjectError):
        coll.get(file_id)
    backend_setup["backend"]._collection.adoption_mode = True
    assert coll.get(file_id).drive_id == file_id


def test_where_mime_list_filters_results(backend_setup: dict[str, Any]) -> None:
    coll = backend_setup["backend"].collection(Asset)
    images = coll.where_mime("image/*").list()
    assert len(images) == 1
    assert images[0].mime_type == "image/png"


def test_mime_filter_preserves_model_config(backend_setup: dict[str, Any]) -> None:
    coll = backend_setup["backend"].collection(Asset)
    images = coll.where_mime("image/*")
    assert images.model is Asset
    assert images.model.gdm_storage() == "collection"
    assert images.model.gdm_model_name() == Asset.gdm_model_name()
    assert images.model.gdm_mapping() == Asset.gdm_mapping()
    assert "image/*" in images.mime_patterns
    image = images.list()[0]
    with Session(backend_setup["backend"]) as session:
        image.campaign = "filtered"
        session.add(image)
        session.commit()
    reloaded = coll.get(image.drive_id)
    assert reloaded.campaign == "filtered"


def test_upload_requires_metadata(backend_setup: dict[str, Any]) -> None:
    coll = backend_setup["backend"].collection(RequiredMeta)
    with pytest.raises(InvalidModelConfigError, match="Missing required metadata"):
        coll.upload("needs-meta.jpg", b"bytes", mime_type="image/jpeg")


def test_drive_conflict_on_external_edit(backend_setup: dict[str, Any]) -> None:
    drive_service = backend_setup["drive_service"]
    coll = backend_setup["backend"].collection(Asset)
    doc = coll.get(backend_setup["ids"]["doc"])
    record = drive_service.files.records[backend_setup["ids"]["doc"]]
    record["version"] = str(int(record.get("version", "1")) + 1)
    with Session(backend_setup["backend"]) as session:
        doc.campaign = "stale"
        session.add(doc)
        with pytest.raises(ConflictError, match="Drive conflict"):
            session.commit()


def test_collection_delete_rejects_external_edit(
    backend_setup: dict[str, Any],
) -> None:
    drive_service = backend_setup["drive_service"]
    coll = backend_setup["backend"].collection(Asset)
    doc = coll.get(backend_setup["ids"]["doc"])
    drive_service.files.records[doc.drive_id]["version"] = "2"
    with Session(backend_setup["backend"]) as session:
        session.delete(doc)
        with pytest.raises(ConflictError, match="Drive conflict"):
            session.commit()
    assert drive_service.files.records[doc.drive_id]["trashed"] is False


def test_content_upload_refreshes_model_drive_snapshot(
    backend_setup: dict[str, Any],
) -> None:
    coll = backend_setup["backend"].collection(Asset)
    image = coll.get(backend_setup["ids"]["image"])
    image.content.upload(b"replacement", mime_type="image/png", resumable=False)
    assert image._gdm_state.drive_version == "2"
    with Session(backend_setup["backend"]) as session:
        image.campaign = "after-upload"
        session.add(image)
        session.commit()
    assert coll.get(image.drive_id).campaign == "after-upload"


def test_client_collection_uses_remote_metadata_store(
    backend_setup: dict[str, Any],
) -> None:
    project = backend_setup["project"]
    folder_id = backend_setup["folder_id"]

    class RemoteAsset(GDMModel, storage="collection"):
        drive_id: str = Field(drive="id")
        name: str = Field(drive="name")

    backend_setup["backend"].bind_model(
        RemoteAsset,
        RegistryEntry(
            model_name=RemoteAsset.gdm_model_name(),
            resource_id=folder_id,
            kind=ResourceKind.FOLDER,
        ),
    )
    client = GDMClient(
        project=project,
        drive=backend_setup["drive"],
        sheets=backend_setup["sheets"],
        registry_backend=backend_setup["backend"],
        drive_service=backend_setup["drive_service"],  # type: ignore[arg-type]
    )
    coll = client.collection(RemoteAsset)
    store = coll._metadata_store
    assert isinstance(store, SheetsCollectionMetadataStore)
    assert not isinstance(store, InMemoryCollectionMetadataStore)


def test_discovery_shaped_drive_executes_once_per_call() -> None:
    from gdm.collection.inventory import DriveInventory
    from gdm.retry import execute_or_return

    inner = CollectionMemoryDriveService()
    folder = inner.files.seed(name="F", mime_type=FOLDER_MIME, parents=[])
    inner.files.seed(name="Doc", mime_type=DOC_MIME, parents=[folder])
    shaped = DiscoveryShapedDriveFiles(inner.files)

    class ShapedService:
        files = shaped

    listed = DriveInventory(
        shaped,
        folder_id=folder,
        call=lambda func: execute_or_return(func()),
    ).scan(scan_limit=10)
    assert len(listed) == 1
    assert shaped.execute_calls == 1


def test_wrapped_and_raw_docs_services_execute_once() -> None:
    raw = FakeDocsService()
    wrapped = wrap_docs_service(raw)
    assert isinstance(wrapped, ExecutingDocsService)
    raw.documents().get(documentId="doc-1").execute()
    assert raw.documents().calls == [("get", {"documentId": "doc-1"})]
    wrapped.documents().get(documentId="doc-2")
    assert ("get", {"documentId": "doc-2"}) in raw.documents().calls


def test_public_and_app_properties_are_separate(
    backend_setup: dict[str, Any],
) -> None:
    drive_service = backend_setup["drive_service"]
    coll = backend_setup["backend"].collection(PropertyAsset)
    created = coll.create_native("Tagged", kind="document")
    created.public_tag = "visible"
    created.route_flag = "internal"
    with Session(backend_setup["backend"]) as session:
        session.add(created)
        session.commit()
    record = drive_service.files.records[created.drive_id]
    assert record["properties"]["public_tag"] == "visible"
    assert record["appProperties"]["route_flag"] == "internal"
    assert "public_tag" not in record["appProperties"]


def test_property_patch_preserves_unrelated_keys(backend_setup: dict[str, Any]) -> None:
    drive_service = backend_setup["drive_service"]
    coll = backend_setup["backend"].collection(PropertyAsset)
    created = coll.create_native("Props", kind="document")
    file_id = created.drive_id
    drive_service.files.records[file_id]["properties"] = {
        "public_tag": "keep",
        "other": "untouched",
    }
    item = coll.get(file_id)
    item.public_tag = "keep"
    with Session(backend_setup["backend"]) as session:
        session.add(item)
        session.commit()
    props = drive_service.files.records[file_id]["properties"]
    assert props["other"] == "untouched"


def test_property_limits_enforced(backend_setup: dict[str, Any]) -> None:
    coll = backend_setup["backend"].collection(PropertyAsset)
    created = coll.create_native("Huge", kind="document")
    created.public_tag = "x" * 200
    with Session(backend_setup["backend"]) as session:
        session.add(created)
        with pytest.raises(PartialCommitError) as excinfo:
            session.commit()
    assert isinstance(excinfo.value.cause, InvalidDrivePropertyError)


def test_create_native_requires_metadata(backend_setup: dict[str, Any]) -> None:
    coll = backend_setup["backend"].collection(RequiredMeta)
    with pytest.raises(InvalidModelConfigError, match="Missing required metadata"):
        coll.create_native("Needs status", kind="document")


def test_create_native_writes_metadata_explicitly(
    backend_setup: dict[str, Any],
) -> None:
    coll = backend_setup["backend"].collection(RequiredMeta)
    created = coll.create_native(
        "Ready",
        kind="document",
        metadata={"status": "draft"},
    )
    assert created.status == "draft"


def test_metadata_conflict_on_external_edit(backend_setup: dict[str, Any]) -> None:
    metadata: InMemoryCollectionMetadataStore = backend_setup["metadata"]
    file_id = backend_setup["ids"]["doc"]
    metadata.rows[file_id] = CollectionMetadataRow(
        file_id=file_id,
        values={"campaign": "external"},
        version="9",
        fingerprint="changed",
    )
    backend = backend_setup["backend"]
    record = backend.read(
        Asset,
        __import__("gdm.backends.base", fromlist=["RecordLocator"]).RecordLocator(
            storage="collection",
            resource_id=backend_setup["folder_id"],
            record_key=file_id,
        ),
    )
    record.metadata_version = "1"
    record.metadata_fingerprint = "fp-doc"
    with pytest.raises(ConflictError, match="Metadata conflict"):
        backend.write(Asset, record, expected_version=record.version)


def test_successful_commit_rebinds_for_second_update(
    backend_setup: dict[str, Any],
) -> None:
    coll = backend_setup["backend"].collection(Asset)
    doc = coll.get(backend_setup["ids"]["doc"])
    with Session(backend_setup["backend"]) as session:
        doc.campaign = "first"
        session.add(doc)
        session.commit()
    with Session(backend_setup["backend"]) as session:
        doc.campaign = "second"
        session.add(doc)
        session.commit()
    reloaded = coll.get(doc.drive_id)
    assert reloaded.campaign == "second"


def test_pagination_beyond_first_page(backend_setup: dict[str, Any]) -> None:
    drive_service = backend_setup["drive_service"]
    folder_id = backend_setup["folder_id"]
    for index in range(105):
        drive_service.files.seed(
            name=f"bulk-{index}",
            mime_type=DOC_MIME,
            parents=[folder_id],
        )
    coll = backend_setup["backend"].collection(Asset)
    items = coll.list()
    assert len(items) >= 105


def test_duplicate_names_beyond_first_page(backend_setup: dict[str, Any]) -> None:
    drive_service = backend_setup["drive_service"]
    folder_id = backend_setup["folder_id"]
    for _ in range(105):
        drive_service.files.seed(
            name="dup-page",
            mime_type=DOC_MIME,
            parents=[folder_id],
        )
    coll = backend_setup["backend"].collection(Asset)
    matches = coll.find_by_name("dup-page")
    assert len(matches) == 105


def test_scan_limit_counts_filtered_mime_items(backend_setup: dict[str, Any]) -> None:
    drive_service = backend_setup["drive_service"]
    folder_id = backend_setup["folder_id"]
    for index in range(5):
        drive_service.files.seed(
            name=f"img-{index}",
            mime_type="image/png",
            parents=[folder_id],
        )
    backend = backend_setup["backend"]
    backend.scan_limit = 3

    class ImageOnly(GDMModel, storage="collection", mime="image/*"):
        drive_id: str = Field(drive="id")

    backend.bind_model(
        ImageOnly,
        RegistryEntry(
            model_name=ImageOnly.gdm_model_name(),
            resource_id=folder_id,
            kind=ResourceKind.FOLDER,
        ),
    )
    backend.register_metadata_store(
        ImageOnly.gdm_model_name(), InMemoryCollectionMetadataStore()
    )
    from gdm.errors import ScanLimitExceeded

    with pytest.raises(ScanLimitExceeded):
        backend.collection(ImageOnly).list()


def test_metadata_write_never_touches_media(backend_setup: dict[str, Any]) -> None:
    drive_service = backend_setup["drive_service"]
    coll = backend_setup["backend"].collection(Asset)
    doc = coll.get(backend_setup["ids"]["doc"])
    drive_service.files.calls.clear()
    with Session(backend_setup["backend"]) as session:
        doc.campaign = "no-media"
        session.add(doc)
        session.commit()
    media_updates = [
        call
        for call in drive_service.files.calls
        if call[0] == "update" and call[1].get("media")
    ]
    assert media_updates == []


def test_move_binds_destination_folder(backend_setup: dict[str, Any]) -> None:
    drive_service = backend_setup["drive_service"]
    dest = drive_service.files.seed(
        name="Dest",
        mime_type=FOLDER_MIME,
        parents=[backend_setup["project"].root_folder_id],
    )
    coll = backend_setup["backend"].collection(Asset)
    file_id = backend_setup["ids"]["image"]
    moved = coll.move(file_id, to_folder_id=dest)
    assert moved.gdm_locator is not None
    assert moved.gdm_locator.resource_id == dest


def test_detach_binds_destination_folder(backend_setup: dict[str, Any]) -> None:
    drive_service = backend_setup["drive_service"]
    dest = drive_service.files.seed(
        name="Archive",
        mime_type=FOLDER_MIME,
        parents=[backend_setup["project"].root_folder_id],
    )
    coll = backend_setup["backend"].collection(Asset)
    file_id = backend_setup["ids"]["image"]
    detached = coll.detach(file_id, to_folder_id=dest)
    assert detached.gdm_locator is not None
    assert detached.gdm_locator.resource_id == dest
    assert file_id in drive_service.files.records


def test_drive_object_mixin_maps_drive_fields() -> None:
    from gdm.fields import field_storage_target

    kind, name = field_storage_target(
        "drive_id", MixinAsset.model_fields["drive_id"], storage="collection"
    )
    assert kind == "drive"
    assert name == "id"


def test_duplicate_metadata_spreadsheet_detected(
    backend_setup: dict[str, Any],
) -> None:
    project = backend_setup["project"]
    collections_root = project.collections_folder_id()
    model_folder = project._ensure_named_subfolder(collections_root, "DupModel")
    drive = backend_setup["drive_service"]
    drive.files.seed(
        name="DupModel-metadata",
        mime_type="application/vnd.google-apps.spreadsheet",
        parents=[model_folder],
    )
    drive.files.seed(
        name="DupModel-metadata-2",
        mime_type="application/vnd.google-apps.spreadsheet",
        parents=[model_folder],
    )
    with pytest.raises(AmbiguousResourceError, match="Multiple metadata spreadsheets"):
        project.ensure_collection_metadata_binding("DupModel")


def test_session_refresh_preserves_collection_state(
    backend_setup: dict[str, Any],
) -> None:
    coll = backend_setup["backend"].collection(Asset)
    doc = coll.get(backend_setup["ids"]["doc"])
    with Session(backend_setup["backend"]) as session:
        session.refresh(doc)
    assert doc._gdm_state.collection_backend is not None
    assert doc._gdm_state.metadata_version is not None
