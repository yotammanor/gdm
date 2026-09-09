"""Branch coverage for DriveBackend and SheetsBackend collection-adjacent paths."""
# ruff: noqa: N801, PT011, B017

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from googleapiclient.errors import HttpError

from gdm.backends.drive import JSON_MIME, DriveBackend
from gdm.backends.sheets import SheetsBackend
from gdm.collection.constants import DOC_MIME
from gdm.errors import (
    AmbiguousResourceError,
    ConflictError,
    NotFoundError,
    UnsupportedOperationError,
)
from tests.fakes import FakeSheetsService


class MemoryDriveFiles:
    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self.content: dict[str, bytes] = {}
        self._counter = 0

    def _next(self) -> str:
        self._counter += 1
        return f"id-{self._counter}"

    def create(self, **kwargs: Any) -> dict[str, Any]:
        body = kwargs["body"]
        fid = self._next()
        record = {
            "id": fid,
            "version": "1",
            "md5Checksum": None,
            "trashed": False,
            **body,
        }
        media = kwargs.get("media_body")
        if media is not None and hasattr(media, "getvalue"):
            self.content[fid] = media.getvalue()
        self.records[fid] = record
        return record

    def update(self, **kwargs: Any) -> dict[str, Any]:
        file_id = kwargs["fileId"]
        if kwargs.get("body"):
            self.records[file_id].update(kwargs["body"])
        media = kwargs.get("media_body")
        if media is not None and hasattr(media, "getvalue"):
            self.content[file_id] = media.getvalue()
        self.records[file_id]["version"] = str(
            int(self.records[file_id].get("version", "1")) + 1
        )
        return self.records[file_id]

    def get(self, **kwargs: Any) -> dict[str, Any]:
        return self.records[kwargs["fileId"]]

    def list(self, **kwargs: Any) -> dict[str, Any]:
        files = list(self.records.values())
        q = kwargs.get("q") or ""
        if " in parents" in q and "name =" in q:
            parent = q.split("'")[1]
            name = q.split("name = '")[1].split("'")[0]
            files = [
                f
                for f in files
                if parent in f.get("parents", []) and f.get("name") == name
            ]
        if "gdm_model" in q:
            model_part = q.split("gdm_model", 1)[1]
            model = model_part.split("value='")[1].split("'")[0].replace("\\'", "'")
            files = [
                f for f in files if f.get("appProperties", {}).get("gdm_model") == model
            ]
        if "gdm_project" in q:
            project_part = q.split("gdm_project", 1)[1]
            project = project_part.split("value='")[1].split("'")[0].replace("\\'", "'")
            files = [
                f
                for f in files
                if f.get("appProperties", {}).get("gdm_project") == project
            ]
        if q and " in parents" in q and "name =" not in q:
            import re

            match = re.search(r"'([^']+)' in parents", q)
            if match:
                parent = match.group(1)
                files = [f for f in files if parent in f.get("parents", [])]
        start = int(kwargs["pageToken"]) if kwargs.get("pageToken") else 0
        limit = kwargs.get("pageSize") or 100
        page = files[start : start + limit]
        next_token = str(start + limit) if start + limit < len(files) else None
        return {"files": page, "nextPageToken": next_token}

    def delete(self, **kwargs: Any) -> None:
        del self.records[kwargs["fileId"]]

    def export(self, **kwargs: Any) -> bytes:
        return self.content.get(kwargs["fileId"], b"exported")

    def get_media(self, **kwargs: Any) -> MagicMock:
        request = MagicMock()

        def next_chunk(num_retries: int = 0) -> tuple[None, bool]:
            del num_retries
            return None, True

        request.next_chunk = next_chunk
        return request


class MemoryDriveService:
    def __init__(self) -> None:
        self.files = MemoryDriveFiles()


def _backend() -> DriveBackend:
    return DriveBackend(MemoryDriveService(), project_id="proj-1")


def test_drive_read_json_non_object_and_conflicts() -> None:
    backend = _backend()
    parent = backend.service.files.create(
        body={"name": "bad.json", "mimeType": JSON_MIME, "parents": ["root"]},
        media_body=MagicMock(getvalue=lambda: b"[]"),
    )
    with patch.object(backend, "_download_bytes", return_value=b"[]"):
        with pytest.raises(UnsupportedOperationError, match="JSON object"):
            backend.read_json(parent["id"])
    good = backend.service.files.create(
        body={"name": "good.json", "mimeType": JSON_MIME, "parents": ["root"]},
        media_body=MagicMock(getvalue=lambda: json.dumps({"a": 1}).encode()),
    )
    backend.service.files.records[good["id"]]["version"] = "2"
    with patch.object(
        backend,
        "_download_bytes",
        return_value=json.dumps({"a": 1}).encode(),
    ):
        with pytest.raises(ConflictError):
            backend.read_json(good["id"], expected_version="1")


def test_drive_write_json_merge_and_native_guard() -> None:
    backend = _backend()
    parent = "folder-1"
    created = backend.write_json(
        parent_id=parent,
        model_name="Tag",
        record_id="t1",
        payload={"id": "t1", "name": "A"},
    )
    with patch.object(
        backend,
        "_download_bytes",
        return_value=json.dumps({"id": "t1", "name": "A", "extra": "keep"}).encode(),
    ):
        merged = backend.write_json(
            parent_id=parent,
            model_name="Tag",
            record_id="t1",
            payload={"name": "B"},
            file_id=created.file_id,
            preserve_unknown=True,
        )
    with patch.object(
        backend,
        "_download_bytes",
        return_value=json.dumps({"id": "t1", "name": "B", "extra": "keep"}).encode(),
    ):
        payload = backend.read_json(merged.file_id)
    assert payload["name"] == "B"
    assert payload["extra"] == "keep"
    native = backend.service.files.create(
        body={"name": "Doc", "mimeType": DOC_MIME, "parents": [parent]},
    )
    with pytest.raises(UnsupportedOperationError, match="Native Workspace"):
        backend.write_json(
            parent_id=parent,
            model_name="Tag",
            record_id="t1",
            payload={"id": "t1"},
            file_id=native["id"],
        )


def test_drive_scan_iter_and_find() -> None:
    backend = _backend()
    parent = "scan-parent"
    for index in range(5):
        backend.write_json(
            parent_id=parent,
            model_name="Item",
            record_id=f"id-{index}",
            payload={"id": f"id-{index}"},
        )
    page = backend.scan_by_model(
        model_name="Item",
        parent_id=parent,
        scan_limit=2,
        page_size=1,
    )
    assert page.truncated is True
    collected = list(
        backend.iter_model_files(model_name="Item", parent_id=parent, scan_limit=3)
    )
    assert len(collected) == 3
    with pytest.raises(NotFoundError):
        backend.find_by_name(name="missing", parent_id=parent)
    backend.service.files.create(
        body={"name": "dup", "mimeType": JSON_MIME, "parents": [parent]},
        media_body=MagicMock(getvalue=lambda: b"{}"),
    )
    backend.service.files.create(
        body={"name": "dup", "mimeType": JSON_MIME, "parents": [parent]},
        media_body=MagicMock(getvalue=lambda: b"{}"),
    )
    with pytest.raises(AmbiguousResourceError):
        backend.find_by_name(name="dup", parent_id=parent)


def test_drive_export_text_and_fingerprint() -> None:
    backend = _backend()
    parent = "text-parent"
    native = backend.service.files.create(
        body={"name": "Brief", "mimeType": DOC_MIME, "parents": [parent]},
    )
    data, mime = backend.export_native(native["id"])
    assert data == b"exported"
    assert mime
    with pytest.raises(UnsupportedOperationError):
        backend.export_native(
            backend.service.files.create(
                body={"name": "x", "mimeType": "text/plain", "parents": [parent]},
            )["id"]
        )
    text_file = backend.write_text_body(
        parent_id=parent,
        name="notes.txt",
        body="hello",
    )
    with patch.object(backend, "_download_bytes", return_value=b"hello"):
        assert backend.read_text_body(text_file.file_id) == "hello"
    with pytest.raises(UnsupportedOperationError):
        backend.write_text_body(
            parent_id=parent,
            name="n",
            body="x",
            file_id=native["id"],
        )
    meta = backend.get_metadata(text_file.file_id)
    assert backend.fingerprint(meta) in {
        meta.file_id,
        meta.md5_checksum or f"version:{meta.version}",
    }


def test_drive_download_without_get_media() -> None:
    class NoMediaService:
        class files:
            @staticmethod
            def get(**kwargs: Any) -> dict[str, Any]:
                return {
                    "id": kwargs["fileId"],
                    "name": "bin",
                    "mimeType": "application/octet-stream",
                    "parents": [],
                    "version": "1",
                }

    backend = DriveBackend(NoMediaService(), project_id="p")
    meta = backend.get_metadata("file-1")
    with pytest.raises(UnsupportedOperationError, match="cannot download media"):
        backend._download_bytes(meta)


def test_sheets_backend_conflicts_and_empty_headers() -> None:
    table = [
        ["row_id", "label", "_gdm_id", "_gdm_version", "_gdm_updated_at"],
        ["r1", "One", "r1", "1", "2024-01-01T00:00:00+00:00"],
    ]
    backend = SheetsBackend(FakeSheetsService(table))
    schema = backend.get_headers("sheet-1", "Sheet1")
    assert schema.headers
    empty = SheetsBackend(FakeSheetsService([]))
    empty_schema = empty.get_headers("sheet-2", "Sheet1")
    assert empty_schema.headers == []
    row = backend.find_row_by_primary_key(
        "sheet-1",
        "Sheet1",
        primary_key="r1",
        primary_key_column="row_id",
    )
    with pytest.raises(ConflictError):
        backend.update_row(
            "sheet-1",
            "Sheet1",
            row_index=row.row_index,
            values={"label": "Two"},
            expected_version=99,
        )
    with pytest.raises(ConflictError):
        backend.update_row(
            "sheet-1",
            "Sheet1",
            row_index=row.row_index,
            values={"label": "Two"},
            expected_fingerprint="wrong",
        )


def test_drive_write_text_update_existing() -> None:
    backend = _backend()
    parent = "text-update"
    created = backend.write_text_body(
        parent_id=parent,
        name="notes.txt",
        body="v1",
    )
    updated = backend.write_text_body(
        parent_id=parent,
        name="notes.txt",
        body="v2",
        file_id=created.file_id,
    )
    assert updated.file_id == created.file_id


def test_drive_fingerprint_conflict() -> None:
    backend = _backend()
    parent = "conflict-parent"
    created = backend.write_json(
        parent_id=parent,
        model_name="Tag",
        record_id="c1",
        payload={"id": "c1"},
    )
    with pytest.raises(ConflictError):
        backend.read_json(created.file_id, expected_fingerprint="wrong-fp")
    with patch.object(backend, "_download_bytes", return_value=b"{}"):
        with pytest.raises(ConflictError):
            backend.read_json(created.file_id, expected_version="999")


def test_drive_content_fingerprint_tolerates_version_drift() -> None:
    backend = _backend()
    created = backend.write_json(
        parent_id="conflict-parent",
        model_name="Tag",
        record_id="c1",
        payload={"id": "c1"},
    )
    record = backend.service.files.records[created.file_id]
    record["md5Checksum"] = "stable-content"
    record["version"] = "2"
    with patch.object(backend, "_download_bytes", return_value=b"{}"):
        assert (
            backend.read_json(
                created.file_id,
                expected_version="1",
                expected_fingerprint="stable-content",
            )
            == {}
        )


def test_drive_http_error_on_delete() -> None:
    class ErrorFiles(MemoryDriveFiles):
        def delete(self, **kwargs: Any) -> None:
            raise HttpError(resp=MagicMock(status=404), content=b"missing")

    class ErrorService:
        files = ErrorFiles()

    backend = DriveBackend(ErrorService(), project_id="p")
    with pytest.raises(Exception):
        backend.delete("missing-id")
