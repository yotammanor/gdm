"""Branch coverage for collection content handles."""
# ruff: noqa: N803

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gdm.collection.constants import DOC_MIME, SHEET_MIME, SLIDE_MIME
from gdm.collection.content import (
    ContentHandle,
    GoogleDocHandle,
    GoogleSheetContentHandle,
    GoogleSlidesHandle,
)
from gdm.collection.inventory import DriveInventoryItem, inventory_item_from_file
from gdm.errors import ConflictError, UnsupportedOperationError
from gdm.retry import execute_or_return
from tests.collection_fakes import (
    CollectionMemoryDriveFiles,
    FakeDocsService,
    FakeNativeSheetsService,
    FakeSlidesService,
)


def _item(mime: str, file_id: str = "f1") -> DriveInventoryItem:
    return inventory_item_from_file(
        {
            "id": file_id,
            "name": "file",
            "mimeType": mime,
            "version": "1",
            "parents": ["folder"],
        }
    )


def _handle(
    item: DriveInventoryItem,
    *,
    files: Any | None = None,
    docs: Any | None = None,
    slides: Any | None = None,
    sheets: Any | None = None,
) -> ContentHandle:
    drive_files = files or CollectionMemoryDriveFiles()
    return ContentHandle(
        item=item,
        drive_files=drive_files,
        call=lambda func: execute_or_return(func()),
        documents_service=docs,
        slides_service=slides,
        sheets_service=sheets,
    )


def test_binary_download_inline_cache() -> None:
    files = CollectionMemoryDriveFiles()
    files.content["bin-1"] = b"cached-bytes"
    item = _item("image/png", "bin-1")
    handle = _handle(item, files=files)
    assert handle.download() == b"cached-bytes"
    assert handle.content_loaded is True


def test_binary_download_via_get_media() -> None:
    files = CollectionMemoryDriveFiles()
    fid = files.seed(name="remote.pdf", mime_type="application/pdf", parents=["folder"])
    item = _item("application/pdf", fid)

    class Downloader:
        def __init__(self, _buffer: Any, _request: Any) -> None:
            _buffer.write(b"streamed")

        def next_chunk(self, num_retries: int = 0) -> tuple[None, bool]:
            del num_retries
            return None, True

    original_get_media = files.get_media

    def tracked_get_media(**kwargs: Any) -> MagicMock:
        original_get_media(**kwargs)
        request = MagicMock()
        request.next_chunk = Downloader(__import__("io").BytesIO(), request).next_chunk
        return request

    files.get_media = tracked_get_media  # type: ignore[method-assign]
    if fid in files.content:
        del files.content[fid]

    with patch(
        "gdm.collection.content.MediaIoBaseDownload",
        lambda _buffer, _request: Downloader(_buffer, _request),
    ):
        handle = _handle(item, files=files)
        assert handle.download() == b"streamed"
    assert any(call[0] == "get_media" for call in files.calls)


def test_binary_upload_updates_media() -> None:
    files = CollectionMemoryDriveFiles()
    fid = files.seed(name="pic.png", mime_type="image/png", parents=["folder"])
    handle = _handle(_item("image/png", fid), files=files)
    handle.upload(b"new-payload", mime_type="image/png", resumable=False)
    assert files.content[fid] == b"new-payload"
    assert handle.content_loaded is True


def test_binary_upload_rejects_external_change() -> None:
    files = CollectionMemoryDriveFiles()
    fid = files.seed(name="pic.png", mime_type="image/png", parents=["folder"])
    handle = _handle(_item("image/png", fid), files=files)
    files.records[fid]["version"] = "2"
    with pytest.raises(ConflictError, match="Drive content conflict"):
        handle.upload(b"new-payload", mime_type="image/png", resumable=False)
    assert fid not in files.content


def test_native_download_and_upload_rejected() -> None:
    handle = _handle(_item(DOC_MIME))
    with pytest.raises(UnsupportedOperationError, match="binary download"):
        handle.download()
    with pytest.raises(UnsupportedOperationError, match="uploaded bytes"):
        handle.upload(b"x", mime_type="text/plain")


def test_export_native_and_reject_binary() -> None:
    files = CollectionMemoryDriveFiles()
    fid = files.seed(name="Brief", mime_type=DOC_MIME, parents=["folder"])
    handle = _handle(_item(DOC_MIME, fid), files=files)
    exported = handle.export("application/pdf")
    assert exported == b"exported-native"
    with pytest.raises(UnsupportedOperationError, match="not a native"):
        _handle(_item("text/plain", "txt")).export("application/pdf")


def test_export_unexpected_payload_type() -> None:
    class BadExportFiles(CollectionMemoryDriveFiles):
        def export(self, *, fileId: str, mimeType: str) -> str:
            return "not-bytes"

    fid = "doc-bad"
    item = _item(DOC_MIME, fid)
    handle = _handle(item, files=BadExportFiles())
    with pytest.raises(UnsupportedOperationError, match="Unexpected export payload"):
        handle.export("application/pdf")


def test_google_doc_handle_get_and_batch_update() -> None:
    docs = FakeDocsService()
    docs._documents.documents["d1"] = {"documentId": "d1", "title": "Hi"}
    handle = GoogleDocHandle(
        file_id="d1",
        documents_service=docs,
        call=lambda func: execute_or_return(func()),
    )
    assert handle.get()["title"] == "Hi"
    assert handle.batch_update([{"insertText": {}}])["documentId"] == "d1"
    _, call = docs._documents.calls[-1]
    assert call["body"]["writeControl"]["requiredRevisionId"] == "doc-revision-1"
    assert handle.service is docs


def test_google_doc_handle_rejects_stale_revision() -> None:
    docs = FakeDocsService()
    docs._documents.documents["d1"] = {
        "documentId": "d1",
        "revisionId": "revision-1",
    }
    handle = GoogleDocHandle(
        file_id="d1",
        documents_service=docs,
        call=lambda func: execute_or_return(func()),
    )
    handle.get()
    docs._documents.documents["d1"]["revisionId"] = "revision-2"
    with pytest.raises(ConflictError, match="Google Doc conflict"):
        handle.batch_update([])


def test_google_slides_handle_get_and_batch_update() -> None:
    slides = FakeSlidesService()
    handle = GoogleSlidesHandle(
        file_id="p1",
        slides_service=slides,
        call=lambda func: execute_or_return(func()),
    )
    assert handle.get()["presentationId"] == "p1"
    assert handle.batch_update([])["presentationId"] == "p1"
    _, call = slides._presentations.calls[-1]
    assert call["body"]["writeControl"]["requiredRevisionId"] == "slides-revision-1"
    assert handle.service is slides


def test_google_sheet_handle_get_with_fields_and_batch_update() -> None:
    sheets = FakeNativeSheetsService()
    handle = GoogleSheetContentHandle(
        file_id="s1",
        sheets_service=sheets,
        call=lambda func: execute_or_return(func()),
    )
    assert handle.get(fields="spreadsheetId")["spreadsheetId"] == "sheet-1"
    assert handle.get()["spreadsheetId"] == "sheet-1"
    assert handle.batch_update([])["replies"] == []
    assert handle.service is sheets


@pytest.mark.parametrize(
    ("method", "mime", "service_kw"),
    [
        ("google_doc", SLIDE_MIME, {"docs": FakeDocsService()}),
        ("google_slides", DOC_MIME, {"slides": FakeSlidesService()}),
        ("google_sheet", DOC_MIME, {"sheets": FakeNativeSheetsService()}),
    ],
)
def test_native_handle_wrong_mime(
    method: str,
    mime: str,
    service_kw: dict[str, Any],
) -> None:
    handle = _handle(_item(mime), **service_kw)
    with pytest.raises(UnsupportedOperationError, match="not a"):
        getattr(handle, method)()


@pytest.mark.parametrize(
    ("method", "mime"),
    [
        ("google_doc", DOC_MIME),
        ("google_slides", SLIDE_MIME),
        ("google_sheet", SHEET_MIME),
    ],
)
def test_native_handle_missing_service(method: str, mime: str) -> None:
    handle = _handle(_item(mime))
    with pytest.raises(UnsupportedOperationError, match="not configured"):
        getattr(handle, method)()


def test_content_handle_properties() -> None:
    item = _item(DOC_MIME, "prop-id")
    handle = _handle(item)
    assert handle.file_id == "prop-id"
    assert handle.mime_type == DOC_MIME
    assert handle.content_loaded is False
