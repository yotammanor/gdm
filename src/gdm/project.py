"""Root-scoped GDM project metadata and registry."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any

from gdm.backends.drive import DriveServiceLike
from gdm.collection.constants import (
    METADATA_BACKEND_JSON,
    METADATA_BACKEND_SHEETS,
    METADATA_JSON_FILENAME,
    METADATA_SIDECAR_FORMAT_VERSION,
)
from gdm.errors import (
    AmbiguousResourceError,
    ForeignProjectError,
    GDMError,
    NotFoundError,
)

logger = logging.getLogger(__name__)

GDM_FOLDER_NAME = ".gdm"
MANIFEST_FILENAME = "manifest.json"
REGISTRY_FILENAME = "registry.json"
FORMAT_VERSION = "1"
COLLECTIONS_FOLDER = "collections"

MetadataBackend = str  # "sheets" | "json"
DEFAULT_METADATA_BACKEND = METADATA_BACKEND_SHEETS


class ResourceKind(StrEnum):
    FILE = "file"
    FOLDER = "folder"
    SPREADSHEET = "spreadsheet"
    WORKSHEET = "worksheet"


@dataclass(frozen=True)
class ProjectManifest:
    project_id: uuid.UUID
    format_version: str
    root_folder_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": str(self.project_id),
            "format_version": self.format_version,
            "root_folder_id": self.root_folder_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProjectManifest:
        return cls(
            project_id=uuid.UUID(str(data["project_id"])),
            format_version=str(data["format_version"]),
            root_folder_id=str(data["root_folder_id"]),
        )


@dataclass(frozen=True)
class RegistryEntry:
    model_name: str
    resource_id: str
    kind: ResourceKind
    parent_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model_name": self.model_name,
            "resource_id": self.resource_id,
            "kind": self.kind.value,
        }
        if self.parent_id is not None:
            payload["parent_id"] = self.parent_id
        if self.extra:
            payload["extra"] = self.extra
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RegistryEntry:
        extra = dict(data.get("extra", {}))
        return cls(
            model_name=str(data["model_name"]),
            resource_id=str(data["resource_id"]),
            kind=ResourceKind(str(data["kind"])),
            parent_id=data.get("parent_id"),
            extra=extra,
        )


@dataclass
class ProjectRegistry:
    entries: dict[str, RegistryEntry] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"entries": {k: v.to_dict() for k, v in self.entries.items()}}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProjectRegistry:
        raw = data.get("entries", {})
        entries = {k: RegistryEntry.from_dict(v) for k, v in raw.items()}
        return cls(entries=entries)

    def register(self, entry: RegistryEntry) -> None:
        self.entries[entry.model_name] = entry

    def get(self, model_name: str) -> RegistryEntry | None:
        return self.entries.get(model_name)


def _escape_drive_query_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


class Project:
    """A GDM project anchored to one Drive root folder."""

    def __init__(
        self,
        *,
        project_id: uuid.UUID,
        root_folder_id: str,
        drive: DriveServiceLike,
        manifest: ProjectManifest | None = None,
        registry: ProjectRegistry | None = None,
        gdm_folder_id: str | None = None,
        supports_all_drives: bool = True,
        adoption_mode: bool = False,
    ) -> None:
        self.project_id = project_id
        self.root_folder_id = root_folder_id
        self.drive = drive
        self.manifest = manifest or ProjectManifest(
            project_id=project_id,
            format_version=FORMAT_VERSION,
            root_folder_id=root_folder_id,
        )
        self.registry = registry or ProjectRegistry()
        self.gdm_folder_id = gdm_folder_id
        self.supports_all_drives = supports_all_drives
        self.adoption_mode = adoption_mode

    @classmethod
    def create_new(
        cls,
        *,
        root_folder_id: str,
        drive: DriveServiceLike,
        project_id: uuid.UUID | None = None,
        supports_all_drives: bool = True,
    ) -> Project:
        pid = project_id or uuid.uuid4()
        project = cls(
            project_id=pid,
            root_folder_id=root_folder_id,
            drive=drive,
            supports_all_drives=supports_all_drives,
        )
        project.initialize()
        return project

    @classmethod
    def open(
        cls,
        *,
        root_folder_id: str,
        drive: DriveServiceLike,
        supports_all_drives: bool = True,
        adoption_mode: bool = False,
    ) -> Project:
        """Discover an existing project under ``root_folder_id``."""
        gdm_id = cls._find_child_folder(
            drive,
            parent_id=root_folder_id,
            name=GDM_FOLDER_NAME,
            supports_all_drives=supports_all_drives,
        )
        if gdm_id is None:
            msg = f"No {GDM_FOLDER_NAME} folder under root {root_folder_id!r}."
            raise NotFoundError(msg)

        manifest = cls._read_manifest(drive, gdm_id, supports_all_drives)
        if manifest.root_folder_id != root_folder_id:
            msg = (
                f"Manifest root_folder_id {manifest.root_folder_id!r} does not "
                f"match requested root {root_folder_id!r}."
            )
            raise ForeignProjectError(msg)

        registry = cls._read_registry(drive, gdm_id, supports_all_drives)
        return cls(
            project_id=manifest.project_id,
            root_folder_id=root_folder_id,
            drive=drive,
            manifest=manifest,
            registry=registry,
            gdm_folder_id=gdm_id,
            supports_all_drives=supports_all_drives,
            adoption_mode=adoption_mode,
        )

    def initialize(self) -> None:
        """Create the ``.gdm`` metadata tree and persist manifest/registry."""
        existing = self._find_gdm_folder()
        if existing is not None:
            foreign = self._read_manifest_if_present(existing)
            if foreign is not None and foreign.project_id != self.project_id:
                msg = (
                    f"Root already contains a foreign GDM project {foreign.project_id}."
                )
                raise ForeignProjectError(msg)
            self.gdm_folder_id = existing
        else:
            self.gdm_folder_id = self._create_folder(
                GDM_FOLDER_NAME,
                parent_id=self.root_folder_id,
            )

        self._ensure_subfolder(COLLECTIONS_FOLDER)

        self._write_json_file(
            MANIFEST_FILENAME,
            self.manifest.to_dict(),
            parent_id=self.gdm_folder_id,
        )
        self._write_json_file(
            REGISTRY_FILENAME,
            self.registry.to_dict(),
            parent_id=self.gdm_folder_id,
        )

    def register_resource(self, entry: RegistryEntry) -> None:
        self.registry.register(entry)
        if self.gdm_folder_id is None:
            msg = "Project is not initialized."
            raise GDMError(msg)
        self._write_json_file(
            REGISTRY_FILENAME,
            self.registry.to_dict(),
            parent_id=self.gdm_folder_id,
        )

    def get_registered(self, model_name: str) -> RegistryEntry:
        entry = self.registry.get(model_name)
        if entry is None:
            msg = f"Model {model_name!r} is not registered."
            raise NotFoundError(msg)
        return entry

    def resolve_path(self, relative_path: str) -> str:
        """Resolve a POSIX path under the project root to a folder ID."""
        if relative_path.startswith("/") or ".." in PurePosixPath(relative_path).parts:
            msg = f"Path must stay under project root: {relative_path!r}"
            raise ValueError(msg)

        current = self.root_folder_id
        parts = [p for p in PurePosixPath(relative_path).parts if p]
        for part in parts:
            matches = self._list_child_folders(current, name=part)
            if not matches:
                msg = f"No folder named {part!r} under {current}."
                raise NotFoundError(msg)
            if len(matches) > 1:
                msg = f"Ambiguous folder name {part!r}: {matches}"
                raise AmbiguousResourceError(msg)
            current = matches[0]
        return current

    def assert_resource_belongs_to_project(
        self,
        *,
        resource_id: str,
        app_properties: dict[str, str] | None,
        adoption_mode: bool | None = None,
    ) -> None:
        """Reject resources tagged for another project unless adopting."""
        allow_foreign = self.adoption_mode if adoption_mode is None else adoption_mode
        if allow_foreign or not app_properties:
            return
        tagged = app_properties.get("gdm_project")
        if tagged and tagged != str(self.project_id):
            msg = (
                f"Resource {resource_id} belongs to project {tagged}, "
                f"not {self.project_id}."
            )
            raise ForeignProjectError(msg)

    def assert_resource_in_project_scope(
        self,
        resource_id: str,
    ) -> dict[str, Any]:
        """Return metadata after verifying ownership and root ancestry.

        Explicit adoption mode bypasses both checks so callers can consolidate
        resources from another GDM project before moving or retagging them.
        """
        metadata = self.drive.files.get(
            fileId=resource_id,
            fields="id,mimeType,parents,appProperties",
            supportsAllDrives=self.supports_all_drives,
        )
        app_properties = metadata.get("appProperties") or {}
        self.assert_resource_belongs_to_project(
            resource_id=resource_id,
            app_properties={
                str(key): str(value) for key, value in app_properties.items()
            },
        )
        if self.adoption_mode or resource_id == self.root_folder_id:
            return metadata

        pending = [str(parent) for parent in metadata.get("parents", [])]
        visited: set[str] = set()
        while pending:
            parent_id = pending.pop()
            if parent_id == self.root_folder_id:
                return metadata
            if parent_id in visited:
                continue
            visited.add(parent_id)
            if len(visited) > 1000:
                msg = (
                    f"Parent traversal for resource {resource_id!r} "
                    "exceeded 1000 folders."
                )
                raise GDMError(msg)
            parent = self.drive.files.get(
                fileId=parent_id,
                fields="id,parents",
                supportsAllDrives=self.supports_all_drives,
            )
            pending.extend(str(value) for value in parent.get("parents", []))

        msg = (
            f"Resource {resource_id!r} is outside project root {self.root_folder_id!r}."
        )
        raise ForeignProjectError(msg)

    def folder_id_for(self, subfolder: str) -> str:
        if self.gdm_folder_id is None:
            msg = "Project is not initialized."
            raise GDMError(msg)
        return self._ensure_subfolder(subfolder)

    def collections_folder_id(self) -> str:
        """Return the ``.gdm/collections`` folder ID."""
        return self.folder_id_for(COLLECTIONS_FOLDER)

    def ensure_collection_metadata_binding(
        self,
        model_name: str,
        *,
        create_spreadsheet: Callable[[str, str], str] | None = None,
    ) -> tuple[str, str]:
        """Ensure metadata spreadsheet/worksheet IDs exist for a collection."""
        entry = self.registry.get(model_name)
        if entry is not None:
            sheet_id = entry.extra.get("metadata_spreadsheet_id")
            worksheet = entry.extra.get("metadata_worksheet", "metadata")
            if sheet_id:
                return str(sheet_id), str(worksheet)
        collections_root = self.collections_folder_id()
        subfolder = self._ensure_named_subfolder(collections_root, model_name)
        sheet_id = self._find_metadata_spreadsheet(subfolder)
        if sheet_id is None:
            if create_spreadsheet is None:
                sheet_id = self._create_metadata_spreadsheet(subfolder, model_name)
            else:
                sheet_id = create_spreadsheet(subfolder, model_name)
        worksheet = "metadata"
        if entry is not None:
            extra = dict(entry.extra)
            extra["metadata_backend"] = METADATA_BACKEND_SHEETS
            extra["metadata_spreadsheet_id"] = sheet_id
            extra["metadata_worksheet"] = worksheet
            self.register_resource(
                RegistryEntry(
                    model_name=entry.model_name,
                    resource_id=entry.resource_id,
                    kind=entry.kind,
                    parent_id=entry.parent_id,
                    extra=extra,
                )
            )
        return sheet_id, worksheet

    def ensure_collection_json_metadata_binding(self, model_name: str) -> str:
        """Ensure a JSON metadata sidecar exists for a collection."""
        entry = self.registry.get(model_name)
        if entry is not None:
            file_id = entry.extra.get("metadata_json_file_id")
            if file_id:
                return str(file_id)
        collections_root = self.collections_folder_id()
        subfolder = self._ensure_named_subfolder(collections_root, model_name)
        file_id = self._find_metadata_json_file(subfolder)
        if file_id is None:
            file_id = self._create_metadata_json_file(subfolder)
        if entry is not None:
            extra = dict(entry.extra)
            extra["metadata_backend"] = METADATA_BACKEND_JSON
            extra["metadata_json_file_id"] = file_id
            self.register_resource(
                RegistryEntry(
                    model_name=entry.model_name,
                    resource_id=entry.resource_id,
                    kind=entry.kind,
                    parent_id=entry.parent_id,
                    extra=extra,
                )
            )
        return file_id

    def metadata_backend_for(
        self,
        model_name: str,
        *,
        default: str = DEFAULT_METADATA_BACKEND,
    ) -> str:
        """Return the configured metadata backend for ``model_name``."""
        entry = self.registry.get(model_name)
        if entry is None:
            return default
        backend = entry.extra.get("metadata_backend")
        if backend in {METADATA_BACKEND_SHEETS, METADATA_BACKEND_JSON}:
            return str(backend)
        return default

    def _ensure_named_subfolder(self, parent_id: str, name: str) -> str:
        existing = self._find_child_folder(
            self.drive,
            parent_id=parent_id,
            name=name,
            supports_all_drives=self.supports_all_drives,
        )
        if existing is not None:
            return existing
        return self._create_folder(name, parent_id=parent_id)

    def _find_metadata_spreadsheet(self, parent_id: str) -> str | None:
        query = (
            f"'{parent_id}' in parents and trashed = false and "
            "mimeType = 'application/vnd.google-apps.spreadsheet'"
        )
        response = self.drive.files.list(
            q=query,
            fields="files(id,name)",
            pageSize=2,
            supportsAllDrives=self.supports_all_drives,
            includeItemsFromAllDrives=self.supports_all_drives,
        )
        files = response.get("files", [])
        if not files:
            return None
        if len(files) > 1:
            ids = [str(entry["id"]) for entry in files]
            msg = f"Multiple metadata spreadsheets under {parent_id}: {ids}"
            raise AmbiguousResourceError(msg)
        return str(files[0]["id"])

    def _create_metadata_spreadsheet(self, parent_id: str, model_name: str) -> str:
        body = {
            "name": f"{model_name}-metadata",
            "mimeType": "application/vnd.google-apps.spreadsheet",
            "parents": [parent_id],
        }
        created = self.drive.files.create(
            body=body,
            fields="id",
            supportsAllDrives=self.supports_all_drives,
        )
        return str(created["id"])

    def _find_metadata_json_file(self, parent_id: str) -> str | None:
        return self._find_child_file_optional(
            parent_id,
            METADATA_JSON_FILENAME,
        )

    def _find_child_file_optional(self, parent_id: str, filename: str) -> str | None:
        try:
            return self._find_child_file(
                self.drive,
                parent_id,
                filename,
                self.supports_all_drives,
            )
        except NotFoundError:
            return None

    def _create_metadata_json_file(self, parent_id: str) -> str:
        return self._write_json_file(
            METADATA_JSON_FILENAME,
            {
                "format_version": METADATA_SIDECAR_FORMAT_VERSION,
                "sidecar_version": "0",
                "rows": {},
            },
            parent_id=parent_id,
        )

    @staticmethod
    def _find_child_folder(
        drive: DriveServiceLike,
        *,
        parent_id: str,
        name: str,
        supports_all_drives: bool,
    ) -> str | None:
        escaped = _escape_drive_query_value(name)
        query = (
            f"'{parent_id}' in parents and name = '{escaped}' "
            "and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        )
        response = drive.files.list(
            q=query,
            fields="files(id,name)",
            pageSize=2,
            supportsAllDrives=supports_all_drives,
            includeItemsFromAllDrives=supports_all_drives,
        )
        files = response.get("files", [])
        if not files:
            return None
        if len(files) > 1:
            ids = [f["id"] for f in files]
            msg = f"Ambiguous folder name {name!r} under {parent_id}: {ids}"
            raise AmbiguousResourceError(msg)
        return str(files[0]["id"])

    def _find_gdm_folder(self) -> str | None:
        return self._find_child_folder(
            self.drive,
            parent_id=self.root_folder_id,
            name=GDM_FOLDER_NAME,
            supports_all_drives=self.supports_all_drives,
        )

    @classmethod
    def _read_manifest(
        cls,
        drive: DriveServiceLike,
        gdm_folder_id: str,
        supports_all_drives: bool,
    ) -> ProjectManifest:
        data = cls._read_json_by_name(
            drive,
            gdm_folder_id,
            MANIFEST_FILENAME,
            supports_all_drives,
        )
        return ProjectManifest.from_dict(data)

    def _read_manifest_if_present(self, gdm_folder_id: str) -> ProjectManifest | None:
        try:
            data = self._read_json_by_name(
                self.drive,
                gdm_folder_id,
                MANIFEST_FILENAME,
                self.supports_all_drives,
            )
        except NotFoundError:
            return None
        return ProjectManifest.from_dict(data)

    @classmethod
    def _read_registry(
        cls,
        drive: DriveServiceLike,
        gdm_folder_id: str,
        supports_all_drives: bool,
    ) -> ProjectRegistry:
        data = cls._read_json_by_name(
            drive,
            gdm_folder_id,
            REGISTRY_FILENAME,
            supports_all_drives,
        )
        return ProjectRegistry.from_dict(data)

    @classmethod
    def _read_json_by_name(
        cls,
        drive: DriveServiceLike,
        parent_id: str,
        filename: str,
        supports_all_drives: bool,
    ) -> dict[str, Any]:
        file_id = cls._find_child_file(drive, parent_id, filename, supports_all_drives)
        meta = drive.files.get(
            fileId=file_id,
            fields="id,mimeType",
            supportsAllDrives=supports_all_drives,
        )
        if meta.get("mimeType") == "application/vnd.google-apps.folder":
            msg = f"Expected JSON file {filename!r}, found folder."
            raise GDMError(msg)
        # Content download is delegated to DriveBackend in full stack; here we
        # expect test doubles to expose media via files().get side channels or
        # callers patch _load_file_bytes. For project metadata we use a helper
        # attribute on fake clients: ``read_file_bytes(file_id)``.
        reader = getattr(drive, "read_file_bytes", None)
        if reader is None:
            msg = "Drive service wrapper must provide read_file_bytes for metadata IO."
            raise GDMError(msg)
        raw = reader(file_id)
        return json.loads(raw.decode("utf-8"))

    @classmethod
    def _find_child_file(
        cls,
        drive: DriveServiceLike,
        parent_id: str,
        name: str,
        supports_all_drives: bool,
    ) -> str:
        escaped = _escape_drive_query_value(name)
        query = (
            f"'{parent_id}' in parents and name = '{escaped}' "
            "and mimeType != 'application/vnd.google-apps.folder' and trashed = false"
        )
        response = drive.files.list(
            q=query,
            fields="files(id,name)",
            pageSize=2,
            supportsAllDrives=supports_all_drives,
            includeItemsFromAllDrives=supports_all_drives,
        )
        files = response.get("files", [])
        if not files:
            msg = f"File {name!r} not found under {parent_id}."
            raise NotFoundError(msg)
        if len(files) > 1:
            ids = [f["id"] for f in files]
            msg = f"Ambiguous file name {name!r} under {parent_id}: {ids}"
            raise AmbiguousResourceError(msg)
        return str(files[0]["id"])

    def _list_child_folders(
        self, parent_id: str, *, name: str | None = None
    ) -> list[str]:
        escaped = _escape_drive_query_value(name) if name else None
        query = (
            f"'{parent_id}' in parents and mimeType = "
            "'application/vnd.google-apps.folder' and trashed = false"
        )
        if escaped is not None:
            query += f" and name = '{escaped}'"
        ids: list[str] = []
        page_token: str | None = None
        while True:
            response = self.drive.files.list(
                q=query,
                fields="nextPageToken,files(id)",
                pageSize=100,
                pageToken=page_token,
                supportsAllDrives=self.supports_all_drives,
                includeItemsFromAllDrives=self.supports_all_drives,
            )
            ids.extend(str(f["id"]) for f in response.get("files", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return ids

    def _create_folder(self, name: str, *, parent_id: str) -> str:
        body = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
        }
        created = self.drive.files.create(
            body=body,
            fields="id",
            supportsAllDrives=self.supports_all_drives,
        )
        return str(created["id"])

    def _ensure_subfolder(self, name: str) -> str:
        assert self.gdm_folder_id is not None
        existing = self._find_child_folder(
            self.drive,
            parent_id=self.gdm_folder_id,
            name=name,
            supports_all_drives=self.supports_all_drives,
        )
        if existing is not None:
            return existing
        return self._create_folder(name, parent_id=self.gdm_folder_id)

    def _write_json_file(
        self,
        filename: str,
        payload: dict[str, Any],
        *,
        parent_id: str,
    ) -> str:
        writer = getattr(self.drive, "write_file_bytes", None)
        if writer is None:
            msg = "Drive service wrapper must provide write_file_bytes for metadata IO."
            raise GDMError(msg)
        data = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        existing = None
        try:
            existing = self._find_child_file(
                self.drive,
                parent_id,
                filename,
                self.supports_all_drives,
            )
        except NotFoundError:
            existing = None
        if existing is None:
            body = {
                "name": filename,
                "mimeType": "application/json",
                "parents": [parent_id],
            }
            created = self.drive.files.create(
                body=body,
                fields="id",
                supportsAllDrives=self.supports_all_drives,
            )
            file_id = str(created["id"])
        else:
            file_id = existing
        writer(file_id, data)
        return file_id
