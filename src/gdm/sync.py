"""Drive Changes API polling primitives and checkpoint types."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from googleapiclient.errors import HttpError

from gdm.errors import GDMError
from gdm.retry import RetryPolicy, execute_with_retry, normalize_http_error

logger = logging.getLogger(__name__)

WEBHOOK_NOTICE = (
    "Push notification channels are optional accelerators only. They carry no "
    "resource bodies, expire, and still require polling the Changes API for "
    "authoritative invalidation."
)


class ChangeType(StrEnum):
    FILE = "file"
    DRIVE = "drive"
    REMOVED = "removed"


@dataclass(frozen=True)
class ChangeRecord:
    change_id: str
    file_id: str | None
    removed: bool
    drive_id: str | None = None
    time: str | None = None
    file: dict[str, Any] | None = None


@dataclass
class SyncCheckpoint:
    """Persisted polling state for incremental Drive sync."""

    start_page_token: str
    drive_id: str | None = None
    shared_drive: bool = False
    last_change_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "start_page_token": self.start_page_token,
            "shared_drive": self.shared_drive,
        }
        if self.drive_id is not None:
            payload["drive_id"] = self.drive_id
        if self.last_change_id is not None:
            payload["last_change_id"] = self.last_change_id
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SyncCheckpoint:
        return cls(
            start_page_token=str(data["start_page_token"]),
            drive_id=data.get("drive_id"),
            shared_drive=bool(data.get("shared_drive", False)),
            last_change_id=data.get("last_change_id"),
        )


@dataclass
class ChangesPage:
    changes: list[ChangeRecord]
    next_page_token: str | None
    new_start_page_token: str | None = None


@runtime_checkable
class DriveChangesClient(Protocol):
    def getStartPageToken(
        self,
        *,
        supportsAllDrives: bool = True,
        driveId: str | None = None,
    ) -> dict[str, Any]: ...

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
    ) -> dict[str, Any]: ...


@runtime_checkable
class DriveChangesService(Protocol):
    @property
    def changes(self) -> DriveChangesClient: ...


class ChangesPoller:
    """Incremental Drive Changes API poller without webhook guarantees."""

    DEFAULT_FIELDS = (
        "nextPageToken,newStartPageToken,"
        "changes(changeId,removed,fileId,driveId,time,file(id,name,mimeType,trashed))"
    )

    def __init__(
        self,
        service: DriveChangesService,
        *,
        retry_policy: RetryPolicy | None = None,
        supports_all_drives: bool = True,
        shared_drive_id: str | None = None,
        page_size: int = 100,
    ) -> None:
        self.service = service
        self.retry_policy = retry_policy or RetryPolicy()
        self.supports_all_drives = supports_all_drives
        self.shared_drive_id = shared_drive_id
        self.page_size = page_size

    @staticmethod
    def webhook_notice() -> str:
        return WEBHOOK_NOTICE

    def _call(self, func: Callable[[], Any]) -> Any:
        if getattr(self.service, "_gdm_executes_with_retry", False):
            return func()
        return execute_with_retry(func, policy=self.retry_policy)

    def fetch_start_token(self) -> str:
        kwargs: dict[str, Any] = {"supportsAllDrives": self.supports_all_drives}
        if self.shared_drive_id:
            kwargs["driveId"] = self.shared_drive_id

        def _get() -> dict[str, Any]:
            try:
                return self.service.changes.getStartPageToken(**kwargs)
            except HttpError as exc:
                raise self._normalize_http_error(exc) from exc

        response = self._call(_get)
        token = response.get("startPageToken")
        if not token:
            msg = "Drive API did not return a startPageToken."
            raise GDMError(msg)
        return str(token)

    def initial_checkpoint(self) -> SyncCheckpoint:
        return SyncCheckpoint(
            start_page_token=self.fetch_start_token(),
            drive_id=self.shared_drive_id,
            shared_drive=self.shared_drive_id is not None,
        )

    def poll(self, checkpoint: SyncCheckpoint) -> tuple[ChangesPage, SyncCheckpoint]:
        token = checkpoint.start_page_token
        all_changes: list[ChangeRecord] = []
        next_page: str | None = None
        new_start: str | None = None

        while True:
            page = self._list_changes(token)
            all_changes.extend(page.changes)
            next_page = page.next_page_token
            if page.new_start_page_token:
                new_start = page.new_start_page_token
            if not next_page:
                break
            token = next_page

        updated = SyncCheckpoint(
            start_page_token=new_start or checkpoint.start_page_token,
            drive_id=checkpoint.drive_id,
            shared_drive=checkpoint.shared_drive,
            last_change_id=all_changes[-1].change_id
            if all_changes
            else checkpoint.last_change_id,
        )
        return (
            ChangesPage(
                changes=all_changes,
                next_page_token=None,
                new_start_page_token=new_start,
            ),
            updated,
        )

    def _list_changes(self, page_token: str) -> ChangesPage:
        kwargs: dict[str, Any] = {
            "pageToken": page_token,
            "pageSize": self.page_size,
            "fields": self.DEFAULT_FIELDS,
            "supportsAllDrives": self.supports_all_drives,
            "includeItemsFromAllDrives": self.supports_all_drives,
            "includeRemoved": True,
        }
        if self.shared_drive_id:
            kwargs["driveId"] = self.shared_drive_id

        def _list() -> dict[str, Any]:
            try:
                return self.service.changes.list(**kwargs)
            except HttpError as exc:
                raise self._normalize_http_error(exc) from exc

        response = self._call(_list)
        changes = [self._parse_change(item) for item in response.get("changes", [])]
        return ChangesPage(
            changes=changes,
            next_page_token=response.get("nextPageToken"),
            new_start_page_token=response.get("newStartPageToken"),
        )

    @staticmethod
    def _parse_change(item: dict[str, Any]) -> ChangeRecord:
        return ChangeRecord(
            change_id=str(item.get("changeId", "")),
            file_id=item.get("fileId"),
            removed=bool(item.get("removed")),
            drive_id=item.get("driveId"),
            time=item.get("time"),
            file=item.get("file"),
        )

    @staticmethod
    def _normalize_http_error(exc: HttpError) -> Exception:
        return normalize_http_error(exc)
