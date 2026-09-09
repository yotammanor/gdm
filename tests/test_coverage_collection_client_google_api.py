"""Branch coverage for GDMClient, Project provisioning, and google_api adapters."""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from googleapiclient.errors import HttpError

from gdm.backends.drive import DriveBackend
from gdm.backends.registry import RegistryBackend
from gdm.backends.sheets import SheetsBackend
from gdm.client import GDMClient
from gdm.collection.constants import FOLDER_MIME
from gdm.collection.metadata import InMemoryCollectionMetadataStore
from gdm.errors import ForeignProjectError, GDMError, PermissionDeniedError
from gdm.fields import Field
from gdm.google_api import (
    DriveExplorerAdapter,
    ExecutingDriveFiles,
    ExecutingDriveService,
    ExecutingSheetsService,
    wrap_drive_service,
    wrap_sheets_service,
)
from gdm.model import GDMModel
from gdm.project import Project, RegistryEntry, ResourceKind
from tests.collection_fakes import (
    CollectionMemoryDriveFiles,
    CollectionMemoryDriveService,
)
from tests.fakes import FakeSheetsService


class Note(GDMModel, storage="collection"):
    drive_id: str = Field(drive="id")
    name: str = Field(drive="name")
    body: str | None = Field(default=None, metadata="body")


class FakeHttpRequest:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def execute(self) -> Any:
        return self._payload


class ChunkyRequest:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._offset = 0

    def next_chunk(self, num_retries: int = 0) -> tuple[int | None, bool]:
        del num_retries
        if self._offset == 0:
            self._offset = len(self._data)
            return None, False
        return None, True


class DriveFilesFake:
    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self.content: dict[str, bytes] = {}
        self._counter = 0

    def _next(self) -> str:
        self._counter += 1
        return f"id-{self._counter}"

    def create(self, **kwargs: Any) -> FakeHttpRequest:
        body = kwargs["body"]
        fid = self._next()
        record = {"id": fid, "version": "1", **body}
        media = kwargs.get("media_body")
        if media is not None and hasattr(media, "getvalue"):
            self.content[fid] = media.getvalue()
        self.records[fid] = record
        return FakeHttpRequest(record)

    def update(self, **kwargs: Any) -> FakeHttpRequest:
        file_id = kwargs["fileId"]
        body = kwargs.get("body") or {}
        self.records[file_id].update(body)
        media = kwargs.get("media_body")
        if media is not None and hasattr(media, "getvalue"):
            self.content[file_id] = media.getvalue()
        return FakeHttpRequest(self.records[file_id])

    def get(self, **kwargs: Any) -> FakeHttpRequest:
        return FakeHttpRequest(self.records[kwargs["fileId"]])

    def list(self, **kwargs: Any) -> FakeHttpRequest:
        del kwargs
        return FakeHttpRequest({"files": list(self.records.values())})

    def delete(self, **kwargs: Any) -> FakeHttpRequest:
        del self.records[kwargs["fileId"]]
        return FakeHttpRequest(None)

    def export(self, **kwargs: Any) -> FakeHttpRequest:
        return FakeHttpRequest(self.content.get(kwargs["fileId"], b"pdf-bytes"))

    def get_media(self, **kwargs: Any) -> ChunkyRequest:
        return ChunkyRequest(self.content.get(kwargs["fileId"], b"chunked"))


class DriveRootFake:
    def __init__(self) -> None:
        self._files = DriveFilesFake()

    def files(self) -> DriveFilesFake:
        return self._files

    def permissions(self) -> MagicMock:
        return MagicMock()

    def changes(self) -> MagicMock:
        return MagicMock()


def test_gdm_client_aliases_and_collection_overrides() -> None:
    drive_service = CollectionMemoryDriveService()
    root = drive_service.files.seed(
        file_id="root",
        name="Root",
        mime_type=FOLDER_MIME,
        parents=[],
    )
    folder = drive_service.files.seed(
        name="Notes",
        mime_type=FOLDER_MIME,
        parents=[root],
    )
    project = Project(project_id=uuid.uuid4(), root_folder_id=root, drive=drive_service)
    project.initialize()
    drive = DriveBackend(drive_service, project_id=str(project.project_id))
    sheets = SheetsBackend(FakeSheetsService([["_gdm_file_id", "_gdm_version"]]))
    backend = RegistryBackend(project=project, drive=drive, sheets=sheets)
    backend.bind_model(
        Note,
        RegistryEntry(
            model_name=Note.gdm_model_name(),
            resource_id=folder,
            kind=ResourceKind.FOLDER,
        ),
    )
    client = GDMClient(
        project=project,
        drive=drive,
        sheets=sheets,
        registry_backend=backend,
        drive_service=drive_service,  # type: ignore[arg-type]
    )
    custom_store = InMemoryCollectionMetadataStore()
    coll = client.collection(Note, folder_id=folder, metadata_store=custom_store)
    assert coll._metadata_store is custom_store
    entry = client.bind(
        Note,
        resource_id=folder,
        kind=ResourceKind.FOLDER,
    )
    assert entry.model_name == Note.gdm_model_name()
    session = client.session(scan_limit=50)
    assert session._backend.scan_limit == 50
    assert client.backend is backend
    assert client.permissions is not None
    assert client.changes is not None


def test_client_bind_validates_scope_and_mime() -> None:
    drive_service = CollectionMemoryDriveService()
    root = drive_service.files.seed(
        file_id="root-bind", name="Root", mime_type=FOLDER_MIME, parents=[]
    )
    text_file = drive_service.files.seed(
        name="not-a-folder", mime_type="text/plain", parents=[root]
    )
    outside = drive_service.files.seed(
        name="outside", mime_type=FOLDER_MIME, parents=[]
    )
    project = Project(project_id=uuid.uuid4(), root_folder_id=root, drive=drive_service)
    drive = DriveBackend(drive_service, project_id=str(project.project_id))
    sheets = SheetsBackend(FakeSheetsService([]))
    backend = RegistryBackend(project=project, drive=drive, sheets=sheets)
    client = GDMClient(
        project=project,
        drive=drive,
        sheets=sheets,
        registry_backend=backend,
        drive_service=drive_service,  # type: ignore[arg-type]
    )
    with pytest.raises(GDMError, match="not a Drive folder"):
        client.bind(Note, resource_id=text_file, kind=ResourceKind.FOLDER)
    with pytest.raises(ForeignProjectError, match="outside project root"):
        client.bind(Note, resource_id=outside, kind=ResourceKind.FOLDER)
    with pytest.raises(GDMError, match="not a Drive folder"):
        client.collection(
            Note,
            folder_id=text_file,
            metadata_store=InMemoryCollectionMetadataStore(),
        )
    with pytest.raises(ForeignProjectError, match="outside project root"):
        client.collection(
            Note,
            folder_id=outside,
            metadata_store=InMemoryCollectionMetadataStore(),
        )
    with pytest.raises(ForeignProjectError, match="outside project root"):
        client.bind(
            Note,
            resource_id=text_file,
            kind=ResourceKind.FILE,
            parent_id=outside,
        )


def test_gdm_client_explore_lazy_init() -> None:
    drive_service = CollectionMemoryDriveService()
    root = drive_service.files.seed(
        file_id="root",
        name="Root",
        mime_type=FOLDER_MIME,
        parents=[],
    )
    project = Project(project_id=uuid.uuid4(), root_folder_id=root, drive=drive_service)
    project.initialize()
    drive = DriveBackend(drive_service, project_id=str(project.project_id))
    sheets = SheetsBackend(FakeSheetsService([["a"]]))
    backend = RegistryBackend(project=project, drive=drive, sheets=sheets)
    client = GDMClient(
        project=project,
        drive=drive,
        sheets=sheets,
        registry_backend=backend,
        drive_service=drive_service,  # type: ignore[arg-type]
        explorer=None,
    )
    with patch("gdm.client.build_explorer_service") as build_explorer:
        build_explorer.return_value = MagicMock()
        with patch("gdm.explore.Explorer") as explorer_cls:
            explorer_cls.return_value.explore.return_value = {"items": []}
            result = client.explore()
            assert result == {"items": []}


def test_gdm_client_connect_create_paths() -> None:
    creds = MagicMock()
    drive_service = wrap_drive_service(DriveRootFake())
    root = drive_service.files.create(
        body={
            "name": "Root",
            "mimeType": FOLDER_MIME,
            "parents": [],
        },
        fields="id",
    )["id"]
    with (
        patch("gdm.client.build_credentials", return_value=creds) as build_credentials,
        patch("gdm.client.build_drive_service", return_value=drive_service),
        patch("gdm.client.build_sheets_service") as build_sheets,
        patch("gdm.client.build_docs_service", return_value=MagicMock()),
        patch("gdm.client.build_slides_service", return_value=MagicMock()),
        patch("gdm.client.Project.create_new") as create_new,
        patch("gdm.client.DriveExplorerAdapter"),
        patch("gdm.explore.Explorer"),
    ):
        project = Project(
            project_id=uuid.uuid4(),
            root_folder_id=root,
            drive=drive_service,
        )
        create_new.return_value = project
        build_sheets.return_value = MagicMock()
        client = GDMClient.connect(root_folder_id=root, create=True)
        assert isinstance(client, GDMClient)
        assert (
            "https://www.googleapis.com/auth/drive"
            in build_credentials.call_args.kwargs["scopes"]
        )
        created = GDMClient.create(root_folder_id=root)
        assert isinstance(created, GDMClient)


def test_project_open_foreign_root_and_adoption() -> None:
    drive_service = CollectionMemoryDriveService()
    root = drive_service.files.seed(
        file_id="root-open",
        name="Root",
        mime_type=FOLDER_MIME,
        parents=[],
    )
    project = Project(project_id=uuid.uuid4(), root_folder_id=root, drive=drive_service)
    project.initialize()
    manifest = project.manifest.to_dict()
    manifest["root_folder_id"] = "other-root"
    gdm_id = project.gdm_folder_id
    assert gdm_id is not None
    drive_service.write_file_bytes(
        next(
            fid
            for fid, record in drive_service.files.records.items()
            if record.get("name") == "manifest.json"
        ),
        json.dumps(manifest).encode(),
    )
    with pytest.raises(ForeignProjectError):
        Project.open(
            root_folder_id=root,
            drive=drive_service,
        )
    project.assert_resource_belongs_to_project(
        resource_id="x",
        app_properties={"gdm_project": str(uuid.uuid4())},
        adoption_mode=True,
    )
    with pytest.raises(ValueError, match="Path must stay under project root"):
        project.resolve_path("../escape")


def test_project_metadata_binding_custom_creator() -> None:
    drive_service = CollectionMemoryDriveService()
    root = drive_service.files.seed(
        file_id="root-meta",
        name="Root",
        mime_type=FOLDER_MIME,
        parents=[],
    )
    project = Project(project_id=uuid.uuid4(), root_folder_id=root, drive=drive_service)
    project.initialize()
    sheet_id, worksheet = project.ensure_collection_metadata_binding(
        "MyModel",
        create_spreadsheet=lambda parent, name: drive_service.files.seed(
            name=f"{name}-metadata",
            mime_type="application/vnd.google-apps.spreadsheet",
            parents=[parent],
        ),
    )
    assert sheet_id
    assert worksheet == "metadata"


def test_wrap_drive_service_read_write_and_explorer() -> None:
    inner = CollectionMemoryDriveFiles()
    fid = inner.seed(
        name="manifest.json",
        mime_type="application/json",
        parents=["root"],
    )
    inner.content[fid] = b"{}"
    raw = MagicMock()
    raw.files.return_value = inner
    raw.permissions.return_value = MagicMock()
    raw.changes.return_value = MagicMock()
    service = wrap_drive_service(raw)
    assert isinstance(service, ExecutingDriveService)
    payload = json.dumps({"ok": True}).encode()
    service.write_file_bytes(fid, payload)
    with patch.object(service, "read_file_bytes", return_value=payload):
        assert service.read_file_bytes(fid) == payload
    adapter = DriveExplorerAdapter(service)
    page = adapter.list_children("root")
    assert isinstance(page.items, list)
    files = ExecutingDriveFiles(inner, auto_execute=False)
    request = files.get_media(fileId=fid)
    assert hasattr(request, "next_chunk")
    updated = files.update(
        fileId=fid,
        addParents="p-new",
        removeParents="p-old",
        body={"name": "manifest.json"},
    )
    assert updated["id"] == fid


def test_executing_resource_http_error_normalization() -> None:
    resource = MagicMock()

    def boom(**kwargs: Any) -> None:
        del kwargs
        raise HttpError(resp=MagicMock(status=403), content=b"denied")

    resource.get.side_effect = boom
    files = ExecutingDriveFiles(resource)
    with pytest.raises(PermissionDeniedError):
        files.get(fileId="x", fields="id")


def test_wrap_sheets_service_values_surface() -> None:
    values = MagicMock()
    spreadsheets = MagicMock()
    raw = MagicMock()
    raw.spreadsheets.return_value = spreadsheets
    spreadsheets.values.return_value = values
    values.get.return_value = FakeHttpRequest({"values": [["h"]]})
    values.append.return_value = FakeHttpRequest({"updates": {"updatedRows": 1}})
    values.clear.return_value = FakeHttpRequest({})
    values.batchGet.return_value = FakeHttpRequest({"valueRanges": []})
    values.batchUpdate.return_value = FakeHttpRequest({})
    spreadsheets.get.return_value = FakeHttpRequest({"sheets": []})
    spreadsheets.batchUpdate.return_value = FakeHttpRequest({})
    service = wrap_sheets_service(raw)
    assert isinstance(service, ExecutingSheetsService)
    assert service.spreadsheets().values().get(
        spreadsheetId="s",
        range="A1",
    )["values"] == [["h"]]
    service.spreadsheets().values().append(
        spreadsheetId="s",
        range="A1",
        body={"values": [["x"]]},
    )
    service.spreadsheets().values().clear(spreadsheetId="s", range="A1")
    service.spreadsheets().values().batchGet(
        spreadsheetId="s",
        ranges=["A1"],
    )
    service.spreadsheets().values().batchUpdate(
        spreadsheetId="s",
        body={"data": []},
    )
    service.spreadsheets().get(spreadsheetId="s", fields="sheets")
    service.spreadsheets().batchUpdate(spreadsheetId="s", body={"requests": []})
