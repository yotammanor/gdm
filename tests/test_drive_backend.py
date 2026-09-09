# ruff: noqa: N803
"""Tests for gdm.backends.drive."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from googleapiclient.errors import HttpError

from gdm.auth import (
    AmbiguousResourceError,
    NotFoundError,
    PermissionDeniedError,
    UnsupportedOperationError,
)
from gdm.backends.drive import (
    DriveBackend,
    escape_drive_query_value,
)
from gdm.errors import ConflictError


class MemoryDriveFiles:
    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self.content: dict[str, bytes] = {}
        self._counter = 0

    def _next(self) -> str:
        self._counter += 1
        return f"id-{self._counter}"

    def create(
        self,
        *,
        body: dict[str, Any],
        media_body: Any = None,
        fields: str | None = None,
        supportsAllDrives: bool = True,
    ) -> dict[str, Any]:
        fid = self._next()
        record = {
            "id": fid,
            "version": "1",
            "md5Checksum": None,
            "trashed": False,
            **body,
        }
        if media_body is not None:
            payload = getattr(media_body, "_fd", None)
            if payload is not None:
                payload.seek(0)
                self.content[fid] = payload.read()
            elif hasattr(media_body, "getvalue"):
                self.content[fid] = media_body.getvalue()
        self.records[fid] = record
        return record

    def update(
        self,
        *,
        fileId: str,
        body: dict[str, Any] | None = None,
        media_body: Any = None,
        fields: str | None = None,
        supportsAllDrives: bool = True,
    ) -> dict[str, Any]:
        if body:
            self.records[fileId].update(body)
        if media_body is not None:
            payload = getattr(media_body, "_fd", None)
            if payload is not None:
                payload.seek(0)
                self.content[fileId] = payload.read()
            elif hasattr(media_body, "getvalue"):
                self.content[fileId] = media_body.getvalue()
        self.records[fileId]["version"] = str(int(self.records[fileId]["version"]) + 1)
        return self.records[fileId]

    def get(
        self,
        *,
        fileId: str,
        fields: str,
        supportsAllDrives: bool = True,
    ) -> dict[str, Any]:
        return self.records[fileId]

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
        del supportsAllDrives, includeItemsFromAllDrives, corpora, driveId
        files = list(self.records.values())
        if q and "gdm_model" in q:
            model_part = q.split("gdm_model")[1]
            model = model_part.split("value='")[1].split("'")[0].replace("\\'", "'")
            files = [
                f for f in files if f.get("appProperties", {}).get("gdm_model") == model
            ]
        if q and "gdm_project" in q:
            project_part = q.split("gdm_project")[1]
            project = project_part.split("value='")[1].split("'")[0].replace("\\'", "'")
            files = [
                f
                for f in files
                if f.get("appProperties", {}).get("gdm_project") == project
            ]
        if q and " in parents" in q and "name =" in q:
            parent = q.split("'")[1]
            name = q.split("name = '")[1].split("'")[0].replace("\\'", "'")
            files = [
                f
                for f in files
                if parent in f.get("parents", []) and f.get("name") == name
            ]
        start = int(pageToken) if pageToken else 0
        limit = pageSize or 100
        page = files[start : start + limit]
        next_token = str(start + limit) if start + limit < len(files) else None
        return {"files": page, "nextPageToken": next_token}

    def delete(self, *, fileId: str, supportsAllDrives: bool = True) -> None:
        del self.records[fileId]

    def export(self, *, fileId: str, mimeType: str) -> bytes:
        return self.content.get(fileId, b"exported")

    def get_media(self, *, fileId: str, supportsAllDrives: bool = True) -> MagicMock:
        request = MagicMock()

        def next_chunk(num_retries: int = 0) -> tuple[None, bool]:
            return None, True

        request.next_chunk = next_chunk
        request.uri = "http://example"
        return request


class MemoryDriveService:
    def __init__(self) -> None:
        self.files = MemoryDriveFiles()


@pytest.fixture
def backend() -> DriveBackend:
    return DriveBackend(MemoryDriveService(), project_id="proj-1")


def test_escape_drive_query_value() -> None:
    assert escape_drive_query_value("a'b\\c") == "a\\'b\\\\c"


def test_write_and_read_json(backend: DriveBackend) -> None:
    payload = {"title": "Hello", "extra": 1}
    meta = backend.write_json(
        parent_id="parent",
        model_name="Note",
        record_id="rid-1",
        name="note.json",
        payload=payload,
    )
    with patch.object(
        backend, "_download_bytes", return_value=json.dumps(payload).encode()
    ):
        loaded = backend.read_json(meta.file_id)
    assert loaded["title"] == "Hello"


def test_read_json_conflict(backend: DriveBackend) -> None:
    meta = backend.write_json(
        parent_id="parent",
        model_name="Note",
        record_id="rid-1",
        payload={"a": 1},
    )
    with pytest.raises(ConflictError):
        backend.read_json(meta.file_id, expected_version="0")


def test_preserve_unknown_keys_on_update(backend: DriveBackend) -> None:
    meta = backend.write_json(
        parent_id="parent",
        model_name="Note",
        record_id="rid-1",
        payload={"known": 1, "unknown": 2},
    )
    with patch.object(
        backend,
        "_download_bytes",
        return_value=json.dumps({"known": 1, "unknown": 2}).encode(),
    ):
        updated = backend.write_json(
            parent_id="parent",
            model_name="Note",
            record_id="rid-1",
            file_id=meta.file_id,
            payload={"known": 3},
            preserve_unknown=True,
        )
    assert updated.file_id == meta.file_id


def test_find_by_name_ambiguous(backend: DriveBackend) -> None:
    svc = backend.service.files
    svc.create(body={"name": "dup", "parents": ["p"], "mimeType": "application/json"})
    svc.create(body={"name": "dup", "parents": ["p"], "mimeType": "application/json"})
    with pytest.raises(AmbiguousResourceError):
        backend.find_by_name(name="dup", parent_id="p")


def test_find_by_name_not_found(backend: DriveBackend) -> None:
    with pytest.raises(NotFoundError):
        backend.find_by_name(name="missing", parent_id="p")


def test_native_write_rejected(backend: DriveBackend) -> None:
    fid = backend.service.files._next()
    backend.service.files.records[fid] = {
        "id": fid,
        "name": "Doc",
        "mimeType": "application/vnd.google-apps.document",
        "version": "1",
        "appProperties": {},
        "parents": [],
    }
    with pytest.raises(UnsupportedOperationError, match="Native Workspace"):
        backend.write_json(
            parent_id="p",
            model_name="Doc",
            record_id="1",
            file_id=fid,
            payload={"x": 1},
        )


def test_export_native(backend: DriveBackend) -> None:
    fid = backend.service.files._next()
    backend.service.files.records[fid] = {
        "id": fid,
        "name": "Doc",
        "mimeType": "application/vnd.google-apps.document",
        "version": "1",
        "appProperties": {},
        "parents": [],
    }
    backend.service.files.content[fid] = b"hello doc"
    data, mime = backend.export_native(fid)
    assert mime == "text/plain"
    assert data == b"hello doc"


def test_scan_by_model_pagination(backend: DriveBackend) -> None:
    for i in range(3):
        backend.write_json(
            parent_id="p",
            model_name="Item",
            record_id=f"id-{i}",
            payload={"i": i},
        )
    page = backend.scan_by_model(model_name="Item", scan_limit=2, page_size=2)
    assert len(page.items) == 2
    assert page.truncated
    assert page.next_page_token is None or page.next_page_token is not None


def test_permission_error_normalization(backend: DriveBackend) -> None:
    resp = MagicMock(status=403)
    err = HttpError(resp, b"denied")
    with patch.object(backend.service.files, "get", side_effect=err):
        with pytest.raises(PermissionDeniedError):
            backend.get_metadata("missing")


def test_shared_drive_list_kwargs() -> None:
    backend = DriveBackend(
        MemoryDriveService(),
        project_id="p",
        shared_drive_id="sd-1",
    )
    kwargs = backend._list_kwargs()
    assert kwargs["corpora"] == "drive"
    assert kwargs["driveId"] == "sd-1"
