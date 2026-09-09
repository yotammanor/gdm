"""Opt-in live tests against a real Google Drive account.

Set environment variables before running:

- ``GDM_LIVE=1`` — enable the suite
- ``GDM_TEST_ROOT_FOLDER_ID`` — dedicated Drive root folder ID
- One of:
  - ``GDM_TEST_TOKEN_PATH`` — authorized-user OAuth token JSON
  - ``GDM_TEST_SERVICE_ACCOUNT_FILE`` — service account JSON path

The suite creates and deletes resources tagged with ``gdm_project`` under the
configured root. Never commit credentials or token files.
"""

from __future__ import annotations

import os
import uuid
from contextlib import suppress
from pathlib import Path

import pytest

from gdm.client import GDMClient
from gdm.collection.constants import FOLDER_MIME, SHORTCUT_MIME
from gdm.errors import (
    AmbiguousResourceError,
    ForeignProjectError,
    NotFoundError,
    UnsupportedOperationError,
)
from gdm.fields import Field
from gdm.model import GDMModel
from gdm.project import ResourceKind

pytestmark = pytest.mark.live


def _live_enabled() -> bool:
    return os.environ.get("GDM_LIVE", "").strip().lower() in {"1", "true", "yes"}


def _root_folder_id() -> str | None:
    value = os.environ.get("GDM_TEST_ROOT_FOLDER_ID")
    return value.strip() if value else None


def _credentials_kwargs() -> dict[str, str]:
    token = os.environ.get("GDM_TEST_TOKEN_PATH")
    if token:
        return {"token_path": token}
    sa = os.environ.get("GDM_TEST_SERVICE_ACCOUNT_FILE")
    if sa:
        return {"service_account_file": sa}
    return {}


def _require_live() -> tuple[str, dict[str, str]]:
    if not _live_enabled():
        pytest.skip("Set GDM_LIVE=1 to run live tests")
    root = _root_folder_id()
    if not root:
        pytest.skip("GDM_TEST_ROOT_FOLDER_ID is required for live tests")
    creds = _credentials_kwargs()
    if not creds:
        pytest.skip("GDM_TEST_TOKEN_PATH or GDM_TEST_SERVICE_ACCOUNT_FILE is required")
    return root, creds


@pytest.fixture(scope="module")
def live_client() -> GDMClient:
    root, creds = _require_live()
    try:
        return GDMClient.open(root_folder_id=root, **creds)
    except NotFoundError:
        return GDMClient.create(root_folder_id=root, **creds)


@pytest.fixture
def live_collection_folders(live_client: GDMClient) -> tuple[str, str]:
    """Create isolated collection/source folders and always remove them."""
    folder_ids: list[str] = []
    for label in ("collection", "outside"):
        created = live_client.drive_service.files.create(
            body={
                "name": f"gdm-live-{label}-{uuid.uuid4()}",
                "mimeType": FOLDER_MIME,
                "parents": [live_client.project.root_folder_id],
            },
            fields="id",
        )
        folder_ids.append(str(created["id"]))
    try:
        yield folder_ids[0], folder_ids[1]
    finally:
        for folder_id in reversed(folder_ids):
            with suppress(NotFoundError):
                live_client.drive.delete(folder_id)


class LiveNote(GDMModel, storage="file", codec="json", mapping="partial"):
    id: str | None = Field(default=None, primary_key=True)
    title: str


class LiveRow(GDMModel, storage="sheet", mapping="partial"):
    id: str | None = Field(default=None, primary_key=True)
    value: str
    asset_id: str = Field(foreign_key="LiveAsset.drive_id", drive_link=True)


class LiveAsset(GDMModel, storage="collection", mapping="partial"):
    drive_id: str = Field(drive="id")
    name: str = Field(drive="name")
    mime_type: str = Field(drive="mimeType")
    parents: list[str] = Field(drive="parents")
    shortcut_details: dict[str, str | None] | None = Field(
        default=None,
        drive="shortcutDetails",
    )
    tag: str | None = Field(default=None, metadata="tag")


def test_live_collection_native_objects(
    live_client: GDMClient,
    live_collection_folders: tuple[str, str],
) -> None:
    """Create native workspace objects; verify metadata queries stay metadata-only."""
    folder_id, _ = live_collection_folders
    live_client.bind(
        LiveAsset,
        resource_id=folder_id,
        kind=ResourceKind.FOLDER,
    )
    assets = live_client.collection(LiveAsset)
    created_ids: list[str] = []
    metadata_id: str | None = None
    try:
        doc = assets.create_native(f"live-doc-{uuid.uuid4()}", kind="document")
        slide = assets.create_native(f"live-slide-{uuid.uuid4()}", kind="slide")
        sheet = assets.create_native(f"live-sheet-{uuid.uuid4()}", kind="sheet")
        image = assets.upload(
            f"live-image-{uuid.uuid4()}.png",
            b"\x89PNG\r\n",
            mime_type="image/png",
        )
        pdf = assets.upload(
            f"live-pdf-{uuid.uuid4()}.pdf",
            b"%PDF-1.4\n",
            mime_type="application/pdf",
        )
        nested = assets.create_native(f"live-nested-{uuid.uuid4()}", kind="folder")
        created_ids.extend(
            [
                doc.drive_id,
                slide.drive_id,
                sheet.drive_id,
                image.drive_id,
                pdf.drive_id,
                nested.drive_id,
            ]
        )

        nested_doc = assets.subcollection(nested.drive_id).create_native(
            f"live-nested-doc-{uuid.uuid4()}",
            kind="document",
        )
        created_ids.append(nested_doc.drive_id)
        direct_ids = {item.drive_id for item in assets.list()}
        assert nested_doc.drive_id not in direct_ids
        assert nested_doc.drive_id in {
            item.drive_id for item in assets.iter_recursive()
        }
        assert [item.drive_id for item in assets.where_mime("image/*").list()] == [
            image.drive_id
        ]

        original_mime = doc.mime_type
        loaded = assets.get(doc.drive_id)
        assert loaded.mime_type == original_mime
        loaded.tag = "live-test"
        with live_client.session() as session:
            session.add(loaded)
            session.commit()
        metadata_id = doc.drive_id
        reloaded = assets.get(doc.drive_id)
        assert reloaded.tag == "live-test"
        assert reloaded.mime_type == original_mime

        doc_content = reloaded.content
        assert doc_content.export("application/pdf")
        doc_content.google_doc().batch_update(
            [{"insertText": {"location": {"index": 1}, "text": "live"}}]
        )
        assets.content(slide.drive_id).google_slides().batch_update(
            [{"createSlide": {"insertionIndex": 0, "slideLayoutReference": {}}}]
        )
        native_sheet = assets.content(sheet.drive_id).google_sheet()
        assert native_sheet.get()["spreadsheetId"] == sheet.drive_id
        native_sheet.batch_update(
            [{"addSheet": {"properties": {"title": f"Live-{uuid.uuid4().hex[:8]}"}}}]
        )

        image_content = assets.content(image.drive_id)
        assert image_content.download() == b"\x89PNG\r\n"
        image_content.upload(b"updated-image", mime_type="image/png", resumable=False)
        assert image_content.download() == b"updated-image"
        assert assets.content(pdf.drive_id).download() == b"%PDF-1.4\n"

        dup_name = f"dup-{uuid.uuid4()}"
        first_dup = assets.create_native(dup_name, kind="document")
        second_dup = assets.create_native(dup_name, kind="document")
        created_ids.extend([first_dup.drive_id, second_dup.drive_id])
        assert len(assets.find_by_name(dup_name)) == 2
        with pytest.raises(AmbiguousResourceError):
            assets.one_by_name(dup_name)

        assets.trash(sheet.drive_id)
        trashed = live_client.drive_service.files.get(
            fileId=sheet.drive_id,
            fields="id,trashed",
        )
        assert trashed["trashed"] is True
    finally:
        if metadata_id is not None:
            assets._metadata_store.delete_row(metadata_id)
        for file_id in reversed(created_ids):
            with suppress(NotFoundError):
                assets.delete_permanently(file_id)


def test_live_collection_membership_operations(
    live_client: GDMClient,
    live_collection_folders: tuple[str, str],
) -> None:
    collection_id, outside_id = live_collection_folders
    live_client.bind(
        LiveAsset,
        resource_id=collection_id,
        kind=ResourceKind.FOLDER,
    )
    assets = live_client.collection(LiveAsset)
    created_ids: list[str] = []
    try:
        source = live_client.drive_service.files.create(
            body={
                "name": f"source-{uuid.uuid4()}",
                "mimeType": "application/vnd.google-apps.document",
                "parents": [outside_id],
            },
            fields=assets._backend.DEFAULT_FIELDS,
        )
        source_id = str(source["id"])
        created_ids.append(source_id)
        with pytest.raises(UnsupportedOperationError):
            assets.adopt(source_id)

        shortcut = assets.adopt(source_id, shortcut=True)
        created_ids.append(shortcut.drive_id)
        assert shortcut.mime_type == SHORTCUT_MIME
        assert shortcut.shortcut_details is not None
        assert shortcut.shortcut_details["targetId"] == source_id

        moving = live_client.drive_service.files.create(
            body={
                "name": f"moving-{uuid.uuid4()}",
                "mimeType": "application/vnd.google-apps.document",
                "parents": [outside_id],
            },
            fields=assets._backend.DEFAULT_FIELDS,
        )
        moving_id = str(moving["id"])
        created_ids.append(moving_id)
        adopted = assets.adopt(moving_id, move=True)
        assert adopted.parents == [collection_id]

        detached = assets.detach(moving_id, to_folder_id=outside_id)
        assert detached.parents == [outside_id]
        assert detached.gdm_locator.resource_id == outside_id

        direct = assets.create_native(f"direct-{uuid.uuid4()}", kind="document")
        created_ids.append(direct.drive_id)
        moved = assets.move(direct.drive_id, to_folder_id=outside_id)
        assert moved.parents == [outside_id]
        assert moved.gdm_locator.resource_id == outside_id
    finally:
        for file_id in reversed(created_ids):
            with suppress(NotFoundError):
                live_client.drive.delete(file_id)


def test_live_sheet_relationship_and_prefetch(
    live_client: GDMClient,
    live_collection_folders: tuple[str, str],
) -> None:
    collection_id, _ = live_collection_folders
    live_client.bind(
        LiveAsset,
        resource_id=collection_id,
        kind=ResourceKind.FOLDER,
    )
    assets = live_client.collection(LiveAsset)
    created_ids: list[str] = []
    metadata_id: str | None = None
    row_id: str | None = None
    try:
        asset = assets.create_native(
            f"related-{uuid.uuid4()}",
            kind="document",
            metadata={"tag": "relation"},
        )
        table = assets.create_native(f"rows-{uuid.uuid4()}", kind="sheet")
        created_ids.extend([asset.drive_id, table.drive_id])
        metadata_id = asset.drive_id

        live_client.bind(
            LiveRow,
            resource_id=table.drive_id,
            kind=ResourceKind.WORKSHEET,
            worksheet="Sheet1",
        )
        with live_client.session() as session:
            row = LiveRow(value="linked", asset_id=asset.drive_id)
            session.add(row)
            session.commit()
            assert row.id is not None
            row_id = row.id
            loaded = session.get(LiveRow, row.id)
            assert loaded is not None
            related = session.prefetch(LiveAsset, [loaded.asset_id])
            assert related[asset.drive_id].name == asset.name
            session.delete(loaded)
            session.commit()

        headers = live_client.sheets.get_headers(table.drive_id, "Sheet1").headers
        assert "asset_id" in headers
        assert "_gdm_version" in headers
    finally:
        if row_id is not None:
            with suppress(NotFoundError):
                with live_client.session() as session:
                    leftover = session.get(LiveRow, row_id)
                    if leftover is not None:
                        session.delete(leftover)
                        session.commit()
        if metadata_id is not None:
            assets._metadata_store.delete_row(metadata_id)
        for file_id in reversed(created_ids):
            with suppress(NotFoundError):
                assets.delete_permanently(file_id)


def test_live_collection_metadata_migration_preserves_native_content(
    live_client: GDMClient,
    live_collection_folders: tuple[str, str],
    tmp_path: Path,
) -> None:
    collection_id, _ = live_collection_folders
    live_client.bind(
        LiveAsset,
        resource_id=collection_id,
        kind=ResourceKind.FOLDER,
    )
    assets = live_client.collection(LiveAsset)
    image = assets.upload(
        f"migration-{uuid.uuid4()}.bin",
        b"stable-content",
        mime_type="application/octet-stream",
        metadata={"tag": "before"},
    )
    try:
        before = live_client.drive.get_metadata(image.drive_id)
        reviewer_column = "live_reviewer"
        owner_column = "live_owner"
        target = live_client.migration_target(
            bindings={LiveAsset.gdm_model_name(): collection_id},
            indexes_dir=str(tmp_path / "indexes"),
        )
        existing_headers = target.metadata_headers(LiveAsset.gdm_model_name())
        if owner_column in existing_headers and reviewer_column not in existing_headers:
            target.metadata_rename_column(
                LiveAsset.gdm_model_name(),
                owner_column,
                reviewer_column,
            )
        changed = target.metadata_add_column(
            LiveAsset.gdm_model_name(),
            reviewer_column,
            "unassigned",
        )
        assert isinstance(changed, int)
        assert (
            target.metadata_add_column(
                LiveAsset.gdm_model_name(),
                reviewer_column,
                "unassigned",
            )
            == 0
        )
        assert reviewer_column in target.metadata_headers(LiveAsset.gdm_model_name())
        target.metadata_rename_column(
            LiveAsset.gdm_model_name(),
            reviewer_column,
            owner_column,
        )
        assert owner_column in target.metadata_headers(LiveAsset.gdm_model_name())
        after = live_client.drive.get_metadata(image.drive_id)
        # Drive can finalize a fresh upload asynchronously and bump its
        # metadata version even when GDM only writes the sidecar Sheet.
        assert after.mime_type == before.mime_type
        assert after.md5_checksum == before.md5_checksum
        assert assets.content(image.drive_id).download() == b"stable-content"
    finally:
        assets._metadata_store.delete_row(image.drive_id)
        assets.delete_permanently(image.drive_id)


def test_live_project_open(live_client: GDMClient) -> None:
    assert live_client.project.gdm_folder_id is not None


def test_live_json_model_crud(live_client: GDMClient) -> None:
    folder_id = live_client.project.folder_id_for("indexes")
    live_client.bind(
        LiveNote,
        resource_id=folder_id,
        kind=ResourceKind.FOLDER,
    )
    key: str | None = None
    try:
        with live_client.session() as session:
            note = LiveNote(title=f"live-{uuid.uuid4()}")
            session.add(note)
            session.commit()
            key = note.id
            assert key is not None
            loaded = session.get(LiveNote, key)
            assert loaded is not None
            loaded.title = "updated"
            session.add(loaded)
            session.commit()
            session.delete(loaded)
            session.commit()
    finally:
        if key is not None:
            with suppress(NotFoundError):
                with live_client.session() as cleanup:
                    leftover = cleanup.get(LiveNote, key)
                    if leftover is not None:
                        cleanup.delete(leftover)
                        cleanup.commit()


def test_live_explorer(live_client: GDMClient) -> None:
    report = live_client.explore(max_resources=50)
    assert report.inventory.total_count >= 1


def test_live_permissions_and_changes(live_client: GDMClient) -> None:
    caps = live_client.permissions.get_capabilities(live_client.project.root_folder_id)
    assert caps.can_download is not None
    checkpoint = live_client.changes.initial_checkpoint()
    assert checkpoint.start_page_token


def test_live_root_isolation_requires_separate_roots(
    live_client: GDMClient,
) -> None:
    temporary_roots: list[str] = []
    try:
        for suffix in ("a", "b"):
            created = live_client.drive.service.files.create(
                body={
                    "name": f"gdm-isolation-{suffix}-{uuid.uuid4()}",
                    "mimeType": FOLDER_MIME,
                    "parents": [live_client.project.root_folder_id],
                },
                fields="id",
                supportsAllDrives=True,
            )
            temporary_roots.append(str(created["id"]))

        first = GDMClient.create(
            root_folder_id=temporary_roots[0],
            credentials=live_client.credentials,
        )
        second = GDMClient.create(
            root_folder_id=temporary_roots[1],
            credentials=live_client.credentials,
        )
        assert first.project.project_id != second.project.project_id
        reopened = GDMClient.open(
            root_folder_id=temporary_roots[0],
            credentials=live_client.credentials,
        )
        assert reopened.project.project_id == first.project.project_id
        with pytest.raises(ForeignProjectError):
            first.project.assert_resource_belongs_to_project(
                resource_id=temporary_roots[1],
                app_properties={"gdm_project": str(second.project.project_id)},
            )
    finally:
        for folder_id in reversed(temporary_roots):
            with suppress(NotFoundError):
                live_client.drive.delete(folder_id)


def test_live_explorer_collection_candidates(live_client: GDMClient) -> None:
    """Collection-aware explorer output (opt-in live)."""
    report = live_client.explore(max_resources=50)
    assert report.inventory.zero_content_reads is True
    assert report.inventory.content_read_guarantee


def test_live_infer_collection_binding_proposal(live_client: GDMClient) -> None:
    from gdm.infer import infer_from_collection_inventory

    report = live_client.explore(max_resources=50)
    root = live_client.project.root_folder_id
    result = infer_from_collection_inventory(
        report.inventory.resources,
        folder_id=root,
        model_name="LiveFolder",
    )
    assert result.binding.storage == "collection"
    assert result.binding.folder_id == root
