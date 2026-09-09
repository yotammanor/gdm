"""Additional google API adapter tests."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from gdm.google_api import (
    ExecutingDriveChanges,
    ExecutingDrivePermissions,
    ExecutingSheetsService,
    wrap_sheets_service,
)


class FakeHttpRequest:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def execute(self) -> Any:
        return self._payload


class FakeSheetsResource:
    def values(self) -> MagicMock:
        values = MagicMock()
        values.get.return_value = FakeHttpRequest({"values": [["a"]]})
        return values

    def get(self, **kwargs: Any) -> FakeHttpRequest:
        return FakeHttpRequest({"sheets": []})

    def batchUpdate(self, **kwargs: Any) -> FakeHttpRequest:
        return FakeHttpRequest({})


class FakeSheetsRoot:
    def spreadsheets(self) -> FakeSheetsResource:
        return FakeSheetsResource()


class FakeChangesResource:
    def getStartPageToken(self, **kwargs: Any) -> FakeHttpRequest:
        return FakeHttpRequest({"startPageToken": "tok"})

    def list(self, **kwargs: Any) -> FakeHttpRequest:
        return FakeHttpRequest({"changes": []})


def test_wrap_sheets_service_executes_values_get() -> None:
    service = wrap_sheets_service(FakeSheetsRoot())
    assert isinstance(service, ExecutingSheetsService)
    result = service.spreadsheets().values().get(spreadsheetId="id", range="Sheet1!A1")
    assert result["values"] == [["a"]]


def test_executing_permissions_and_changes() -> None:
    perms = ExecutingDrivePermissions(MagicMock())
    perms._resource.create.return_value = FakeHttpRequest({"id": "p1"})
    created = perms.create(fileId="f", body={"role": "reader", "type": "anyone"})
    assert created["id"] == "p1"

    changes = ExecutingDriveChanges(FakeChangesResource())
    token = changes.getStartPageToken(supportsAllDrives=True)
    assert token["startPageToken"] == "tok"
