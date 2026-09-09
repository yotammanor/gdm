# ruff: noqa: N803, SIM102
"""Tests for gdm.project."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from gdm.auth import (
    ForeignProjectError,
)
from gdm.project import (
    GDM_FOLDER_NAME,
    Project,
    ProjectManifest,
    RegistryEntry,
    ResourceKind,
)


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
        fields: str | None = None,
        supportsAllDrives: bool = True,
    ) -> dict[str, Any]:
        fid = self._new_id()
        record = {"id": fid, **body}
        self.files[fid] = record
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
            if (
                (q and "name = '.gdm'" in q and record.get("name") == GDM_FOLDER_NAME)
                or (
                    q
                    and "name = 'manifest.json'" in q
                    and record.get("name") == "manifest.json"
                )
                or (
                    q
                    and "name = 'registry.json'" in q
                    and record.get("name") == "registry.json"
                )
                or (q and "name = 'schemas'" in q and record.get("name") == "schemas")
            ):
                matches.append(record)
            elif q and "parents" in q:
                parent = q.split("'")[1]
                if parent in record.get("parents", []) and "mimeType" in q:
                    if (
                        "folder" in q
                        and record.get("mimeType")
                        == "application/vnd.google-apps.folder"
                    ):
                        if "name =" in q:
                            name = q.split("name = '")[1].split("'")[0]
                            if record.get("name") == name.replace("\\'", "'"):
                                matches.append(record)
                        else:
                            matches.append(record)
                    elif (
                        "folder" not in q
                        and record.get("mimeType")
                        != "application/vnd.google-apps.folder"
                    ):
                        if (
                            "manifest.json" in q
                            and record.get("name") == "manifest.json"
                        ) or (
                            "registry.json" in q
                            and record.get("name") == "registry.json"
                        ):
                            matches.append(record)
        return {"files": matches[: pageSize or 100]}

    def get(
        self, *, fileId: str, fields: str, supportsAllDrives: bool = True
    ) -> dict[str, Any]:
        return self.files[fileId]

    def update(
        self,
        *,
        fileId: str,
        body: dict[str, Any] | None = None,
        addParents: str | None = None,
        removeParents: str | None = None,
        fields: str | None = None,
        supportsAllDrives: bool = True,
    ) -> dict[str, Any]:
        if body:
            self.files[fileId].update(body)
        return self.files[fileId]


class FakeDrive:
    def __init__(self) -> None:
        self.files = FakeDriveFiles()

    def read_file_bytes(self, file_id: str) -> bytes:
        return self.files.contents[file_id]

    def write_file_bytes(self, file_id: str, data: bytes) -> None:
        self.files.contents[file_id] = data


@pytest.fixture
def drive() -> FakeDrive:
    d = FakeDrive()
    root_id = "root-folder"
    d.files.files[root_id] = {
        "id": root_id,
        "name": "Root",
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [],
    }
    return d


def test_project_initialize_creates_gdm_tree(drive: FakeDrive) -> None:
    project_id = uuid.uuid4()
    project = Project(
        project_id=project_id,
        root_folder_id="root-folder",
        drive=drive,
    )
    project.initialize()
    assert project.gdm_folder_id is not None
    assert any(f.get("name") == "manifest.json" for f in drive.files.files.values())
    assert drive.files.contents  # manifest/registry bytes written


def test_foreign_project_detected(drive: FakeDrive) -> None:
    other_id = uuid.uuid4()
    project = Project(
        project_id=other_id,
        root_folder_id="root-folder",
        drive=drive,
    )
    project.initialize()
    manifest_id = next(
        fid
        for fid, record in drive.files.files.items()
        if record.get("name") == "manifest.json"
    )
    drive.files.contents[manifest_id] = json.dumps(
        {
            "project_id": str(uuid.uuid4()),
            "format_version": "1",
            "root_folder_id": "root-folder",
        }
    ).encode()
    with pytest.raises(ForeignProjectError):
        Project(
            project_id=other_id,
            root_folder_id="root-folder",
            drive=drive,
        ).initialize()


def test_resolve_path_rejects_escape(drive: FakeDrive) -> None:
    project = Project(
        project_id=uuid.uuid4(), root_folder_id="root-folder", drive=drive
    )
    with pytest.raises(ValueError, match="under project root"):
        project.resolve_path("../secret")


def test_register_resource_persists_registry(drive: FakeDrive) -> None:
    project = Project(
        project_id=uuid.uuid4(), root_folder_id="root-folder", drive=drive
    )
    project.initialize()
    entry = RegistryEntry(
        model_name="Note",
        resource_id="file-99",
        kind=ResourceKind.FILE,
    )
    project.register_resource(entry)
    assert project.registry.get("Note") == entry


def test_assert_resource_belongs_to_project(drive: FakeDrive) -> None:
    pid = uuid.uuid4()
    project = Project(project_id=pid, root_folder_id="root-folder", drive=drive)
    project.assert_resource_belongs_to_project(
        resource_id="x",
        app_properties={"gdm_project": str(pid)},
    )
    with pytest.raises(ForeignProjectError):
        project.assert_resource_belongs_to_project(
            resource_id="x",
            app_properties={"gdm_project": str(uuid.uuid4())},
        )


def test_manifest_round_trip() -> None:
    pid = uuid.uuid4()
    manifest = ProjectManifest(
        project_id=pid, format_version="1", root_folder_id="root"
    )
    restored = ProjectManifest.from_dict(manifest.to_dict())
    assert restored == manifest
