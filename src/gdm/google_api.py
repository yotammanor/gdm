"""Executing adapters for google-api-python-client discovery resources."""

from __future__ import annotations

import io
import logging
from typing import Any

from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

from gdm.retry import (
    RetryPolicy,
    execute_or_return,
    execute_with_retry,
    normalize_http_error,
)

logger = logging.getLogger(__name__)

JSON_MIME = "application/json"
_RETRYABLE_METHODS = frozenset(
    {
        "batchGet",
        "export",
        "get",
        "getStartPageToken",
        "get_media",
        "list",
    }
)


class _ExecutingResource:
    """Base wrapper that executes discovery ``HttpRequest`` objects."""

    def __init__(
        self,
        resource: Any,
        *,
        retry_policy: RetryPolicy | None = None,
        auto_execute: bool = True,
    ) -> None:
        self._resource = resource
        self._retry_policy = retry_policy or RetryPolicy()
        self._auto_execute = auto_execute

    def _invoke(self, name: str, **kwargs: Any) -> Any:
        method = getattr(self._resource, name)

        def _call() -> Any:
            try:
                result = method(**kwargs)
            except Exception as exc:
                from googleapiclient.errors import HttpError

                if isinstance(exc, HttpError):
                    raise normalize_http_error(exc) from exc
                raise
            if self._auto_execute:
                return execute_or_return(result)
            return result

        return execute_with_retry(
            _call,
            policy=self._retry_policy,
            retryable=name in _RETRYABLE_METHODS,
        )


class ExecutingDriveFiles(_ExecutingResource):
    """Drive ``files`` resource with automatic ``HttpRequest.execute()``."""

    def get(
        self,
        *,
        fileId: str,
        fields: str,
        supportsAllDrives: bool = True,
    ) -> dict[str, Any]:
        return self._invoke(
            "get", fileId=fileId, fields=fields, supportsAllDrives=supportsAllDrives
        )

    def list(
        self,
        *,
        q: str | None = None,
        fields: str | None = None,
        pageSize: int | None = None,
        pageToken: str | None = None,
        supportsAllDrives: bool = True,
        includeItemsFromAllDrives: bool = True,
        corpora: str | None = None,
        driveId: str | None = None,
    ) -> dict[str, Any]:
        return self._invoke(
            "list",
            q=q,
            fields=fields,
            pageSize=pageSize,
            pageToken=pageToken,
            supportsAllDrives=supportsAllDrives,
            includeItemsFromAllDrives=includeItemsFromAllDrives,
            corpora=corpora,
            driveId=driveId,
        )

    def create(
        self,
        *,
        body: dict[str, Any],
        media_body: Any = None,
        fields: str | None = None,
        supportsAllDrives: bool = True,
    ) -> dict[str, Any]:
        return self._invoke(
            "create",
            body=body,
            media_body=media_body,
            fields=fields,
            supportsAllDrives=supportsAllDrives,
        )

    def update(
        self,
        *,
        fileId: str,
        body: dict[str, Any] | None = None,
        media_body: Any = None,
        fields: str | None = None,
        supportsAllDrives: bool = True,
        addParents: str | None = None,
        removeParents: str | None = None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "fileId": fileId,
            "body": body,
            "media_body": media_body,
            "fields": fields,
            "supportsAllDrives": supportsAllDrives,
        }
        if addParents is not None:
            kwargs["addParents"] = addParents
        if removeParents is not None:
            kwargs["removeParents"] = removeParents
        return self._invoke("update", **kwargs)

    def delete(self, *, fileId: str, supportsAllDrives: bool = True) -> None:
        self._invoke("delete", fileId=fileId, supportsAllDrives=supportsAllDrives)

    def export(self, *, fileId: str, mimeType: str) -> bytes:
        return self._invoke("export", fileId=fileId, mimeType=mimeType)

    def get_media(self, *, fileId: str, supportsAllDrives: bool = True) -> Any:
        """Return a media ``HttpRequest`` for resumable download helpers."""
        media = ExecutingDriveFiles(
            self._resource, retry_policy=self._retry_policy, auto_execute=False
        )
        return media._invoke(
            "get_media", fileId=fileId, supportsAllDrives=supportsAllDrives
        )


class ExecutingDrivePermissions(_ExecutingResource):
    """Drive ``permissions`` resource with automatic execution."""

    def list(
        self,
        *,
        fileId: str,
        fields: str | None = None,
        supportsAllDrives: bool = True,
        pageSize: int | None = None,
        pageToken: str | None = None,
    ) -> dict[str, Any]:
        return self._invoke(
            "list",
            fileId=fileId,
            fields=fields,
            supportsAllDrives=supportsAllDrives,
            pageSize=pageSize,
            pageToken=pageToken,
        )

    def create(
        self,
        *,
        fileId: str,
        body: dict[str, Any],
        supportsAllDrives: bool = True,
        sendNotificationEmail: bool = False,
        fields: str | None = None,
    ) -> dict[str, Any]:
        return self._invoke(
            "create",
            fileId=fileId,
            body=body,
            supportsAllDrives=supportsAllDrives,
            sendNotificationEmail=sendNotificationEmail,
            fields=fields,
        )

    def delete(
        self,
        *,
        fileId: str,
        permissionId: str,
        supportsAllDrives: bool = True,
    ) -> None:
        self._invoke(
            "delete",
            fileId=fileId,
            permissionId=permissionId,
            supportsAllDrives=supportsAllDrives,
        )


class ExecutingDriveChanges(_ExecutingResource):
    """Drive ``changes`` resource with automatic execution."""

    def getStartPageToken(
        self,
        *,
        supportsAllDrives: bool = True,
        driveId: str | None = None,
    ) -> dict[str, Any]:
        return self._invoke(
            "getStartPageToken", supportsAllDrives=supportsAllDrives, driveId=driveId
        )

    def list(
        self,
        *,
        pageToken: str,
        pageSize: int | None = None,
        fields: str | None = None,
        supportsAllDrives: bool = True,
        includeItemsFromAllDrives: bool = True,
        driveId: str | None = None,
        restrictToMyDrive: bool | None = None,
        includeRemoved: bool = True,
    ) -> dict[str, Any]:
        return self._invoke(
            "list",
            pageToken=pageToken,
            pageSize=pageSize,
            fields=fields,
            supportsAllDrives=supportsAllDrives,
            includeItemsFromAllDrives=includeItemsFromAllDrives,
            driveId=driveId,
            restrictToMyDrive=restrictToMyDrive,
            includeRemoved=includeRemoved,
        )


class ExecutingSheetsValues(_ExecutingResource):
    """Sheets ``values`` resource with automatic execution."""

    def get(
        self,
        *,
        spreadsheetId: str,
        range: str,
        majorDimension: str = "ROWS",
        valueRenderOption: str | None = None,
    ) -> dict[str, Any]:
        return self._invoke(
            "get",
            spreadsheetId=spreadsheetId,
            range=range,
            majorDimension=majorDimension,
            valueRenderOption=valueRenderOption,
        )

    def update(
        self,
        *,
        spreadsheetId: str,
        range: str,
        body: dict[str, Any],
        valueInputOption: str = "USER_ENTERED",
    ) -> dict[str, Any]:
        return self._invoke(
            "update",
            spreadsheetId=spreadsheetId,
            range=range,
            body=body,
            valueInputOption=valueInputOption,
        )

    def append(
        self,
        *,
        spreadsheetId: str,
        range: str,
        body: dict[str, Any],
        valueInputOption: str = "USER_ENTERED",
        insertDataOption: str = "INSERT_ROWS",
    ) -> dict[str, Any]:
        return self._invoke(
            "append",
            spreadsheetId=spreadsheetId,
            range=range,
            body=body,
            valueInputOption=valueInputOption,
            insertDataOption=insertDataOption,
        )

    def batchUpdate(
        self,
        *,
        spreadsheetId: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        return self._invoke("batchUpdate", spreadsheetId=spreadsheetId, body=body)

    def batchGet(
        self,
        *,
        spreadsheetId: str,
        ranges: list[str],
        majorDimension: str = "ROWS",
    ) -> dict[str, Any]:
        return self._invoke(
            "batchGet",
            spreadsheetId=spreadsheetId,
            ranges=ranges,
            majorDimension=majorDimension,
        )

    def clear(
        self,
        *,
        spreadsheetId: str,
        range: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._invoke(
            "clear", spreadsheetId=spreadsheetId, range=range, body=body or {}
        )


class ExecutingSheetsSpreadsheets(_ExecutingResource):
    """Sheets ``spreadsheets`` resource exposing nested ``values``."""

    def __init__(
        self,
        resource: Any,
        *,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        super().__init__(resource, retry_policy=retry_policy)
        self._values: ExecutingSheetsValues | None = None

    def values(self) -> ExecutingSheetsValues:
        if self._values is None:
            self._values = ExecutingSheetsValues(
                self._resource.values(), retry_policy=self._retry_policy
            )
        return self._values

    def get(
        self,
        *,
        spreadsheetId: str,
        fields: str | None = None,
    ) -> dict[str, Any]:
        return self._invoke("get", spreadsheetId=spreadsheetId, fields=fields)

    def batchUpdate(
        self,
        *,
        spreadsheetId: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        return self._invoke("batchUpdate", spreadsheetId=spreadsheetId, body=body)


class ExecutingDriveService:
    """Drive v3 service surface used by GDM backends and project metadata IO."""

    DEFAULT_FIELDS = (
        "id,name,mimeType,version,md5Checksum,appProperties,parents,trashed"
    )

    files: ExecutingDriveFiles
    permissions: ExecutingDrivePermissions
    changes: ExecutingDriveChanges
    _gdm_executes_with_retry = True

    def __init__(
        self,
        raw_service: Any,
        *,
        retry_policy: RetryPolicy | None = None,
        supports_all_drives: bool = True,
    ) -> None:
        self._raw = raw_service
        self._retry_policy = retry_policy or RetryPolicy()
        self.supports_all_drives = supports_all_drives
        files_resource = raw_service.files()
        self.files = ExecutingDriveFiles(
            files_resource, retry_policy=self._retry_policy
        )
        permissions_resource = raw_service.permissions()
        self.permissions = ExecutingDrivePermissions(
            permissions_resource, retry_policy=self._retry_policy
        )
        changes_resource = raw_service.changes()
        self.changes = ExecutingDriveChanges(
            changes_resource, retry_policy=self._retry_policy
        )

    def read_file_bytes(self, file_id: str) -> bytes:
        """Download binary file content for project metadata files."""
        request = self.files.get_media(
            fileId=file_id, supportsAllDrives=self.supports_all_drives
        )
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return buffer.getvalue()

    def write_file_bytes(self, file_id: str, data: bytes) -> None:
        """Upload binary file content for project metadata files."""
        media = MediaIoBaseUpload(io.BytesIO(data), mimetype=JSON_MIME, resumable=False)

        def _update() -> None:
            self.files.update(
                fileId=file_id,
                media_body=media,
                fields="id",
                supportsAllDrives=self.supports_all_drives,
            )

        execute_with_retry(_update, policy=self._retry_policy, retryable=False)


class ExecutingSheetsService:
    """Sheets v4 service surface used by GDM backends."""

    _gdm_executes_with_retry = True

    def __init__(
        self,
        raw_service: Any,
        *,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self._raw = raw_service
        self._retry_policy = retry_policy or RetryPolicy()
        self._spreadsheets = ExecutingSheetsSpreadsheets(
            raw_service.spreadsheets(), retry_policy=self._retry_policy
        )

    def spreadsheets(self) -> ExecutingSheetsSpreadsheets:
        return self._spreadsheets


class ExecutingDocsDocuments(_ExecutingResource):
    """Docs ``documents`` resource with automatic execution."""

    def get(self, *, documentId: str) -> dict[str, Any]:
        return self._invoke("get", documentId=documentId)

    def batchUpdate(
        self,
        *,
        documentId: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        return self._invoke("batchUpdate", documentId=documentId, body=body)


class ExecutingDocsService:
    """Google Docs API service surface."""

    _gdm_executes_with_retry = True

    def __init__(
        self,
        raw_service: Any,
        *,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self._raw = raw_service
        self._retry_policy = retry_policy or RetryPolicy()
        self._documents = ExecutingDocsDocuments(
            raw_service.documents(), retry_policy=self._retry_policy
        )

    def documents(self) -> ExecutingDocsDocuments:
        return self._documents


class ExecutingSlidesPresentations(_ExecutingResource):
    """Slides ``presentations`` resource with automatic execution."""

    def get(self, *, presentationId: str) -> dict[str, Any]:
        return self._invoke("get", presentationId=presentationId)

    def batchUpdate(
        self,
        *,
        presentationId: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        return self._invoke("batchUpdate", presentationId=presentationId, body=body)


class ExecutingSlidesService:
    """Google Slides API service surface."""

    _gdm_executes_with_retry = True

    def __init__(
        self,
        raw_service: Any,
        *,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self._raw = raw_service
        self._retry_policy = retry_policy or RetryPolicy()
        self._presentations = ExecutingSlidesPresentations(
            raw_service.presentations(), retry_policy=self._retry_policy
        )

    def presentations(self) -> ExecutingSlidesPresentations:
        return self._presentations


def wrap_docs_service(
    raw_service: Any,
    *,
    retry_policy: RetryPolicy | None = None,
) -> ExecutingDocsService:
    """Wrap a discovery Docs client for GDM content handles."""
    return ExecutingDocsService(raw_service, retry_policy=retry_policy)


def wrap_slides_service(
    raw_service: Any,
    *,
    retry_policy: RetryPolicy | None = None,
) -> ExecutingSlidesService:
    """Wrap a discovery Slides client for GDM content handles."""
    return ExecutingSlidesService(raw_service, retry_policy=retry_policy)


class DriveExplorerAdapter:
    """Adapt an executing Drive service for :class:`gdm.explore.Explorer`."""

    def __init__(
        self,
        service: ExecutingDriveService,
        *,
        page_size: int = 100,
        supports_all_drives: bool = True,
        include_items_from_all_drives: bool = True,
    ) -> None:
        self._service = service
        self.page_size = page_size
        self.supports_all_drives = supports_all_drives
        self.include_items_from_all_drives = include_items_from_all_drives

    def list_children(
        self,
        parent_id: str,
        *,
        page_token: str | None = None,
        page_size: int = 100,
        supports_all_drives: bool = True,
        include_items_from_all_drives: bool = True,
    ) -> _DriveListPage:
        del supports_all_drives, include_items_from_all_drives
        response = self._service.files.list(
            q=f"'{parent_id}' in parents and trashed = false",
            fields=(
                "nextPageToken,files("
                "id,name,mimeType,size,driveId,parents,shortcutDetails,"
                "appProperties,properties,description,starred,createdTime,"
                "modifiedTime,webViewLink,md5Checksum,capabilities,resourceKey"
                ")"
            ),
            pageSize=page_size,
            pageToken=page_token,
            supportsAllDrives=self.supports_all_drives,
            includeItemsFromAllDrives=self.include_items_from_all_drives,
        )
        return _DriveListPage(
            items=response.get("files", []),
            next_page_token=response.get("nextPageToken"),
        )


class _DriveListPage:
    """One page of Drive list results for :class:`Explorer`."""

    def __init__(
        self,
        *,
        items: list[dict[str, Any]],
        next_page_token: str | None,
    ) -> None:
        self.items: list[dict[str, Any]] = items
        self.next_page_token = next_page_token


def wrap_drive_service(
    raw_service: Any,
    *,
    retry_policy: RetryPolicy | None = None,
    supports_all_drives: bool = True,
) -> ExecutingDriveService:
    """Wrap a discovery Drive client for GDM backends."""
    return ExecutingDriveService(
        raw_service,
        retry_policy=retry_policy,
        supports_all_drives=supports_all_drives,
    )


def wrap_sheets_service(
    raw_service: Any,
    *,
    retry_policy: RetryPolicy | None = None,
) -> ExecutingSheetsService:
    """Wrap a discovery Sheets client for GDM backends."""
    return ExecutingSheetsService(raw_service, retry_policy=retry_policy)
