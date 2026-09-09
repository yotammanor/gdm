"""Branch coverage for Project metadata provisioning and discovery."""

from __future__ import annotations

import uuid

import pytest

from gdm.collection.constants import FOLDER_MIME
from gdm.errors import (
    AmbiguousResourceError,
    ForeignProjectError,
    GDMError,
    NotFoundError,
)
from gdm.project import Project, RegistryEntry, ResourceKind
from tests.collection_fakes import CollectionMemoryDriveService


def test_project_resolve_path_and_folder_helpers() -> None:
    drive = CollectionMemoryDriveService()
    root = drive.files.seed(
        file_id="root", name="Root", mime_type=FOLDER_MIME, parents=[]
    )
    child = drive.files.seed(name="Child", mime_type=FOLDER_MIME, parents=[root])
    project = Project(project_id=uuid.uuid4(), root_folder_id=root, drive=drive)
    project.initialize()
    assert project.resolve_path("Child") == child
    assert project.collections_folder_id()
    with pytest.raises(NotFoundError):
        project.resolve_path("Missing")
    duplicate = drive.files.seed(name="Dup", mime_type=FOLDER_MIME, parents=[root])
    drive.files.seed(name="Dup", mime_type=FOLDER_MIME, parents=[root])
    with pytest.raises(AmbiguousResourceError):
        project.resolve_path("Dup")
    del duplicate


def test_project_foreign_resource_and_uninitialized_errors() -> None:
    drive = CollectionMemoryDriveService()
    root = drive.files.seed(
        file_id="root2", name="Root", mime_type=FOLDER_MIME, parents=[]
    )
    project = Project(project_id=uuid.uuid4(), root_folder_id=root, drive=drive)
    other = uuid.uuid4()
    with pytest.raises(ForeignProjectError):
        project.assert_resource_belongs_to_project(
            resource_id="file-1",
            app_properties={"gdm_project": str(other)},
        )
    with pytest.raises(GDMError, match="not initialized"):
        project.register_resource(
            RegistryEntry(
                model_name="M",
                resource_id="x",
                kind=ResourceKind.FOLDER,
            )
        )
    with pytest.raises(GDMError, match="not initialized"):
        project.folder_id_for("schemas")


def test_project_open_and_registry_roundtrip() -> None:
    drive = CollectionMemoryDriveService()
    root = drive.files.seed(
        file_id="root3", name="Root", mime_type=FOLDER_MIME, parents=[]
    )
    created = Project.create_new(root_folder_id=root, drive=drive)
    opened = Project.open(root_folder_id=root, drive=drive)
    assert opened.project_id == created.project_id
    entry = RegistryEntry(
        model_name="Widget",
        resource_id="widget-folder",
        kind=ResourceKind.FOLDER,
    )
    opened.register_resource(entry)
    assert opened.get_registered("Widget").resource_id == "widget-folder"
    with pytest.raises(NotFoundError):
        opened.get_registered("Missing")


def test_project_open_persists_adoption_mode() -> None:
    drive = CollectionMemoryDriveService()
    root = drive.files.seed(
        file_id="root-adopt", name="Root", mime_type=FOLDER_MIME, parents=[]
    )
    Project.create_new(root_folder_id=root, drive=drive)
    opened = Project.open(
        root_folder_id=root,
        drive=drive,
        adoption_mode=True,
    )
    assert opened.adoption_mode is True
    opened.assert_resource_belongs_to_project(
        resource_id="foreign",
        app_properties={"gdm_project": str(uuid.uuid4())},
    )
    outside = drive.files.seed(
        name="Outside",
        mime_type=FOLDER_MIME,
        parents=[],
        appProperties={"gdm_project": str(uuid.uuid4())},
    )
    assert opened.assert_resource_in_project_scope(outside)["id"] == outside


def test_project_scope_requires_root_ancestry() -> None:
    drive = CollectionMemoryDriveService()
    root = drive.files.seed(
        file_id="root-scope", name="Root", mime_type=FOLDER_MIME, parents=[]
    )
    nested = drive.files.seed(name="Nested", mime_type=FOLDER_MIME, parents=[root])
    child = drive.files.seed(name="Child", mime_type="text/plain", parents=[nested])
    outside = drive.files.seed(name="Outside", mime_type="text/plain", parents=[])
    project = Project(project_id=uuid.uuid4(), root_folder_id=root, drive=drive)
    assert project.assert_resource_in_project_scope(child)["id"] == child
    with pytest.raises(ForeignProjectError, match="outside project root"):
        project.assert_resource_in_project_scope(outside)


def test_project_initialize_foreign_manifest() -> None:
    drive = CollectionMemoryDriveService()
    root = drive.files.seed(
        file_id="root4", name="Root", mime_type=FOLDER_MIME, parents=[]
    )
    first = Project(project_id=uuid.uuid4(), root_folder_id=root, drive=drive)
    first.initialize()
    second = Project(project_id=uuid.uuid4(), root_folder_id=root, drive=drive)
    with pytest.raises(ForeignProjectError):
        second.initialize()


def test_project_manifest_registry_dict_helpers() -> None:
    manifest = Project(
        project_id=uuid.uuid4(),
        root_folder_id="r",
        drive=CollectionMemoryDriveService(),
    ).manifest
    data = manifest.to_dict()
    restored = type(manifest).from_dict(data)
    assert restored.project_id == manifest.project_id
    entry = RegistryEntry(
        model_name="E",
        resource_id="id",
        kind=ResourceKind.FILE,
        parent_id="p",
        extra={"worksheet": "Sheet1"},
    )
    payload = entry.to_dict()
    assert payload["parent_id"] == "p"
    assert RegistryEntry.from_dict(payload).extra["worksheet"] == "Sheet1"
