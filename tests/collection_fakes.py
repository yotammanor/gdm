"""Shared fakes for collection-centered storage tests."""

from __future__ import annotations

from typing import Any

from gdm.collection.constants import (
    DOC_MIME,
    FOLDER_MIME,
    SHORTCUT_MIME,
    SLIDE_MIME,
)
from gdm.project import GDM_FOLDER_NAME


class CollectionMemoryDriveFiles:
    """Drive files fake with parent-folder listing for collections."""

    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self.content: dict[str, bytes] = {}
        self._counter = 0
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _next(self) -> str:
        self._counter += 1
        return f"id-{self._counter}"

    def seed(
        self,
        *,
        file_id: str | None = None,
        name: str,
        mime_type: str,
        parents: list[str],
        **extra: Any,
    ) -> str:
        fid = file_id or self._next()
        self.records[fid] = {
            "id": fid,
            "name": name,
            "mimeType": mime_type,
            "version": "1",
            "md5Checksum": extra.get("md5Checksum"),
            "parents": list(parents),
            "trashed": False,
            "properties": extra.get("properties", {}),
            "appProperties": extra.get("appProperties", {}),
            "description": extra.get("description"),
            "starred": extra.get("starred"),
            "createdTime": extra.get("createdTime", "2024-01-01T00:00:00.000Z"),
            "modifiedTime": extra.get("modifiedTime", "2024-01-01T00:00:00.000Z"),
            "webViewLink": extra.get("webViewLink"),
            "size": extra.get("size"),
            "capabilities": extra.get("capabilities"),
            "shortcutDetails": extra.get("shortcutDetails"),
            "resourceKey": extra.get("resourceKey"),
        }
        return fid

    def create(
        self,
        *,
        body: dict[str, Any],
        media_body: Any = None,
        fields: str | None = None,
        supportsAllDrives: bool = True,
    ) -> dict[str, Any]:
        del fields, supportsAllDrives
        self.calls.append(("create", dict(body)))
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
        addParents: str | None = None,
        removeParents: str | None = None,
    ) -> dict[str, Any]:
        del fields, supportsAllDrives
        self.calls.append(
            (
                "update",
                {
                    "fileId": fileId,
                    "body": body,
                    "addParents": addParents,
                    "removeParents": removeParents,
                    "media": media_body is not None,
                },
            )
        )
        record = self.records[fileId]
        if body:
            record.update(body)
        if addParents:
            parents = set(record.get("parents", []))
            parents.add(addParents)
            record["parents"] = list(parents)
        if removeParents:
            remove = set(removeParents.split(","))
            record["parents"] = [
                p for p in record.get("parents", []) if p not in remove
            ]
        if media_body is not None:
            payload = getattr(media_body, "_fd", None)
            if payload is not None:
                payload.seek(0)
                self.content[fileId] = payload.read()
            elif hasattr(media_body, "getvalue"):
                self.content[fileId] = media_body.getvalue()
            elif hasattr(media_body, "_body"):
                self.content[fileId] = media_body._body
        record["version"] = str(int(record.get("version", "1")) + 1)
        return record

    def get(
        self,
        *,
        fileId: str,
        fields: str,
        supportsAllDrives: bool = True,
    ) -> dict[str, Any]:
        del fields, supportsAllDrives
        self.calls.append(("get", {"fileId": fileId}))
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
        del fields, supportsAllDrives, includeItemsFromAllDrives, corpora, driveId
        self.calls.append(("list", {"q": q}))
        files = [f for f in self.records.values() if not f.get("trashed")]
        if q:
            files = self._filter_query(files, q)
        start = int(pageToken) if pageToken else 0
        limit = pageSize or 100
        page = files[start : start + limit]
        next_token = str(start + limit) if start + limit < len(files) else None
        return {"files": page, "nextPageToken": next_token}

    def delete(self, *, fileId: str, supportsAllDrives: bool = True) -> None:
        del supportsAllDrives
        self.calls.append(("delete", {"fileId": fileId}))
        del self.records[fileId]

    def export(self, *, fileId: str, mimeType: str) -> bytes:
        self.calls.append(("export", {"fileId": fileId, "mimeType": mimeType}))
        return self.content.get(fileId, b"exported-native")

    def get_media(self, *, fileId: str, supportsAllDrives: bool = True) -> Any:
        del supportsAllDrives
        self.calls.append(("get_media", {"fileId": fileId}))
        from unittest.mock import MagicMock

        request = MagicMock()
        data = self.content.get(fileId, b"")

        def next_chunk(num_retries: int = 0) -> tuple[None, bool]:
            del num_retries
            return None, True

        request.next_chunk = next_chunk
        buffer = __import__("io").BytesIO(data)
        request._buffer = buffer
        return request

    @staticmethod
    def _filter_query(files: list[dict[str, Any]], q: str) -> list[dict[str, Any]]:
        parent_id = None
        if " in parents" in q:
            parent_id = q.split("'")[1]
        name = None
        if "name = '" in q:
            name = q.split("name = '")[1].split("'")[0].replace("\\'", "'")
        mime_exclude_folder = "mimeType != 'application/vnd.google-apps.folder'" in q
        mime_type = None
        if "mimeType = '" in q:
            mime_type = q.split("mimeType = '")[1].split("'")[0]
        matches: list[dict[str, Any]] = []
        for record in files:
            if parent_id and parent_id not in record.get("parents", []):
                continue
            if name and record.get("name") != name:
                continue
            if mime_exclude_folder and record.get("mimeType") == FOLDER_MIME:
                continue
            if mime_type and record.get("mimeType") != mime_type:
                continue
            if "gdm_model" in q:
                model = q.split("value='")[1].split("'")[0].replace("\\'", "'")
                if record.get("appProperties", {}).get("gdm_model") != model:
                    continue
            if "gdm_project" in q:
                project = q.split("value='")[1].split("'")[0].replace("\\'", "'")
                if record.get("appProperties", {}).get("gdm_project") != project:
                    continue
            if f"name = '{GDM_FOLDER_NAME}'" in q or "name = '.gdm'" in q:
                if record.get("name") != GDM_FOLDER_NAME:
                    continue
            matches.append(record)
        return matches


class CollectionMemoryDriveService:
    def __init__(self) -> None:
        self.files = CollectionMemoryDriveFiles()

    def read_file_bytes(self, file_id: str) -> bytes:
        return self.files.content.get(file_id, b"{}")

    def write_file_bytes(self, file_id: str, data: bytes) -> None:
        self.files.content[file_id] = data


class FakeDocsDocuments:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.documents: dict[str, dict[str, Any]] = {}

    def get(self, *, documentId: str) -> Any:
        self.calls.append(("get", {"documentId": documentId}))

        class Request:
            def execute(self) -> dict[str, Any]:
                payload = FakeDocsDocuments._owner.documents.setdefault(
                    documentId,
                    {
                        "documentId": documentId,
                        "body": {},
                        "revisionId": "doc-revision-1",
                    },
                )
                payload.setdefault("revisionId", "doc-revision-1")
                return dict(payload)

        FakeDocsDocuments._owner = self
        return Request()

    def batchUpdate(self, *, documentId: str, body: dict[str, Any]) -> Any:
        self.calls.append(("batchUpdate", {"documentId": documentId, "body": body}))

        class Request:
            def execute(self) -> dict[str, Any]:
                owner = FakeDocsDocuments._owner
                payload = owner.documents.setdefault(
                    documentId,
                    {
                        "documentId": documentId,
                        "body": {},
                        "revisionId": "doc-revision-1",
                    },
                )
                current = str(payload.setdefault("revisionId", "doc-revision-1"))
                required = body.get("writeControl", {}).get("requiredRevisionId")
                if required != current:
                    raise ValueError("stale document revision")
                updated = f"{current}-next"
                payload["revisionId"] = updated
                return {
                    "documentId": documentId,
                    "replies": [],
                    "writeControl": {"requiredRevisionId": updated},
                }

        return Request()


class FakeDocsService:
    def __init__(self) -> None:
        self._documents = FakeDocsDocuments()

    def documents(self) -> FakeDocsDocuments:
        return self._documents


class FakeSlidesPresentations:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.presentations: dict[str, dict[str, Any]] = {}

    def get(self, *, presentationId: str) -> Any:
        self.calls.append(("get", {"presentationId": presentationId}))

        class Request:
            def execute(self) -> dict[str, Any]:
                payload = FakeSlidesPresentations._owner.presentations.setdefault(
                    presentationId,
                    {
                        "presentationId": presentationId,
                        "slides": [],
                        "revisionId": "slides-revision-1",
                    },
                )
                payload.setdefault("revisionId", "slides-revision-1")
                return dict(payload)

        FakeSlidesPresentations._owner = self
        return Request()

    def batchUpdate(self, *, presentationId: str, body: dict[str, Any]) -> Any:
        self.calls.append(
            ("batchUpdate", {"presentationId": presentationId, "body": body})
        )

        class Request:
            def execute(self) -> dict[str, Any]:
                owner = FakeSlidesPresentations._owner
                payload = owner.presentations.setdefault(
                    presentationId,
                    {
                        "presentationId": presentationId,
                        "slides": [],
                        "revisionId": "slides-revision-1",
                    },
                )
                current = str(payload.setdefault("revisionId", "slides-revision-1"))
                required = body.get("writeControl", {}).get("requiredRevisionId")
                if required != current:
                    raise ValueError("stale presentation revision")
                updated = f"{current}-next"
                payload["revisionId"] = updated
                return {
                    "presentationId": presentationId,
                    "replies": [],
                    "writeControl": {"requiredRevisionId": updated},
                }

        FakeSlidesPresentations._owner = self
        return Request()


class FakeNativeSheetsService:
    """Minimal Sheets API fake for native sheet content handles."""

    def spreadsheets(self) -> FakeNativeSheetsService:
        return self

    def get(self, *, spreadsheetId: str, fields: str | None = None) -> dict[str, Any]:
        del spreadsheetId, fields
        return {"spreadsheetId": "sheet-1"}

    def batchUpdate(
        self,
        *,
        spreadsheetId: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        del spreadsheetId, body
        return {"replies": []}


class FakeSlidesService:
    def __init__(self) -> None:
        self._presentations = FakeSlidesPresentations()

    def presentations(self) -> FakeSlidesPresentations:
        return self._presentations


def seed_mixed_collection(
    files: CollectionMemoryDriveFiles, folder_id: str
) -> dict[str, str]:
    """Seed doc, slide, image, pdf, nested folder, shortcut, duplicate names."""
    ids: dict[str, str] = {}
    ids["doc"] = files.seed(
        name="Brief",
        mime_type=DOC_MIME,
        parents=[folder_id],
    )
    ids["slide"] = files.seed(
        name="Deck",
        mime_type=SLIDE_MIME,
        parents=[folder_id],
    )
    ids["image"] = files.seed(
        name="logo.png",
        mime_type="image/png",
        parents=[folder_id],
        md5Checksum="abc123",
    )
    ids["pdf"] = files.seed(
        name="spec.pdf",
        mime_type="application/pdf",
        parents=[folder_id],
        md5Checksum="pdf999",
    )
    ids["nested"] = files.seed(
        name="Nested",
        mime_type=FOLDER_MIME,
        parents=[folder_id],
    )
    ids["nested_doc"] = files.seed(
        name="Inner",
        mime_type=DOC_MIME,
        parents=[ids["nested"]],
    )
    ids["shortcut"] = files.seed(
        name="Brief shortcut",
        mime_type=SHORTCUT_MIME,
        parents=[folder_id],
        shortcutDetails={"targetId": ids["doc"], "targetMimeType": DOC_MIME},
    )
    ids["dup_a"] = files.seed(name="duplicate", mime_type=DOC_MIME, parents=[folder_id])
    ids["dup_b"] = files.seed(name="duplicate", mime_type=DOC_MIME, parents=[folder_id])
    ids["unmodeled"] = files.seed(
        name="notes.txt",
        mime_type="text/plain",
        parents=[folder_id],
    )
    return ids
