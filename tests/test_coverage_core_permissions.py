# ruff: noqa: N803
"""Branch coverage for gdm.permissions."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from googleapiclient.errors import HttpError

from gdm.errors import NotFoundError, PermissionDeniedError
from gdm.permissions import (
    PermissionRole,
    PermissionsManager,
    PermissionType,
)


class PaginatedPermissions:
    def __init__(self) -> None:
        self.calls: list[str | None] = []

    def list(
        self,
        *,
        fileId: str,
        fields: str | None = None,
        supportsAllDrives: bool = True,
        pageSize: int | None = None,
        pageToken: str | None = None,
    ) -> dict:
        del fileId, fields, supportsAllDrives, pageSize
        self.calls.append(pageToken)
        if pageToken is None:
            return {
                "nextPageToken": "page-2",
                "permissions": [
                    {
                        "id": "p1",
                        "role": "reader",
                        "type": "user",
                        "emailAddress": "a@example.com",
                    },
                ],
            }
        return {
            "permissions": [
                {
                    "id": "p2",
                    "role": "writer",
                    "type": "domain",
                    "domain": "example.com",
                    "allowFileDiscovery": False,
                    "deleted": True,
                },
            ],
        }

    def create(
        self,
        *,
        fileId: str,
        body: dict,
        supportsAllDrives: bool = True,
        sendNotificationEmail: bool = False,
        fields: str | None = None,
    ) -> dict:
        del fileId, supportsAllDrives, sendNotificationEmail, fields
        return {"id": "created", **body}

    def delete(
        self,
        *,
        fileId: str,
        permissionId: str,
        supportsAllDrives: bool = True,
    ) -> None:
        del fileId, permissionId, supportsAllDrives


class FakeFiles:
    def get(
        self,
        *,
        fileId: str,
        fields: str,
        supportsAllDrives: bool = True,
    ) -> dict:
        del fileId, fields, supportsAllDrives
        return {
            "capabilities": {
                "canEdit": False,
                "canShare": False,
                "canCopy": True,
                "canDownload": True,
            },
        }


class FakeService:
    def __init__(self) -> None:
        self.permissions = PaginatedPermissions()
        self.files = FakeFiles()


def test_list_permissions_paginates() -> None:
    manager = PermissionsManager(FakeService())
    perms = manager.list_permissions("file-1")
    assert len(perms) == 2
    assert perms[1].domain == "example.com"
    assert perms[1].deleted is True
    assert manager.service.permissions.calls == [None, "page-2"]


def test_share_with_domain_and_anyone() -> None:
    svc = FakeService()
    manager = PermissionsManager(svc)
    domain_perm = manager.share(
        "file-1",
        role=PermissionRole.READER,
        type=PermissionType.DOMAIN,
        domain="example.com",
    )
    assert domain_perm.domain == "example.com"
    anyone_perm = manager.share(
        "file-1",
        role=PermissionRole.READER,
        type=PermissionType.ANYONE,
        allow_file_discovery=True,
        notify=True,
    )
    assert anyone_perm.type == PermissionType.ANYONE.value


def test_unshare_http_error_normalized() -> None:
    svc = FakeService()

    def _delete(**_kwargs: object) -> None:
        resp = MagicMock(status=404)
        raise HttpError(resp, b"missing")

    svc.permissions.delete = _delete  # type: ignore[method-assign]
    manager = PermissionsManager(svc)
    with pytest.raises(NotFoundError):
        manager.unshare("file-1", "perm-1")


def test_get_capabilities_view_only() -> None:
    manager = PermissionsManager(FakeService())
    caps = manager.get_capabilities("file-1")
    assert caps.view_only is True
    assert caps.can_copy is True


def test_ensure_writable_raises_permission_denied() -> None:
    manager = PermissionsManager(FakeService())
    with pytest.raises(PermissionDeniedError, match="Insufficient permissions"):
        manager.ensure_writable("file-1")


def test_assert_not_rls_logs_notice(caplog: pytest.LogCaptureFixture) -> None:
    manager = PermissionsManager(FakeService())
    with caplog.at_level("DEBUG"):
        manager.assert_not_rls()
    assert any("row-level security" in rec.message for rec in caplog.records)


def test_service_account_root_hint() -> None:
    manager = PermissionsManager(FakeService())
    assert "service account" in manager.service_account_root_hint().lower()
