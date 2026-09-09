# ruff: noqa: N803
"""Tests for google-api-python-client executing adapters."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from gdm.google_api import (
    ExecutingDriveFiles,
    ExecutingDriveService,
    wrap_drive_service,
)
from gdm.permissions import DrivePermissionsService, PermissionsManager
from gdm.sync import ChangesPoller, DriveChangesService


class FakeHttpRequest:
    def __init__(self, payload: Any) -> None:
        self._payload = payload
        self.execute = MagicMock(return_value=payload)


class FakeDriveFilesResource:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, **kwargs: Any) -> FakeHttpRequest:
        self.calls.append(("get", kwargs))
        return FakeHttpRequest({"id": kwargs["fileId"], "name": "file"})

    def list(self, **kwargs: Any) -> FakeHttpRequest:
        self.calls.append(("list", kwargs))
        return FakeHttpRequest({"files": []})


class FakeDriveService:
    def files(self) -> FakeDriveFilesResource:
        return FakeDriveFilesResource()

    def permissions(self) -> MagicMock:
        return MagicMock()

    def changes(self) -> MagicMock:
        return MagicMock()


def test_executing_drive_files_runs_execute() -> None:
    resource = FakeDriveFilesResource()
    files = ExecutingDriveFiles(resource)
    result = files.get(fileId="abc", fields="id,name")
    assert result["id"] == "abc"
    assert resource.calls[0][0] == "get"


def test_wrap_drive_service_accepts_discovery_shape() -> None:
    wrapped = wrap_drive_service(FakeDriveService())
    meta = wrapped.files.get(fileId="x", fields="id")
    assert meta["id"] == "x"


def test_direct_dict_injection_still_works() -> None:
    class DirectFiles:
        def get(self, *, fileId: str, fields: str, supportsAllDrives: bool = True):
            return {"id": fileId, "fields": fields}

    files = ExecutingDriveFiles(DirectFiles())
    assert files.get(fileId="1", fields="id")["id"] == "1"


def test_executing_drive_service_satisfies_permissions_and_changes_protocols() -> None:
    service = ExecutingDriveService(FakeDriveService())
    assert isinstance(service, DrivePermissionsService)
    assert isinstance(service, DriveChangesService)
    PermissionsManager(service)
    ChangesPoller(service)


def test_get_media_returns_unexecuted_request() -> None:
    resource = MagicMock()
    request = FakeHttpRequest(b"data")
    resource.get_media.return_value = request
    files = ExecutingDriveFiles(resource)
    media = files.get_media(fileId="1")
    assert media is request
    request.execute.assert_not_called()
