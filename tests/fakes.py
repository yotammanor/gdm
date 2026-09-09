"""Shared test doubles for backend and project tests."""

from __future__ import annotations

import re
from typing import Any

from gdm.errors import NotFoundError


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
        addParents: str | None = None,
        removeParents: str | None = None,
        fields: str | None = None,
        supportsAllDrives: bool = True,
    ) -> dict[str, Any]:
        if body:
            self.records[fileId].update(body)
        if addParents:
            parents = self.records[fileId].setdefault("parents", [])
            for parent in addParents.split(","):
                if parent and parent not in parents:
                    parents.append(parent)
        if removeParents:
            parents = self.records[fileId].setdefault("parents", [])
            remove = {p for p in removeParents.split(",") if p}
            self.records[fileId]["parents"] = [p for p in parents if p not in remove]
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
        try:
            return self.records[fileId]
        except KeyError as exc:
            raise NotFoundError(f"Drive file {fileId!r} not found.") from exc

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
        if q and " in parents" in q:
            for clause in q.split(" and "):
                if " in parents" in clause:
                    parent = clause.split("'")[1]
                    files = [f for f in files if parent in f.get("parents", [])]
        start = int(pageToken) if pageToken else 0
        limit = pageSize or 100
        page = files[start : start + limit]
        next_token = str(start + limit) if start + limit < len(files) else None
        return {"files": page, "nextPageToken": next_token}

    def delete(self, *, fileId: str, supportsAllDrives: bool = True) -> None:
        del self.records[fileId]

    def export(self, *, fileId: str, mimeType: str) -> bytes:
        return self.content.get(fileId, b"exported")

    def get_media(self, *, fileId: str, supportsAllDrives: bool = True) -> Any:
        from unittest.mock import MagicMock

        request = MagicMock()

        def next_chunk(num_retries: int = 0) -> tuple[None, bool]:
            return None, True

        request.next_chunk = next_chunk
        return request


class MemoryDriveService:
    def __init__(self) -> None:
        self.files = MemoryDriveFiles()


class FakeDriveFiles:
    def __init__(self) -> None:
        self.files: dict[str, dict[str, Any]] = {}
        self.contents: dict[str, bytes] = {}
        self._next_id = 1

    def _new_id(self) -> str:
        fid = f"file-{self._next_id}"
        self._next_id += 1
        return fid

    def create(
        self,
        *,
        body: dict[str, Any],
        media_body: Any = None,
        fields: str | None = None,
        supportsAllDrives: bool = True,
    ) -> dict[str, Any]:
        fid = self._new_id()
        record = {"id": fid, **body}
        self.files[fid] = record
        if media_body is not None and hasattr(media_body, "getvalue"):
            self.contents[fid] = media_body.getvalue()
        return {"id": fid}

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
        del pageToken, corpora, driveId
        matches = []
        for record in self.files.values():
            if record.get("trashed"):
                continue
            if not q:
                matches.append(record)
                continue
            parent_match = re.search(r"'([^']+)' in parents", q)
            if parent_match and parent_match.group(1) not in record.get("parents", []):
                continue
            name_match = re.search(r"name = '((?:\\'|[^'])*)'", q)
            if name_match and record.get("name") != name_match.group(1).replace(
                "\\'",
                "'",
            ):
                continue
            mime_match = re.search(r"mimeType = '([^']+)'", q)
            if mime_match and record.get("mimeType") != mime_match.group(1):
                continue
            mime_exclusion = re.search(r"mimeType != '([^']+)'", q)
            if mime_exclusion and record.get("mimeType") == mime_exclusion.group(1):
                continue
            matches.append(record)
        return {"files": matches[: pageSize or 100]}

    def get(
        self, *, fileId: str, fields: str, supportsAllDrives: bool = True
    ) -> dict[str, Any]:
        try:
            return self.files[fileId]
        except KeyError as exc:
            raise NotFoundError(f"Drive file {fileId!r} not found.") from exc

    def update(
        self,
        *,
        fileId: str,
        body: dict[str, Any] | None = None,
        media_body: Any = None,
        addParents: str | None = None,
        removeParents: str | None = None,
        fields: str | None = None,
        supportsAllDrives: bool = True,
    ) -> dict[str, Any]:
        if body:
            self.files[fileId].update(body)
        if addParents:
            parents = self.files[fileId].setdefault("parents", [])
            for parent in addParents.split(","):
                if parent and parent not in parents:
                    parents.append(parent)
        if removeParents:
            parents = self.files[fileId].setdefault("parents", [])
            remove = {p for p in removeParents.split(",") if p}
            self.files[fileId]["parents"] = [p for p in parents if p not in remove]
        if media_body is not None and hasattr(media_body, "getvalue"):
            self.contents[fileId] = media_body.getvalue()
        return self.files[fileId]


class FakeDrive:
    def __init__(self) -> None:
        self.files = FakeDriveFiles()

    def read_file_bytes(self, file_id: str) -> bytes:
        return self.files.contents[file_id]

    def write_file_bytes(self, file_id: str, data: bytes) -> None:
        self.files.contents[file_id] = data


class FakeValues:
    def __init__(self, table: list[list[Any]]) -> None:
        self.table = table

    def get(
        self,
        *,
        spreadsheetId: str,
        range: str,
        majorDimension: str = "ROWS",
        valueRenderOption: str | None = None,
    ) -> dict[str, Any]:
        del spreadsheetId, majorDimension, valueRenderOption
        if range.endswith("1:1"):
            return {"values": [self.table[0]]} if self.table else {"values": []}
        if ":ZZ" in range:
            return {"values": list(self.table)}
        if ":" in range.split("!")[1]:
            start = int(range.split("!")[1].split(":")[0]) - 1
            return {"values": self.table[start:]}
        return {"values": []}

    def append(
        self,
        *,
        spreadsheetId: str,
        range: str,
        body: dict[str, Any],
        valueInputOption: str = "USER_ENTERED",
        insertDataOption: str = "INSERT_ROWS",
    ) -> dict[str, Any]:
        del spreadsheetId, range, valueInputOption, insertDataOption
        self.table.append(body["values"][0])
        return {"updates": {"updatedRows": 1}}

    def update(
        self,
        *,
        spreadsheetId: str,
        range: str,
        body: dict[str, Any],
        valueInputOption: str = "USER_ENTERED",
    ) -> dict[str, Any]:
        del spreadsheetId, valueInputOption
        row = int(range.split("!")[1].split(":")[0])
        values = body["values"][0]
        if not self.table:
            self.table.append(values)
            return {"updatedRows": 1}
        if row - 1 >= len(self.table):
            while len(self.table) < row:
                self.table.append([""] * len(self.table[0]))
        self.table[row - 1] = values
        return {"updatedRows": 1}

    def clear(
        self,
        *,
        spreadsheetId: str,
        range: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del spreadsheetId, body
        row = int(range.split("!")[1].split(":")[0])
        if 0 < row <= len(self.table):
            self.table[row - 1] = [""] * len(self.table[row - 1])
        return {}

    def batchUpdate(
        self, *, spreadsheetId: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        del spreadsheetId
        for item in body["data"]:
            row = int(item["range"].split("!")[1].split(":")[0])
            self.table[row - 1] = item["values"][0]
        return {"totalUpdatedRows": len(body["data"])}


class FakeSpreadsheets:
    def __init__(
        self,
        values: FakeValues,
        *,
        titles: list[str] | None = None,
    ) -> None:
        self._values = values
        self.titles = list(titles or ["Sheet1"])
        self.add_sheet_calls: list[str] = []

    def values(self) -> FakeValues:
        return self._values

    def get(self, *, spreadsheetId: str, fields: str | None = None) -> dict[str, Any]:
        del spreadsheetId, fields
        return {
            "sheets": [
                {"properties": {"sheetId": index, "title": title}}
                for index, title in enumerate(self.titles)
            ]
        }

    def batchUpdate(
        self, *, spreadsheetId: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        del spreadsheetId
        for req in body.get("requests", []):
            add_sheet = req.get("addSheet")
            if add_sheet is not None:
                title = str(add_sheet["properties"]["title"])
                if title not in self.titles:
                    self.titles.append(title)
                    self.add_sheet_calls.append(title)
            delete = req.get("deleteDimension")
            if delete and delete.get("dimension") == "COLUMNS":
                idx = int(delete["range"]["startIndex"])
                for row in self._values.table:
                    if idx < len(row):
                        del row[idx]
        return {}


class FakeSheetsService:
    def __init__(
        self,
        table: list[list[Any]],
        *,
        titles: list[str] | None = None,
    ) -> None:
        self._values = FakeValues(table)
        self._spreadsheets = FakeSpreadsheets(self._values, titles=titles)

    def spreadsheets(self) -> FakeSpreadsheets:
        return self._spreadsheets
