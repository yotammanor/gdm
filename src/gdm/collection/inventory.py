"""Drive folder inventory helpers for collections."""

from __future__ import annotations

import fnmatch
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from gdm.backends.drive import escape_drive_query_value
from gdm.collection.constants import DEFAULT_LIST_FIELDS, FOLDER_MIME, SHORTCUT_MIME
from gdm.errors import AmbiguousResourceError, NotFoundError, ScanLimitExceeded


@dataclass(frozen=True, slots=True)
class DriveInventoryItem:
    """One Drive file discovered in a collection folder."""

    file_id: str
    name: str
    mime_type: str
    version: str | None = None
    md5_checksum: str | None = None
    parents: tuple[str, ...] = ()
    properties: dict[str, str] = field(default_factory=dict)
    app_properties: dict[str, str] = field(default_factory=dict)
    description: str | None = None
    starred: bool | None = None
    created_time: str | None = None
    modified_time: str | None = None
    web_view_link: str | None = None
    size: str | None = None
    capabilities: dict[str, Any] | None = None
    shortcut_target_id: str | None = None
    shortcut_target_mime: str | None = None
    resource_key: str | None = None
    trashed: bool = False

    @property
    def is_folder(self) -> bool:
        return self.mime_type == FOLDER_MIME

    @property
    def is_shortcut(self) -> bool:
        return self.mime_type == SHORTCUT_MIME


@dataclass
class DriveInventoryPage:
    items: list[DriveInventoryItem]
    next_page_token: str | None = None
    truncated: bool = False


def inventory_item_from_file(data: dict[str, Any]) -> DriveInventoryItem:
    """Build an inventory item from a Drive files resource."""
    shortcut = data.get("shortcutDetails") or {}
    app_props = data.get("appProperties") or {}
    public_props = data.get("properties") or {}
    return DriveInventoryItem(
        file_id=str(data["id"]),
        name=str(data.get("name", "")),
        mime_type=str(data.get("mimeType", "")),
        version=str(data["version"]) if data.get("version") is not None else None,
        md5_checksum=data.get("md5Checksum"),
        parents=tuple(str(p) for p in data.get("parents", [])),
        properties={str(k): str(v) for k, v in public_props.items()},
        app_properties={str(k): str(v) for k, v in app_props.items()},
        description=data.get("description"),
        starred=data.get("starred"),
        created_time=data.get("createdTime"),
        modified_time=data.get("modifiedTime"),
        web_view_link=data.get("webViewLink"),
        size=str(data["size"]) if data.get("size") is not None else None,
        capabilities=data.get("capabilities"),
        shortcut_target_id=shortcut.get("targetId"),
        shortcut_target_mime=shortcut.get("targetMimeType"),
        resource_key=data.get("resourceKey"),
        trashed=bool(data.get("trashed", False)),
    )


def drive_fingerprint(item: DriveInventoryItem) -> str:
    if item.md5_checksum:
        return item.md5_checksum
    if item.version:
        return f"version:{item.version}"
    return item.file_id


def collection_inventory_fingerprint(
    folder_id: str,
    items: list[DriveInventoryItem],
) -> str:
    """Hash direct-child metadata so out-of-band Drive edits invalidate indexes."""
    payload = [
        {
            "id": item.file_id,
            "name": item.name,
            "mime_type": item.mime_type,
            "version": item.version,
            "md5_checksum": item.md5_checksum,
            "parents": item.parents,
            "properties": item.properties,
            "app_properties": item.app_properties,
            "description": item.description,
            "starred": item.starred,
            "modified_time": item.modified_time,
            "size": item.size,
            "shortcut_target_id": item.shortcut_target_id,
            "shortcut_target_mime": item.shortcut_target_mime,
            "trashed": item.trashed,
        }
        for item in sorted(items, key=lambda candidate: candidate.file_id)
    ]
    encoded = json.dumps(
        {"folder_id": folder_id, "items": payload},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def mime_matches(mime_type: str, pattern: str) -> bool:
    if pattern.endswith("/*"):
        prefix = pattern[:-1]
        return mime_type.startswith(prefix)
    return fnmatch.fnmatch(mime_type, pattern)


class DriveInventory:
    """List and query direct children of a Drive folder."""

    def __init__(
        self,
        files_client: Any,
        *,
        folder_id: str,
        list_kwargs: Callable[[], dict[str, Any]] | None = None,
        call: Callable[[Callable[[], Any]], Any] | None = None,
    ) -> None:
        self._files = files_client
        self.folder_id = folder_id
        self._list_kwargs = list_kwargs or (dict)
        self._call = call or (lambda func: func())

    def list_children(
        self,
        *,
        page_token: str | None = None,
        page_size: int = 100,
    ) -> DriveInventoryPage:
        query = (
            f"'{self.folder_id}' in parents and trashed = false "
            f"and mimeType != '{FOLDER_MIME}'"
        )
        return self._list_query(query, page_token=page_token, page_size=page_size)

    def list_all_children(
        self,
        *,
        page_token: str | None = None,
        page_size: int = 100,
        include_folders: bool = True,
    ) -> DriveInventoryPage:
        query = f"'{self.folder_id}' in parents and trashed = false"
        if not include_folders:
            query += f" and mimeType != '{FOLDER_MIME}'"
        return self._list_query(query, page_token=page_token, page_size=page_size)

    def scan(
        self,
        *,
        scan_limit: int = 1000,
        include_folders: bool = True,
        mime_patterns: tuple[str, ...] | None = None,
    ) -> list[DriveInventoryItem]:
        collected: list[DriveInventoryItem] = []
        token: str | None = None
        while len(collected) < scan_limit:
            page = self.list_all_children(
                page_token=token,
                page_size=min(100, scan_limit - len(collected)),
                include_folders=include_folders,
            )
            for item in page.items:
                if mime_patterns and not any(
                    mime_matches(item.mime_type, pattern) for pattern in mime_patterns
                ):
                    continue
                collected.append(item)
                if len(collected) >= scan_limit:
                    if page.next_page_token:
                        raise ScanLimitExceeded(
                            f"Collection scan exceeded limit {scan_limit}.",
                            limit=scan_limit,
                            observed=len(collected),
                        )
                    return collected
            token = page.next_page_token
            if not token:
                return collected
        return collected

    def find_by_name(
        self,
        name: str,
        *,
        mime_type: str | None = None,
    ) -> list[DriveInventoryItem]:
        escaped = escape_drive_query_value(name)
        query = (
            f"'{self.folder_id}' in parents and name = '{escaped}' and trashed = false"
        )
        if mime_type:
            query += f" and mimeType = '{escape_drive_query_value(mime_type)}'"
        page = self._list_query(query, page_size=100)
        collected = list(page.items)
        token = page.next_page_token
        while token:
            page = self._list_query(query, page_token=token, page_size=100)
            collected.extend(page.items)
            token = page.next_page_token
        return collected

    def one_by_name(
        self,
        name: str,
        *,
        mime_type: str | None = None,
    ) -> DriveInventoryItem:
        matches = self.find_by_name(name, mime_type=mime_type)
        if not matches:
            msg = f"No file named {name!r} under {self.folder_id}."
            raise NotFoundError(msg)
        if len(matches) > 1:
            ids = [item.file_id for item in matches]
            msg = f"Ambiguous file name {name!r}: {ids}"
            raise AmbiguousResourceError(msg)
        return matches[0]

    def get(self, file_id: str) -> DriveInventoryItem:
        def _get() -> dict[str, Any]:
            try:
                return self._files.get(
                    fileId=file_id,
                    fields=DEFAULT_LIST_FIELDS,
                    supportsAllDrives=True,
                )
            except KeyError as exc:
                msg = f"File {file_id} not found."
                raise NotFoundError(msg) from exc

        data = self._call(_get)
        item = inventory_item_from_file(data)
        if self.folder_id not in item.parents and not item.is_shortcut:
            msg = f"File {file_id} is not a direct child of {self.folder_id}."
            raise NotFoundError(msg)
        return item

    def _list_query(
        self,
        query: str,
        *,
        page_token: str | None = None,
        page_size: int = 100,
    ) -> DriveInventoryPage:
        def _list() -> dict[str, Any]:
            return self._files.list(
                q=query,
                fields=f"nextPageToken,files({DEFAULT_LIST_FIELDS})",
                pageSize=page_size,
                pageToken=page_token,
                **self._list_kwargs(),
            )

        response = self._call(_list)
        items = [inventory_item_from_file(entry) for entry in response.get("files", [])]
        return DriveInventoryPage(
            items=items,
            next_page_token=response.get("nextPageToken"),
        )
