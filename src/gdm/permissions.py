"""Drive ACL inspection and sharing helpers."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from googleapiclient.errors import HttpError

from gdm.errors import PermissionDeniedError
from gdm.retry import RetryPolicy, execute_with_retry, normalize_http_error

logger = logging.getLogger(__name__)

PROTECTED_RANGES_NOTICE = (
    "Google Sheets protected ranges are an editing convenience only. "
    "They are not row-level security and must not be treated as an "
    "authorization boundary. Use separately permissioned files or folders "
    "for different audiences."
)


class PermissionRole(StrEnum):
    OWNER = "owner"
    ORGANIZER = "organizer"
    FILE_ORGANIZER = "fileOrganizer"
    WRITER = "writer"
    COMMENTER = "commenter"
    READER = "reader"


class PermissionType(StrEnum):
    USER = "user"
    GROUP = "group"
    DOMAIN = "domain"
    ANYONE = "anyone"


@dataclass(frozen=True)
class PermissionView:
    permission_id: str
    role: str
    type: str
    email_address: str | None = None
    domain: str | None = None
    allow_file_discovery: bool | None = None
    deleted: bool = False


@dataclass(frozen=True)
class CapabilityView:
    can_edit: bool
    can_share: bool
    can_copy: bool
    can_download: bool
    view_only: bool


@runtime_checkable
class DrivePermissionsClient(Protocol):
    def list(
        self,
        *,
        fileId: str,
        fields: str | None = None,
        supportsAllDrives: bool = True,
        pageSize: int | None = None,
        pageToken: str | None = None,
    ) -> dict[str, Any]: ...

    def create(
        self,
        *,
        fileId: str,
        body: dict[str, Any],
        supportsAllDrives: bool = True,
        sendNotificationEmail: bool = False,
        fields: str | None = None,
    ) -> dict[str, Any]: ...

    def delete(
        self,
        *,
        fileId: str,
        permissionId: str,
        supportsAllDrives: bool = True,
    ) -> None: ...


@runtime_checkable
class DriveFilesCapabilityClient(Protocol):
    def get(
        self,
        *,
        fileId: str,
        fields: str,
        supportsAllDrives: bool = True,
    ) -> dict[str, Any]: ...


@runtime_checkable
class DrivePermissionsService(Protocol):
    @property
    def permissions(self) -> DrivePermissionsClient: ...

    @property
    def files(self) -> DriveFilesCapabilityClient: ...


class PermissionsManager:
    """Inspect and mutate Drive ACLs. Does not implement row-level security."""

    CAPABILITY_FIELDS = (
        "capabilities/canEdit,capabilities/canShare,capabilities/canCopy,"
        "capabilities/canDownload,viewersCanCopyContent,copyRequiresWriterPermission"
    )

    def __init__(
        self,
        service: DrivePermissionsService,
        *,
        retry_policy: RetryPolicy | None = None,
        supports_all_drives: bool = True,
    ) -> None:
        self.service = service
        self.retry_policy = retry_policy or RetryPolicy()
        self.supports_all_drives = supports_all_drives

    @staticmethod
    def protected_ranges_notice() -> str:
        return PROTECTED_RANGES_NOTICE

    def _call(
        self,
        func: Callable[[], Any],
        *,
        retryable: bool = True,
    ) -> Any:
        if getattr(self.service, "_gdm_executes_with_retry", False):
            return func()
        return execute_with_retry(
            func,
            policy=self.retry_policy,
            retryable=retryable,
        )

    def _normalize_http_error(self, exc: HttpError) -> Exception:
        return normalize_http_error(exc)

    def get_capabilities(self, file_id: str) -> CapabilityView:
        def _get() -> dict[str, Any]:
            try:
                return self.service.files.get(
                    fileId=file_id,
                    fields=self.CAPABILITY_FIELDS,
                    supportsAllDrives=self.supports_all_drives,
                )
            except HttpError as exc:
                raise self._normalize_http_error(exc) from exc

        data = self._call(_get)
        caps = data.get("capabilities", {})
        return CapabilityView(
            can_edit=bool(caps.get("canEdit")),
            can_share=bool(caps.get("canShare")),
            can_copy=bool(caps.get("canCopy")),
            can_download=bool(caps.get("canDownload")),
            view_only=not bool(caps.get("canEdit")),
        )

    def list_permissions(self, file_id: str) -> list[PermissionView]:
        permissions: list[PermissionView] = []
        page_token: str | None = None
        while True:

            def _list(token: str | None = page_token) -> dict[str, Any]:
                try:
                    return self.service.permissions.list(
                        fileId=file_id,
                        fields="nextPageToken,permissions(id,role,type,emailAddress,domain,allowFileDiscovery,deleted)",
                        pageSize=100,
                        pageToken=token,
                        supportsAllDrives=self.supports_all_drives,
                    )
                except HttpError as exc:
                    raise self._normalize_http_error(exc) from exc

            response = self._call(_list)
            for item in response.get("permissions", []):
                permissions.append(
                    PermissionView(
                        permission_id=str(item["id"]),
                        role=str(item.get("role", "")),
                        type=str(item.get("type", "")),
                        email_address=item.get("emailAddress"),
                        domain=item.get("domain"),
                        allow_file_discovery=item.get("allowFileDiscovery"),
                        deleted=bool(item.get("deleted")),
                    )
                )
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return permissions

    def share(
        self,
        file_id: str,
        *,
        role: PermissionRole,
        type: PermissionType,  # noqa: A002
        email_address: str | None = None,
        domain: str | None = None,
        allow_file_discovery: bool = False,
        notify: bool = False,
    ) -> PermissionView:
        body: dict[str, Any] = {"role": role.value, "type": type.value}
        if email_address is not None:
            body["emailAddress"] = email_address
        if domain is not None:
            body["domain"] = domain
        if type == PermissionType.ANYONE:
            body["allowFileDiscovery"] = allow_file_discovery

        def _create() -> dict[str, Any]:
            try:
                return self.service.permissions.create(
                    fileId=file_id,
                    body=body,
                    supportsAllDrives=self.supports_all_drives,
                    sendNotificationEmail=notify,
                    fields="id,role,type,emailAddress,domain,allowFileDiscovery",
                )
            except HttpError as exc:
                raise self._normalize_http_error(exc) from exc

        created = self._call(_create, retryable=False)
        return PermissionView(
            permission_id=str(created["id"]),
            role=str(created.get("role", "")),
            type=str(created.get("type", "")),
            email_address=created.get("emailAddress"),
            domain=created.get("domain"),
            allow_file_discovery=created.get("allowFileDiscovery"),
        )

    def unshare(self, file_id: str, permission_id: str) -> None:
        def _delete() -> None:
            try:
                self.service.permissions.delete(
                    fileId=file_id,
                    permissionId=permission_id,
                    supportsAllDrives=self.supports_all_drives,
                )
            except HttpError as exc:
                raise self._normalize_http_error(exc) from exc

        self._call(_delete, retryable=False)

    def ensure_writable(self, file_id: str) -> None:
        caps = self.get_capabilities(file_id)
        if not caps.can_edit:
            msg = f"Insufficient permissions to modify file {file_id}."
            raise PermissionDeniedError(msg)

    def service_account_root_hint(self) -> str:
        return (
            "Service accounts do not own My Drive content. Share the GDM root "
            "folder (and shared drives as needed) with the service account "
            "email before automation can read or write project data."
        )

    def assert_not_rls(self) -> None:
        """Document that worksheet protected ranges are not authorization."""
        logger.debug(PROTECTED_RANGES_NOTICE)
