# ruff: noqa: N803
"""Tests for gdm.permissions."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gdm.auth import PermissionDeniedError
from gdm.permissions import (
    PROTECTED_RANGES_NOTICE,
    PermissionRole,
    PermissionsManager,
    PermissionType,
)


class FakePermissions:
    def __init__(self) -> None:
        self.items: list[dict] = [
            {
                "id": "perm-1",
                "role": "writer",
                "type": "user",
                "emailAddress": "alice@example.com",
            }
        ]

    def list(
        self,
        *,
        fileId: str,
        fields: str | None = None,
        supportsAllDrives: bool = True,
        pageSize: int | None = None,
        pageToken: str | None = None,
    ) -> dict:
        del fileId, fields, supportsAllDrives, pageSize, pageToken
        return {"permissions": self.items}

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
        created = {"id": "perm-2", **body}
        self.items.append(created)
        return created

    def delete(
        self,
        *,
        fileId: str,
        permissionId: str,
        supportsAllDrives: bool = True,
    ) -> None:
        del fileId, supportsAllDrives
        self.items = [p for p in self.items if p["id"] != permissionId]


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
                "canEdit": True,
                "canShare": True,
                "canCopy": True,
                "canDownload": True,
            }
        }


class FakeDriveService:
    def __init__(self) -> None:
        self.permissions = FakePermissions()
        self.files = FakeFiles()


@pytest.fixture
def manager() -> PermissionsManager:
    return PermissionsManager(FakeDriveService())


def test_protected_ranges_notice() -> None:
    assert "not row-level security" in PROTECTED_RANGES_NOTICE
    assert "editing convenience" in PermissionsManager.protected_ranges_notice()


def test_get_capabilities(manager: PermissionsManager) -> None:
    caps = manager.get_capabilities("file-1")
    assert caps.can_edit
    assert caps.can_share


def test_list_permissions(manager: PermissionsManager) -> None:
    perms = manager.list_permissions("file-1")
    assert len(perms) == 1
    assert perms[0].email_address == "alice@example.com"


def test_share_and_unshare(manager: PermissionsManager) -> None:
    created = manager.share(
        "file-1",
        role=PermissionRole.READER,
        type=PermissionType.USER,
        email_address="bob@example.com",
    )
    assert created.permission_id == "perm-2"
    manager.unshare("file-1", created.permission_id)
    assert len(manager.list_permissions("file-1")) == 1


def test_ensure_writable_denied() -> None:
    svc = FakeDriveService()
    svc.files = MagicMock()
    svc.files.get.return_value = {"capabilities": {"canEdit": False}}
    manager = PermissionsManager(svc)
    with pytest.raises(PermissionDeniedError):
        manager.ensure_writable("file-1")


def test_service_account_hint(manager: PermissionsManager) -> None:
    assert "Share the GDM root folder" in manager.service_account_root_hint()
