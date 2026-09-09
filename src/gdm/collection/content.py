"""Lazy native content handles for collection objects."""

from __future__ import annotations

import inspect
import io
from collections.abc import Callable
from typing import Any

from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

from gdm.collection.constants import (
    DEFAULT_LIST_FIELDS,
    DOC_MIME,
    NATIVE_WORKSPACE_MIMES,
    SHEET_MIME,
    SLIDE_MIME,
)
from gdm.collection.inventory import DriveInventoryItem, inventory_item_from_file
from gdm.errors import ConflictError, UnsupportedOperationError
from gdm.retry import execute_or_return


def _call_with_retry_hint(
    call: Callable[..., Any],
    func: Callable[[], Any],
    *,
    retryable: bool,
) -> Any:
    """Pass retry policy to modern callbacks while accepting legacy one-arg hooks."""
    try:
        parameters = inspect.signature(call).parameters.values()
    except (TypeError, ValueError):
        return call(func, retryable=retryable)
    supports_keyword = any(
        parameter.name == "retryable" or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )
    if supports_keyword:
        return call(func, retryable=retryable)
    return call(func)


def _is_revision_conflict(exc: HttpError) -> bool:
    status = getattr(exc, "status_code", None) or getattr(exc.resp, "status", None)
    content = getattr(exc, "content", b"")
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="ignore")
    return status == 400 and "revision" in str(content).lower()


class GoogleDocHandle:
    """Structured Google Docs API access."""

    def __init__(
        self,
        *,
        file_id: str,
        documents_service: Any,
        call: Callable[..., Any],
        preflight: Callable[[], None] | None = None,
        postflight: Callable[[], None] | None = None,
    ) -> None:
        self.file_id = file_id
        self._documents = documents_service
        self._call = call
        self._preflight = preflight
        self._postflight = postflight
        self._revision_id: str | None = None

    def get(self) -> dict[str, Any]:
        payload = self._get()
        revision_id = payload.get("revisionId")
        self._revision_id = str(revision_id) if revision_id is not None else None
        return payload

    def _get(self) -> dict[str, Any]:
        def _get() -> dict[str, Any]:
            result = self._documents.documents().get(documentId=self.file_id)
            return execute_or_return(result)

        return self._call(_get)

    def batch_update(
        self,
        requests: list[dict[str, Any]],
        *,
        expected_revision_id: str | None = None,
    ) -> dict[str, Any]:
        revision_id = expected_revision_id or self._revision_id
        if revision_id is None and self._preflight is not None:
            self._preflight()
        current = self._get()
        actual_revision = current.get("revisionId")
        if actual_revision is None:
            msg = f"Google Doc {self.file_id} did not expose a revisionId."
            raise UnsupportedOperationError(msg)
        actual_revision = str(actual_revision)
        if revision_id is not None and actual_revision != revision_id:
            msg = (
                f"Google Doc conflict for {self.file_id}: expected revision "
                f"{revision_id}, found {actual_revision}."
            )
            raise ConflictError(
                msg,
                expected_version=revision_id,
                actual_version=actual_revision,
            )
        revision_id = actual_revision

        def _update() -> dict[str, Any]:
            result = self._documents.documents().batchUpdate(
                documentId=self.file_id,
                body={
                    "requests": requests,
                    "writeControl": {"requiredRevisionId": revision_id},
                },
            )
            return execute_or_return(result)

        try:
            response = _call_with_retry_hint(self._call, _update, retryable=False)
        except HttpError as exc:
            if not _is_revision_conflict(exc):
                raise
            msg = (
                f"Google Doc conflict for {self.file_id}: revision "
                f"{revision_id} changed during update."
            )
            raise ConflictError(msg, expected_version=revision_id) from exc
        write_control = response.get("writeControl") or {}
        updated_revision = write_control.get("requiredRevisionId")
        self._revision_id = (
            str(updated_revision) if updated_revision is not None else None
        )
        if self._postflight is not None:
            self._postflight()
        return response

    @property
    def service(self) -> Any:
        return self._documents


class GoogleSlidesHandle:
    """Structured Google Slides API access."""

    def __init__(
        self,
        *,
        file_id: str,
        slides_service: Any,
        call: Callable[..., Any],
        preflight: Callable[[], None] | None = None,
        postflight: Callable[[], None] | None = None,
    ) -> None:
        self.file_id = file_id
        self._slides = slides_service
        self._call = call
        self._preflight = preflight
        self._postflight = postflight
        self._revision_id: str | None = None

    def get(self) -> dict[str, Any]:
        payload = self._get()
        revision_id = payload.get("revisionId")
        self._revision_id = str(revision_id) if revision_id is not None else None
        return payload

    def _get(self) -> dict[str, Any]:
        def _get() -> dict[str, Any]:
            result = self._slides.presentations().get(
                presentationId=self.file_id,
            )
            return execute_or_return(result)

        return self._call(_get)

    def batch_update(
        self,
        requests: list[dict[str, Any]],
        *,
        expected_revision_id: str | None = None,
    ) -> dict[str, Any]:
        revision_id = expected_revision_id or self._revision_id
        if revision_id is None and self._preflight is not None:
            self._preflight()
        current = self._get()
        actual_revision = current.get("revisionId")
        if actual_revision is None:
            msg = f"Google Slides file {self.file_id} did not expose a revisionId."
            raise UnsupportedOperationError(msg)
        actual_revision = str(actual_revision)
        if revision_id is not None and actual_revision != revision_id:
            msg = (
                f"Google Slides conflict for {self.file_id}: expected revision "
                f"{revision_id}, found {actual_revision}."
            )
            raise ConflictError(
                msg,
                expected_version=revision_id,
                actual_version=actual_revision,
            )
        revision_id = actual_revision

        def _update() -> dict[str, Any]:
            result = self._slides.presentations().batchUpdate(
                presentationId=self.file_id,
                body={
                    "requests": requests,
                    "writeControl": {"requiredRevisionId": revision_id},
                },
            )
            return execute_or_return(result)

        try:
            response = _call_with_retry_hint(self._call, _update, retryable=False)
        except HttpError as exc:
            if not _is_revision_conflict(exc):
                raise
            msg = (
                f"Google Slides conflict for {self.file_id}: revision "
                f"{revision_id} changed during update."
            )
            raise ConflictError(msg, expected_version=revision_id) from exc
        write_control = response.get("writeControl") or {}
        updated_revision = write_control.get("requiredRevisionId")
        self._revision_id = (
            str(updated_revision) if updated_revision is not None else None
        )
        if self._postflight is not None:
            self._postflight()
        return response

    @property
    def service(self) -> Any:
        return self._slides


class GoogleSheetContentHandle:
    """Native Google Sheets content access."""

    def __init__(
        self,
        *,
        file_id: str,
        sheets_service: Any,
        call: Callable[..., Any],
        preflight: Callable[[], None] | None = None,
        postflight: Callable[[], None] | None = None,
    ) -> None:
        self.file_id = file_id
        self._sheets = sheets_service
        self._call = call
        self._preflight = preflight
        self._postflight = postflight

    def get(self, *, fields: str | None = None) -> dict[str, Any]:
        def _get() -> dict[str, Any]:
            kwargs: dict[str, Any] = {"spreadsheetId": self.file_id}
            if fields is not None:
                kwargs["fields"] = fields
            result = self._sheets.spreadsheets().get(**kwargs)
            return execute_or_return(result)

        return self._call(_get)

    def batch_update(self, requests: list[dict[str, Any]]) -> dict[str, Any]:
        if self._preflight is not None:
            self._preflight()

        def _update() -> dict[str, Any]:
            result = self._sheets.spreadsheets().batchUpdate(
                spreadsheetId=self.file_id,
                body={"requests": requests},
            )
            return execute_or_return(result)

        response = _call_with_retry_hint(self._call, _update, retryable=False)
        if self._postflight is not None:
            self._postflight()
        return response

    @property
    def service(self) -> Any:
        return self._sheets


class ContentHandle:
    """Lazy content accessor for one Drive collection object."""

    def __init__(
        self,
        *,
        item: DriveInventoryItem,
        drive_files: Any,
        call: Callable[..., Any],
        documents_service: Any | None = None,
        slides_service: Any | None = None,
        sheets_service: Any | None = None,
        on_item_updated: Callable[[DriveInventoryItem], None] | None = None,
    ) -> None:
        self._item = item
        self._files = drive_files
        self._call = call
        self._documents = documents_service
        self._slides = slides_service
        self._sheets = sheets_service
        self._on_item_updated = on_item_updated
        self._loaded = False

    @property
    def file_id(self) -> str:
        return self._item.file_id

    @property
    def mime_type(self) -> str:
        return self._item.mime_type

    def download(self) -> bytes:
        if self._item.mime_type in NATIVE_WORKSPACE_MIMES:
            msg = (
                f"Native Workspace file {self._item.file_id} ({self._item.mime_type}) "
                "does not support binary download; use export() or a native handle."
            )
            raise UnsupportedOperationError(msg)

        inline = getattr(self._files, "content", None)
        if isinstance(inline, dict) and self._item.file_id in inline:
            self._loaded = True
            return inline[self._item.file_id]

        def _download() -> bytes:
            request = self._files.get_media(
                fileId=self._item.file_id,
                supportsAllDrives=True,
            )
            buffer = io.BytesIO()
            downloader = MediaIoBaseDownload(buffer, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            return buffer.getvalue()

        self._loaded = True
        return self._call(_download)

    def upload(
        self,
        data: bytes,
        *,
        mime_type: str,
        resumable: bool = True,
    ) -> None:
        if self._item.mime_type in NATIVE_WORKSPACE_MIMES:
            msg = (
                f"Native Workspace file {self._item.file_id} ({self._item.mime_type}) "
                "cannot be replaced with uploaded bytes."
            )
            raise UnsupportedOperationError(msg)

        self._assert_drive_snapshot()
        media = MediaIoBaseUpload(
            io.BytesIO(data),
            mimetype=mime_type,
            resumable=resumable,
        )

        def _update() -> dict[str, Any]:
            result = self._files.update(
                fileId=self._item.file_id,
                media_body=media,
                fields=DEFAULT_LIST_FIELDS,
                supportsAllDrives=True,
            )
            return execute_or_return(result)

        updated = _call_with_retry_hint(self._call, _update, retryable=False)
        self._set_item(inventory_item_from_file(updated))
        self._loaded = True

    def export(self, mime_type: str) -> bytes:
        if self._item.mime_type not in NATIVE_WORKSPACE_MIMES:
            msg = (
                f"File {self._item.file_id} ({self._item.mime_type}) is not a native "
                "Workspace file; use download() instead."
            )
            raise UnsupportedOperationError(msg)

        def _export() -> bytes:
            result = self._files.export(fileId=self._item.file_id, mimeType=mime_type)
            exported = execute_or_return(result)
            if isinstance(exported, bytes):
                return exported
            msg = f"Unexpected export payload type for {self._item.file_id}."
            raise UnsupportedOperationError(msg)

        self._loaded = True
        return self._call(_export)

    def google_doc(self) -> GoogleDocHandle:
        if self._item.mime_type != DOC_MIME:
            msg = f"File {self._item.file_id} is not a Google Doc."
            raise UnsupportedOperationError(msg)
        if self._documents is None:
            msg = "Docs API service is not configured on this client."
            raise UnsupportedOperationError(msg)
        return GoogleDocHandle(
            file_id=self._item.file_id,
            documents_service=self._documents,
            call=self._call,
            preflight=self._assert_drive_snapshot,
            postflight=self._refresh_drive_snapshot,
        )

    def google_slides(self) -> GoogleSlidesHandle:
        if self._item.mime_type != SLIDE_MIME:
            msg = f"File {self._item.file_id} is not a Google Slides presentation."
            raise UnsupportedOperationError(msg)
        if self._slides is None:
            msg = "Slides API service is not configured on this client."
            raise UnsupportedOperationError(msg)
        return GoogleSlidesHandle(
            file_id=self._item.file_id,
            slides_service=self._slides,
            call=self._call,
            preflight=self._assert_drive_snapshot,
            postflight=self._refresh_drive_snapshot,
        )

    def google_sheet(self) -> GoogleSheetContentHandle:
        if self._item.mime_type != SHEET_MIME:
            msg = f"File {self._item.file_id} is not a Google Sheet."
            raise UnsupportedOperationError(msg)
        if self._sheets is None:
            msg = "Sheets API service is not configured on this client."
            raise UnsupportedOperationError(msg)
        return GoogleSheetContentHandle(
            file_id=self._item.file_id,
            sheets_service=self._sheets,
            call=self._call,
            preflight=self._assert_drive_snapshot,
            postflight=self._refresh_drive_snapshot,
        )

    def _assert_drive_snapshot(self) -> None:
        current = self._read_drive_item()
        expected_version = self._item.version
        actual_version = current.version
        version_changed = (
            expected_version is not None
            and actual_version is not None
            and expected_version != actual_version
        )
        checksum_changed = (
            self._item.md5_checksum is not None
            and current.md5_checksum is not None
            and self._item.md5_checksum != current.md5_checksum
        )
        if not version_changed and not checksum_changed:
            return
        expected = expected_version or self._item.md5_checksum
        actual = actual_version or current.md5_checksum
        msg = (
            f"Drive content conflict for {self._item.file_id}: expected "
            f"{expected}, found {actual}."
        )
        raise ConflictError(
            msg,
            expected_version=expected,
            actual_version=actual,
        )

    def _read_drive_item(self) -> DriveInventoryItem:
        def _get() -> dict[str, Any]:
            result = self._files.get(
                fileId=self._item.file_id,
                fields=DEFAULT_LIST_FIELDS,
                supportsAllDrives=True,
            )
            return execute_or_return(result)

        return inventory_item_from_file(self._call(_get))

    def _refresh_drive_snapshot(self) -> None:
        self._set_item(self._read_drive_item())

    def _set_item(self, item: DriveInventoryItem) -> None:
        self._item = item
        if self._on_item_updated is not None:
            self._on_item_updated(item)

    @property
    def content_loaded(self) -> bool:
        return self._loaded
