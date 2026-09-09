"""Additional google_api adapter branch coverage."""
# ruff: noqa: PT011, B017

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from googleapiclient.errors import HttpError

from gdm.google_api import (
    ExecutingDriveChanges,
    ExecutingDriveFiles,
    ExecutingDrivePermissions,
    ExecutingDriveService,
    ExecutingSheetsValues,
)


class FakeHttpRequest:
    def __init__(self, payload: Any) -> None:
        self._payload = payload
        self.uri = "https://example.test/download"

    def execute(self) -> Any:
        return self._payload


def test_executing_drive_files_delete_and_export() -> None:
    resource = MagicMock()
    resource.delete.return_value = FakeHttpRequest(None)
    resource.export.return_value = FakeHttpRequest(b"pdf")
    files = ExecutingDriveFiles(resource)
    files.delete(fileId="f1")
    assert files.export(fileId="f1", mimeType="application/pdf") == b"pdf"


def test_executing_permissions_and_changes_methods() -> None:
    perms = MagicMock()
    perms.list.return_value = FakeHttpRequest({"permissions": []})
    perms.delete.return_value = FakeHttpRequest(None)
    wrapper = ExecutingDrivePermissions(perms)
    assert wrapper.list(fileId="f1")["permissions"] == []
    wrapper.delete(fileId="f1", permissionId="p1")

    changes = MagicMock()
    changes.list.return_value = FakeHttpRequest({"changes": []})
    change_wrapper = ExecutingDriveChanges(changes)
    assert change_wrapper.list(pageToken="tok")["changes"] == []


def test_executing_drive_service_read_file_bytes() -> None:
    service = ExecutingDriveService(MagicMock())
    request = FakeHttpRequest(b"bytes")

    class Downloader:
        def next_chunk(self, num_retries: int = 0) -> tuple[None, bool]:
            del num_retries
            return None, True

    with patch.object(service.files, "get_media", return_value=request):
        with patch("gdm.google_api.MediaIoBaseDownload") as downloader_cls:
            downloader_cls.return_value = Downloader()
            with patch("gdm.google_api.io.BytesIO") as bio:
                bio.return_value.getvalue.return_value = b"bytes"
                assert service.read_file_bytes("f1") == b"bytes"


def test_executing_resource_non_http_error_reraises() -> None:
    resource = MagicMock()
    resource.get.side_effect = RuntimeError("boom")
    files = ExecutingDriveFiles(resource)
    with pytest.raises(RuntimeError, match="boom"):
        files.get(fileId="x", fields="id")


def test_executing_sheets_values_append_and_clear() -> None:
    resource = MagicMock()
    resource.append.return_value = FakeHttpRequest({"updates": {}})
    resource.clear.return_value = FakeHttpRequest({})
    resource.batchGet.return_value = FakeHttpRequest({"valueRanges": []})
    values = ExecutingSheetsValues(resource)
    values.append(spreadsheetId="s", range="A1", body={"values": [["x"]]})
    values.clear(spreadsheetId="s", range="A1")
    assert values.batchGet(spreadsheetId="s", ranges=["A1"])["valueRanges"] == []


def test_executing_resource_http_error_path() -> None:
    resource = MagicMock()
    calls = 0

    def boom(**kwargs: Any) -> None:
        nonlocal calls
        del kwargs
        calls += 1
        raise HttpError(resp=MagicMock(status=500), content=b"err")

    resource.update.side_effect = boom
    files = ExecutingDriveFiles(resource)
    with pytest.raises(Exception):
        files.update(fileId="f1", body={"name": "n"})
    assert calls == 1
